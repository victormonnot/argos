"""control/radio_map.py — des axes bruts à une INTENTION d'opérateur (HITL-2).

Même découpe que le reste du projet : `radio.py` est le *pilote* (octets evdev ->
axes normalisés), ce fichier est la *sémantique* (axes -> qui commande, et quoi).
Pur : ni MAVLink, ni console, ni drone. Donc testable au banc, comme la loi de
guidage — et c'est ce qui permet de vérifier une cartographie SANS décoller.

    radio.py      : /dev/input/eventN -> RadioEtat   (8 axes, -1..1)
    radio_map.py  : RadioEtat         -> Intention   (autorité + manches + actions)
    console.py    : Intention         -> AttitudeCmd -> CommandGate -> véhicule

Cartographie **constatée** sur la RadioMaster Pocket en mode USB Joystick (le
pilote HID répartit les canaux comme il veut ; rien ici n'est deviné) :

    ABS_Z         manche G vertical   +1 = haut     -> gaz     (NE SE RECENTRE PAS)
    ABS_RX        manche G horizontal +1 = droite   -> lacet
    ABS_Y         manche D vertical   +1 = haut     -> avance
    ABS_X         manche D horizontal +1 = droite   -> droite
    ABS_RZ        inter G 3 crans     +1 = haut     -> AUTORITÉ
    ABS_THROTTLE  inter D 3 crans     +1 = haut     -> ENGAGE ; -1 = bas -> REPLI
    ABS_RY        inter G 2 crans     +1 = enfoncé  -> LOCK
    ABS_RUDDER    inter D 2 crans     +1 = enfoncé  -> ABANDON

Trois règles de sûreté, qui sont le vrai contenu de ce fichier :

1. **Aucune prise d'autorité silencieuse au branchement.** Tant que le sélecteur
   n'a pas été *bougé* depuis la connexion, la radio ne commande rien et la
   console garde exactement le comportement qu'elle avait sans elle. Brancher un
   périphérique ne doit jamais changer qui pilote.
2. **Transfert sans à-coup sur les gaz.** Le manche des gaz ne se recentre pas :
   on mémorise sa position à la prise de main et on ne commande que l'ÉCART.
   La prise de main vaut donc toujours `thrust = 0,5` — tenir l'altitude.
3. **Radio absente -> HOLD, jamais AUTO.** Perdre l'opérateur ne doit pas
   promouvoir le pilote automatique. La dégradation va vers moins d'autorité,
   jamais vers plus.
"""
from dataclasses import dataclass, field

from .radio import RadioEtat

# ── la cartographie, telle que mesurée ──────────────────────────────────────
AXE_GAZ = "ABS_Z"
AXE_LACET = "ABS_RX"
AXE_AVANCE = "ABS_Y"
AXE_DROITE = "ABS_X"
INTER_AUTORITE = "ABS_RZ"          # 3 crans
INTER_ENGAGE = "ABS_THROTTLE"      # 3 crans, et les DEUX extrêmes servent :
                                   # haut = engagement, bas = repli (RTL). Un seul
                                   # axe pour « va vers la cible » / « rentre », et
                                   # le neutre entre les deux.
INTER_LOCK = "ABS_RY"              # 2 crans
INTER_ABANDON = "ABS_RUDDER"       # 2 crans

ZONE_MORTE = 0.05      # au repos les manches lisent ±0,002 ; 0,05 couvre large
SEUIL_CRAN = 0.5       # frontière entre les crans d'un inter 3 positions


class Autorite:
    """QUI commande ce cycle. Un seul à la fois, c'est tout l'intérêt (§1.5-A)."""
    ABSENTE = "absente"      # pas de radio -> la console web garde la main
    INACTIVE = "inactive"    # radio là, sélecteur pas encore bougé -> idem
    ABANDON = "abandon"      # inter d'abandon tiré -> plus personne ne commande
    HOLD = "hold"            # sélecteur en bas -> le drone tient, à plat
    MANUEL = "manuel"        # sélecteur au milieu -> les manches commandent
    AUTO = "auto"            # sélecteur en haut -> la loi de guidage commande
    REPLI = "repli"          # cran bas de l'inter d'engagement -> RTL, le firmware
                             # rentre tout seul et la console cesse de commander


