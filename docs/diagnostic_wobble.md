# ARGOS — Diagnostic vol : wobble + envolée AltHold (topo complet)

> **RÉSOLU le 2026-07-25 — voir §9. Les §6-§8 ci-dessous sont conservés comme historique
> des hypothèses, mais elles sont FAUSSES.**
>
> État au 2026-07-24 pour §1-§8. Document de synthèse pour réflexion / forums / reprise de
> contexte. Tout est tiré de tests réels + analyse des logs DataFlash (pymavlink).

---

## 1. Matériel

| Élément | Détail |
|---|---|
| **Frame** | FlyFishRC Volador VX3.5 (3,5", freestyle) |
| **FC** | SpeedyBee F405 Mini (STM32F405, IMU Invensense v3 / ICM42688P sur SPI, baro DPS310 sur I2C1, **pas de compas embarqué**) |
| **ESC** | SpeedyBee BLS 35A Mini V2 4-in-1, BLHeli_S `J-H-40`, DShot300 |
| **Moteurs** | T-Motor F1404 3800KV |
| **Hélices** | Gemfan Hurricane 3525 tripales (changées 1×, sans effet) |
| **Batterie** | LiPo 4S 850 mAh 150C, montée en **longueur avant-arrière**, sur le dessus (top mount) |
| **Firmware** | ArduCopter **4.8.0-dev** custom (« ArduCopter-ARGOS », build perso depuis fork, hash `8927564c`), target `SpeedyBeeF405Mini` |
| **GPS** | HGLRC M100-5883 (M10 + compas QMC5883) sur UART6 + I2C — **GPS OK (fix 3D, 14 sats), COMPAS MORT** |
| **Radio** | RadioMaster Pocket, ELRS 2.4G interne, RX SpeedyBee Nano ELRS, CRSF sur UART2 |
| **Vidéo** | analogique : cam RunCam Phoenix 2 + VTX SpeedyBee TX800 (IRC Tramp, 25 mW). Pas de lunettes ; réception sol RC832 → capture MS2130 |

**Mapping radio** : arm = voie 5 (RC5_OPTION=153), modes = voie 6 (Stab/AltHold/Land), kill = voie 8 (RC8_OPTION=31), Autotune = voie 7 (RC7_OPTION=17).

---

## 2. Paramètres pertinents (tous les gains = défauts d'usine ArduCopter)

```
ATC_RAT_RLL_P = 0.135    ATC_RAT_PIT_P = 0.135     (défaut)
ATC_RAT_RLL_I = 0.135    ATC_RAT_PIT_I = 0.135     (défaut)
ATC_RAT_RLL_D = 0.0036   ATC_RAT_PIT_D = 0.0036    (défaut)
ATC_RAT_YAW_P = 0.18                                (défaut)
ATC_ANG_RLL_P = 4.5      ATC_ANG_PIT_P = 4.5        (défaut)
INS_GYRO_FILTER = 20 Hz                             (défaut)
SCHED_LOOP_RATE = 400
MOT_THST_HOVER = 0.35
FRAME_TYPE = 12 (Betaflight X)   MOT_PWM_TYPE = 5 (DShot300)
COMPASS_ENABLE = 0 (compas HS)   FS_THR_ENABLE = 3 (Land)
RC2 (pitch) reversé (RC2_REVERSED) — corrigé, était inversé au 1er vol
```
**⚠ Aucun filtre notch harmonique (`INS_HNTCH`) configuré. Jamais tuné (jamais d'Autotune réussi).**

---

## 3. Symptômes observés (pilote)

1. **Wobble** : oscillation persistante **dès le tout premier vol** (intérieur ET extérieur), manches centrés, en montée pure. Décrit comme « circulaire en boucle » / « ça vibre ».
2. **Envolée AltHold** : dès qu'on passe en AltHold (ou pendant l'Autotune), le drone **monte vite et ne s'arrête pas, même manche des gaz à fond en bas**. A causé une chute (+ hélice tordue).
3. **Dérive arrière** constante, persiste même après avoir avancé la batterie.
4. **Dérive incohérente** : direction change au sein d'un vol et d'un vol à l'autre.
5. **Lacet incohérent** : parfois manche à fond → tourne à peine, parfois tourne beaucoup.
6. **« Shake » qui augmente avec le régime moteur** (fort à haut régime, faible à bas régime).

---

## 4. Ce qu'on a testé et ÉLIMINÉ

| Test | Résultat |
|---|---|
| Baisser rate P (0.135 → 0.08 → 0.06) | **aucun changement** sur le wobble |
| Baisser angle P (4.5 → 3.0) | **aucun changement** |
| Changer les 4 hélices | **aucun changement** |
| Plots anti-vibration FC | pas trop serrés, OK |
| Test moteurs individuels (MP) | les 4 identiques, smooth, aucun bruit anormal |
| Rotation moteurs à la main | tous soyeux, pas de jeu/grattage |
| Recalibration accéléro + niveau | dérive arrière **persiste** |
| Centre pitch radio | = 1500 (trims OK) → pas la cause de la dérive |
| Isoler les épissures I2C (SDA/SCL) | corrige une panne baro, wobble **persiste** |
| Débrancher/tester sans GPS | wobble présent **en intérieur sans fix GPS** aussi |
| Chronologie | wobble présent **dès le 1er vol**, avant toute chute |

**Conclusions d'élimination** : ce n'est PAS le tune symétrique, PAS les hélices, PAS un moteur unique abîmé, PAS le GPS, PAS une chute, PAS le baro (voir §5).

---

## 5. Données extraites des logs (analyse pymavlink)

### Vibrations
- VibeX/Y/Z moyennes **3,4 / 4,9 / 4,1**, pics ~49, **clipping = 0**.
- **Monte avec les gaz** : VibeZ = 1,2 à bas régime → **5,8 à haut régime**.
- Moyennes techniquement dans la plage « acceptable » ArduPilot (< 15).

### 🔑 Asymétrie gyro — LE point clé
Sur tous les vols réels, en fenêtre calme :
- **GyrX (roulis) oscille 2 à 3× plus que GyrY (tangage)** et ~5× le lacet.
- Ex. log 14-07 : GyrX = 1,78 vs GyrY = 0,64 rad/s (2,8×). Log 10-17 : 0,79 vs 0,38 (2,1×).
- **Fréquence dominante de l'oscillation roulis : ~0,5–0,7 Hz** (lent).
- Accéléro : bruit modéré (AccZ éc-type 1,93 m/s²).
- → Le wobble est **spécifique à l'axe de ROULIS** (gauche-droite), pas symétrique.

### Équilibre moteurs (hover)
- **Gauche-droite : ÉQUILIBRÉ** (D 1257 vs G 1259 µs).
- **Avant-arrière : arrière plus chargé** (avant 1245 vs arrière 1270 µs) → **CG trop reculé**.

### Baromètre
- **Propre** : bruit 0,12 m, pas de sauts → **l'envolée AltHold n'est PAS un baro qui déraille**.

### Envolée AltHold (CTUN)
- BAlt passe de **−1 m à 14,3 m en 3 s** alors que **ThIn (manche) = −1,6 (à fond en bas)**.
- → contrôleur d'altitude qui reçoit des **données de vitesse verticale corrompues** (accéléro/vibration), croit que ça tombe → plein gaz → monte → emballement.

### EKF / estimation
- L'EKF **gagne et perd le GPS en boucle** (« yaw aligned to GPS velocity » / « stopped aiding » répétés) — normal sans compas (yaw dérivé du GPS) + connecteur GPS branlant.
- → cause probable de la **dérive et du lacet incohérents**.
- 1er vol extérieur : erreurs d'attitude EKF (« DCM Roll/Pitch inconsistent 47° ») — en partie de vrais retournements dans des logs de sauts chaotiques.

---

## 6. Hypothèses au 2026-07-24 — ⚠ TOUTES RÉFUTÉES, voir §9

1. **Wobble = roulis sous-amorti.** Le drone répond **plus vite en roulis qu'en tangage** avec les mêmes gains → oscillation spécifique au roulis. Cause physique probable : **batterie montée en longueur avant-arrière → faible inertie en roulis** (masse proche de l'axe de roulis), forte inertie en tangage. Les gains par défaut (symétriques) sont trop chauds pour l'axe roulis léger.
   → **Remède attendu : Autotune** (règle chaque axe séparément → gains adaptés au roulis).

