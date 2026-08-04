"""La loi de guidage visuelle — erreur pixel -> attitude désirée (PORTFOLIO §1.1).

Pure : `math` et rien d'autre. Aucun MAVLink, aucune caméra, aucun simulateur.
C'est ce qui la rend testable au banc (`test_guidance.py`) avant tout vol, et
réutilisable telle quelle sur le whoop.

Elle répond aux **deux vraies difficultés** du §1.1 :

1. *Pas de retour de vitesse, donc pas d'amortissement.* Sans état de position,
   un P pur sur l'erreur pixel oscille : le drone s'incline, prend de la vitesse
   latérale, arrive au centre à pleine vitesse et dépasse. Le terme **D sur
   l'erreur pixel EST le retour de vitesse** — c'est la vision qui le fournit,
   pas l'EKF. Filtré, parce que la détection scintille (~30 %).
2. *Pas de compas, le cap dérive.* On ne produit donc **jamais un cap absolu**,
   seulement un `dyaw` relatif au cap mesuré au moment de l'encodage. Écrêté
   petit : le cap commandé reste par construction à quelques degrés du cap réel,
   ce qui garde aussi le roll/pitch dans le bon repère si le véhicule ne suit
   pas son lacet (le cas de l'iris Gazebo, qui n'a pas de couple de lacet).

L'axe d'approche, lui, n'a pas de capteur de distance — sauf un : **la taille de
la bounding box**. Elle grandit = on approche. La commande d'avance est donc
tapée en fonction de la taille, et devient négative (freinage) au-delà du seuil.

⚠ **`size_near` est une CALIBRATION, pas une constante.** La taille atteignable
est plafonnée par la géométrie : un drone à `alt` mètres au-dessus d'une cible de
hauteur `h` ne verra jamais mieux que

        taille_max ≈ h / (2 · alt · tan(demi-champ vertical))

soit ~0,15 pour une personne (1,7 m) à 12 m avec un demi-champ de 0,45 rad. Un
seuil au-dessus de ce plafond **ne se déclenche jamais** : le drone ne freine
pas, et survole la cible. Trouvé exactement comme ça en SITL avec un seuil à
0,34 (`sitl/nogps_engage_test.py`, qui mesure ce plafond et le rapporte).
Régler en vol via `/tune?near=`.
"""
import math
from dataclasses import dataclass

from .commands import AttitudeCmd, TargetView

DEG = math.pi / 180.0


