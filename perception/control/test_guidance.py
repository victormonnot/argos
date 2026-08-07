"""Tests au banc de la loi de guidage et de la porte de sortie.

Discipline du projet : la loi de commande est une fonction pure, testée avant de
voler. Ici on teste aussi la porte (§1.5-A) — la régression qu'on veut interdire
pour toujours, c'est « l'opérateur contourne la garde de proximité ».

    perception/.venv/bin/python -m control.test_guidance      # depuis perception/
    perception/.venv/bin/python -m pytest control/            # si pytest est là
"""
import math

from .commands import AttitudeCmd, TargetView, VehicleState
from .gate import CommandGate, Limits
from .guidance import DEG, GuidanceGains, VisualGuidance, operator_command
from .mavlink_backend import MASK_ANGLE, MASK_RATES, quat_from_euler


class FakeBackend:
    """Backend qui n'envoie rien : on inspecte ce que la porte a laissé passer."""
    name = "fake"

    def __init__(self):
        self.calls = []

    def send_attitude(self, cmd, heading):
        self.calls.append((cmd, heading))

    def send_ctbr(self, cmd):
        self.calls.append((cmd, None))


def _far(err, size=0.10, found=True):
    return TargetView(has=True, found=found, error_x=err, size=size)


# ── la loi ──────────────────────────────────────────────────────────────────
def test_pas_de_cible_pas_de_commande():
    c = VisualGuidance().step(TargetView(has=False), engage=True, dt=0.1)
    assert (c.roll, c.pitch, c.dyaw) == (0.0, 0.0, 0.0)
    assert c.thrust == 0.5, "0,5 = tenir l'altitude, jamais 0"


def test_le_roll_pointe_vers_la_cible_et_est_symetrique():
    g = VisualGuidance()
    droite = g.step(_far(+0.5), engage=False, dt=0.1)
    g.reset()
    gauche = g.step(_far(-0.5), engage=False, dt=0.1)
    assert droite.roll > 0, "cible à droite -> incliner à droite"
    assert gauche.roll < 0
    assert math.isclose(droite.roll, -gauche.roll, abs_tol=1e-9)


def test_le_terme_D_amortit_une_cible_qui_revient_au_centre():
    """Le cœur du §1.1 : sans retour de vitesse, un P pur dépasse. Une cible qui
    se recentre vite doit produire MOINS de roll qu'une cible immobile à la même
    erreur — c'est la vision qui joue le rôle du retour de vitesse."""
    fixe = VisualGuidance()
    for _ in range(20):
        c_fixe = fixe.step(_far(+0.5), engage=False, dt=0.1)

    mobile = VisualGuidance()
    for err in (0.9, 0.8, 0.7, 0.6, 0.5):        # se recentre à ~1,0 /s
        c_mobile = mobile.step(_far(err), engage=False, dt=0.1)

    assert c_mobile.roll < c_fixe.roll, "le D doit retenir l'inclinaison"


def test_le_terme_D_freine_une_approche_rapide():
    """Le pendant du test précédent, sur l'axe d'approche. Sans lui, un P pur sur
    la distance oscille et l'oscillation DIVERGE : le drone arrive à la bonne
    distance lancé, dépasse, corrige plus fort, et finit par perdre la cible.
    À taille identique, une cible qui grossit vite doit produire moins de piqué."""
    fixe = VisualGuidance()
    for _ in range(20):
        c_fixe = fixe.step(_far(0.0, size=0.09), engage=True, dt=0.1)

    fonce = VisualGuidance()
    for i in range(10):                          # 0,072 -> 0,090, soit 0,02 /s
        c_fonce = fonce.step(_far(0.0, size=0.072 + 0.002 * i), engage=True, dt=0.1)

    assert c_fonce.pitch > c_fixe.pitch, "s'approcher vite doit réduire le piqué"


def test_sans_kd_size_le_freinage_disparait():
    """Interrupteur du défaut : à kd_size = 0 on retrouve exactement le P pur."""
    g = GuidanceGains(kd_size=0.0)
    fonce = VisualGuidance(g)
    for i in range(10):
        c = fonce.step(_far(0.0, size=0.072 + 0.002 * i), engage=True, dt=0.1)
    approach, brake = fonce._closure(0.09)
    attendu = -g.k_pitch * approach + g.k_brake * brake
    assert math.isclose(c.pitch, attendu, abs_tol=1e-9)


def test_le_coast_ne_provoque_pas_de_pic_de_derivee():
    g = VisualGuidance()
    for _ in range(5):
        g.step(_far(+0.3), engage=False, dt=0.1)
    perdu = g.step(_far(+0.3, found=False), engage=False, dt=0.1)
    assert abs(perdu.roll) <= abs(g.g.kp_roll * 0.3) + 1e-9


# ── la garde de distance, capteur = taille de bbox ───────────────────────────
def test_sans_engage_aucune_avance():
    c = VisualGuidance().step(_far(0.0, size=0.05), engage=False, dt=0.1)
    assert c.pitch == 0.0


def test_loin_on_avance_pres_on_arrete_trop_pres_on_freine():
    g = GuidanceGains()
    loin = VisualGuidance().step(_far(0.0, size=g.size_far), engage=True, dt=0.1)
    garde = VisualGuidance().step(_far(0.0, size=g.size_near), engage=True, dt=0.1)
    colle = VisualGuidance().step(_far(0.0, size=g.size_near + g.size_brake),
                                  engage=True, dt=0.1)
    assert loin.pitch < 0, "piqué = avance"
    assert math.isclose(garde.pitch, 0.0, abs_tol=1e-9), "distance de garde -> on tient"
    assert colle.pitch > 0, "trop près -> cabré = freinage"
    assert loin.pitch < garde.pitch < colle.pitch, "la taille de bbox module l'avance"