2. **Dérive arrière = CG trop reculé** (moteurs arrière plus chargés). Matériel monté à l'arrière (VTX/GPS/antennes) + batterie. → déplacer de la masse vers l'avant.

3. **Envolée AltHold = estimation verticale corrompue** (accéléro/vibration à haut régime), **pas le baro**. Dangereux → **éviter AltHold**. **C'est LE blocage** : l'Autotune exige AltHold, qui s'envole.

4. **Dérive/lacet incohérents = EKF instable** (yaw GPS sans compas + connecteur GPS branlant).

---

## 7. Le blocage & prochaines étapes

**Nœud du problème** : le remède du wobble (Autotune) a besoin d'AltHold ; or AltHold s'envole (§5). Donc **il faut d'abord fiabiliser le vertical/AltHold**.

Pistes à tester :
1. **Confirmer l'hypothèse roulis** : baisser **uniquement le roulis** (`ATC_RAT_RLL_P` 0.135→0.09, `ATC_RAT_RLL_D` 0.0036→0.005, laisser le tangage), vol **Stabilize** → le wobble roulis doit se calmer spécifiquement.
2. **Débloquer AltHold** : réduire la vibration à haut régime (équilibrage hélices/moteurs), envisager un **filtre notch harmonique** (`INS_HNTCH`) calé sur la fréquence moteur ; entrer en AltHold depuis un **hover stable, manche au milieu** (pas depuis le sol).
3. **AltHold stable → Autotune** (roulis d'abord).
4. **CG** : avancer la masse (VTX/antennes) pour réduire la dérive arrière.

---

## 8. Notes / questions ouvertes pour experts

- Firmware = build perso from-source (fork), mais **gains = défauts stock**.
- **Pas de filtre notch harmonique** configuré (pertinent pour un 3,5" nerveux ?).
- `INS_GYRO_FILTER = 20 Hz` (défaut) — trop bas pour un 3,5" ?
- **Stockage log = 8 Mo flash embarquée seulement** → se remplit vite, limite le **batch logging IMU** nécessaire à une vraie **FFT** (analyse fréquentielle des vibrations).
- Un wobble **spécifiquement roulis** à ~0,5-0,7 Hz sur un quad symétrique : inertie (batterie) + gains par défaut, ou autre chose ?
- L'envolée AltHold avec **baro propre** et **vibration moyenne « acceptable »** (<15) : suffisant pour corrompre la vitesse verticale ? ou autre mécanisme ?

---

---

## 9. ✅ RÉSOLUTION (2026-07-25) — le mélangeur sature en permanence

**Cause racine unique : le drone est tellement surmotorisé que son point de hover tombe hors
de la plage de contrôle utile d'ArduPilot. Le mélangeur sature 99 % du temps.**

### 9.1 Mesures (3 vols exploitables, hover stabilisé)

| grandeur | mesure | attendu |
|---|---|---|
| sortie moteur au hover | 1246-1264 µs | — |
| `MOT_SPIN_MIN` = 0.15 | 1150 µs → **96 µs de marge sous le hover** | plusieurs centaines |
| **poussée de hover réelle** (échelle 0→1 ArduPilot) | **0.052** | 0.25-0.40 |
| `MOT_THST_HOVER` | 0.35 → **faux d'un facteur 6,8** | = poussée réelle |
| `PIDR/PIDP/PIDY` drapeau `LIMIT` (bit 0) | **98-99,4 %**, tous vols depuis le 1er | < 10 % |
| ≥1 moteur collé sur `MOT_SPIN_MIN` | **100 % du temps** | ponctuel |
| terme I des boucles de rate | **≈ 0** (std 0.0002) | trime l'erreur |
| erreur d'assiette moyenne | **+6° roulis / +8° tangage** | < 2° |
| `CTUN.ThO` (gaz demandés) → `MOTB.ThrOut` (appliqués) | 0.024 → 0.056 (**x2,4**) | ≈ égaux |
| course de manche des gaz au hover | **6,5 %** | ~45-55 % |

### 9.2 Mécanisme (vérifié dans le source, `AP_MotorsMatrix::output_armed_stabilizing`)

Quand la commande roulis+tangage+lacet ne tient pas dans la plage disponible :
`rpy_scale = -throttle_avg_max / rpy_low`, `limit.set_rpy(true)`, et
`_thrust_rpyt_out[i] = throttle_best + rpy_scale * _thrust_rpyt_out[i]`.

1. **Les gains PID sont mathématiquement sans effet.** Doubler la sortie des PID double
   `rpy_low`, donc divise `rpy_scale` par deux : le produit est **inchangé**. → explique
   « baisser rate P / angle P ne change rien » (§4). Le tune n'était pas bon : il était
   **hors circuit**. (Aucun des 13 logs ne contient de gains modifiés, tous à 0.135/4.5.)
2. **Intégrateurs gelés** (`_motors.limit.roll` → argument `limit` de `AC_PID::update_all`)
   → plus de trim → **erreur d'assiette permanente → dérive constante et de direction
   variable** (§3.3, §3.4). Ce n'est pas le CG.
3. **La poussée moyenne n'est plus pilotée par le manche** mais par
   `get_throttle_avg_max() = throttle_in*(1-mix) + MOT_THST_HOVER*mix` :
   - Stabilize (`ATC_THR_MIX_MAN`=0.1) → plancher 0.035 **> hover réel 0.026** : le drone
     monte gaz fermés, d'où le hover à 6,5 % de manche.
   - AltHold (`ATC_THR_MIX_MAX`=0.5) → plancher **0.175 = 7x le hover réel**.
4. **Autorité lacet bridée** : `yaw_allowed` est calculé sur la marge moteur restante, quasi
   nulle ici, puis forcé à `MOT_YAW_HEADROOM`=0.2 → explique le **lacet incohérent** (§3.5).

### 9.3 Envolée AltHold, à la seconde près (log 14-07-40, t=188.57)

Bascule AltHold → `MOTB.ThrOut` saute à **0.1750** (= 0.35 × 0.5 exactement) et **reste figé
1,3 s** pendant que `CTUN.ThO` = **0.0000** et que le manche est en bas. BAlt 3,97 → 13,68 m
= **+7,5 m/s**. Ce n'est pas une estimation verticale corrompue : c'est un **feed-forward de
gaz faux d'un facteur 7**.

Aggravant : `AP_MOTORS_THST_HOVER_MIN = 0.125f` est un **clamp dur** dans
`get_throttle_hover()` → avec un hover réel à 0.052, `MOT_THST_HOVER` ne peut PAS descendre
assez bas : **AltHold est structurellement impossible dans cette config**. Et
`Copter::update_throttle_hover()` sort tôt si `flightmode->has_manual_throttle()` → **n'apprend
jamais en Stabilize**. Cercle vicieux complet.

### 9.4 Ce que ça invalide dans les §4-§8

- **Balourd mécanique — FAUX.** Un balourd apparaît à 1x le régime (~245 Hz au hover ici :
  24,6 % de gaz × 15,7 V × 3800 KV). Des VIBE qui montent avec le régime, c'est le
  comportement **normal** de tout multirotor ; 5,8 est très bon (seuil ArduPilot : 30),
  clipping = 0 en vol. Rien à changer côté mécanique.
- **Tune par défaut — FAUX** (gains hors circuit).
- **Baro coupable de l'envolée — FAUX** (mais voir §9.5).
- **« Oscillation à 0,5-0,7 Hz » — ARTEFACT D'ALIASING.** ATT/RATE loggés à **10 Hz**
  (Nyquist 5 Hz), IMU à 25 Hz, ~23 % de messages perdus. Le spectre du gyro roulis a **97 %
  de son énergie dans 8-12,5 Hz, empilée sur Nyquist** = repliement. Vraie fréquence
  probablement ~15 Hz, **non mesurable avec ce réglage de log**. Il reste une oscillation
  roulis HF réelle (`GyrX` rms 2,06 rad/s = 118 °/s vs `GyrY` 0,65 en fenêtre calme) à
  requalifier une fois la saturation levée ET le logging réparé.

### 9.5 Problème secondaire réel : le baro voit le souffle des hélices

Drone au sol, altitude vraie constante, moteurs OFF vs ralenti, sur 5 logs :
**BAlt chute de 0,86 à 1,68 m** rien qu'au ralenti. Le DPS310 n'est pas bruité (0,12 m
statique) mais **biaisé par le débit d'air**. Mousse à cellules ouvertes sur le capteur avant
de compter sur AltHold. Ce n'est pas la cause de l'envolée, mais ça la rendrait instable
ensuite.

### 9.6 Plan de correction (ordre impératif)

1. **Calibration `MOT_SPIN_ARM` / `MOT_SPIN_MIN`** (MP → Motor Test). Attendu ~0.02-0.04 et
   ~0.04-0.06 (les défauts 0.10/0.15 sont dimensionnés pour du 5"+ lent). Vérifier que les 4
   moteurs démarrent et tournent **sous charge** sans décrocher (BLHeli_S = risque de désync).
2. **`MOT_THST_EXPO` ≈ 0.55** (0.65 = grosses hélices).
3. **Vol Stabilize court → `tools/thrust_range.py` → recalculer.** Si la poussée de hover est
   encore < 0.20, **plafonner `MOT_SPIN_MAX`** (~0.57-0.60 attendu = 1570-1600 µs). On sacrifie
   une poussée max dont il y a un excès absurde (T/W restant ≈ 4).
4. **`MOT_THST_HOVER` = valeur mesurée** (cible 0.25). AltHold redevient sûr, et
   `MOT_HOVER_LEARN=2` l'affinera seul.
5. **⚠ SÉCURITÉ — baisser les gains AVANT de revoler.** Gain effectif actuel
   ≈ `0.135 × rpy_scale(~0.1)`. Saturation levée → `rpy_scale → 1` → gain d'assiette réel
   **x8-10 d'un coup**. Passer par **MP → SETUP → Mandatory Hardware → Initial Tune
   Parameters** (hélice 3,5", 4S) : pose d'un bloc `ATC_RAT_*`, `INS_GYRO_FILTER` (20 Hz est
   trop bas pour du 3,5") et `MOT_THST_EXPO`. Premier vol bas, court, sur herbe, pouce sur le
   kill.
6. **Réparer le logging avant de rediagnostiquer** : `LOG_BITMASK` + bit 0 (`ATTITUDE_FAST`)
   et décocher GPS/COMPASS/NTUN/CAMERA pour tenir dans 8 Mo ; pour une vraie FFT,
   `INS_LOG_BAT_MASK=1` (échantillonneur par lots ~1 kHz) sur 60-90 s, puce effacée avant.
7. Puis : hover propre → requalifier le wobble résiduel → notch `INS_HNTCH` si besoin →
   **Autotune**.

**Outil** : `tools/thrust_range.py` — inverse la courbe de poussée d'ArduPilot et sort la
poussée de hover réelle, le % de saturation du mélangeur, et le `MOT_SPIN_MAX` à viser.

---

*Historique détaillé jour par jour dans `docs/journal.md` (entrées 2026-07-20 → 2026-07-25).*