@dataclass(frozen=True)
class Intention:
    """Ce que l'opérateur demande, normalisé. Aucun angle, aucune poussée : la
    conversion en `AttitudeCmd` appartient à `operator_command()`, pas ici."""
    autorite: str = Autorite.ABSENTE
    avance: float = 0.0        # -1..1, + = vers l'avant
    droite: float = 0.0        # -1..1, + = vers la droite
    monte: float = 0.0         # -1..1, + = monter (ÉCART depuis la prise de main)
    lacet: float = 0.0         # -1..1, + = tourner à droite
    lock: bool = False         # position de l'inter, pas un front
    engage: bool = False
    rtl: bool = False          # cran bas de l'inter d'engagement
    abandon: bool = False
    actions: tuple = ()        # fronts consommables : "lock", "unlock", "engage",
                               # "disengage", "rtl", "fin_repli", "abandon", "reprise"
    raison: str = ""           # pourquoi cette autorité — affiché au HUD


def _cran(v: float) -> int:
    """Décode un inter 3 positions : -1 bas, 0 milieu, +1 haut."""
    if v >= SEUIL_CRAN:
        return 1
    if v <= -SEUIL_CRAN:
        return -1
    return 0


def _haut(v: float) -> bool:
    """Décode un inter 2 positions. Au repos EdgeTX envoie -1, pas 0."""
    return v > 0.0


def _mort(v: float, zone: float = ZONE_MORTE) -> float:
    """Zone morte AVEC remise à l'échelle : sans ça, la commande sauterait de 0 à
    `zone` dès qu'on sort de la zone. On veut une pente continue à partir de 0."""
    if -zone < v < zone:
        return 0.0
    signe = 1.0 if v > 0 else -1.0
    return signe * min((abs(v) - zone) / (1.0 - zone), 1.0)


