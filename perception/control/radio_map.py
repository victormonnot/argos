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

**L'échelle d'autorité** (inter G), du « je pilote » au « il se débrouille » :

    bas     PILOTE   STABILIZE       les manches vont au FIRMWARE (override RC)
    milieu  MANUEL   GUIDED_NOGPS    les manches -> AttitudeCmd -> CommandGate
    haut    AUTO     GUIDED_NOGPS    la loi de guidage -> CommandGate

Le barreau du bas est le seul où ARGOS **n'émet rien** : c'est le firmware qui
vole, exactement comme sur le vrai drone où la RadioMaster parlera au contrôleur
de vol en **ELRS**, une liaison physiquement séparée que la console ne traverse
jamais. `RC_CHANNELS_OVERRIDE` en SITL est le substitut de cette liaison — pas
une deuxième porte dans le chemin ARGOS. Ce qui préserve le §1.5-A, c'est
l'**exclusivité** : un seul émetteur par barreau, garanti par cette machine à
états, et c'est le même inter qui commande le mode ArduPilot.

Trois règles de sûreté, qui sont le vrai contenu de ce fichier :

1. **Aucune prise d'autorité silencieuse au branchement.** Tant que le sélecteur
   n'a pas été *bougé* depuis la connexion, la radio ne commande rien et la
   console garde exactement le comportement qu'elle avait sans elle. Brancher un
   périphérique ne doit jamais changer qui pilote.
2. **Transfert sans à-coup sur les gaz.** Le manche des gaz ne se recentre pas :
   on mémorise sa position à la prise de main et on ne commande que l'ÉCART.
   La prise de main vaut donc toujours `thrust = 0,5` — tenir l'altitude.
3. **Radio absente -> jamais AUTO.** Perdre l'opérateur ne doit pas promouvoir
   le pilote automatique. La dégradation va vers moins d'autorité, jamais plus.
4. **Contrôle de position des gaz avant STABILIZE.** Ce mode lit les gaz en
   absolu : y entrer manche en bas coupe la poussée en vol. Le refus est latché,
   et renvoie sur le barreau du milieu — sûr par construction, donc toujours
   saisissable en urgence.
