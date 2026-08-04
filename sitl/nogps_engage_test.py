"""Preuve en SITL de l'engagement GUIDED_NOGPS — sans Gazebo, sans YOLO, sans caméra.

On remplace le détecteur par une **cible virtuelle** posée à une position connue,
et on synthétise ce que la caméra en verrait : une erreur horizontale normalisée
et une taille de bounding box. Le reste de la chaîne est le VRAI code de vol —
`control.guidance`, `control.gate`, `control.mavlink_backend` — et le vrai
firmware ArduCopter.

Ce que ça vérifie, et qu'aucun test unitaire ne peut vérifier :
  1. le mode 20 est atteignable et `SET_ATTITUDE_TARGET` y est bien accepté ;
  2. `thrust = 0,5` tient réellement l'altitude, sans GPS dans la boucle ;
  3. la loi converge : l'erreur horizontale part à zéro ;
  4. la garde sur la taille de bbox arrête l'approche à la bonne distance,
     au lieu de laisser le drone traverser la cible (il n'a aucun amortissement).

Le GPS du SITL sert de RÈGLE DE MESURE (distance vraie), jamais de commande.

⚠ Demande un SITL **fraîchement démarré** : la cible virtuelle est posée en NED
relatif à `home`, donc rejouer le test sans redémarrer part d'une géométrie où la
cible est déjà derrière le drone, hors du champ simulé.

    ./sitl/run_sitl.sh                                    # terminal 1
    perception/.venv/bin/python sitl/nogps_engage_test.py # terminal 2
"""
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "perception"))

from pymavlink import mavutil                                   # noqa: E402

from control import CommandGate, Limits, TargetView, VehicleState   # noqa: E402
from control.guidance import (GuidanceGains, VisualGuidance,        # noqa: E402
                              operator_command)
from control.mavlink_backend import MavlinkBackend                  # noqa: E402

CONN = os.environ.get("CONN", "udp:127.0.0.1:14551")
ALT = 12.0
TARGET_NED = (25.0, 8.0, 0.0)     # cible virtuelle : 25 m Nord, 8 m Est, au sol
TARGET_H = 1.7                    # m, hauteur de la "personne" -> taille de bbox
HFOV_HALF = 0.60                  # rad, demi-champ horizontal de la caméra simulée
VFOV_HALF = 0.45                  # rad, demi-champ vertical
HZ = 10.0
DUREE = 35.0        # phase 1 : le suivi engage
DUREE_OP = 12.0     # phase 2 : l'opérateur force « avancer » contre la garde


def view_from_geometry(pos_ned, heading):
    """Le simulacre de perception : géométrie -> ce que la caméra rapporterait.

    C'est le SEUL endroit qui utilise la position. La loi, elle, ne voit que
    deux nombres — décalage horizontal et taille — exactement comme en vol réel.
    """
    dn = TARGET_NED[0] - pos_ned[0]
    de = TARGET_NED[1] - pos_ned[1]
    dd = TARGET_NED[2] - pos_ned[2]
    sol = math.hypot(dn, de)
    portee = math.sqrt(sol * sol + dd * dd)

    releve = math.atan2(de, dn) - heading                   # gisement relatif au nez
    releve = (releve + math.pi) % (2 * math.pi) - math.pi
    if abs(releve) > math.pi / 2:                            # derrière : hors champ
        return TargetView(has=False), portee, sol
    err = math.tan(releve) / math.tan(HFOV_HALF)
    if abs(err) > 1.0:
        return TargetView(has=False), portee, sol
    taille = TARGET_H / max(2.0 * portee * math.tan(VFOV_HALF), 1e-3)
    return TargetView(has=True, found=True, error_x=err, size=min(taille, 1.0)), portee, sol


