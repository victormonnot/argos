"""Tests au banc de la cartographie radio (HITL-2).

Même discipline que `test_guidance.py` : la couche est pure, donc elle se teste
sans radio, sans drone et sans vol. Ce qui est vérifié ici, ce ne sont pas les
numéros d'axes — ce sont les **trois règles de sûreté** du transfert d'autorité,
celles qu'on ne veut jamais voir régresser :

    1. brancher une radio ne prend pas la main toute seule ;
    2. la prise de main ne bouge pas le drone (transfert sans à-coup) ;
    3. perdre la radio retire de l'autorité, jamais n'en donne.

    perception/.venv/bin/python -m control.test_radio      # depuis perception/
"""
from .radio import RadioEtat
from .radio_map import (AXE_AVANCE, AXE_DROITE, AXE_GAZ, AXE_LACET,
                        INTER_ABANDON, INTER_AUTORITE, INTER_ENGAGE, INTER_LOCK,
                        Autorite, Cartographie, _cran, _mort)


def etat(sel=-1.0, gaz=0.0, avance=0.0, droite=0.0, lacet=0.0,
         lock=-1.0, engage=0.0, abandon=-1.0, presente=True):
    """Un `RadioEtat` fabriqué. Valeurs au REPOS telles qu'EdgeTX les envoie :
    un inter 2 crans relâché est à -1 (pas 0), un inter 3 crans au cran MILIEU
    est à 0 — et `engage` par défaut est au milieu, sinon chaque test demanderait
    un repli sans le savoir."""
    return RadioEtat(presente=presente, nom="banc", axes={
        AXE_GAZ: gaz, AXE_AVANCE: avance, AXE_DROITE: droite, AXE_LACET: lacet,
        INTER_AUTORITE: sel, INTER_LOCK: lock, INTER_ENGAGE: engage,
        INTER_ABANDON: abandon,
    })


def _armer(c, sel=0.0):
    """Bouge le sélecteur une fois : c'est ce qui donne la main à la radio."""
    c.lire(etat(sel=-1.0))
    return c.lire(etat(sel=sel))


# ── règle 1 : aucune prise d'autorité silencieuse ───────────────────────────
def test_radio_branchee_ne_prend_pas_la_main():
    """Le scénario interdit : la radio apparaît, le sélecteur traîne au milieu,
    et la console bascule en manuel sans que personne n'ait rien demandé."""
    c = Cartographie()
    i = c.lire(etat(sel=0.0))            # sélecteur DÉJÀ en position manuel
    assert i.autorite == Autorite.INACTIVE
    assert (i.avance, i.droite, i.monte, i.lacet) == (0.0, 0.0, 0.0, 0.0)


def test_bouger_le_selecteur_arme_la_radio():
    c = Cartographie()
    assert c.lire(etat(sel=0.0)).autorite == Autorite.INACTIVE
    assert c.lire(etat(sel=1.0)).autorite == Autorite.AUTO


def test_rebranchement_redemande_un_armement():
    """Une radio qui revient est inconnue : elle ne doit pas récupérer la main à
    la position où traînent les inters."""
    c = Cartographie()
    _armer(c, sel=0.0)
    assert c.lire(etat(sel=0.0)).autorite == Autorite.MANUEL
    c.lire(etat(presente=False))                       # débranchée
    assert c.lire(etat(sel=0.0)).autorite == Autorite.INACTIVE


# ── règle 2 : transfert sans à-coup ─────────────────────────────────────────
def test_prise_de_main_avec_les_gaz_n_importe_ou():
    """LE piège du manche non centré. À la prise de main, `monte` doit valoir 0
    quelle que soit la position du manche — donc `thrust = 0,5`, altitude tenue."""
    for gaz in (-1.0, -0.18, 0.0, 0.63, 1.0):
        c = Cartographie()
        c.lire(etat(sel=-1.0, gaz=gaz))
        i = c.lire(etat(sel=0.0, gaz=gaz))
        assert i.autorite == Autorite.MANUEL
        assert i.monte == 0.0, f"prise de main à gaz={gaz} -> monte={i.monte}"


def test_les_gaz_commandent_l_ecart_pas_la_position():
    c = Cartographie()
    c.lire(etat(sel=-1.0, gaz=-0.18))                  # au repos, hors manuel
    # l'origine est posée à CET instant précis : l'entrée en manuel
    assert c.lire(etat(sel=0.0, gaz=-0.18)).monte == 0.0
    assert c.lire(etat(sel=0.0, gaz=0.32)).monte > 0.0, "monter depuis l'origine"
    assert c.lire(etat(sel=0.0, gaz=-0.68)).monte < 0.0, "descendre depuis l'origine"


def test_sortir_du_manuel_efface_l_origine():
    """Sinon un aller-retour manuel -> auto -> manuel reprendrait une origine
    périmée, et rendrait la main avec un écart non nul."""
    c = Cartographie()
    _armer(c, sel=0.0)
    c.lire(etat(sel=0.0, gaz=0.0))
    c.lire(etat(sel=1.0, gaz=0.0))                     # passage en auto
    assert c.lire(etat(sel=0.0, gaz=0.8)).monte == 0.0, "nouvelle prise de main"