# ── le cap : relatif, borné, avec zone morte ────────────────────────────────
def test_le_cap_est_borne_et_a_une_zone_morte():
    g = VisualGuidance()
    assert g.step(_far(0.05), engage=False, dt=0.1).dyaw == 0.0, "zone morte"
    g.reset()
    fort = g.step(_far(1.0), engage=False, dt=0.1)
    assert 0 < fort.dyaw <= g.g.max_dyaw + 1e-9


def test_dt_aberrant_ne_fait_pas_exploser_la_commande():
    g = VisualGuidance()
    g.step(_far(-0.9), engage=True, dt=0.1)
    c = g.step(_far(+0.9), engage=True, dt=1e-9)          # boucle qui bégaie
    lim = g.g.max_tilt + 1e-9
    assert abs(c.roll) <= lim and abs(c.pitch) <= lim


# ── la porte de sortie (§1.5-A) ─────────────────────────────────────────────
def test_au_sol_rien_ne_sort():
    gate = CommandGate(FakeBackend())
    r = gate.submit(AttitudeCmd(pitch=-0.2), VehicleState(flying=False))
    assert not r.sent and not gate._backend.calls


def test_la_porte_ecrete_au_dela_des_bornes():
    be = FakeBackend()
    gate = CommandGate(be, Limits(max_tilt=10 * DEG))
    r = gate.submit(AttitudeCmd(roll=45 * DEG, pitch=-45 * DEG),
                    VehicleState(flying=True))
    assert r.sent and "écrêté" in r.reasons
    assert math.isclose(r.cmd.roll, 10 * DEG) and math.isclose(r.cmd.pitch, -10 * DEG)


def test_LA_REGRESSION_loperateur_ne_contourne_pas_la_garde_de_proximite():
    """Le bug de conception que le §1.5-A corrige : avant, seul le suivi était
    gardé. Ici l'opérateur pousse « avancer » plein pot sur une cible collée —
    la porte doit lui refuser le piqué, exactement comme au suivi."""
    be = FakeBackend()
    gate = CommandGate(be)
    proche = TargetView(has=True, found=True, error_x=0.0, size=0.50)
    etat = VehicleState(flying=True)

    r_op = gate.submit(operator_command(fwd=1.0, right=0.0, up=0.0), etat, proche)
    r_track = gate.submit(VisualGuidance().step(proche, engage=True, dt=0.1), etat, proche)

    assert r_op.cmd.pitch >= 0.0, "l'opérateur ne doit PAS pouvoir avancer"
    assert r_track.cmd.pitch >= 0.0
    assert any("proximité" in x for x in r_op.reasons)
    assert all(c.pitch >= 0.0 for c, _ in be.calls)


def test_la_garde_laisse_freiner():
    gate = CommandGate(FakeBackend())
    proche = TargetView(has=True, found=True, error_x=0.0, size=0.50)
    r = gate.submit(AttitudeCmd(pitch=+0.1), VehicleState(flying=True), proche)
    assert math.isclose(r.cmd.pitch, 0.1), "cabrer pour freiner reste autorisé"


def test_loin_la_garde_ne_bride_rien():
    gate = CommandGate(FakeBackend())
    loin = TargetView(has=True, found=True, error_x=0.0, size=0.05)
    r = gate.submit(AttitudeCmd(pitch=-0.1), VehicleState(flying=True), loin)
    assert math.isclose(r.cmd.pitch, -0.1) and not r.reasons


def test_loperateur_parle_le_meme_type_que_la_loi():
    c = operator_command(fwd=1.0, right=-0.5, up=1.0)
    assert isinstance(c, AttitudeCmd) and c.source == "operator"
    assert c.pitch < 0 and c.roll < 0 and c.thrust > 0.5


# ── l'encodage MAVLink ──────────────────────────────────────────────────────
def _to_euler(q):
    w, x, y, z = q
    return (math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
            math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))),
            math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))


def test_le_quaternion_est_unitaire_et_reversible():
    """Le handler refuse le message si la norme s'écarte de 1 de plus de 1e-3 —
    et il le refuse en SILENCE (`hold_position()`), donc autant le vérifier ici."""
    for r, p, y in [(0, 0, 0), (0.2, -0.1, 1.7), (-0.26, 0.26, -3.0)]:
        q = quat_from_euler(r, p, y)
        assert math.isclose(math.sqrt(sum(v * v for v in q)), 1.0, abs_tol=1e-9)
        er, ep, ey = _to_euler(q)
        assert math.isclose(er, r, abs_tol=1e-6)
        assert math.isclose(ep, p, abs_tol=1e-6)
        assert math.isclose(ey, y, abs_tol=1e-6)


def test_les_masques_respectent_la_regle_du_tout_ou_rien():
    """Un mélange de bits `*_RATE_IGNORE` -> `hold_position()` silencieux."""
    assert MASK_ANGLE == 0b00000111, "les 3 rates ignorés, attitude + poussée lues"
    assert MASK_RATES == 0b10000000, "attitude ignorée, les 3 rates + poussée lus"
    for mask in (MASK_ANGLE, MASK_RATES):
        assert not (mask & 0b01000000), "THROTTLE_IGNORE -> hold_position()"
        assert (mask & 0b111) in (0b000, 0b111), "tout ou rien sur les rates"


if __name__ == "__main__":
    ok = 0
    for nom, fn in sorted(globals().items()):
        if nom.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {nom}")
            ok += 1
    print(f"\n{ok} tests verts.")
