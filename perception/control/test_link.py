"""Tests au banc de l'instrumentation de liaison (§1.5-C).

On fabrique des flux dont on connaît la vérité — tant de messages émis, tant
perdus — et on vérifie que le compteur retrouve le bon chiffre. Ça se teste ici
et pas en vol, parce qu'en vol on ne connaît justement pas la vérité.

    perception/.venv/bin/python -m control.test_link      # depuis perception/
"""
from .link import LinkStats, VisionStats


def _flux(stats, seqs, t0=0.0, dt=0.01, octets=20, mtype="HEARTBEAT", src=(1, 1)):
    for i, s in enumerate(seqs):
        stats.on_rx(t0 + i * dt, src[0], src[1], s % 256, octets, mtype)


def test_flux_parfait_zero_perte():
    st = LinkStats(fenetre=10.0)
    _flux(st, range(100))
    s = st.snapshot(1.0)
    assert s.perdus == 0 and s.perte_pct == 0.0
    assert s.recus == 100


def test_une_perte_est_comptee():
    st = LinkStats(fenetre=10.0)
    _flux(st, [0, 1, 2, 4, 5])          # le 3 manque
    s = st.snapshot(1.0)
    assert s.perdus == 1
    assert s.recus == 5
    assert abs(s.perte_pct - 100 * 1 / 6) < 0.01


def test_le_compteur_boucle_a_255_sans_fausse_perte():
    """Le seul vrai piège du procédé : le compteur est sur 8 bits."""
    st = LinkStats(fenetre=10.0)
    _flux(st, [253, 254, 255, 0, 1, 2])
    s = st.snapshot(1.0)
    assert s.perdus == 0, "le passage 255 -> 0 n'est pas une perte"


def test_perte_a_cheval_sur_le_bouclage():
    st = LinkStats(fenetre=10.0)
    _flux(st, [254, 1])                 # 255 et 0 manquent
    assert st.snapshot(1.0).perdus == 2


def test_un_saut_aberrant_est_range_a_part_pas_compte_en_perte():
    """Un doublon ou un émetteur qui redémarre son compteur donnerait un saut
    de 255 : le compter en perte ferait exploser la mesure sur un seul événement."""
    st = LinkStats(fenetre=10.0)
    _flux(st, [10, 11, 11])             # doublon -> saut apparent de 255
    s = st.snapshot(1.0)
    assert s.perdus == 0
    assert s.desordres == 1


def test_deux_emetteurs_ont_chacun_leur_compteur():
    """Le drone et la station émettent chacun leur propre suite. Les mélanger
    fabriquerait des pertes qui n'existent pas."""
    st = LinkStats(fenetre=10.0)
    for i in range(20):
        st.on_rx(i * 0.01, 1, 1, i, 20, "ATTITUDE")        # autopilote
        st.on_rx(i * 0.01, 255, 190, 200 + i, 20, "HEARTBEAT")  # station sol
    s = st.snapshot(1.0)
    assert s.perdus == 0
    assert sorted(src for src, _ in s.par_source) == ["1:1", "255:190"]


def test_debit_et_cadence():
    st = LinkStats(fenetre=2.0)
    for i in range(20):
        st.on_rx(i * 0.1, 1, 1, i, 50, "ATTITUDE")
    s = st.snapshot(2.0)
    assert s.rx_hz == 10.0                      # 20 messages sur 2 s
    assert s.rx_bps == 500.0                    # 20 x 50 octets sur 2 s


def test_la_fenetre_glisse():
    st = LinkStats(fenetre=1.0)
    _flux(st, range(50), t0=0.0, dt=0.01)       # tout avant t=0.5
    assert st.snapshot(0.6).recus == 50
    assert st.snapshot(5.0).recus == 0, "au-delà de la fenêtre, plus rien ne compte"


def test_par_message_trie_par_cadence():
    st = LinkStats(fenetre=1.0)
    for i in range(30):
        st.on_rx(i * 0.01, 1, 1, i, 20, "ATTITUDE" if i % 3 else "HEARTBEAT")
    top = st.snapshot(0.5).par_message
    assert top[0][0] == "ATTITUDE" and top[0][1] > top[1][1]


def test_le_trou_max_en_emission():
    """Le chiffre qui a revele le begaiement de la boucle de commande."""
    st = LinkStats(fenetre=10.0)
    for t in (0.0, 0.1, 0.2, 0.9, 1.0):
        st.on_tx(t, 36)
    assert abs(st.snapshot(1.0).tx_trou_max_s - 0.7) < 1e-6


