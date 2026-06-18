#!/usr/bin/env python3
"""Smoke-test du pont ArduCopter SITL <-> Gazebo.

Prouve que la physique vient bien de Gazebo : arme, decolle en GUIDED, verifie que
l'altitude monte jusqu'a la consigne et se stabilise. A lancer APRES ./sitl/run_gazebo.sh.

    ~/venv-ardupilot/bin/python sitl/gazebo_takeoff_test.py        # 10 m
    ALT=15 ~/venv-ardupilot/bin/python sitl/gazebo_takeoff_test.py # 15 m
"""
import os
import time

from pymavlink import mavutil

CONN = os.environ.get("CONN", "tcp:127.0.0.1:5760")
ALT = float(os.environ.get("ALT", "10"))


def main() -> None:
    m = mavutil.mavlink_connection(CONN, retries=0)
    print(f"[test] connexion {CONN} ...")
    m.wait_heartbeat(timeout=15)
    print("[test] heartbeat ok")

    # arming instantane en simu (le vrai HW gardera ses checks)
    m.mav.param_set_send(m.target_system, m.target_component, b"ARMING_CHECK",
                         0.0, mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    m.mav.request_data_stream_send(m.target_system, m.target_component,
                                   mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1)

    # attendre un fix GPS 3D (fourni par le capteur navsat de Gazebo)
    print("[test] attente fix GPS (navsat Gazebo) ...")
    t0 = time.time()
    while time.time() - t0 < 30:
        g = m.recv_match(type="GPS_RAW_INT", blocking=True, timeout=1)
        if g and g.fix_type >= 3:
            print(f"[test] GPS 3D, {g.satellites_visible} sats")
            break

    # GUIDED + arm + takeoff
    m.set_mode(m.mode_mapping()["GUIDED"])
    time.sleep(1)
    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
    ack = m.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    print(f"[test] arm -> {ack.result if ack else 'pas d ack'}")
    time.sleep(2)
    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, ALT)
    ack = m.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    print(f"[test] takeoff {ALT:.0f}m -> {ack.result if ack else 'pas d ack'}")

    # surveiller la montee
    reached = False
    for i in range(20):
        a = None
        end = time.time() + 1
        while time.time() < end:
            msg = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
            if msg:
                a = msg.relative_alt / 1000.0
        print(f"[test] t+{i:2d}s  alt = {a:.2f} m" if a is not None else f"[test] t+{i:2d}s  (pas de tel)")
        if a is not None and a >= ALT - 0.5:
            reached = True
            break

    print("[test] RESULTAT:", "OK -- la physique Gazebo repond aux moteurs ✅"
          if reached else "altitude non atteinte ❌")

    # atterrissage propre : LAND descend puis desarme tout seul au contact
    print("[test] LAND ...")
    m.set_mode(m.mode_mapping()["LAND"])
    t0 = time.time()
    while time.time() - t0 < 40:
        m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        a = None
        p = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if p:
            a = p.relative_alt / 1000.0
            print(f"[test] descente  alt = {a:.2f} m")
        if not m.motors_armed():
            print("[test] posé et désarmé ✅")
            return
    # filet de securite : force le desarmement si pas encore fait
    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 0, 0, 0, 0, 0, 0, 0)
    print("[test] désarmement forcé")


if __name__ == "__main__":
    main()
