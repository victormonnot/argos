#!/home/victorfixe/venv-ardupilot/bin/python
"""Santé de la PLAGE DE POUSSÉE d'un multirotor ArduPilot, lue dans un log DataFlash.

Usage :
    tools/thrust_range.py <log.bin> [t_debut t_fin]
    (sans fenêtre, le script cherche lui-même le plus long vol stabilisé)

POURQUOI CE SCRIPT
------------------
ArduPilot ne raisonne pas en µs mais en « poussée normalisée » 0→1, où 0 = MOT_SPIN_MIN
et 1 = MOT_SPIN_MAX. Tout le mélangeur (AP_MotorsMatrix::output_armed_stabilizing) et
tout le contrôle d'altitude vivent dans cet espace-là.

Si le hover tombe très bas dans cette échelle (drone très surmotorisé), le mélangeur n'a
plus de marge pour BAISSER des moteurs afin de corriger l'assiette :
  - il rabote la commande roulis/tangage/lacet (rpy_scale < 1) → les gains PID n'ont
    plus d'effet, ils sont renormalisés ;
  - il lève le drapeau `limit` → les intégrateurs des boucles de rate sont gelés
    (anti-windup) → erreur d'assiette permanente, dérive ;
  - il impose une poussée moyenne plancher = MOT_THST_HOVER × ATC_THR_MIX_*, qui peut
    dépasser la poussée de hover réelle → le drone monte manche des gaz fermé.

Ce script mesure la poussée de hover RÉELLE en inversant la courbe de poussée
d'ArduPilot, et dit si la configuration laisse de la marge de contrôle.

CE QU'ON REGARDE
----------------
  hover  : sortie moteur moyenne en vol stabilisé (RCOU C1..C4)
  thrust : cette sortie reconvertie en poussée normalisée 0→1
           => c'est la valeur que devrait avoir MOT_THST_HOVER
  LIMIT  : % du temps où le mélangeur sature (PIDR.Flags bit 0)
           0-10 % = sain ; > 50 % = le contrôle d'assiette est bridé en permanence
  floor  : % du temps où au moins un moteur est collé sur MOT_SPIN_MIN
"""
import sys
import numpy as np
from pymavlink import mavutil

THRUST_HOVER_MIN = 0.125   # AP_MOTORS_THST_HOVER_MIN : plancher dur du firmware
THRUST_HOVER_TARGET = 0.25  # cible confortable (marge symétrique haut/bas)


def load(path, types):
    m = mavutil.mavlink_connection(path)
    out = {t: [] for t in types}
    params = {}
    while True:
        msg = m.recv_match(type=types + ['PARM'])
        if msg is None:
            break
        if msg.get_type() == 'PARM':
            params[msg.Name] = msg.Value
        else:
            out[msg.get_type()].append(msg.to_dict())
    arr = {t: ({} if not v else
               {k: np.array([d.get(k, np.nan) for d in v], dtype=float)
                for k in v[0] if k != 'mavpackettype'})
           for t, v in out.items()}
    return arr, params


def actuator_to_thrust(actuator, spin_min, spin_max, expo):
    """Inverse de AP_Motors_Thrust_Linearization::thrust_to_actuator().

    thrust_to_actuator(t) = spin_min + (spin_max - spin_min) * r(t)
    avec r(t) = (-(1-e) + sqrt((1-e)^2 + 4*e*t)) / (2*e)
    (lift_max = 1 tant que MOT_BAT_VOLT_MAX = 0, i.e. pas de compensation tension)
    """
    r = (actuator - spin_min) / max(spin_max - spin_min, 1e-9)
    if abs(expo) < 1e-6:
        return r
    return ((2 * expo * r + (1 - expo)) ** 2 - (1 - expo) ** 2) / (4 * expo)


def thrust_to_actuator(thrust, spin_min, spin_max, expo):
    if abs(expo) < 1e-6:
        r = thrust
    else:
        r = (-(1 - expo) + np.sqrt((1 - expo) ** 2 + 4 * expo * thrust)) / (2 * expo)
    return spin_min + (spin_max - spin_min) * r


def find_hover(d):
    """Plus longue plage où les 4 moteurs tournent en vol et le pilote commande des gaz."""
    tc = d['CTUN']['TimeUS'] / 1e6
    to = d['RCOU']['TimeUS'] / 1e6
    mot = np.vstack([d['RCOU'][f'C{i}'] for i in (1, 2, 3, 4)]).mean(0)
    moti = np.interp(tc, to, mot)
    fly = (moti > 1180) & (d['CTUN']['ThI'] > 0.005)
    idx = np.where(fly)[0]
    if len(idx) < 30:
        return None
    cuts = np.where(np.diff(idx) > 20)[0]
    starts = np.r_[idx[0], idx[cuts + 1]]
    ends = np.r_[idx[cuts], idx[-1]]
    best = np.argmax(tc[ends] - tc[starts])
    return tc[starts[best]], tc[ends[best]]


