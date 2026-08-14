"""Publication de la désignation de cible dans le dialecte ARGOS (PORTFOLIO §1.3).

Ce que la perception sait — où est la cible dans l'image, quelle taille, quel état
de verrou — ne vivait que dans les variables d'un processus Python. Ici ça devient
un message sur un fil, que n'importe qui peut lire : `mavlink/consumers/` en C et
en C++, une appli MAVSDK, un deuxième drone, un enregistreur.

**Pourquoi une liaison SÉPARÉE de celle du drone.** On aurait pu injecter
ARGOS_TARGET dans la connexion vers l'autopilote. Deux raisons de ne pas le faire :
  - la liaison de commande est **critique et mesurée** (§1.5-C). Y ajouter 10 msg/s
    de charge utile change les chiffres qu'on compare au HITL puis au réel, et fait
    dépendre le vol d'un flux qui ne pilote rien ;
  - sur le vrai drone la radio de commande est à 57 600 baud, la désignation
    partirait sur le lien WiFi/vidéo. Deux canaux physiques différents dès le
    départ, donc deux canaux logiques dès maintenant.

C'est la même logique qu'au §1.5-C : le canal est un objet de première classe, et
celui-ci est instrumenté comme les autres (`LinkStats`), donc comparable.

Aucun import de pymavlink ici : le dialecte généré suffit, et il écrit dans
n'importe quel objet muni d'une méthode `write()`.
"""
import socket
import sys
import time
from pathlib import Path

from .link import LinkStats

# Le dialecte généré. `make -C mavlink` le fabrique ; il n'est pas versionné.
_GEN = Path(__file__).resolve().parents[2] / "mavlink" / "generated" / "python"
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))
try:
    import argos as dialecte
except ImportError:                    # dialecte non généré -> la console vole quand même
    dialecte = None

DEST_DEFAUT = "127.0.0.1:14650"
SYSID, COMPID = 42, 190                # 42:190 = « la console ARGOS », pas l'autopilote


class _SortieUDP:
    """Le « fichier » dans lequel l'encodeur MAVLink écrit ses trames.

    MAVLink ignore tout du transport : il lui faut un objet avec `write()`. C'est
    pour ça que le même encodeur sert sur UDP, sur une série 57 600 baud ou sur
    un lien SPI, sans une ligne de différence."""

    def __init__(self, hote, port):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.adresse = (hote, port)
        self.octets = 0

    def write(self, octets):
        self.octets += len(octets)
        self.s.sendto(octets, self.adresse)


class TargetPublisher:
    """Traducteur : état du traqueur -> ARGOS_TARGET sur le fil.

    Symétrique de `mavlink_backend.py` (décision -> commande MAVLink), mais dans
    l'autre sens : perception -> observation. Aucune décision ne passe par ici,
    et rien de ce qui est émis ne peut faire bouger le drone.
    """

    def __init__(self, dest: str = DEST_DEFAUT):
        self.actif = dialecte is not None
        self.dest = dest
        self.envoyes = 0
        self.stats = LinkStats(fenetre=3.0)
        if not self.actif:
            return
        hote, _, port = dest.partition(":")
        self._sortie = _SortieUDP(hote, int(port or 14650))
        self.mav = dialecte.MAVLink(self._sortie, srcSystem=SYSID, srcComponent=COMPID)

    # ── traduction des états console -> énumérés du dialecte ────────────────
    @staticmethod
    def _etat_verrou(locked: bool, has: bool, found: bool) -> int:
        """Les quatre états sont exclusifs, et la distinction qui compte est
        TRACK/COAST : dans les deux cas la loi commande, mais en COAST elle
        commande sur une position MÉMORISÉE. À l'autre bout du fil, rien d'autre
        ne permet de faire la différence."""
        if not locked:
            return dialecte.ARGOS_LOCK_IDLE
        if found:
            return dialecte.ARGOS_LOCK_TRACK
        return dialecte.ARGOS_LOCK_COAST if has else dialecte.ARGOS_LOCK_LOST

    @staticmethod
    def _classe(cls_id) -> int:
        return {0: dialecte.ARGOS_CLASS_PERSON,
                1: dialecte.ARGOS_CLASS_VEHICLE}.get(cls_id,
                                                     dialecte.ARGOS_CLASS_UNKNOWN)

    def publish(self, now: float, t_cap: float, *, u: float, v: float, size: float,
                confidence: float, track_id: int, age_s: float, cls_id,
                locked: bool, has: bool, found: bool,
                engaged: bool, guard: bool) -> None:
        """Émet UNE désignation. Appelée à la cadence de la boucle de commande.

        `t_cap` est l'instant de CAPTURE de l'image, pas celui de l'émission : le
        message porte donc l'âge de sa propre information, et le consommateur
        décide lui-même s'il agit dessus."""
        if not self.actif:
            return
        drapeaux = 0
        if engaged:
            drapeaux |= dialecte.ARGOS_TARGET_FLAG_ENGAGED
        if guard:
            drapeaux |= dialecte.ARGOS_TARGET_FLAG_GUARD

        avant = self._sortie.octets
        self.mav.argos_target_send(
            time_usec=int((t_cap or now) * 1e6),
            u=float(u), v=float(v), size=float(size),
            confidence=float(confidence),
            track_age_ms=int(max(0.0, age_s) * 1000),
            track_id=int(track_id) & 0xFFFF,
            target_class=self._classe(cls_id),
            lock_state=self._etat_verrou(locked, has, found),
            flags=drapeaux)
        self.envoyes += 1
        self.stats.on_tx(now, self._sortie.octets - avant)

    def snapshot(self, now: float):
        """Ce canal est mesuré comme les deux autres : Hz, débit, plus grand
        silence. Un flux de désignation qui bégaie est un flux dont on ne peut
        rien conclure à l'autre bout."""
        s = self.stats.snapshot(now)
        return {"actif": self.actif, "dest": self.dest, "envoyes": self.envoyes,
                "hz": s.tx_hz, "bps": s.tx_bps, "trou_max_s": s.tx_trou_max_s}