class Cartographie:
    """Traduit une suite de `RadioEtat` en `Intention`. A une mémoire (fronts,
    origine des gaz, armement) — donc un objet, pas une fonction."""

    def __init__(self):
        self._arme = False              # le sélecteur a-t-il bougé depuis la connexion ?
        self._sel_vu = None             # dernière position connue du sélecteur
        self._lock = False
        self._engage = False
        self._rtl = False
        self._abandon = False
        self._gaz_ref = None            # origine des gaz, posée à la prise de main
        self._presente = False

    def _reset_connexion(self):
        """Une radio qui revient est une radio inconnue : on repart en INACTIVE.
        Sinon un rebranchement en plein vol rendrait l'autorité d'un coup, à la
        position où traînent les inters — exactement le scénario interdit."""
        self._arme = False
        self._sel_vu = None
        self._gaz_ref = None

    def lire(self, etat: RadioEtat) -> Intention:
        # ── 1. la radio est-elle là ? ────────────────────────────────────────
        if not etat.presente:
            if self._presente:
                self._reset_connexion()
            self._presente = False
            return Intention(autorite=Autorite.ABSENTE,
                             raison="aucune radio sur /dev/input")
        if not self._presente:
            self._reset_connexion()
        self._presente = True

        a = etat.axes
        manque = [n for n in (AXE_GAZ, AXE_LACET, AXE_AVANCE, AXE_DROITE,
                              INTER_AUTORITE) if n not in a]
        if manque:
            return Intention(autorite=Autorite.ABSENTE,
                             raison=f"axes absents : {', '.join(manque)}")

        sel = _cran(a[INTER_AUTORITE])
        lock = _haut(a.get(INTER_LOCK, -1.0))
        # ⚠ défaut 0.0 et non -1.0 : un axe manquant doit valoir NEUTRE. Avec
        # -1.0 par défaut, une cartographie incomplète demanderait un RTL.
        cran_eng = _cran(a.get(INTER_ENGAGE, 0.0))
        engage_sw, rtl_sw = cran_eng > 0, cran_eng < 0
        abandon = _haut(a.get(INTER_ABANDON, -1.0))

        # ── 2. armement : pas de prise d'autorité silencieuse ────────────────
        if self._sel_vu is None:
            self._sel_vu = sel
        elif sel != self._sel_vu:
            self._sel_vu = sel
            self._arme = True

        # ── 3. les fronts, calculés AVANT tout filtrage par l'autorité ───────
        # Un inter qu'on bouge pendant que la radio n'a pas la main ne doit pas
        # déclencher l'action plus tard, quand elle la prend : on met l'état à
        # jour dans tous les cas, on ne publie l'action que si elle a la main.
        actions = []
        if lock != self._lock:
            actions.append("lock" if lock else "unlock")
        if engage_sw != self._engage:
            actions.append("engage" if engage_sw else "disengage")
        if rtl_sw != self._rtl:
            actions.append("rtl" if rtl_sw else "fin_repli")
        if abandon != self._abandon:
            actions.append("abandon" if abandon else "reprise")
        self._lock, self._engage = lock, engage_sw
        self._rtl, self._abandon = rtl_sw, abandon

        if not self._arme:
            return Intention(autorite=Autorite.INACTIVE, lock=lock,
                             engage=engage_sw, rtl=rtl_sw, abandon=abandon,
                             raison="bouge le sélecteur pour prendre la main")

        # ── 4. l'abandon prime sur tout ──────────────────────────────────────
        if abandon:
            self._gaz_ref = None
            return Intention(autorite=Autorite.ABANDON, lock=lock, abandon=True,
                             actions=tuple(actions),
                             raison="inter d'abandon tiré")

        # Le repli prime sur le sélecteur, mais pas sur l'abandon. Ordre voulu :
        # abandon (plus personne ne commande) > repli (le firmware commande) >
        # sélecteur (la console commande). On ne descend jamais d'un cran de
        # sûreté en montant d'un cran d'automatisme.
        if rtl_sw:
            self._gaz_ref = None
            return Intention(autorite=Autorite.REPLI, lock=lock, rtl=True,
                             actions=tuple(actions),
                             raison="repli demandé — RTL, la console ne commande plus")

        autorite = {(-1): Autorite.HOLD, 0: Autorite.MANUEL,
                    1: Autorite.AUTO}[sel]

        # ── 5. transfert sans à-coup des gaz ─────────────────────────────────
        # L'origine est posée à l'ENTRÉE en manuel et effacée à la sortie. Le
        # manche ne se recentrant pas, commander sa position absolue ferait
        # plonger ou grimper le drone à l'instant précis de la prise de main.
        gaz = a[AXE_GAZ]
        if autorite == Autorite.MANUEL:
            if self._gaz_ref is None:
                self._gaz_ref = gaz
            monte = _mort(max(-1.0, min(1.0, gaz - self._gaz_ref)))
        else:
            self._gaz_ref = None
            monte = 0.0

        if autorite != Autorite.MANUEL:
            # Hors manuel les manches ne commandent rien : les publier quand
            # même laisserait croire au HUD qu'ils agissent.
            return Intention(autorite=autorite, lock=lock,
                             engage=engage_sw and autorite == Autorite.AUTO,
                             actions=tuple(actions),
                             raison=("suivi automatique" if autorite == Autorite.AUTO
                                     else "sélecteur en bas — le drone tient"))

        return Intention(
            autorite=Autorite.MANUEL,
            avance=_mort(a[AXE_AVANCE]),
            droite=_mort(a[AXE_DROITE]),
            monte=monte,
            lacet=_mort(a[AXE_LACET]),
            lock=lock, engage=False, abandon=False,
            actions=tuple(actions),
            raison="manches opérateur",
        )


# ── vérification de la cartographie, sans drone ─────────────────────────────
def _cli():
    import time

    from .radio import Radio, trouver

    lisibles = [c for c in trouver() if c.lisible]
    if not lisibles:
        print("Pas de radio lisible — lance `python3 -m control.radio` pour le diagnostic.")
        return
    radio = Radio(lisibles[0].chemin).start()
    carte = Cartographie()
    journal = []
    time.sleep(0.3)
    try:
        while True:
            i = carte.lire(radio.etat())
            for act in i.actions:
                journal.append(f"{time.strftime('%H:%M:%S')}  {act}")
            print("\033[H\033[2J", flush=False)
            print(f"  AUTORITÉ   {i.autorite.upper():<10}  {i.raison}")
            print()
            print(f"  avance {i.avance:+6.2f}   droite {i.droite:+6.2f}"
                  f"   monte {i.monte:+6.2f}   lacet {i.lacet:+6.2f}")
            print(f"  lock {'OUI' if i.lock else 'non':<4}"
                  f"   engage {'OUI' if i.engage else 'non':<4}"
                  f"   repli {'OUI' if i.rtl else 'non':<4}"
                  f"   abandon {'OUI' if i.abandon else 'non'}")
            print()
            print("  derniers fronts :")
            for ligne in journal[-8:]:
                print("   ", ligne)
            time.sleep(0.1)
    except KeyboardInterrupt:
        radio.stop()
        print()


if __name__ == "__main__":
    _cli()