"""
import time
from dataclasses import dataclass

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
GAIN_MAX = 3.0         # facteur de remise à l'échelle maximal des gaz (voir _ecart)

# Prise des commandes en STABILIZE : les gaz y sont ABSOLUS (pas de tenue
# d'altitude, le manche commande la poussée directement). Entrer dans ce mode
# avec le manche en bas, c'est couper les gaz en vol. On exige donc qu'il soit
# proche du milieu — c'est le contrôle de position des gaz de n'importe quel
# poste de pilotage, et il n'existe QUE sur ce barreau : le barreau du milieu
# (GUIDED_NOGPS) est sans à-coup par construction et n'a rien à vérifier.
FENETRE_PRISE = 0.25

# Geste d'armement, celui d'un pilote RC : gaz au minimum + lacet à fond.
# L'exigence « gaz au mini » est elle-même la sécurité — on ne peut pas armer
# avec une commande de poussée en attente.
SEUIL_GESTE = 0.9      # |valeur| au-delà de laquelle un manche est « à fond »
MAINTIEN_ARM = 1.0     # s de maintien : un frôlement ne doit pas armer


class Autorite:
    """QUI commande ce cycle. Un seul à la fois, c'est tout l'intérêt (§1.5-A)."""
    ABSENTE = "absente"      # pas de radio -> la console web garde la main
    INACTIVE = "inactive"    # radio là, sélecteur pas encore bougé -> idem
    ABANDON = "abandon"      # inter d'abandon tiré -> plus personne ne commande
    PILOTE = "pilote"        # sélecteur en bas -> STABILIZE, les manches vont au
                             # FIRMWARE en override RC. ARGOS n'émet plus aucune
                             # consigne : c'est le substitut SITL de la liaison
                             # ELRS qui existera sur le vrai drone.
    MANUEL = "manuel"        # sélecteur au milieu -> GUIDED_NOGPS, les manches
                             # deviennent un AttitudeCmd et traversent la porte
    AUTO = "auto"            # sélecteur en haut -> GUIDED_NOGPS, la loi commande
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
    gaz_absolu: float = 0.0    # position BRUTE du manche des gaz, -1..1. Sert au
                               # seul barreau PILOTE : en STABILIZE le firmware
                               # attend une poussée absolue, pas un écart.
    marge_montee: float = 1.0  # autorité de montée réellement disponible (0..1) —
    marge_descente: float = 1.0  # < 1 quand l'origine des gaz est près d'une butée
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


def _ecart(v: float, ref: float) -> tuple:
    """Écart depuis l'origine des gaz, REMIS À L'ÉCHELLE de la course restante.

    Le transfert sans à-coup a un prix : si l'origine est à +0,6, il ne reste que
    0,4 de course vers le haut. Commander l'écart brut donnerait 0,4 au maximum —
    l'opérateur pousse à fond et le drone monte à 40 %. Inacceptable : c'est
    précisément quand on reprend la main en urgence qu'il faut toute l'autorité.

    On divise donc chaque moitié par sa course restante, ce qui rend la butée
    égale à ±1 quelle que soit l'origine. Contrepartie assumée : la sensibilité
    n'est plus la même vers le haut et vers le bas. On la borne à `GAIN_MAX` pour
    qu'une origine collée à la butée ne rende pas le manche inutilisable — dans ce
    cas l'autorité est réduite, et la marge est REMONTÉE pour que le HUD le dise
    plutôt que de le cacher.

    Rend (écart -1..1, marge montée 0..1, marge descente 0..1).
    """
    haut = min(1.0 / max(1.0 - ref, 1e-6), GAIN_MAX)
    bas = min(1.0 / max(1.0 + ref, 1e-6), GAIN_MAX)
    gain = haut if v >= ref else bas
    return (max(-1.0, min(1.0, (v - ref) * gain)),
            min(1.0, haut * (1.0 - ref)), min(1.0, bas * (1.0 + ref)))


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
        self._refus_pilote = False      # prise des commandes refusée, gaz mal placés
        self._precedent = Autorite.ABSENTE   # autorité du cycle précédent
        self._geste_t0 = None           # début du geste d'armement en cours
        self._geste_tire = False        # action déjà émise pour CE geste

    def _reset_connexion(self):
        """Une radio qui revient est une radio inconnue : on repart en INACTIVE.
        Sinon un rebranchement en plein vol rendrait l'autorité d'un coup, à la
        position où traînent les inters — exactement le scénario interdit."""
        self._arme = False
        self._sel_vu = None
        self._gaz_ref = None
        self._refus_pilote = False
        self._geste_t0 = None

    def _geste_armement(self, gaz, lacet, now, actions):
        """Gaz au minimum + lacet à fond, maintenu. Le geste d'un pilote RC.

        Il faut le MAINTIEN : un manche qui balaie sa course passe par le coin
        « gaz mini + lacet à fond » sans que personne n'ait demandé à armer.
        Et il faut relâcher avant de rejouer — sinon un geste tenu réarmerait en
        boucle, ce qui rendrait le désarmement impossible à obtenir.
        """
        if gaz > -SEUIL_GESTE or abs(lacet) < SEUIL_GESTE:
            self._geste_t0, self._geste_tire = None, False
            return
        if self._geste_t0 is None:
            self._geste_t0 = now
        if not self._geste_tire and now - self._geste_t0 >= MAINTIEN_ARM:
            self._geste_tire = True
            actions.append("arm" if lacet > 0 else "disarm")

    def lire(self, etat: RadioEtat, now: float | None = None) -> Intention:
        """`now` est injectable pour que le geste d'armement, qui dépend d'une
        durée, reste testable au banc sans faire dormir le test."""
        now = time.monotonic() if now is None else now
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

        gaz = a[AXE_GAZ]
        voulu = {(-1): Autorite.PILOTE, 0: Autorite.MANUEL, 1: Autorite.AUTO}[sel]

        # ── 5. contrôle de position des gaz avant de prendre les commandes ───
        # STABILIZE lit les gaz en ABSOLU. Y entrer avec le manche en bas coupe
        # la poussée en vol ; en haut, c'est une montée pleins gaz. On exige donc
        # le milieu — et le refus est LATCHÉ : une fois refusé, il faut ramener
        # l'inter puis le rebasculer. Sans ce verrou, l'autorité sauterait toute
        # seule dans STABILIZE à l'instant où le manche traverse la fenêtre, ce
        # qui est exactement la surprise qu'on cherche à éviter.
        if voulu != Autorite.PILOTE:
            self._refus_pilote = False
        elif abs(gaz) > FENETRE_PRISE and self._precedent != Autorite.PILOTE:
            self._refus_pilote = True
        if self._refus_pilote:
            voulu = Autorite.MANUEL

        autorite = voulu

        self._precedent = autorite
        avance, droite, lacet = (_mort(a[AXE_AVANCE]), _mort(a[AXE_DROITE]),
                                 _mort(a[AXE_LACET]))

        # ── 6. le geste d'armement ───────────────────────────────────────────
        # Uniquement là où les manches sont vivants. En AUTO ils ne commandent
        # rien : y armer sur un geste serait armer sans intention de piloter.
        if autorite in (Autorite.PILOTE, Autorite.MANUEL):
            self._geste_armement(a[AXE_GAZ], a[AXE_LACET], now, actions)
        else:
            self._geste_t0, self._geste_tire = None, False

        # ── 7. AUTO : la loi commande, les manches ne sortent pas ────────────
        if autorite == Autorite.AUTO:
            self._gaz_ref = None
            return Intention(autorite=Autorite.AUTO, lock=lock, engage=engage_sw,
                             actions=tuple(actions), raison="suivi automatique")

        # ── 8. PILOTE : STABILIZE, les manches partent BRUTS au firmware ─────
        # Aucune remise à l'échelle, aucun transfert sans à-coup : en STABILIZE
        # le manche EST la poussée, et un pilote attend que sa position compte.
        # C'est le contrôle de position à l'entrée qui rend ça sûr, pas un
        # filtrage a posteriori.
        if autorite == Autorite.PILOTE:
            self._gaz_ref = None
            return Intention(autorite=Autorite.PILOTE,
                             avance=avance, droite=droite, lacet=lacet,
                             gaz_absolu=a[AXE_GAZ], lock=lock,
                             actions=tuple(actions),
                             raison="STABILIZE — les manches vont au firmware")

        # ── 9. MANUEL : GUIDED_NOGPS, transfert sans à-coup des gaz ──────────
        # L'origine est posée à l'ENTRÉE et effacée à la sortie. Le manche ne se
        # recentrant pas, commander sa position absolue ferait plonger ou grimper
        # le drone à l'instant précis de la prise de main.
        if self._gaz_ref is None:
            self._gaz_ref = gaz
        brut, m_haut, m_bas = _ecart(gaz, self._gaz_ref)
        return Intention(
            autorite=Autorite.MANUEL,
            avance=avance, droite=droite, lacet=lacet,
            monte=_mort(brut), gaz_absolu=gaz,
            marge_montee=round(m_haut, 3), marge_descente=round(m_bas, 3),
            lock=lock, engage=False, abandon=False,
            actions=tuple(actions),
            raison=("gaz mal placés à la prise — recentre et rebascule l'inter"
                    if self._refus_pilote else "manches opérateur"),
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
                  f"   monte {i.monte:+6.2f}   lacet {i.lacet:+6.2f}"
                  f"   gaz {i.gaz_absolu:+6.2f}")
            if min(i.marge_montee, i.marge_descente) < 0.999:
                print(f"  marge gaz : montée {i.marge_montee:.0%}"
                      f"   descente {i.marge_descente:.0%}"
                      "   <- origine près d'une butée")
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