def test_latence_p50_p95():
    st = LinkStats(fenetre=10.0)
    for i, ms in enumerate([5, 6, 7, 8, 100]):
        st.on_rtt(i * 0.1, ms)
    s = st.snapshot(1.0)
    assert s.latence_p50_ms == 7
    assert s.latence_p95_ms == 100, "le p95 doit voir la queue, pas la moyenne"


def test_le_compteur_retrouve_un_taux_de_perte_connu():
    """L'étalonnage. On jette une fraction connue d'un flux parfait et on vérifie
    que le compteur lit cette fraction. Sans ça, « 0 % de perte » sur une liaison
    locale ne prouve rien : ni que la liaison est bonne, ni que l'outil marche."""
    import random
    for taux in (0.05, 0.20, 0.50):
        rng = random.Random(1234)
        st = LinkStats(fenetre=1e9)
        for seq in range(20000):
            if rng.random() < taux:
                continue                       # message perdu en route
            st.on_rx(0.0, 1, 1, seq % 256, 20, "ATTITUDE")
        mesure = st.snapshot(0.0).perte_pct
        assert abs(mesure - 100 * taux) < 1.5, f"demande {taux}, mesure {mesure}"


def test_les_emetteurs_sont_fenetres_comme_le_reste():
    """Un émetteur vu une fois au démarrage ne doit pas rester listé pour toujours :
    sinon le tableau mélange « depuis toujours » et « ces 3 dernières secondes »,
    et plus aucune ligne n'est interprétable à côté des autres."""
    st = LinkStats(fenetre=1.0)
    st.on_rx(0.0, 0, 0, 0, 20, "HEARTBEAT")          # un fantome au demarrage
    for i in range(10):
        st.on_rx(5.0 + i * 0.01, 1, 1, i, 20, "ATTITUDE")
    srcs = [src for src, _ in st.snapshot(5.2).par_source]
    assert srcs == ["1:1"], f"le fantome 0:0 ne doit plus etre la : {srcs}"


def test_les_desordres_sont_fenetres_aussi():
    st = LinkStats(fenetre=1.0)
    st.on_rx(0.0, 1, 1, 10, 20, "A")
    st.on_rx(0.1, 1, 1, 10, 20, "A")                 # doublon -> 1 desordre
    assert st.snapshot(0.5).desordres == 1
    assert st.snapshot(9.0).desordres == 0, "un vieux desordre ne compte plus"


def test_liaison_muette():
    """Aucun message : tout à zéro, aucune exception, pas de division par zéro."""
    s = LinkStats().snapshot(123.0)
    assert s.rx_hz == 0.0 and s.perte_pct == 0.0
    assert s.latence_p50_ms is None


# ── la deuxieme liaison : la video ──────────────────────────────────────────
def test_video_cadence_et_age():
    v = VisionStats(fenetre=2.0)
    for i in range(30):
        v.on_frame(i * 0.05)                 # 20 images/s pendant 1,5 s
    s = v.snapshot(1.45)
    assert s.cam_hz == 15.0                  # 30 images sur la fenetre de 2 s
    assert abs(s.age_image_ms - 0.0) < 1.0


def test_video_un_flux_mort_se_voit():
    """Aujourd'hui rien ne signale une camera qui s'arrete : l'age de la derniere
    image est le seul indicateur qui monte tout seul quand plus rien n'arrive."""
    v = VisionStats(fenetre=2.0)
    v.on_frame(0.0)
    assert v.snapshot(3.0).age_image_ms == 3000.0
    assert v.snapshot(3.0).cam_hz == 0.0, "plus aucune image dans la fenetre"


def test_video_taux_de_detection_ignore_les_images_sans_lock():
    v = VisionStats(fenetre=10.0)
    for _ in range(4):
        v.on_frame(0.0, None)                # pas de cible verrouillee -> hors calcul
    for vue in (True, True, True, False):
        v.on_frame(0.0, vue)
    assert v.snapshot(0.1).detection_pct == 75.0


def test_video_latence_p50_p95():
    v = VisionStats(fenetre=10.0)
    for ms in (40, 45, 50, 55, 300):
        v.on_command(0.0, ms)
    s = v.snapshot(0.1)
    assert s.latence_p50_ms == 50
    assert s.latence_p95_ms == 300


def test_video_muette():
    s = VisionStats().snapshot(9.0)
    assert s.cam_hz == 0.0 and s.age_image_ms is None and s.detection_pct is None


if __name__ == "__main__":
    ok = 0
    for nom, fn in sorted(globals().items()):
        if nom.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {nom}")
            ok += 1
    print(f"\n{ok} tests verts.")
