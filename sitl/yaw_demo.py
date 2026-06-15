"""yaw_demo.py — faire pivoter le drone vers des caps successifs (SITL only).

>>> LE COEUR D'ARGOS MODE B, EN MINIATURE <<<
Commander l'orientation (yaw) du drone pour qu'il "regarde" une direction.
Ici les caps sont codés en dur ; en S5, le cap viendra d'une détection vidéo
(le drone tournera son nez — et donc sa caméra — vers la cible repérée).

Deux messages NOUVEAUX par rapport à mission_basic.py :
  - MAV_CMD_CONDITION_YAW (via COMMAND_LONG) : "tourne le nez vers ce cap"
  - ATTITUDE : l'orientation réelle (roll/pitch/yaw) du drone — pour CONFIRMER
    qu'il a tourné (boucle fermée), exactement comme LOCAL_POSITION_NED servait
    à confirmer l'arrivée dans goto().

Prérequis : un SITL qui tourne (sitl/run_sitl.sh).
Lancer :  ~/venv-ardupilot/bin/python ~/argos-project/argos/sitl/yaw_demo.py
"""
import time
import math
from pymavlink import mavutil

master = mavutil.mavlink_connection('udpin:127.0.0.1:14551')
master.wait_heartbeat()
print(f"Connecté — système {master.target_system}")


# --- Helpers repris de mission_basic.py (on les factorisera dans un module commun plus tard) ---
def set_mode(name):
    mode_id = master.mode_mapping()[name]
    master.mav.set_mode_send(
        master.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id)
    while master.recv_match(type='HEARTBEAT', blocking=True).custom_mode != mode_id:
        pass
    print(f"Mode {name} actif")


def arm():
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
    master.motors_armed_wait()
    print("Moteurs armés")


def takeoff(alt, timeout=60):
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, alt)
    start = time.time()
    while True:
        msg = master.recv_match(type='GLOBAL_POSITION_INT', blocking=True, timeout=1)
        if msg and msg.relative_alt / 1000.0 >= alt * 0.95:
            print("Altitude atteinte")
            return
        if time.time() - start > timeout:
            raise TimeoutError("takeoff timeout")
        time.sleep(0.3)


# --- LE NOUVEAU : commander et lire le cap ---
def point_to(cap_deg, vitesse_dps=30, timeout=20, tol_deg=3):
    """Tourne le nez du drone vers un cap ABSOLU (0=Nord, 90=Est...), puis
    confirme via ATTITUDE. Même motif consigne→feedback que goto()."""
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_CONDITION_YAW,
        0,
        cap_deg,        # param1 : cap cible en degrés
        vitesse_dps,    # param2 : vitesse de rotation (deg/s)
        1,              # param3 : sens (1 = horaire)
        0,              # param4 : 0 = cap ABSOLU, 1 = relatif au cap actuel
        0, 0, 0)

    start = time.time()
    while True:
        att = master.recv_match(type='ATTITUDE', blocking=True, timeout=1)
        if att is not None:
            cap = math.degrees(att.yaw) % 360            # yaw radians → cap 0–360
            # écart = plus court chemin vers la cible (gère le passage 359°→0°)
            ecart = (cap_deg - cap + 180) % 360 - 180
            print(f"  cap {cap:6.1f}°   (cible {cap_deg}°, écart {ecart:+.1f}°)")
            if abs(ecart) < tol_deg:
                print(f"  → cap {cap_deg}° atteint")
                return
        if time.time() - start > timeout:
            raise TimeoutError(f"yaw {cap_deg}° non atteint en {timeout}s")
        time.sleep(0.3)


if __name__ == "__main__":
    NOMS = {0: "Nord", 90: "Est", 180: "Sud", 270: "Ouest"}
    CAPS = [90, 180, 270, 0]

    try:
        set_mode('GUIDED')
        arm()
        takeoff(15)
        for cap in CAPS:
            print(f"→ vise {cap}° ({NOMS[cap]})")
            point_to(cap)
            time.sleep(1)
        set_mode('LAND')
        print("Démo yaw terminée")
    except TimeoutError as e:
        print(f"\n⚠ ÉCHEC : {e} → LAND de sécurité")
        set_mode('LAND')
