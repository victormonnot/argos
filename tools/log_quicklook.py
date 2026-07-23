#!/home/victorfixe/venv-ardupilot/bin/python
"""Bilan de santé rapide d'un log DataFlash ArduPilot (.bin).

Usage :
    tools/log_quicklook.py <chemin/vers/log.bin> [autres.bin ...]
    (les chemins Windows sont visibles depuis WSL sous /mnt/c/... ;
     le shebang pointe sur ~/venv-ardupilot qui contient pymavlink —
     pas besoin d'activer un venv, et le venv perception n'irait pas)

Ce qu'on lit, et pourquoi :
  VIBE  — vibrations vues par les accéléros (m/s/s). Moyenne < ~20 = sain,
          > 30 = problème mécanique (hélice abîmée, vis desserrée, plots écrasés).
          Clip = nombre de saturations accéléro : doit rester ~0 (pics lors
          d'impacts/atterrissages durs = tolérés).
  RCOU  — commande envoyée à chaque moteur (µs, ~1000-2000). Au hover, les 4
          doivent être proches (< ~10 % d'écart) : sinon CG décalé, moteur
          faible ou frame vrillée.
  ATT   — attitude demandée (DesRoll/DesPitch) vs obtenue (Roll/Pitch).
          Erreur moyenne < 2-3° = les PID suivent. Oscillation visible = tune.
  BAT   — tension et courant. On surveille l'affaissement sous charge et la
          plausibilité du capteur de courant.
  ERR   — erreurs système (subsystem/code). En intérieur sans fix GPS, les
          erreurs EKF (16/17) et GPS glitch (11) sont attendues.
"""
import sys
from pymavlink import mavutil

VIBE_OK, VIBE_BAD = 20.0, 30.0
RCOU_SPREAD_OK = 0.10
ATT_ERR_OK = 3.0


def analyze(path: str) -> None:
    print("=" * 70)
    print("LOG:", path)
    m = mavutil.mavlink_connection(path)
    vibes, clips, rcou, att, bat, errs = [], [0], [], [], [], []
    t0 = t1 = None
    while True:
        msg = m.recv_match(type=["VIBE", "RCOU", "ATT", "BAT", "ERR"], blocking=False)
        if msg is None:
            break
        t = getattr(msg, "TimeUS", None)
        if t is not None:
            t0 = t if t0 is None else t0
            t1 = t
        k = msg.get_type()
        if k == "VIBE":
            vibes.append((msg.VibeX, msg.VibeY, msg.VibeZ))
            clips = [getattr(msg, "Clip", 0)]
        elif k == "RCOU":
            rcou.append((msg.C1, msg.C2, msg.C3, msg.C4))
        elif k == "ATT":
            att.append((msg.DesRoll, msg.Roll, msg.DesPitch, msg.Pitch))
        elif k == "BAT":
            bat.append((msg.Volt, msg.Curr))
        elif k == "ERR":
            errs.append((msg.Subsys, msg.ECode))

    if t0:
        print(f"durée : {(t1 - t0) / 1e6:.0f}s")

    fly = [r for r in rcou if max(r) > 1200]
    if fly:
        avg = [sum(c[i] for c in fly) / len(fly) for i in range(4)]
        spread = (max(avg) - min(avg)) / (sum(avg) / 4)
        flag = "OK" if spread < RCOU_SPREAD_OK else "ATTENTION déséquilibre"
        print(f"MOTEURS  moy poussée C1-C4 : {[f'{a:.0f}' for a in avg]}  écart {spread * 100:.1f}%  [{flag}]")
    else:
        print("MOTEURS  aucun échantillon en poussée (log au sol ?)")

    if vibes:
        n = len(vibes)
        worst = 0.0
        for i, ax in enumerate("XYZ"):
            vals = [v[i] for v in vibes]
            mean = sum(vals) / n
            worst = max(worst, mean)
            print(f"VIBE {ax}   moy {mean:.1f}  max {max(vals):.1f}")
        flag = "OK" if worst < VIBE_OK else ("LIMITE" if worst < VIBE_BAD else "PROBLEME MECANIQUE")
        print(f"VIBE     clipping {clips}  [{flag}]")

    if att:
        er = sum(abs(a[0] - a[1]) for a in att) / len(att)
        ep = sum(abs(a[2] - a[3]) for a in att) / len(att)
        flag = "OK" if max(er, ep) < ATT_ERR_OK else "ATTENTION suivi PID"
        print(f"ATT      err moy roll {er:.1f}°  pitch {ep:.1f}°  [{flag}]")

    if bat:
        v = [b[0] for b in bat]
        c = [b[1] for b in bat]
        print(f"BAT      V min {min(v):.2f} / moy {sum(v) / len(v):.2f}   I max {max(c):.1f}A")

    if errs:
        print(f"ERR      {errs}")
        print("         (rappel indoor sans fix : 16/17=EKF, 11=GPS glitch = attendues)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        analyze(p)
