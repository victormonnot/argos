"""Banc du dialecte ARGOS (PORTFOLIO §1.3).

Un protocole ne se teste pas « à l'œil sur le HUD » : les deux modes de panne qui
comptent sont invisibles en vol.

  1. **La collision d'ID.** Deux définitions pour le même numéro, et deux logiciels
     décodent la même trame différemment — sans jamais lever d'erreur, puisque
     chacun croit avoir raison. C'est le pire mode de panne d'un protocole, et le
     seul moyen de s'en protéger est de re-vérifier le bloc à chaque exécution.
  2. **La dérive entre les deux langages.** Le CRC_EXTRA de MAVLink est un hash de
     la SIGNATURE du message (noms, types, ordre). Deux bouts qui n'ont pas le même
     CRC_EXTRA rejettent mutuellement leurs trames. Le comparer entre le Python
     généré et le C généré, c'est vérifier mécaniquement la promesse « une source,
     deux langages ».

    ../perception/.venv/bin/python test_dialect.py     # depuis mavlink/
"""
import glob
import os
import re
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent
GEN_PY = ICI / "generated" / "python" / "argos.py"
GEN_C = ICI / "generated" / "c" / "argos" / "mavlink_msg_argos_target.h"

BLOC = range(44000, 44100)          # le bloc revendiqué par ARGOS
ID_ARGOS_TARGET = 44000

os.environ["MAVLINK20"] = "1"       # id > 255 : ce message n'existe qu'en MAVLink 2
sys.path.insert(0, str(GEN_PY.parent))
if not GEN_PY.exists():
    sys.exit("dialecte non généré — lance `make` dans mavlink/")
import argos                                                    # noqa: E402


def _dialectes_amont():
    """Les XML livrés par pymavlink, hors le nôtre."""
    import pymavlink
    d = Path(pymavlink.__file__).parent / "dialects" / "v20"
    return [f for f in glob.glob(str(d / "*.xml"))]


# ── 1. le bloc d'ID est-il toujours libre ? ──────────────────────────────────
def test_bloc_44000_libre_chez_tout_le_monde():
    pris = {}
    for f in _dialectes_amont():
        txt = Path(f).read_text(encoding="utf-8", errors="replace")
        for mid, nom in re.findall(r'<message\s+id="(\d+)"\s+name="([A-Z0-9_]+)"', txt):
            if int(mid) in BLOC:
                pris[int(mid)] = (nom, Path(f).name)
    assert not pris, (
        f"COLLISION : le bloc ARGOS 44000-44099 n'est plus libre -> {pris}. "
        "Changer de bloc dans argos.xml AVANT de faire voler quoi que ce soit.")


def test_le_nom_argos_target_est_unique():
    for f in _dialectes_amont():
        txt = Path(f).read_text(encoding="utf-8", errors="replace")
        assert 'name="ARGOS_TARGET"' not in txt, f"nom déjà pris dans {f}"


# ── 2. le message est bien celui qu'on croit ─────────────────────────────────
def test_id_et_taille():
    assert argos.MAVLINK_MSG_ID_ARGOS_TARGET == ID_ARGOS_TARGET
    m = argos.MAVLink_argos_target_message
    # 8 + 4*4 + 4 + 2 + 3*1 = 33 octets utiles
    assert m.unpacker.size == 33, m.unpacker.size
    assert m.fieldnames == ["time_usec", "u", "v", "size", "confidence",
                            "track_age_ms", "track_id", "target_class",
                            "lock_state", "flags"]


def test_le_dialecte_contient_toujours_ardupilotmega():
    """Sans ça, ~30 % du flux descendant devient indécodable (AHRS, MEMINFO...)."""
    assert argos.MAVLINK_MSG_ID_AHRS in argos.mavlink_map          # ardupilotmega
    assert argos.MAVLINK_MSG_ID_ATTITUDE in argos.mavlink_map      # common


