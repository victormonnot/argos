"""control/radio.py — la radio de pilotage comme PÉRIPHÉRIQUE D'ENTRÉE (PORTFOLIO §1.2, HITL-2).

La RadioMaster en mode « USB Joystick » n'est **pas une liaison** : c'est un
capteur d'intention opérateur. La distinction porte tout le reste du fichier.

**Ce qu'on ne fait PAS : `RC_CHANNELS_OVERRIDE`.** C'est la solution évidente
(QGroundControl et le module `joystick` de MAVProxy font ça), et elle est fausse
ici pour trois raisons :

1. Elle injecte les manches **au niveau RC du firmware**, donc en amont de tout
   ce que ce projet a construit : la commande n'entre plus par `CommandGate`, la
   garde de proximité ne s'applique plus. §1.5-A dit qu'il n'existe qu'une porte
   de sortie ; un override RC en fabrique une deuxième, invisible.
2. En `GUIDED_NOGPS` les canaux RC ne commandent de toute façon pas l'attitude —
   c'est `SET_ATTITUDE_TARGET` qui le fait. L'override serait ignoré ou, pire,
   interprété par un autre étage.
3. La radio produit une **intention** (« avance », « va à droite »), pas un
   protocole. Exprimée en `AttitudeCmd`, exactement comme la loi de guidage
   (§1.5-B), elle repartira telle quelle sur le CRSF du whoop plus tard.

Donc : evdev -> axes normalisés -> `AttitudeCmd` -> `CommandGate` -> backend.
Le même chemin que le suivi automatique, et le même garde-fou.

**Pourquoi evdev à la main et pas `pygame` / `python-evdev`.** Le noyau WSL2
n'a pas `CONFIG_INPUT_JOYDEV` : `/dev/input/js0` n'existera jamais ici. Reste
`/dev/input/eventN`, qui est un flux de `struct input_event` de 24 octets. Les
décoder soi-même coûte quarante lignes, ne rajoute aucune dépendance, et donne
accès aux `ioctl` de calibration que les couches hautes cachent — dont celui
qui sert de battement de cœur (voir `_sonde`).

    struct input_event {          struct input_absinfo {
        struct timeval time;          __s32 value;      <- position courante
        __u16 type;                   __s32 minimum;    <- butée basse
        __u16 code;                   __s32 maximum;    <- butée haute
        __s32 value;                  __s32 fuzz, flat, resolution;
    };                            };

Utilisation autonome (aucun drone, aucun SITL requis) :

    python3 -m control.radio          # table live des axes : bouge un manche, regarde
"""
import fcntl
import glob
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field

# ── Le protocole d'entrée du noyau, ce qui nous en sert ─────────────────────
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
_EVENT = struct.Struct("llHHi")        # struct input_event — 24 octets sur x86-64
_ABSINFO = struct.Struct("iiiiii")     # struct input_absinfo — 6 × __s32

# Noms des axes absolus (linux/input-event-codes.h). Le pilote HID générique
# répartit les canaux de la radio là-dedans dans un ordre qui dépend du
# descripteur HID d'EdgeTX — on ne le devine pas, on le CONSTATE (mode calibration).
ABS_NOMS = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z",
    0x03: "ABS_RX", 0x04: "ABS_RY", 0x05: "ABS_RZ",
    0x06: "ABS_THROTTLE", 0x07: "ABS_RUDDER", 0x08: "ABS_WHEEL",
    0x09: "ABS_GAS", 0x0a: "ABS_BRAKE",
    0x10: "ABS_HAT0X", 0x11: "ABS_HAT0Y", 0x12: "ABS_HAT1X", 0x13: "ABS_HAT1Y",
    0x14: "ABS_HAT2X", 0x15: "ABS_HAT2Y", 0x16: "ABS_HAT3X", 0x17: "ABS_HAT3Y",
    0x18: "ABS_PRESSURE", 0x19: "ABS_DISTANCE",
    0x1a: "ABS_TILT_X", 0x1b: "ABS_TILT_Y", 0x1c: "ABS_TOOL_WIDTH",
    0x20: "ABS_VOLUME", 0x28: "ABS_MISC",
}

