"""Émetteur d'essai : envoie des ARGOS_TARGET en UDP, comme le fera la console.

Sert à exercer le consommateur C sans démarrer Gazebo ni le SITL. La cible
oscille, se perd un instant (coast) puis revient — de quoi voir bouger tous les
champs à l'autre bout.

    ../perception/.venv/bin/python send_target.py       # depuis mavlink/

Point de méthode : un objet MAVLink écrit dans n'importe quoi qui a une
méthode `write()`. Pas besoin de mavutil ni de « connexion » — le protocole ne
sait rien du transport, et c'est exactement pour ça qu'il tourne aussi bien sur
UDP, sur une radio série 57 600 baud ou sur un lien SPI.
"""
import math
import os
import socket
import sys
import time
from pathlib import Path

os.environ["MAVLINK20"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent / "generated" / "python"))
try:
    import argos
except ImportError:
    sys.exit("dialecte non généré — lance `make` dans mavlink/ d'abord")

CIBLE = ("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 14650)
HZ = 10.0


class SortieUDP:
    """Le « fichier » dans lequel MAVLink écrit ses trames."""

    def __init__(self, adresse):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.adresse = adresse

    def write(self, octets):
        self.s.sendto(octets, self.adresse)


mav = argos.MAVLink(SortieUDP(CIBLE), srcSystem=42, srcComponent=190)   # 42:190 = la console
print(f"émission vers {CIBLE[0]}:{CIBLE[1]} à {HZ:.0f} Hz — Ctrl-C pour arrêter")

t0 = time.time()
n = 0
while True:
    t = time.time() - t0
    n += 1

    # La cible balaie de gauche à droite ; entre 6 et 8 s le détecteur la perd
    # (coast) ; l'engagement démarre à 3 s.
    vue = not (6.0 < (t % 12.0) < 8.0)
    engage = t > 3.0

    mav.argos_target_send(
        time_usec=int(time.time() * 1e6),
        u=0.6 * math.sin(t * 0.7),
        v=0.15 * math.sin(t * 0.3),
        size=0.08 + 0.03 * math.sin(t * 0.2),
        confidence=0.85 if vue else 0.0,
        track_age_ms=int(t * 1000),
        track_id=7,
        target_class=argos.ARGOS_CLASS_VEHICLE,
        lock_state=argos.ARGOS_LOCK_TRACK if vue else argos.ARGOS_LOCK_COAST,
        flags=argos.ARGOS_TARGET_FLAG_ENGAGED if engage else 0)

    # Un HEARTBEAT de temps en temps : il vient de common.xml, pas de notre
    # dialecte. Le consommateur C le compte comme « autre message », ce qui
    # prouve qu'il parle bien le dialecte COMPLET et pas seulement notre ajout.
    if n % 10 == 0:
        mav.heartbeat_send(argos.MAV_TYPE_ONBOARD_CONTROLLER,
                           argos.MAV_AUTOPILOT_INVALID, 0, 0,
                           argos.MAV_STATE_ACTIVE)

    time.sleep(1.0 / HZ)
