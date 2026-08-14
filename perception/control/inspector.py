"""Inspecteur du flux MAVLink montant (PORTFOLIO §1.3).

`link.py` répond « combien » : cadence, perte, débit, latence. Cet inspecteur
répond **« quoi »** : quels messages passent, à quelle fréquence chacun, ce qu'ils
contiennent, et — quand la question devient sérieuse — quels octets exactement.

Trois raisons d'avoir les octets bruts à côté des champs décodés :
  - un champ qui semble faux peut venir d'un mauvais décodage OU d'une valeur
    réellement fausse. Sans les octets, on ne peut pas trancher ;
  - un message d'un dialecte inconnu n'a aucun champ à afficher, mais il a
    toujours des octets et un identifiant. C'est là qu'on découvre qu'un
    équipement parle un dialecte qu'on n'a pas ;
  - la version de trame (0xFE = MAVLink 1, 0xFD = MAVLink 2) ne se lit nulle part
    ailleurs que dans le premier octet, et elle change la longueur maximale des
    identifiants — donc ce qu'on peut se permettre de définir dans son dialecte.

Ne garde qu'UN message par type : le dernier. Un inspecteur n'est pas un
enregistreur — les logs DataFlash et les .tlog font déjà ça, mieux.
"""
import threading
import time
from collections import deque

MAGIC_V1 = 0xFE
MAGIC_V2 = 0xFD


class MessageInspector:
    """Mémoire du flux : par type de message, le dernier exemplaire + sa cadence.

    Nourri depuis le même point que `LinkStats` — donc il voit TOUT le flux, y
    compris ce que la console ne sait pas interpréter. Un inspecteur qui ne
    montrerait que les messages déjà compris ne servirait à rien : c'est
    exactement l'inverse du besoin.
    """

    def __init__(self, fenetre: float = 5.0):
        self.fenetre = fenetre
        self._lock = threading.Lock()
        self._dernier = {}          # type -> (msg, t_reception)
        self._vus = {}              # type -> deque de timestamps (fenêtrée)
        self._total = {}            # type -> compteur depuis le démarrage

    # ── entrée ──────────────────────────────────────────────────────────────
    def on_msg(self, now: float, msg) -> None:
        """Un message reçu. Volontairement bon marché : on RANGE l'objet, on ne
        le décode pas. Le décodage n'a lieu que si quelqu'un regarde — à 180
        msg/s, décoder pour l'écran serait du travail jeté."""
        t = msg.get_type()
        with self._lock:
            self._dernier[t] = (msg, now)
            d = self._vus.get(t)
            if d is None:
                d = self._vus[t] = deque()
            d.append(now)
            limite = now - self.fenetre
            while d and d[0] < limite:
                d.popleft()
            self._total[t] = self._total.get(t, 0) + 1

    # ── sorties ─────────────────────────────────────────────────────────────
    def table(self, now: float) -> list:
        """Une ligne par type vu récemment, la plus bavarde en premier."""
        with self._lock:
            types = list(self._dernier.items())
            vus = {t: len(d) for t, d in self._vus.items()}
            total = dict(self._total)
        lignes = []
        for t, (msg, t_rx) in types:
            n = vus.get(t, 0)
            lignes.append({
                "type": t,
                "id": msg.get_msgId(),
                "hz": round(n / self.fenetre, 1),
                "total": total.get(t, 0),
                "octets": len(msg.get_msgbuf()),
                "age_ms": round((now - t_rx) * 1000),
                "src": f"{msg.get_srcSystem()}:{msg.get_srcComponent()}",
            })
        return sorted(lignes, key=lambda x: (-x["hz"], x["type"]))

    def detail(self, type_: str, now: float):
        """Le dernier exemplaire d'un type : champs décodés ET octets bruts."""
        with self._lock:
            entree = self._dernier.get(type_)
        if entree is None:
            return None
        msg, t_rx = entree
        brut = bytes(msg.get_msgbuf())
        return {
            "type": type_,
            "id": msg.get_msgId(),
            "src": f"{msg.get_srcSystem()}:{msg.get_srcComponent()}",
            "seq": msg.get_seq(),
            "age_ms": round((now - t_rx) * 1000),
            "version": 2 if brut[:1] == bytes([MAGIC_V2]) else 1,
            "octets": len(brut),
            "hex": brut.hex(" ").upper(),
            "entete": self._entete(brut),
            "champs": self._champs(msg),
        }

    # ── décodage à la demande ───────────────────────────────────────────────
    @staticmethod
    def _champs(msg) -> list:
        """[(nom, valeur, type déclaré), ...] dans l'ordre de la DÉFINITION.

        L'ordre déclaré n'est pas l'ordre sur le fil : MAVLink réordonne les
        champs par taille décroissante à l'encodage, pour que chacun tombe sur
        une frontière alignée. Comparer les octets bruts au tableau des champs
        sans le savoir mène à conclure qu'on décode mal alors que tout va bien.
        """
        out = []
        types = dict(zip(msg.get_fieldnames(), getattr(msg, "fieldtypes", [])))
        for nom in msg.get_fieldnames():
            v = getattr(msg, nom, None)
            if isinstance(v, (bytes, bytearray)):
                v = bytes(v).rstrip(b"\x00").decode("utf-8", "replace")
            elif isinstance(v, (list, tuple)):
                v = list(v)
            out.append({"nom": nom, "valeur": v, "type": types.get(nom, "")})
        return out

    @staticmethod
    def _entete(brut: bytes) -> list:
        """Découpage de l'en-tête. Les positions sont figées par le protocole :
        c'est ce qui permet à n'importe quel logiciel de router un message sans
        savoir ce qu'il contient."""
        if not brut:
            return []
        if brut[0] == MAGIC_V2 and len(brut) >= 10:
            return [
                ("marqueur", f"{brut[0]:02X}", "MAVLink 2"),
                ("longueur", f"{brut[1]:02X}", f"{brut[1]} octets de charge utile"),
                ("drapeaux", f"{brut[2]:02X} {brut[3]:02X}", "incompat / compat"),
                ("sequence", f"{brut[4]:02X}", f"{brut[4]} — les trous = la perte"),
                ("emetteur", f"{brut[5]:02X} {brut[6]:02X}", f"systeme {brut[5]}, composant {brut[6]}"),
                ("identifiant", brut[7:10].hex(" ").upper(),
                 f"{int.from_bytes(brut[7:10], 'little')} sur 24 bits"),
            ]
        if brut[0] == MAGIC_V1 and len(brut) >= 6:
            return [
                ("marqueur", f"{brut[0]:02X}", "MAVLink 1"),
                ("longueur", f"{brut[1]:02X}", f"{brut[1]} octets de charge utile"),
                ("sequence", f"{brut[2]:02X}", f"{brut[2]}"),
                ("emetteur", f"{brut[3]:02X} {brut[4]:02X}", f"systeme {brut[3]}, composant {brut[4]}"),
                ("identifiant", f"{brut[5]:02X}", f"{brut[5]} sur 8 bits — 255 max"),
            ]
        return [("brut", brut[:1].hex().upper(), "marqueur inconnu")]
