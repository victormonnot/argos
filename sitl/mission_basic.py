"""mission_basic.py — arm, takeoff, carré, land. SITL only.

Contrat : se connecter au SITL, passer en GUIDED, armer, décoller à 10 m,
voler un carré de 5 m de côté, atterrir. Chaque mouvement est confirmé en
LISANT la télémétrie (boucle fermée), jamais deviné avec un sleep().

Lancer le SITL d'abord (dans un autre terminal), avec une sortie locale
dédiée au script :
    cd ~/argos-project/ardupilot
    Tools/autotest/sim_vehicle.py -v ArduCopter \
        --out udp:192.168.1.18:14550 \
        --out udp:127.0.0.1:14551
"""

import time
import math
from pymavlink import mavutil

# ─────────────────────────────────────────────────────────────────────────
# PLOMBERIE (donnée) — la connexion et les actions "ponctuelles"
# ─────────────────────────────────────────────────────────────────────────

# HEARTBEAT : on ouvre le lien et on apprend à qui on parle.
# udpin: => le script ÉCOUTE sur 14551, là où MAVProxy pousse le flux.
master = mavutil.mavlink_connection('udpin:127.0.0.1:14551')
master.wait_heartbeat()
print(f"Connecté — système {master.target_system}, "
      f"composant {master.target_component}")


def set_mode(name):
    """Change de mode de vol, puis confirme en lisant le HEARTBEAT."""
    mode_id = master.mode_mapping()[name]          # pas de numéro en dur
    master.mav.set_mode_send(
        master.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mode_id)
    while master.recv_match(type='HEARTBEAT', blocking=True).custom_mode != mode_id:
        pass
    print(f"Mode {name} actif")


def arm():
    """COMMAND_LONG ARM_DISARM → on attend le COMMAND_ACK (motif requête→réponse)."""
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        0, 1, 0, 0, 0, 0, 0, 0)                    # param1 = 1 → armer
    ack = master.recv_match(type='COMMAND_ACK', blocking=True)
    print(f"ARM → résultat {ack.result}  (0 = accepté)")
    master.motors_armed_wait()
    print("Moteurs armés")


def takeoff(alt):
    """COMMAND_LONG NAV_TAKEOFF, PUIS on boucle jusqu'à atteindre l'altitude.

    >>> C'EST TON EXEMPLE DE RÉFÉRENCE <<<
    Le motif "envoyer une consigne, puis lire la télémétrie en boucle
    jusqu'à ce que la réalité rejoigne la consigne" est EXACTEMENT celui
    que tu vas réécrire toi-même dans goto(). Lis-le bien.
    """
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
        0, 0, 0, 0, 0, 0, 0, alt)                  # param7 = altitude
    while True:
        msg = master.recv_match(type='GLOBAL_POSITION_INT', blocking=True)
        current = msg.relative_alt / 1000.0        # mm → m
        print(f"  altitude {current:.1f} m")
        if current >= alt * 0.95:
            print("Altitude atteinte")
            break
        time.sleep(0.3)


# ─────────────────────────────────────────────────────────────────────────
# Repère NED — LE piège. Down est POSITIF vers le sol.
#   x = Nord (+nord), y = Est (+est), z = Down → altitude = NÉGATIVE.
#   Voler à 10 m d'altitude => z = -10.
# ─────────────────────────────────────────────────────────────────────────


def goto(north, east, down):
    """Consigne de position GUIDED, puis attente d'arrivée (boucle fermée).

    L'ENVOI t'est donné (les champs sont pénibles). L'ATTENTE est à toi.
    """
    master.mav.set_position_target_local_ned_send(
        0,                                          # time_boot_ms (0 = ok)
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_FRAME_LOCAL_NED,        # NED relatif au home
        0b110111111000,                             # type_mask : chaque bit à 1
        #                                             = "ignore ce champ". Ici on
        #                                             garde x,y,z (bits 0-2 à 0) et
        #                                             on ignore vit/accel/yaw. Tu dois
        #                                             savoir défendre ce masque.
        north, east, down,                          # position NED en mètres
        0, 0, 0,                                    # vitesse (ignorée)
        0, 0, 0,                                    # accél. (ignorée)
        0, 0)                                       # yaw, yaw_rate (ignorés)

    # Attente d'arrivée (boucle fermée) — même motif que takeoff(), mais la
    # "réalité" se lit dans LOCAL_POSITION_NED (.x .y .z en mètres, NED).
    while True:
        pos = master.recv_match(type='LOCAL_POSITION_NED', blocking=True)
        dist = math.sqrt((pos.x - north) ** 2 + (pos.y - east) ** 2)  # distance horizontale
        print(f"  distance au point {dist:.1f} m")
        if dist < 0.5:
            print("Point atteint")
            break
        time.sleep(0.3)


if __name__ == "__main__":
    ALT = 30

    # ── PALIER 1 : valide d'abord ceci SEUL (carré commenté) ──
    set_mode('GUIDED')
    arm()
    takeoff(ALT)

    # Le carré de 5 m de côté, à 10 m d'altitude (z = -ALT).
    # Coins en (north, east). z reste négatif partout (on garde l'altitude).
    SQUARE = [(50, 0), (50, -150), (-100, -100), (-80, -20), (0, 0)]
    for north, east in SQUARE:
        print(f"→ coin ({north}, {east})")
        goto(north, east, -ALT)

    set_mode('LAND')
    print("Mission terminée")
