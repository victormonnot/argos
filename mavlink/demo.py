"""Voir un message MAVLink : de nos variables Python -> des octets -> retour.

Aucun drone, aucun réseau. On fabrique un ARGOS_TARGET, on regarde les octets
qui partiraient sur le fil, et on les redonne au décodeur pour vérifier qu'on
retrouve exactement ce qu'on avait mis.

    ../perception/.venv/bin/python demo.py      # depuis mavlink/
"""
import os
import sys
import time
from pathlib import Path

os.environ["MAVLINK20"] = "1"          # notre id (44000) dépasse 255 : MAVLink 2 obligatoire
sys.path.insert(0, str(Path(__file__).resolve().parent / "generated" / "python"))
try:
    import argos
except ImportError:
    sys.exit("dialecte non généré — lance `make` dans mavlink/ d'abord")


def hexa(b):
    return " ".join(f"{x:02X}" for x in b)


# ── 1. ce que la console SAIT, dans ses variables Python ────────────────────
mav = argos.MAVLink(None, srcSystem=42, srcComponent=190)   # 42:190 = « la console »
msg = argos.MAVLink_argos_target_message(
    time_usec=int(time.time() * 1e6),      # instant de la CAPTURE de l'image
    u=-0.25,                               # cible à 25 % à gauche du centre
    v=0.10,                                # un peu sous le centre
    size=0.12,                             # boîte = 12 % de la largeur d'image
    confidence=0.87,
    track_age_ms=1240,
    track_id=7,
    target_class=argos.ARGOS_CLASS_VEHICLE,
    lock_state=argos.ARGOS_LOCK_TRACK,
    flags=argos.ARGOS_TARGET_FLAG_ENGAGED)

print("1) CE QU'ON VEUT DIRE (variables Python)")
print(f"   cible {msg.target_class} · u={msg.u} v={msg.v} taille={msg.size} "
      f"conf={msg.confidence} piste #{msg.track_id}\n")

# ── 2. la même chose, en octets ─────────────────────────────────────────────
trame = msg.pack(mav)

print(f"2) LA TRAME QUI PART SUR LE FIL — {len(trame)} octets")
print(f"   {hexa(trame)}\n")

# Découpage d'une trame MAVLink 2 (le format est figé, ces positions ne bougent
# jamais ; c'est ce qui permet à n'importe quel logiciel de la lire).
entete, charge, crc = trame[:10], trame[10:-2], trame[-2:]
print("   en-tête (10 octets, toujours au même endroit) :")
print(f"     {hexa(entete[0:1])}      marqueur MAVLink 2 (0xFD ; 0xFE = MAVLink 1)")
print(f"     {hexa(entete[1:2])}      longueur de la charge utile = {entete[1]} octets")
print(f"     {hexa(entete[2:4])}   drapeaux (signature, compatibilité)")
print(f"     {hexa(entete[4:5])}      n° de séquence — c'est LUI qui donne la perte de paquets")
print(f"     {hexa(entete[5:7])}   émetteur : système {entete[5]}, composant {entete[6]}")
print(f"     {hexa(entete[7:10])} identifiant du message sur 24 bits = "
      f"{int.from_bytes(entete[7:10], 'little')} (ARGOS_TARGET)")
print(f"   charge utile ({len(charge)} octets) : nos 10 champs, collés bout à bout")
print(f"     {hexa(charge)}")
print(f"   {hexa(crc)}   contrôle d'intégrité (inclut une empreinte du FORMAT :")
print("            un bout qui n'aurait pas la même définition rejette la trame)\n")

# ── 3. le chemin inverse : des octets vers des champs ───────────────────────
recu = argos.MAVLink(None).parse_char(trame)

print("3) CE QUE LE DÉCODEUR EN RESSORT")
print(f"   type       {recu.get_type()}")
print(f"   émetteur   {recu.get_srcSystem()}:{recu.get_srcComponent()}")
for nom in recu.fieldnames:
    print(f"   {nom:<13}{getattr(recu, nom)}")
print(f"\n   âge de l'information : {(time.time() - recu.time_usec / 1e6) * 1000:.1f} ms")
print("   (le message porte l'instant de la CAPTURE, donc n'importe quel")
print("    consommateur calcule lui-même sur quelle fraîcheur il agit)")