def main():
    m = mavutil.mavlink_connection(CONN)
    print(f"[test] connexion {CONN} ...")
    if not m.wait_heartbeat(timeout=20):
        sys.exit("pas de SITL")
    m.mav.request_data_stream_send(m.target_system, m.target_component,
                                   mavutil.mavlink.MAV_DATA_STREAM_ALL, 20, 1)
    backend = MavlinkBackend(m)
    gains = GuidanceGains()
    gate = CommandGate(backend, Limits(size_stop=gains.size_near))
    guidance = VisualGuidance(gains)
    backend.configure_nogps(0.5)

    for p, v in ((b"ARMING_CHECK", 0), (b"WP_YAW_BEHAVIOR", 0)):
        m.mav.param_set_send(m.target_system, m.target_component, p, v,
                             mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    time.sleep(0.5)

    print("[test] attente fix GPS 3D (règle de mesure, pas commande)...")
    t0 = time.time()
    while time.time() - t0 < 60:
        g = m.recv_match(type="GPS_RAW_INT", blocking=True, timeout=1)
        if g and g.fix_type >= 3:
            break

    print("[test] GUIDED -> arm -> décollage 12 m")
    m.set_mode(m.mode_mapping()["GUIDED"])
    time.sleep(1)
    t0 = time.time()
    while time.time() - t0 < 90:              # l'EKF peut encore être en train de
        m.mav.command_long_send(              # converger : on redemande, on n'abandonne pas
            m.target_system, m.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
        t1 = time.time()
        while time.time() - t1 < 3:
            msg = m.recv_match(type=["HEARTBEAT", "STATUSTEXT"], blocking=True, timeout=1)
            if msg and msg.get_type() == "STATUSTEXT" and "rm" in msg.text:
                print("   ", msg.text)
        if m.motors_armed():
            break
    else:
        sys.exit("armement refusé")
    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0,
                            0, 0, 0, 0, 0, 0, ALT)
    t0 = time.time()
    while time.time() - t0 < 40:
        p = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=2)
        if p and p.relative_alt / 1000.0 >= ALT * 0.9:
            break

    print("[test] bascule GUIDED_NOGPS (mode 20)")
    if not backend.set_mode("GUIDED_NOGPS"):
        sys.exit("GUIDED_NOGPS absent du mode_mapping")
    time.sleep(1.5)
    m.recv_match(type="HEARTBEAT", blocking=True, timeout=3)
    if m.flightmode != "GUIDED_NOGPS":
        sys.exit(f"mode refusé, on est en {m.flightmode}")
    print(f"[test] mode = {m.flightmode}\n")

    pos = [0.0, 0.0, -ALT]
    hdg = 0.0
    hist = []                        # (t, phase, portée, sol, err, taille, alt, pitch)
    t0 = t_prev = time.time()
    print(f"{'t':>5} {'ph':>3} {'portée':>7} {'sol':>6} {'err':>6} {'taille':>7} "
          f"{'roll':>6} {'pitch':>6} {'alt':>6}  garde")
    while True:
        msg = m.recv_match(type=["LOCAL_POSITION_NED", "ATTITUDE", "HEARTBEAT"],
                           blocking=True, timeout=0.02)
        if msg:
            t = msg.get_type()
            if t == "LOCAL_POSITION_NED":
                pos = [msg.x, msg.y, msg.z]
            elif t == "ATTITUDE":
                hdg = msg.yaw
            elif t == "HEARTBEAT" and m.flightmode != "GUIDED_NOGPS":
                sys.exit(f"\nsorti du mode 20 -> {m.flightmode}")

        now = time.time()
        if now - t_prev < 1.0 / HZ:
            continue
        dt, t_prev = now - t_prev, now
        t = now - t0
        if t > DUREE + DUREE_OP:
            break
        # Phase 1 : le suivi engage. Phase 2 : l'OPÉRATEUR pousse « avancer » à
        # fond sur une cible déjà à distance de garde — la porte doit le refuser
        # exactement comme elle le refuserait à la loi (§1.5-A).
        phase = 1 if t <= DUREE else 2

        vue, portee, sol = view_from_geometry(pos, hdg)
        alt = -pos[2]
        if phase == 1:
            cmd = guidance.step(vue, engage=True, dt=dt)
        else:
            cmd = operator_command(fwd=1.0, right=0.0, up=0.0, max_tilt=gate.lim.max_tilt)
        res = gate.submit(cmd, VehicleState(flying=True, heading=hdg, alt=alt), vue)
        hist.append((t, phase, portee, sol, vue.error_x, vue.size, alt, res.cmd.pitch))

        if len(hist) % 10 == 0 or (phase == 2 and hist[-2][1] == 1):
            print(f"{t:5.1f} {phase:>3} {portee:7.1f} {sol:6.1f} {vue.error_x:+6.2f} "
                  f"{vue.size:7.3f} {math.degrees(res.cmd.roll):+6.1f} "
                  f"{math.degrees(res.cmd.pitch):+6.1f} {alt:6.1f}  "
                  f"{'·'.join(res.reasons) or '—'}")

    print("\n[test] LAND")
    m.set_mode(m.mode_mapping()["LAND"])

    # ── verdict ─────────────────────────────────────────────────────────────
    p1 = [h for h in hist if h[1] == 1]
    p2 = [h for h in hist if h[1] == 2]
    portee0 = p1[0][2]
    stab = p1[-int(5 * HZ):]                         # 5 dernières s de l'engagement
    err_fin = max(abs(h[4]) for h in stab)
    portee_fin = sum(h[2] for h in stab) / len(stab)
    ecart_stab = max(h[2] for h in stab) - min(h[2] for h in stab)
    alt_min, alt_max = min(h[6] for h in hist), max(h[6] for h in hist)
    sol_min = min(h[3] for h in hist)
    sol_min_op = min(h[3] for h in p2)
    pitch_max_op = max(-h[7] for h in p2)            # piqué le plus fort concédé
    garde = TARGET_H / (2 * gains.size_near * math.tan(VFOV_HALF))
    # LA mesure qui compte : le plafond de taille imposé par l'ALTITUDE de vol.
    # Un `size_near` au-dessus de ce plafond = une garde qui ne se déclenche
    # jamais = un drone qui survole la cible. C'est ce que ce test a trouvé.
    plafond = TARGET_H / (2 * ALT * math.tan(VFOV_HALF))

    print(f"\n  PHASE 1 — engagement")
    print(f"    portée        {portee0:.1f} m  ->  {portee_fin:.1f} m "
          f"(distance sol mini {sol_min:.1f})")
    print(f"    taille bbox   max vue {max(h[5] for h in hist):.3f}   "
          f"plafond géométrique à {ALT:.0f} m = {plafond:.3f}")
    print(f"    seuil size_near {gains.size_near:.2f} -> freinage vers {garde:.1f} m")
    print(f"    |erreur| finale {err_fin:.2f}   amplitude résiduelle {ecart_stab:.1f} m")
    print(f"    altitude      {alt_min:.1f} .. {alt_max:.1f} m (consigne {ALT})")
    print(f"  PHASE 2 — l'opérateur pousse « avancer » à fond")
    print(f"    distance sol mini {sol_min_op:.1f} m   "
          f"piqué max concédé {math.degrees(pitch_max_op):+.1f}°")
    print(f"  commandes émises {backend.sent}   bloquées par la porte {gate.blocked}")

    ok = True
    for nom, cond in [
        ("le seuil de garde est ATTEIGNABLE à cette altitude", gains.size_near < plafond),
        ("le drone s'est rapproché", portee_fin < portee0 - 5),
        ("il ne survole pas la cible", sol_min > 3.0),
        ("l'approche se stabilise (pas d'oscillation)", ecart_stab < 2.0),
        ("l'erreur horizontale converge", err_fin < 0.25),
        ("l'altitude tient sans GPS dans la boucle",
         abs(alt_max - ALT) < 3 and abs(alt_min - ALT) < 3),
        ("la porte refuse le piqué à l'OPÉRATEUR", math.degrees(pitch_max_op) < 1.0),
        ("... et elle l'a dit", gate.blocked > 0),
        ("l'opérateur n'a pas pu percer la garde", sol_min_op > 3.0),
    ]:
        print(f"  [{'OK ' if cond else 'NON'}] {nom}")
        ok &= bool(cond)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