# Motif du nom de périphérique. EdgeTX s'annonce en général « RadioMaster
# Pocket Joystick » ou « OpenTX ... Joystick ». Réglable : ARGOS_RADIO_MOTIF.
MOTIF = os.environ.get("ARGOS_RADIO_MOTIF", r"radiomaster|edgetx|opentx|joystick|gamepad")


def _ioc(sens, typ, nr, taille):
    """Reconstruit un numéro d'ioctl comme la macro `_IOC` du noyau."""
    return (sens << 30) | (taille << 16) | (ord(typ) << 8) | nr


_IOC_READ = 2
EVIOCGNAME = lambda n: _ioc(_IOC_READ, "E", 0x06, n)
EVIOCGABS = lambda code: _ioc(_IOC_READ, "E", 0x40 + code, _ABSINFO.size)


def _ioctl(fd, requete, tampon):
    """`fcntl.ioctl` avec le repli signé.

    Les numéros d'ioctl en lecture ont le bit 31 à 1 (sens = 2 décalé de 30) :
    selon la version de Python, l'argument est attendu en entier non signé ou
    en `int` C signé. On tente les deux plutôt que de parier sur la version.
    """
    try:
        return fcntl.ioctl(fd, requete, tampon)
    except OverflowError:
        return fcntl.ioctl(fd, requete - (1 << 32), tampon)


@dataclass
class Axe:
    """Un axe absolu et sa calibration, telle que le PILOTE la déclare.

    On ne code jamais les butées en dur : elles viennent du descripteur HID via
    `EVIOCGABS`. Un firmware EdgeTX qui change d'échelle (0..2047 -> -1024..1023)
    ne casse donc rien.
    """
    code: int
    nom: str
    mini: int
    maxi: int
    flat: int = 0                     # zone morte annoncée par le pilote
    brut: int = 0

    def norme(self) -> float:
        """-1..1, centre à 0. La convention de toute la couche décision."""
        span = self.maxi - self.mini
        if span <= 0:
            return 0.0
        return 2.0 * (self.brut - self.mini) / span - 1.0


@dataclass
class RadioEtat:
    """Photo instantanée de la radio. Aucune notion de drone, de MAVLink ni de
    canal : juste « où sont les manches », normalisé."""
    presente: bool = False
    chemin: str = ""
    nom: str = ""
    axes: dict = field(default_factory=dict)        # nom d'axe -> -1..1
    bruts: dict = field(default_factory=dict)       # nom d'axe -> valeur entière
    boutons: dict = field(default_factory=dict)     # nom de touche -> 0/1
    age_mouvement: float = 0.0     # s depuis le dernier ÉVÉNEMENT reçu
    age_sonde: float = 0.0         # s depuis la dernière réponse du périphérique
    evenements: int = 0