# ── règle 3 : la dégradation va vers MOINS d'autorité ───────────────────────
def test_radio_absente_ne_promeut_pas_le_pilote_automatique():
    c = Cartographie()
    _armer(c, sel=1.0)
    assert c.lire(etat(sel=1.0)).autorite == Autorite.AUTO
    i = c.lire(etat(presente=False))
    assert i.autorite == Autorite.ABSENTE
    assert not i.engage, "perdre l'opérateur ne doit pas laisser un engagement actif"


def test_l_abandon_prime_sur_le_selecteur():
    c = Cartographie()
    _armer(c, sel=0.0)
    i = c.lire(etat(sel=0.0, avance=1.0, abandon=1.0))
    assert i.autorite == Autorite.ABANDON
    assert i.avance == 0.0, "abandon = plus personne ne commande, manches compris"


def test_engage_impossible_hors_auto():
    """L'inter d'engagement en position haute pendant qu'on pilote aux manches ne
    doit pas armer l'approche : ce sont deux autorités différentes."""
    c = Cartographie()
    _armer(c, sel=0.0)
    assert not c.lire(etat(sel=0.0, engage=1.0)).engage
    assert c.lire(etat(sel=1.0, engage=1.0)).engage


# ── les fronts ──────────────────────────────────────────────────────────────
def test_les_fronts_ne_sortent_qu_une_fois():
    c = Cartographie()
    _armer(c, sel=1.0)
    assert "lock" in c.lire(etat(sel=1.0, lock=1.0)).actions
    assert c.lire(etat(sel=1.0, lock=1.0)).actions == (), "maintenu != répété"
    assert "unlock" in c.lire(etat(sel=1.0, lock=-1.0)).actions


def test_un_inter_bouge_hors_autorite_ne_declenche_pas_plus_tard():
    """Sinon l'action partirait au moment de la prise de main, avec un retard
    arbitraire — le pire mode de panne d'une interface opérateur."""
    c = Cartographie()
    i = c.lire(etat(sel=-1.0, lock=1.0))               # pas encore armée
    assert i.autorite == Autorite.INACTIVE
    assert i.actions == ()
    i = c.lire(etat(sel=0.0, lock=1.0))                # armement, inter inchangé
    assert i.autorite == Autorite.MANUEL
    assert i.actions == (), "l'état a été absorbé, pas mis en attente"


# ── le repli (RTL), cran bas de l'inter d'engagement ────────────────────────
def test_le_cran_bas_demande_un_repli():
    """Un seul axe porte les deux sens de l'engagement : haut = « va vers la
    cible », bas = « rentre ». Le neutre est entre les deux, donc on ne passe
    jamais de l'un à l'autre sans franchir un cran d'arrêt."""
    c = Cartographie()
    _armer(c, sel=1.0)
    i = c.lire(etat(sel=1.0, engage=-1.0))
    assert i.autorite == Autorite.REPLI
    assert i.rtl and not i.engage
    assert "rtl" in i.actions


def test_le_repli_prime_sur_le_selecteur_mais_pas_sur_l_abandon():
    """L'ordre de priorité EST la hiérarchie de sûreté : abandon (plus personne
    ne commande) > repli (le firmware commande) > sélecteur (la console)."""
    c = Cartographie()
    _armer(c, sel=0.0)
    assert c.lire(etat(sel=0.0, engage=-1.0, avance=1.0)).autorite == Autorite.REPLI
    i = c.lire(etat(sel=0.0, engage=-1.0, abandon=1.0))
    assert i.autorite == Autorite.ABANDON, "l'abandon reste au-dessus de tout"


def test_le_repli_ne_commande_pas_les_manches():
    c = Cartographie()
    _armer(c, sel=0.0)
    i = c.lire(etat(sel=0.0, engage=-1.0, avance=1.0, droite=-1.0))
    assert (i.avance, i.droite, i.monte, i.lacet) == (0.0, 0.0, 0.0, 0.0)


def test_un_inter_engage_absent_vaut_neutre_pas_repli():
    """Le défaut d'un axe manquant doit être le NEUTRE. Une cartographie
    incomplète qui déclencherait un retour au terrain serait le pire défaut
    possible : silencieux, et il fait rentrer le drone tout seul."""
    c = Cartographie()
    c.lire(etat(sel=-1.0))
    e = etat(sel=0.0)
    del e.axes[INTER_ENGAGE]
    i = c.lire(e)
    assert i.autorite == Autorite.MANUEL
    assert not i.rtl


# ── décodage ────────────────────────────────────────────────────────────────
def test_les_trois_crans_se_decodent():
    assert (_cran(-1.0), _cran(0.0), _cran(1.0)) == (-1, 0, 1)
    assert _cran(0.001) == 0, "le repos d'EdgeTX au milieu reste le milieu"


def test_la_zone_morte_est_continue():
    """Une zone morte qui ne remet pas à l'échelle fait sauter la commande de 0 à
    la largeur de la zone dès qu'on en sort."""
    assert _mort(0.002) == 0.0, "le bruit au repos ne commande rien"
    assert _mort(1.0) == 1.0, "la butée commande toujours le maximum"
    assert 0.0 < _mort(0.06) < 0.05, "juste après la zone morte : petit, pas nul"
    assert _mort(-0.5) == -_mort(0.5), "symétrique"


if __name__ == "__main__":
    ok = 0
    for nom, fn in sorted(globals().items()):
        if nom.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {nom}")
            ok += 1
    print(f"\n{ok} tests verts.")
