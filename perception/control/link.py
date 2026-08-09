"""Instrumentation de la liaison (PORTFOLIO §1.5-C) — le canal comme objet de première classe.

Pure : `collections` et `dataclasses`, rien d'autre. Aucun MAVLink ici — on lui
donne des nombres, elle rend des nombres. C'est ce qui la rend testable au banc et
réutilisable sur la liaison CRSF du whoop, qui n'a pas du tout le même protocole.

**Le point clé, et il est gratuit :** chaque message MAVLink porte un numéro de
séquence sur 8 bits, incrémenté par l'émetteur. Les trous dans cette suite donnent
la perte de paquets **sans rien ajouter au protocole** — pas de champ, pas de
message de test, pas d'accord préalable. On lit ce qui passe déjà.

Ce qui transforme « je connais le format MAVLink » en « j'instrumente un lien
dégradé », et c'est l'instrument que réclame la phase 2 du plan swarm.

⚠ Deux limites à connaître avant de citer un chiffre :
  - le compteur est sur 8 bits : une rafale perdant plus de 255 messages d'affilée
    est sous-comptée. Sur les liaisons visées, ça n'arrive pas.
  - il faut lire **tout** le flux. Un filtre par type en amont fabrique des trous
    qui ressemblent à s'y méprendre à des pertes.
"""
from collections import deque
from dataclasses import dataclass, field

SEQ_MOD = 256
SAUT_SUSPECT = 64       # au-delà, c'est un désordre/redémarrage, pas une perte


@dataclass(frozen=True)
class LinkSnapshot:
    """Photo de la liaison sur la fenêtre glissante."""
    fenetre_s: float = 0.0
    rx_hz: float = 0.0
    rx_bps: float = 0.0             # octets/s reçus
    recus: int = 0
    perdus: int = 0
    perte_pct: float = 0.0
    desordres: int = 0              # sauts de séquence jugés non crédibles
    tx_hz: float = 0.0
    tx_bps: float = 0.0
    tx_trou_max_s: float = 0.0      # plus grand silence entre deux émissions
    latence_p50_ms: float | None = None
    latence_p95_ms: float | None = None
    par_message: list = field(default_factory=list)   # [(type, hz), ...]
    sources: list = field(default_factory=list)       # ["1:1", "255:190", ...]


def _pct(valeurs, p):
    if not valeurs:
        return None
    tri = sorted(valeurs)
    i = min(len(tri) - 1, max(0, int(round(p * (len(tri) - 1)))))
    return tri[i]


class LinkStats:
    """Comptabilité d'une liaison, sur une fenêtre glissante.

    Une instance par liaison. Le jour où il y en a deux (§1.2 : la vraie radio et
    le lien WiFi), on en met deux et on compare — c'est tout l'intérêt d'avoir
    séparé la mesure du transport.
    """

    def __init__(self, fenetre: float = 3.0):
        self.fenetre = fenetre
        self._rx = deque()          # (t, octets, type, perdus_avant_ce_message)
        self._tx = deque()          # (t, octets)
        self._rtt = deque()         # (t, ms)
        self._dernier_seq = {}      # (sysid, compid) -> dernier seq vu
        self.desordres = 0

    # ── entrées ─────────────────────────────────────────────────────────────
    def on_rx(self, now: float, sysid: int, compid: int, seq: int,
              octets: int, mtype: str):
        """Un message reçu. `seq` est le compteur 8 bits de son émetteur."""
        cle = (sysid, compid)
        perdus = 0
        precedent = self._dernier_seq.get(cle)
        if precedent is not None:
            saut = (seq - precedent - 1) % SEQ_MOD
            if saut > SAUT_SUSPECT:
                # Réordonnancement, doublon, ou émetteur qui a redémarré son
                # compteur. Compter ça comme des pertes gonflerait la mesure d'un
                # facteur énorme sur un seul événement.
                self.desordres += 1
            else:
                perdus = saut
        self._dernier_seq[cle] = seq
        self._rx.append((now, octets, mtype, perdus))

    def on_tx(self, now: float, octets: int):
        self._tx.append((now, octets))

    def on_rtt(self, now: float, ms: float):
        self._rtt.append((now, ms))

    # ── sortie ──────────────────────────────────────────────────────────────
    def _elaguer(self, now):
        limite = now - self.fenetre
        for d in (self._rx, self._tx, self._rtt):
            while d and d[0][0] < limite:
                d.popleft()

    def snapshot(self, now: float) -> LinkSnapshot:
        self._elaguer(now)
        f = self.fenetre
        recus = len(self._rx)
        perdus = sum(p for _, _, _, p in self._rx)
        attendus = recus + perdus

        par_type = {}
        for _, _, mtype, _ in self._rx:
            par_type[mtype] = par_type.get(mtype, 0) + 1

        t_tx = [t for t, _ in self._tx]
        trou = max((b - a for a, b in zip(t_tx, t_tx[1:])), default=0.0)
        lat = [ms for _, ms in self._rtt]

        return LinkSnapshot(
            fenetre_s=f,
            rx_hz=round(recus / f, 1),
            rx_bps=round(sum(o for _, o, _, _ in self._rx) / f, 1),
            recus=recus,
            perdus=perdus,
            perte_pct=round(100.0 * perdus / attendus, 2) if attendus else 0.0,
            desordres=self.desordres,
            tx_hz=round(len(self._tx) / f, 1),
            tx_bps=round(sum(o for _, o in self._tx) / f, 1),
            tx_trou_max_s=round(trou, 2),
            latence_p50_ms=None if not lat else round(_pct(lat, 0.50), 1),
            latence_p95_ms=None if not lat else round(_pct(lat, 0.95), 1),
            par_message=sorted(((t, round(n / f, 1)) for t, n in par_type.items()),
                               key=lambda x: -x[1])[:8],
            sources=[f"{s}:{c}" for s, c in sorted(self._dernier_seq)],
        )