@dataclass
class GuidanceGains:
    """Un jeu de gains = un profil véhicule/capteur. Tout est en RADIANS."""
    kp_roll: float = 7.0 * DEG      # inclinaison par unité d'erreur horizontale
    kd_roll: float = 9.0 * DEG      # amortissement, par unité d'erreur/seconde
    kp_yaw: float = 1.5 * DEG       # correction de cap par unité d'erreur
    yaw_deadband: float = 0.08      # sous ça, on ne touche pas au cap (bruit)
    k_pitch: float = 5.5 * DEG      # piqué d'approche à pleine distance
    k_brake: float = 6.0 * DEG      # cabré de freinage quand on est trop près
    size_far: float = 0.05          # bbox <= : cible loin, approche à fond
    size_near: float = 0.12         # bbox >= : distance de garde, avance nulle
    size_brake: float = 0.05        # largeur de la rampe de freinage au-delà
    tau_d: float = 0.25             # s, filtre passe-bas du terme dérivé
    max_tilt: float = 12.0 * DEG    # écrêtage roll/pitch produit par la loi
    max_dyaw: float = 5.0 * DEG     # écrêtage du delta de cap par cycle


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class VisualGuidance:
    """Asservissement visuel en commande d'attitude. Zéro estimation de position.

    Un objet (et pas une fonction) parce que le terme dérivé a une mémoire ;
    `step()` reste déterministe pour un état interne donné, donc testable.
    """

    def __init__(self, gains: GuidanceGains | None = None):
        self.g = gains or GuidanceGains()
        self._err_prev = 0.0
        self._derr = 0.0            # dérivée filtrée de l'erreur horizontale
        self._primed = False        # a-t-on déjà vu une détection ? (évite le pic initial)
        self.telemetry = {"derr": 0.0, "approach": 0.0, "brake": 0.0}

    def reset(self):
        """À appeler à chaque nouveau lock : la cible précédente n'informe plus rien."""
        self._err_prev = 0.0
        self._derr = 0.0
        self._primed = False
        self.telemetry = {"derr": 0.0, "approach": 0.0, "brake": 0.0}

    # ── le capteur de distance : la taille de la bbox ────────────────────────
    def _closure(self, size: float) -> tuple[float, float]:
        """(approche, freinage), tous deux dans 0..1, à partir de la taille de bbox.

        approche = 1 tant qu'on est loin, descend à 0 à la distance de garde.
        freinage > 0 seulement au-delà : c'est le seul terme qui pousse en arrière.
        """
        g = self.g
        span = max(g.size_near - g.size_far, 1e-6)
        approach = _clamp((g.size_near - size) / span, 0.0, 1.0)
        brake = _clamp((size - g.size_near) / max(g.size_brake, 1e-6), 0.0, 1.0)
        return approach, brake

    # ── la loi ──────────────────────────────────────────────────────────────
    def step(self, target: TargetView, engage: bool, dt: float) -> AttitudeCmd:
        """Un cycle. `dt` = temps écoulé depuis le précédent appel, en secondes."""
        g = self.g
        if not target.has:
            self.reset()
            return AttitudeCmd(source="idle")     # à plat, cap et altitude tenus

        dt = _clamp(dt, 1e-3, 0.5)          # une boucle qui bégaie ne doit pas
        err = _clamp(target.error_x, -1.0, 1.0)   # faire exploser la dérivée

        # Dérivée : mise à jour uniquement sur une VRAIE détection. Pendant un
        # coast l'erreur est gelée — la dériver donnerait 0 puis un pic à la
        # reprise. On la laisse simplement décroître vers 0.
        if target.found:
            raw = (err - self._err_prev) / dt if self._primed else 0.0
            alpha = dt / (g.tau_d + dt)     # passe-bas du premier ordre
            self._derr += alpha * (raw - self._derr)
            self._err_prev = err
            self._primed = True
        else:
            alpha = dt / (g.tau_d + dt)
            self._derr += alpha * (0.0 - self._derr)

        # Latéral : P sur la position de la cible, D pour l'amortissement manquant.
        roll = _clamp(g.kp_roll * err + g.kd_roll * self._derr, -g.max_tilt, g.max_tilt)

        # Cap : relatif, avec zone morte. Jamais de cap absolu (compas mort).
        e_yaw = 0.0 if abs(err) < g.yaw_deadband else err
        dyaw = _clamp(g.kp_yaw * e_yaw, -g.max_dyaw, g.max_dyaw)

        # Approche : piqué proportionnel à « reste-t-il de la distance ? »,
        # freinage au-delà de la garde. Nez en haut = pitch positif -> avancer
        # est un pitch NÉGATIF.
        approach, brake = self._closure(target.size)
        pitch = 0.0
        if engage:
            pitch = _clamp(-g.k_pitch * approach + g.k_brake * brake,
                           -g.max_tilt, g.max_tilt)

        self.telemetry = {"derr": round(self._derr, 3),
                          "approach": round(approach, 2),
                          "brake": round(brake, 2)}
        # thrust laissé à 0,5 : ArduPilot tient l'altitude au baro pour nous.
        return AttitudeCmd(roll=roll, pitch=pitch, dyaw=dyaw, thrust=0.5, source="track")


def operator_command(fwd: float, right: float, up: float,
                     max_tilt: float = 12.0 * DEG) -> AttitudeCmd:
    """Le pilotage manuel, exprimé dans le MÊME type que la loi de guidage.

    L'opérateur envoie une intention normalisée (-1..1) ; elle devient une
    attitude ici, et repart par la même porte de sortie que le suivi. C'est tout
    l'intérêt du §1.5-A : la garde de proximité s'applique aussi à lui.
    """
    return AttitudeCmd(
        roll=_clamp(right, -1.0, 1.0) * max_tilt,
        pitch=-_clamp(fwd, -1.0, 1.0) * max_tilt,     # avancer = piqué = négatif
        dyaw=0.0,
        thrust=_clamp(0.5 + 0.5 * _clamp(up, -1.0, 1.0), 0.0, 1.0),
        source="operator",
    )