def trouver(motif: str = MOTIF) -> list:
    """Les `/dev/input/event*` qui ressemblent à une radio. Rend [(chemin, nom)]."""
    trouves = []
    rx = re.compile(motif, re.I)
    for chemin in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(chemin, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue                        # droits insuffisants : on passe
        try:
            tampon = bytearray(256)
            _ioctl(fd, EVIOCGNAME(len(tampon)), tampon)
            nom = tampon.split(b"\x00")[0].decode("utf-8", "replace")
            if rx.search(nom):
                trouves.append((chemin, nom))
        except OSError:
            pass
        finally:
            os.close(fd)
    return trouves


class Radio:
    """Lit un `/dev/input/eventN` dans son propre fil. Zéro dépendance.

    ⚠ **Un périphérique d'entrée n'a pas de battement de cœur.** evdev est
    événementiel : un manche immobile n'émet RIEN. L'âge du dernier événement ne
    dit donc pas si la radio est vivante — seulement si elle a bougé. Pour la
    liveness on interroge périodiquement le pilote (`EVIOCGABS`, `_sonde`), qui
    répond même sans mouvement et échoue net quand l'USB est débranché. C'est la
    différence exacte entre une liaison (qui a des messages réguliers, §1.5-C) et
    un périphérique (qui n'en a pas) — et c'est pour ça que la radio ne peut pas
    être instrumentée avec `LinkStats`.
    """

    PERIODE_SONDE = 0.5               # s entre deux interrogations du pilote

    def __init__(self, chemin: str | None = None, motif: str = MOTIF):
        self.chemin = chemin or os.environ.get("ARGOS_RADIO") or ""
        self.motif = motif
        self.nom = ""
        self._fd = -1
        self._axes: dict = {}          # code -> Axe
        self._boutons: dict = {}       # code -> 0/1
        self._verrou = threading.Lock()
        self._t_evt = 0.0
        self._t_sonde = 0.0
        self._n = 0
        self._stop = threading.Event()
        self._fil: threading.Thread | None = None

    # ── ouverture / fermeture ────────────────────────────────────────────────
    def _ouvrir(self) -> bool:
        chemin = self.chemin
        if not chemin:
            candidats = trouver(self.motif)
            if not candidats:
                return False
            chemin, _ = candidats[0]
        try:
            fd = os.open(chemin, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return False
        tampon = bytearray(256)
        try:
            _ioctl(fd, EVIOCGNAME(len(tampon)), tampon)
            nom = tampon.split(b"\x00")[0].decode("utf-8", "replace")
        except OSError:
            nom = "?"
        # Calibration : on demande au pilote les butées de chaque axe possible.
        # Un axe absent fait échouer l'ioctl -> il n'entre pas dans la table.
        axes = {}
        for code in ABS_NOMS:
            info = bytearray(_ABSINFO.size)
            try:
                _ioctl(fd, EVIOCGABS(code), info)
            except OSError:
                continue
            valeur, mini, maxi, _fuzz, flat, _res = _ABSINFO.unpack(bytes(info))
            if maxi <= mini:
                continue               # axe déclaré mais sans échelle : inutilisable
            axes[code] = Axe(code, ABS_NOMS[code], mini, maxi, flat, valeur)
        if not axes:
            os.close(fd)
            return False               # un périphérique sans axe n'est pas une radio
        with self._verrou:
            self._fd, self.chemin, self.nom, self._axes = fd, chemin, nom, axes
            self._boutons = {}
            self._t_evt = self._t_sonde = time.time()
        return True

    def _fermer(self):
        with self._verrou:
            if self._fd >= 0:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            self._fd = -1

    # ── le fil ───────────────────────────────────────────────────────────────
    def start(self) -> "Radio":
        if self._fil is None:
            self._fil = threading.Thread(target=self._boucle, daemon=True)
            self._fil.start()
        return self

    def stop(self):
        self._stop.set()

    def _boucle(self):
        """Ouvre, lit, et se rebranche tout seul si la radio disparaît.

        Le rebranchement à chaud n'est pas du confort : côté WSL, un
        `usbipd detach` / `attach` change le numéro de `eventN`. Une console qui
        exige un redémarrage à chaque replug serait inutilisable en séance.
        """
        while not self._stop.is_set():
            if self._fd < 0:
                if not self._ouvrir():
                    time.sleep(1.0)
                    continue
            try:
                self._lire()
                self._sonde()
            except OSError:
                self._fermer()          # débranchée : on repartira en découverte
                time.sleep(0.5)
            time.sleep(0.002)

    def _lire(self):
        """Vide la file d'événements. Non bloquant : rien à lire n'est pas une erreur."""
        try:
            paquet = os.read(self._fd, _EVENT.size * 64)
        except BlockingIOError:
            return
        for i in range(0, len(paquet) - _EVENT.size + 1, _EVENT.size):
            _sec, _usec, typ, code, valeur = _EVENT.unpack_from(paquet, i)
            with self._verrou:
                if typ == EV_ABS and code in self._axes:
                    self._axes[code].brut = valeur
                elif typ == EV_KEY:
                    self._boutons[code] = 1 if valeur else 0
                elif typ == EV_SYN:
                    continue            # marqueur de fin de rapport, rien à ranger
                self._t_evt = time.time()
                self._n += 1

    def _sonde(self):
        """Le battement de cœur qu'evdev ne fournit pas.

        `EVIOCGABS` lit la position COURANTE d'un axe, y compris quand rien ne
        bouge. Il échoue (`ENODEV`) dès que le périphérique s'en va. C'est donc
        lui, et non l'âge du dernier événement, qui répond à « la radio est-elle
        encore là ? ».
        """
        now = time.time()
        if now - self._t_sonde < self.PERIODE_SONDE:
            return
        with self._verrou:
            codes = list(self._axes)
        if not codes:
            return
        info = bytearray(_ABSINFO.size)
        _ioctl(self._fd, EVIOCGABS(codes[0]), info)     # OSError -> _boucle rebranche
        valeur, *_ = _ABSINFO.unpack(bytes(info))
        with self._verrou:
            self._axes[codes[0]].brut = valeur
            self._t_sonde = now

    # ── lecture par les consommateurs ────────────────────────────────────────
    def etat(self) -> RadioEtat:
        now = time.time()
        with self._verrou:
            if self._fd < 0:
                return RadioEtat(presente=False, chemin=self.chemin)
            return RadioEtat(
                presente=True, chemin=self.chemin, nom=self.nom,
                axes={a.nom: round(a.norme(), 4) for a in self._axes.values()},
                bruts={a.nom: a.brut for a in self._axes.values()},
                boutons=dict(self._boutons),
                age_mouvement=round(now - self._t_evt, 2),
                age_sonde=round(now - self._t_sonde, 2),
                evenements=self._n,
            )


# ── mode calibration : ce qu'on lance AVANT d'écrire la moindre cartographie ──
def _barre(v: float, largeur: int = 31) -> str:
    """Une jauge -1..1 en ASCII. Le centre est marqué : un manche au repos qui
    ne tombe pas sur `|` trahit un trim ou un `SUBTRIM` EdgeTX, pas un bug ici."""
    milieu = largeur // 2
    pos = int(round((v + 1.0) / 2.0 * (largeur - 1)))
    pos = max(0, min(largeur - 1, pos))
    cases = ["-"] * largeur
    cases[milieu] = "|"
    cases[pos] = "#"
    return "".join(cases)


def _cli():
    candidats = trouver()
    if not candidats:
        print("Aucune radio trouvée dans /dev/input/.")
        print()
        print("  1. la radio est-elle en mode USB Joystick (HID) ?")
        print("     (EdgeTX demande le mode au branchement : Joystick / Serial / Storage)")
        print("  2. côté Windows, en PowerShell ADMIN :")
        print("       usbipd list                 # repérer le BUSID de la radio")
        print("       usbipd bind   --busid X-Y")
        print("       usbipd attach --wsl --busid X-Y")
        print("  3. côté WSL :")
        print("       sudo modprobe vhci-hcd evdev usbhid hid-generic")
        print("       ls -l /dev/input/")
        print()
        print("  périphériques présents :", sorted(glob.glob('/dev/input/event*')) or "aucun")
        return

    print("Radios candidates :")
    for chemin, nom in candidats:
        print(f"  {chemin}  {nom}")
    radio = Radio(candidats[0][0]).start()
    time.sleep(0.3)
    print("\nBouge les manches et les inters. Ctrl-C pour sortir.\n")
    try:
        while True:
            e = radio.etat()
            lignes = [
                "\033[H\033[2J",
                f"  {e.nom}   ({e.chemin})",
                f"  événements {e.evenements:<8} dernier mouvement {e.age_mouvement:>5.1f} s"
                f"   sonde {e.age_sonde:>4.1f} s"
                + ("" if e.presente else "   *** RADIO ABSENTE ***"),
                "",
                f"  {'axe':<14} {'brut':>6}  {'norme':>6}   {'-1':<15}{'+1':>16}",
            ]
            for nom, v in e.axes.items():
                lignes.append(f"  {nom:<14} {e.bruts[nom]:>6}  {v:>+6.3f}   {_barre(v)}")
            if e.boutons:
                lignes += ["", "  boutons : " + "  ".join(
                    f"{c}={v}" for c, v in sorted(e.boutons.items()))]
            print("\n".join(lignes), flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        radio.stop()
        print()


if __name__ == "__main__":
    _cli()