def main(path, t0=None, t1=None):
    d, P = load(path, ['CTUN', 'RCOU', 'PIDR', 'MOTB', 'ATT'])
    if not d['RCOU'] or not d['CTUN']:
        print("log sans RCOU/CTUN — rien à analyser"); return

    spin_min = P.get('MOT_SPIN_MIN', 0.15)
    spin_max = P.get('MOT_SPIN_MAX', 0.95)
    spin_arm = P.get('MOT_SPIN_ARM', 0.10)
    expo = P.get('MOT_THST_EXPO', 0.65)
    thst_hover = P.get('MOT_THST_HOVER', 0.35)

    if t0 is None:
        w = find_hover(d)
        if w is None:
            print("aucun vol trouvé dans ce log"); return
        t0, t1 = w
    print(f"\nfenêtre analysée : {t0:.1f} → {t1:.1f} s  ({t1-t0:.1f} s)")

    to = d['RCOU']['TimeUS'] / 1e6
    k = (to >= t0) & (to <= t1)
    mot = np.vstack([d['RCOU'][f'C{i}'] for i in (1, 2, 3, 4)])[:, k]
    hover_us = mot.mean()
    actuator = (hover_us - 1000.0) / 1000.0
    thrust = actuator_to_thrust(actuator, spin_min, spin_max, expo)

    print(f"\n--- paramètres moteurs ---")
    print(f"  MOT_SPIN_ARM  = {spin_arm:.3f}  ({1000+1000*spin_arm:.0f} us)")
    print(f"  MOT_SPIN_MIN  = {spin_min:.3f}  ({1000+1000*spin_min:.0f} us)  <- poussée « zéro » du modèle")
    print(f"  MOT_SPIN_MAX  = {spin_max:.3f}  ({1000+1000*spin_max:.0f} us)  <- poussée « pleine » du modèle")
    print(f"  MOT_THST_EXPO = {expo:.3f}")
    print(f"  MOT_THST_HOVER= {thst_hover:.3f}  (ce que le firmware CROIT)")

    print(f"\n--- mesure ---")
    print(f"  sortie moteur au hover      : {hover_us:.0f} us   (min {mot.min():.0f} / max {mot.max():.0f})")
    print(f"  marge sous le hover         : {hover_us - (1000+1000*spin_min):.0f} us avant MOT_SPIN_MIN")
    print(f"  POUSSÉE DE HOVER RÉELLE     : {thrust:.3f}   (échelle 0→1 d'ArduPilot)")
    print(f"  écart avec MOT_THST_HOVER   : x{thst_hover/max(thrust,1e-6):.1f}")

    if d['PIDR']:
        tp = d['PIDR']['TimeUS'] / 1e6
        kp = (tp >= t0) & (tp <= t1)
        lim = 100 * np.mean(d['PIDR']['Flags'][kp].astype(int) & 1)
    else:
        lim = float('nan')
    floor = 100 * np.mean((mot <= 1000 + 1000 * spin_min + 2).any(axis=0))
    print(f"  mélangeur saturé (LIMIT)    : {lim:.1f} % du temps")
    print(f"  ≥1 moteur collé au plancher : {floor:.1f} % du temps")
    if d['MOTB']:
        tm = d['MOTB']['TimeUS'] / 1e6
        km = (tm >= t0) & (tm <= t1)
        tho = np.interp(tm[km], d['CTUN']['TimeUS'] / 1e6, d['CTUN']['ThO'])
        print(f"  gaz demandés vs appliqués   : ThO {tho.mean():.4f}  ->  ThrOut {d['MOTB']['ThrOut'][km].mean():.4f} "
              f"(x{d['MOTB']['ThrOut'][km].mean()/max(tho.mean(),1e-9):.1f})")

    print(f"\n--- verdict ---")
    if thrust < THRUST_HOVER_MIN:
        print(f"  ✗ CRITIQUE : {thrust:.3f} < {THRUST_HOVER_MIN} = le plancher dur du firmware.")
        print(f"    MOT_THST_HOVER ne PEUT PAS descendre jusqu'à la vraie valeur (clamp dans")
        print(f"    get_throttle_hover()) → AltHold/Loiter imposeront toujours trop de gaz.")
    elif thrust < 0.18:
        print(f"  ⚠ BAS : {thrust:.3f}. Vol possible mais peu de marge de contrôle vers le bas.")
    else:
        print(f"  ✓ OK : {thrust:.3f}, marge de contrôle correcte.")
    if lim == lim and lim > 50:
        print(f"  ✗ Le mélangeur sature {lim:.0f} % du temps : les gains d'assiette sont")
        print(f"    renormalisés (rpy_scale < 1) et les intégrateurs gelés. Retoucher les PID")
        print(f"    dans cet état ne sert à rien — corriger d'abord la plage de poussée.")

    if thrust < THRUST_HOVER_TARGET:
        target_act = thrust_to_actuator(THRUST_HOVER_TARGET, 0.0, 1.0, expo)
        new_max = spin_min + (actuator - spin_min) / max(target_act, 1e-9)
        print(f"\n--- pour ramener le hover à {THRUST_HOVER_TARGET:.2f} (à MOT_SPIN_MIN={spin_min:.3f} inchangé) ---")
        print(f"  MOT_SPIN_MAX ≈ {new_max:.3f}  ({1000+1000*new_max:.0f} us de sortie max)")
        print(f"  puis MOT_THST_HOVER = {THRUST_HOVER_TARGET:.2f}")
        print(f"  (baisser d'abord MOT_SPIN_MIN par la calibration remonte déjà la poussée :")
        print(f"   relancer ce script après chaque changement, sur un nouveau vol.)")
    print()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    main(sys.argv[1],
         float(sys.argv[2]) if len(sys.argv) > 3 else None,
         float(sys.argv[3]) if len(sys.argv) > 3 else None)
