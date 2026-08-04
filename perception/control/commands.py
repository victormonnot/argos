"""Le VOCABULAIRE partagé entre la décision et l'encodage (PORTFOLIO §1.5-B).

Aucun import de pymavlink, de simulateur ou d'OpenCV ici — c'est volontaire :
la loi de guidage produit ces objets, un backend véhicule les traduit (MAVLink
pour le 3,5", CRSF pour le whoop plus tard). Un texte, deux traducteurs.

Hiérarchie (§2.3) : **CTBR est la primitive**, plus petit dénominateur commun
entre `GUIDED_NOGPS` barreau 3 et l'ACRO Betaflight — c'est aussi l'espace
d'action des politiques RL. L'**attitude est une couche de confort au-dessus**,
celle que consomme la loi de guidage écrite à la main (barreaux 1-2).

Conventions de signe, une bonne fois (celles d'ArduPilot, euler 3-2-1 NED) :
  roll   > 0  -> incline à DROITE      -> accélère vers la droite
  pitch  > 0  -> nez en HAUT           -> donc AVANCER = pitch NÉGATIF (piqué)
  dyaw   > 0  -> tourne à DROITE, et c'est un **delta au cap courant**, jamais
                 un cap absolu : le compas est mort et le cap dérive (§1.1).
  thrust      -> 0..1, **0,5 = tenir l'altitude**. Vrai tant que `GUID_OPTIONS`
                 bit 3 = 0 : ArduPilot lit alors `thrust` comme un taux de montée
                 et ferme la boucle d'altitude au baro, sans GPS.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CtbrCmd:
    """Collective Thrust + Body Rates — la primitive (barreau 3 du mode 20).

    `thrust` est ici une poussée brute 0..1 (exige `GUID_OPTIONS` bit 3 = 1 côté
    MAVLink). Pas encore utilisé par la loi de guidage : c'est l'interface que la
    politique apprise consommera, on la fige maintenant pour ne pas la rétrofitter.
    """
    roll_rate: float = 0.0        # rad/s, axe corps
    pitch_rate: float = 0.0       # rad/s
    yaw_rate: float = 0.0         # rad/s
    thrust: float = 0.0           # 0..1, poussée collective
    source: str = "idle"          # qui a produit la commande (traçabilité au HUD)


@dataclass(frozen=True)
class AttitudeCmd:
    """Attitude désirée + poussée — la couche de confort (barreaux 1-2 du mode 20)."""
    roll: float = 0.0             # rad, + = à droite
    pitch: float = 0.0            # rad, + = nez en haut (avancer = négatif)
    dyaw: float = 0.0             # rad, RELATIF au cap courant
    thrust: float = 0.5           # 0..1, 0,5 = tenir l'altitude
    source: str = "idle"          # "track" | "operator" | "policy" | "idle"

    def replace(self, **kw) -> "AttitudeCmd":
        """Copie modifiée — la porte de sortie écrête sans muter l'original."""
        return AttitudeCmd(**{**self.__dict__, **kw})


HOVER = AttitudeCmd(source="idle")
"""Commande neutre : à plat, cap tenu, altitude tenue. Ce qu'on émet par défaut
pour ne jamais laisser expirer `GUID_TIMEOUT` sans le vouloir."""


@dataclass(frozen=True)
class TargetView:
    """Ce que la vision sait de la cible, normalisé — l'ENTRÉE de la loi.

    Volontairement sans pixels ni résolution : la loi ne doit pas connaître la
    caméra, et ces trois nombres sont les mêmes sur le 3,5", le whoop ou en simu.
    """
    has: bool = False             # cible active (détectée, ou en coast)
    found: bool = False           # détectée sur CETTE image (vs coast)
    error_x: float = 0.0          # -1..1, décalage horizontal / demi-largeur image
    size: float = 0.0             # hauteur bbox / hauteur image — LE capteur de
                                  # distance (§1.1) : ça grandit = on approche


@dataclass(frozen=True)
class VehicleState:
    """L'état minimal dont la porte de sortie a besoin pour décider d'émettre."""
    flying: bool = False
    heading: float = 0.0          # rad, cap courant mesuré (sert à rendre dyaw absolu)
    alt: float = 0.0              # m, relatif au décollage