def test_aucun_champ_de_position():
    """Invariant GNSS-denied porté jusque dans le protocole : un consommateur de
    ce message ne PEUT PAS reconstruire une position, l'information n'y est pas."""
    interdits = ("lat", "lon", "alt", "x", "y", "z", "north", "east", "down",
                 "range", "distance", "heading", "yaw")
    for nom in argos.MAVLink_argos_target_message.fieldnames:
        assert nom not in interdits, f"champ de position interdit : {nom}"


# ── 3. aller-retour d'encodage ───────────────────────────────────────────────
def _mav():
    return argos.MAVLink(None, srcSystem=42, srcComponent=190)


def test_round_trip():
    mav = _mav()
    envoye = argos.MAVLink_argos_target_message(
        time_usec=1_754_000_000_123_456, u=-0.25, v=0.5, size=0.12,
        confidence=0.87, track_age_ms=1234, track_id=7,
        target_class=argos.ARGOS_CLASS_VEHICLE,
        lock_state=argos.ARGOS_LOCK_COAST,
        flags=argos.ARGOS_TARGET_FLAG_ENGAGED)
    buf = envoye.pack(mav)
    recu = _mav().parse_char(buf)
    assert recu is not None and recu.get_type() == "ARGOS_TARGET"
    assert recu.time_usec == 1_754_000_000_123_456
    assert abs(recu.u + 0.25) < 1e-6 and abs(recu.size - 0.12) < 1e-6
    assert recu.track_id == 7 and recu.target_class == 2
    assert recu.lock_state == argos.ARGOS_LOCK_COAST
    assert recu.flags & argos.ARGOS_TARGET_FLAG_ENGAGED


def test_trame_v2_et_troncature_des_zeros():
    """MAVLink 2 tronque les octets nuls de FIN de charge utile. Une cible à zéro
    voyage donc plus court qu'une cible pleine — sur une radio à 57 600 baud ce
    n'est pas un détail, et ça surprend quand on lit les octets bruts."""
    mav = _mav()
    plein = argos.MAVLink_argos_target_message(
        time_usec=1, u=1.0, v=1.0, size=1.0, confidence=1.0,
        track_age_ms=99, track_id=1, target_class=1, lock_state=1, flags=1).pack(mav)
    vide = argos.MAVLink_argos_target_message(
        time_usec=1, u=0.0, v=0.0, size=0.0, confidence=0.0,
        track_age_ms=0, track_id=0, target_class=0, lock_state=0, flags=0).pack(_mav())
    assert plein[0] == 0xFD and vide[0] == 0xFD          # marqueur MAVLink 2
    assert len(vide) < len(plein)
    assert _mav().parse_char(vide).u == 0.0              # décodé pareil malgré tout


# ── 4. la promesse « une source, deux langages », vérifiée mécaniquement ─────
def test_crc_extra_identique_en_python_et_en_c():
    if not GEN_C.exists():
        sys.exit(f"en-tête C absent ({GEN_C}) — lance `make c` dans mavlink/")
    entete = GEN_C.read_text(encoding="utf-8")
    crc_c = int(re.search(r"#define MAVLINK_MSG_ID_ARGOS_TARGET_CRC (\d+)", entete).group(1))
    crc_py = argos.mavlink_map[ID_ARGOS_TARGET].crc_extra
    assert crc_c == crc_py, (
        f"CRC_EXTRA divergent : C={crc_c} Python={crc_py}. Les deux bouts "
        "rejetteraient mutuellement leurs trames.")
    len_c = int(re.search(r"#define MAVLINK_MSG_ID_ARGOS_TARGET_LEN (\d+)", entete).group(1))
    assert len_c == argos.MAVLink_argos_target_message.unpacker.size


if __name__ == "__main__":
    ok = 0
    for nom, fn in sorted(globals().items()):
        if nom.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {nom}")
            ok += 1
    print(f"\n{ok} tests verts.")
