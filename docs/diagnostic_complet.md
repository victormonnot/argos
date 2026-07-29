# ARGOS — Diagnostic complet : oscillations d'un quad 3,5" sous ArduCopter

> **État au 2026-07-29.** Document autoportant : il ne suppose aucune connaissance du projet.
> Destiné à être lu seul, posté sur un forum, ou donné à un assistant dans une conversation
> vierge.
>
> ## ✅ LES DEUX PROBLÈMES SONT RÉSOLUS
>
> - **N°1 — saturation permanente du mélangeur** (§4) : le drone était trop surmotorisé pour
>   l'échelle de poussée d'ArduPilot. Corrigé par `MOT_SPIN_MIN/ARM`, `MOT_THST_EXPO` et
>   `MOT_THST_HOVER` mesuré.
> - **N°2 — cycle limite de roulis à 15,5 Hz** (§5) : mode mécanique du montage de la FC,
>   **entretenu par la boucle de commande**. Résolu en faisant passer le gain de boucle sous le
>   seuil d'auto-entretien : `ATC_RAT_RLL/PIT_P = I = 0,06`, `D = 0,0005`.
>   → **temps en résonance : 44 % → 0 %**, amplitude ÷25, **et le suivi d'attitude s'améliore**
>   (erreur roulis std 0,71° → 0,40°, saturation mélangeur 11 % → 0 %). Voir §5.9.
>
> ⚠ **Le mode mécanique existe toujours**, on a seulement cessé de l'exciter — et on n'est pas
> loin du seuil. Ne pas lancer l'Autotune sans précaution (§5.9), et rigidifier le montage de
> la stack reste souhaitable.
>
> Les incertitudes assumées sont regroupées en §7 — elles restent valables et documentent
> **trois conclusions que j'ai dû retirer** en cours d'enquête.
>
> Toutes les valeurs chiffrées viennent de logs DataFlash réels, lus avec pymavlink. Les
> affirmations sur le comportement du firmware ont été vérifiées dans le code source de la
> branche exacte qui tourne sur l'appareil, pas déduites de la documentation.

---

## 0. Les deux correctifs, au propre

Pour qui veut seulement le résultat. Détail et preuves en §4 et §5.

### FIX 1 — la plage de poussée

Corrige : gros wobble lent, dérive permanente, envolée AltHold, manche des gaz utilisable sur
6 % de sa course, lacet incohérent.

```
MOT_SPIN_ARM     0.10   ->  0.03
MOT_SPIN_MIN     0.15   ->  0.05
MOT_THST_EXPO    0.65   ->  0.43
MOT_THST_HOVER   0.35   ->  0.179     <- valeur MESUREE, pas devinee
```

Le hover tombait à **0,052** sur l'échelle 0→1 d'ArduPilot, à 96 µs au-dessus de
`MOT_SPIN_MIN`. Mélangeur saturé **99 % du temps**, un moteur au plancher en permanence →
gains PID annulés mathématiquement, intégrateurs gelés (la dérive), et poussée moyenne pilotée
par `MOT_THST_HOVER` au lieu du manche.

### FIX 2 — le gain de boucle

Corrige : le petit shaking rapide à 15,5 Hz.

```
ATC_RAT_RLL_P = ATC_RAT_RLL_I    0.135  ->  0.06
ATC_RAT_RLL_D                    0.0036 ->  0.0005
ATC_RAT_PIT_P = ATC_RAT_PIT_I    0.135  ->  0.06
ATC_RAT_PIT_D                    0.0036 ->  0.0005
```

Mode mécanique du montage de la FC à 15,5 Hz, **entretenu par la boucle**. Le gain de boucle à
cette fréquence (`P + ω·D`) passe de **0,486 à 0,109** : sous le seuil d'auto-entretien.
Temps en résonance **44 % → 0 %**, et le suivi d'attitude **s'améliore** au passage.

> **L'ordre est contraint.** Tant que le mélangeur saturait, toucher aux gains ne pouvait rien
> donner : `rpy_scale = -throttle_avg_max / rpy_low` les annule (§4.3). Il fallait réparer la
> plage de poussée pour que les gains **existent**, puis les régler.

Confirmé sans les masses ajoutées sur la batterie (rapport pilote, non mesuré au log).

---

## 0bis. Effet de chaque paramètre, hors problème de vibration

**Les gains PID ne changent PAS la puissance du drone.** Ils changent seulement *comment* la FC
répartit la poussée entre moteurs. La puissance vient des moteurs/hélices/batterie, et côté
logiciel uniquement de la plage `MOT_SPIN_MIN`↔`MOT_SPIN_MAX`.

| paramètre | ↑ donne | ↓ donne |
|---|---|---|
| `ATC_RAT_*_P` / `_I` | assiette plus ferme, tient mieux au vent | drone plus mou, « flotte », dérive |
| `ATC_RAT_*_D` | moins de dépassement | rebond après correction |
| `ATC_ANG_*_P` | **réponse au manche plus vive** | plus doux, « cinéma » |
| `MOT_SPIN_MAX` | plus de poussée max, throttle plus sensible | **moins sensible**, plus de marge de contrôle |

Notes utiles :
- `_I` doit suivre `_P` (convention ArduCopter ; l'Autotune met I = P). L'I supprime l'erreur
  **permanente** (CG décalé, vent constant).
- `_D` amplifie le bruit gyro quand on le monte : moteurs chauds, sifflement aigu. Sa
  contribution croît avec la fréquence (`ω·D`) — c'est pour ça qu'il pesait 49 % du gain à
  15,5 Hz.
- **Plafond connu sur cet appareil : au-dessus de `ATC_RAT_*_P ≈ 0,09`, le cycle limite à
  15,5 Hz revient.**
- `ATC_ANG_*_P` (4,5, jamais touché) agit à ~0,7 Hz, très loin de 15,5 Hz : **le monter ne
  réveille pas la résonance**. C'est le levier de feeling sans risque.

**Si le throttle est trop sensible** : le bouton est `MOT_SPIN_MAX` (0,95 d'origine). Le
passer à ~0,70 plafonne la sortie à 1700 µs → ~25 % moins sensible, T/W de 5,6 à 4,3 (excès
largement suffisant), et **remonte la poussée de hover** donc la marge de contrôle.
⚠ **Ne jamais retoucher `MOT_THST_HOVER` pour le feeling** : c'est une mesure, et le contrôleur
d'altitude s'en sert en feed-forward — le fausser ramène le problème n°1. Après tout changement
de `MOT_SPIN_MAX`, **re-mesurer** `MOT_THST_HOVER` avec `tools/thrust_range.py`.

---

## 1. Matériel et firmware

| Élément | Détail |
|---|---|
| **Frame** | FlyFishRC Volador VX3.5 (3,5", freestyle) |
| **FC** | SpeedyBee F405 Mini — STM32F405, IMU **ICM42688P** (SPI, 1 kHz), baro DPS310 (I2C1), **pas de compas** |
| **ESC** | SpeedyBee BLS 35A Mini V2 4-in-1, BLHeli_S `J-H-40`, **DShot300** |
| **Moteurs** | T-Motor F1404 **3800 KV** |
| **Hélices** | Gemfan Hurricane 3525 **tripales** |
| **Batterie** | LiPo 4S 850 mAh, montée **sur le dessus, en longueur (avant-arrière)** |
| **Masse** | ~280-300 g estimés — **jamais pesée** (voir §7) |
| **Firmware** | ArduCopter **4.8.0-dev**, build perso depuis fork (`ArduCopter-ARGOS`, hash `8927564c`), cible `SpeedyBeeF405Mini` |
| **Radio** | RadioMaster Pocket, ELRS 2.4G, CRSF sur UART2 |
| **GPS / compas** | **aucun** (GPS démonté, compas défectueux) — vols en Stabilize uniquement |
| **Montage FC** | silentblocs silicone d'origine dans les 4 trous de la carte |

Vols quasi exclusivement **en intérieur** (chambre), en **Stabilize**.

---

## 2. Symptômes rapportés au départ

1. Oscillation permanente dès le tout premier vol, intérieur comme extérieur, manches centrés.
2. En AltHold : montée incontrôlable, **manche des gaz à fond en bas**, jusqu'à taper le plafond.
3. Dérive constante, de direction variable d'un vol à l'autre.
4. Lacet incohérent (parfois manche à fond → tourne à peine).
5. Vibration ressentie qui **augmente avec le régime moteur**.

Tests initiaux restés sans effet : changement des 4 hélices, baisse des gains PID, recalibration
accéléro, isolation de câblage I2C, vol sans GPS.

---

## 3. Piège méthodologique majeur (à lire avant tout)

**Les messages `ATT` et `RATE` d'ArduPilot sont loggés à 10 Hz par défaut.** Nyquist = 5 Hz.

Le premier diagnostic concluait à une oscillation lente à **0,7 Hz**. C'était un **repliement
de spectre** : la vraie fréquence est **15,4 Hz**. Toute conclusion tirée d'un spectre calculé
sur `ATT`/`RATE` à cadence par défaut est invalide au-dessus de 5 Hz.

Instrumentation nécessaire pour toute affirmation sur une fréquence :

| besoin | paramètre | cadence obtenue |
|---|---|---|
| gyro/accel **bruts, avant filtres** | `INS_LOG_BAT_MASK=1`, `INS_LOG_BAT_OPT=1` | **~989 Hz** (Nyquist 494 Hz) |
| boucle de rate et PID | `LOG_BITMASK` bit 0 (`ATTITUDE_FAST`) | **400 Hz** |

Vérifié dans le source pour cet IMU : le driver `AP_InertialSensor_Invensensev3` n'active pas
le chemin « sensor rate » dédié, donc l'échantillonneur par lots est alimenté via
`log_gyro_raw()` avec `raw_gyro` — **donnée brute, avant les filtres ArduPilot et avant le
notch**. C'est ce qui rend le §5.6 interprétable.

Autres pièges rencontrés, tous coûteux en temps :
- `LOG_DISARMED` **exige un redémarrage** sur ce backend (puce SPI) : le changer en cours de
  session n'ouvre aucun log.
- Avec l'échantillonneur par lots actif, la puce de 8 Mo **se remplit en ~5 minutes** dès la
  mise sous tension, désarmée. Au-delà : « Chip full, logging stopped ».
- Les logs apparaissent datés **1970/1980** (pas de pile RTC). Prendre le **numéro le plus
  élevé**, jamais la date.
- Mission Planner en locale FR : `ConvertToDouble` plante sur « 3.5 ». **Taper « 3,5 »**, et
  **relire chaque valeur après écriture**.

---

## 4. PROBLÈME 1 — Saturation permanente du mélangeur (RÉSOLU)

### 4.1 Cause

Le drone est tellement surmotorisé que son point de hover tombe **hors de la plage de contrôle
utile d'ArduPilot**. Paramètres d'origine : tous par défaut (`MOT_SPIN_MIN=0.15`,
`MOT_SPIN_ARM=0.10`, `MOT_THST_EXPO=0.65`, `MOT_THST_HOVER=0.35`).

### 4.2 Mesures (hover stabilisé, 3 vols, 2 jours différents)

| grandeur | mesure |
|---|---|
| sortie moteur au hover | **1246 µs** |
| `MOT_SPIN_MIN` = 0.15 → 1150 µs | **96 µs de marge sous le hover** |
| poussée de hover réelle (échelle 0→1 d'ArduPilot) | **0.052** |
| `MOT_THST_HOVER` = 0.35 | **faux d'un facteur 6,8** |
| drapeau `LIMIT` de `PIDR`/`PIDP`/`PIDY` (bit 0) | **98-99,4 % du temps, tous les vols** |
| ≥1 moteur collé sur `MOT_SPIN_MIN` | **100 % du temps** |
| terme I des boucles de rate | **≈ 0** (std 0.0002) |
| erreur d'assiette moyenne | **+5,96° roulis / +8,03° tangage** |
| `CTUN.ThO` → `MOTB.ThrOut` | 0.024 → 0.056 (**×2,4**) |
| course de manche des gaz au hover | **6,5 %** |

### 4.3 Mécanisme (vérifié dans `AP_MotorsMatrix::output_armed_stabilizing`)

Quand la commande roulis+tangage+lacet ne tient pas dans la plage de poussée disponible :

```c
rpy_scale = -throttle_avg_max / rpy_low;
limit.set_rpy(true);
_thrust_rpyt_out[i] = throttle_best + rpy_scale * _thrust_rpyt_out[i];
```

Trois conséquences, toutes observées :

1. **Les gains PID deviennent mathématiquement sans effet.** Doubler la sortie des PID double
   `rpy_low`, donc divise `rpy_scale` par deux : le produit est **inchangé**. C'est la raison
   pour laquelle baisser rate P ou angle P n'a jamais rien changé. Le tune n'était pas bon —
   il était **hors circuit**.
2. **Intégrateurs gelés** (`_motors.limit.roll` passé en argument `limit` de
   `AC_PID::update_all`) → plus de trim → erreur d'assiette permanente → **dérive constante de
   direction variable**.
3. **La poussée moyenne n'est plus pilotée par le manche** mais par
   `get_throttle_avg_max() = throttle_in*(1-mix) + MOT_THST_HOVER*mix` :
   - Stabilize (`ATC_THR_MIX_MAN`=0.1) → plancher 0.035 **> hover réel 0.026** : monte gaz fermés ;
   - AltHold (`ATC_THR_MIX_MAX`=0.5) → plancher **0.175 = 7× le hover réel**.

L'**autorité lacet** est bridée par le même mécanisme (`yaw_allowed` calculé sur une marge
moteur quasi nulle puis forcé à `MOT_YAW_HEADROOM`), ce qui explique le symptôme n°4.

### 4.4 L'envolée AltHold, à la seconde près

Log, bascule en AltHold à t=188,57 s : `MOTB.ThrOut` saute à **0,1750** — soit exactement
`MOT_THST_HOVER × ATC_THR_MIX_MAX` = 0,35 × 0,5 — et **reste figé 1,3 s** pendant que
`CTUN.ThO` (gaz commandés) vaut **0,0000** et que le manche est en bas.
`BAlt` : 3,97 → 13,68 m, soit **+7,5 m/s**.

Aggravant, deux verrous du firmware :
- `AP_MOTORS_THST_HOVER_MIN = 0.125f` est un **clamp dur** dans `get_throttle_hover()` : avec
  un hover réel à 0,052, `MOT_THST_HOVER` ne peut PAS descendre assez bas → **AltHold est
  structurellement impossible** dans cette configuration ;
- `Copter::update_throttle_hover()` sort tôt si `flightmode->has_manual_throttle()` → **il
  n'apprend jamais en Stabilize**. Cercle vicieux complet.

### 4.5 Correction appliquée

```
MOT_SPIN_ARM   0.10  → 0.03
MOT_SPIN_MIN   0.15  → 0.05      (calibration au Motor Test, hélices démontées)
MOT_THST_EXPO  0.65  → 0.43      (posé par MP « Initial Tune Parameters », hélice 3,5")
MOT_THST_HOVER 0.35  → 0.179     (valeur MESURÉE, pas devinée)
```

Note : `MOT_SPIN_MAX` a été laissé à 0,95. Le plafonner n'a pas été nécessaire, la poussée de
hover étant repassée au-dessus du plancher dur de 0,125.

### 4.6 Résultat

| | avant | après |
|---|---|---|
| erreur d'assiette roulis (std / moyenne) | 5,13° / **+5,96°** | **0,82-1,31° / +0,67°** |
| erreur tangage | 3,03° / +8,03° | **0,46° / +1,87°** |
| poussée de hover | 0,052 | **0,173-0,178** |
| manche des gaz au hover | 6 % | **47 %** |
| gaz demandés → appliqués | ×2,4 | **×1,0** |
| intégrateurs | gelés | **actifs** |
| mélangeur saturé | 99,4 % | 68-78 % (le reste vient du problème n°2) |
| AltHold | structurellement impossible | débloqué (non retesté en vol) |

Pilote : *« première fois que je peux le faire voler stable dans ma chambre »*. Dérive
supprimée, gros wobble lent disparu.

**⚠ Piège de la correction :** tant que le mélangeur saturait, le gain d'assiette effectif
valait `P × rpy_scale ≈ 0,135 × 0,1`. Une fois la saturation levée (`rpy_scale → 1`), **le
gain réel est multiplié par ~8-10 d'un coup**. Les gains ont dû être baissés en conséquence
(§6).

**Incident** : entre deux vols, `MOT_THST_HOVER` s'est retrouvé à **0,6864** (= le maximum du
firmware, 3,9× le hover réel), écrit au clavier — probablement victime du séparateur décimal
(voir §3). Effet : courbe de manche ~1,5× plus raide (hover à 10 % de manche) **et** plancher
du mélangeur repassé au-dessus de la commande (`ThO 0,113 → ThrOut 0,165`). Résultat : montée
au plafond. Cause non formellement établie (§7).

---

## 5. PROBLÈME 2 — Cycle limite de roulis à 15,5 Hz (RÉSOLU)

Ce problème était **masqué** par le n°1 et n'est apparu clairement qu'après sa correction.
Ressenti : vibration rapide, faible amplitude, audible (bruit moteur « pas lisse »).

### 5.1 Signature spectrale (gyro brut, 989 Hz, en hover)

```
GYRO  à 15,47 Hz :  X = 101,0 deg/s     Y = 2,06      Z = 14,3      → X/Y = 49
ACCEL à 15,47 Hz :  X = 0,037 m/s²      Y = 0,577     Z = 0,231
ACCEL pics moteur :  223 / 237 / 256 / 270 / 286 / 301 Hz  (amplitudes 0,23-0,44 m/s²)
```

- **Rotation de roulis quasi pure** (X/Y = 49).
- Amplitude angulaire : `101 / (2π × 15,47)` → **±1,04°**.
- Le gyro ne contient que **0,8 %** de son énergie dans 120-250 Hz : `INS_GYRO_FILTER = 101 Hz`
  traite déjà le bruit moteur. **Un notch harmonique classique (suivi du régime) est inutile ici.**

### 5.2 Localisation de l'axe de rotation

L'accéléro voit la somme de la projection de gravité (en phase avec l'angle) et de
l'accélération tangentielle (en opposition) :

```
|a_y| = θ · |g − r·ω²|
0,577 = 0,0181 · |9,81 − r·9448|   →   r ≈ 4 mm
```

**L'axe de rotation passe à ~4 mm de la puce IMU.** Deux vols indépendants donnent 2,3 et
4,8 mm. Si c'était **la cellule** qui roulait, l'axe passerait par son centre de gravité,
situé 10 à 25 mm au-dessus de la FC (batterie en top-mount) : l'accéléro verrait alors
**1,9 à 4,5 m/s²** au lieu de 0,577.

### 5.3 Argument énergétique (exclut la cellule)

Faire rouler la **cellule entière** de ±1,04° à 15,4 Hz demanderait :

```
accélération angulaire = 0,0181 × (2π×15,4)²   = 180 rad/s²
inertie de roulis (280 g, moteurs à 85 mm)     ≈ 4·10⁻⁴ kg·m²   (ESTIMÉE, §7)
couple                                          = 0,072 N·m
→ différentiel de poussée                       ≈ 61 g PAR MOTEUR, à 15 Hz
```

Le hover complet fait ~70 g par moteur, et la constante de temps d'un moteur de cette taille
(~10-20 ms) atténue déjà fortement à 15 Hz. **Physiquement impossible.** L'argument résiste à
une erreur d'un facteur 3 sur l'inertie.

Le même raisonnement, appliqué aux câbles / RX / VTX : un câble de 3 g à 5 cm oscillant de
±2 mm à 15 Hz produit un moment cinétique **~50× trop faible** pour secouer 300 g. En
revanche, **une pièce qui porte l'IMU n'a besoin de faire tourner qu'elle-même** — d'où
l'asymétrie entre gyro (96 % de l'énergie) et accéléro (~1 %).

### 5.4 Invariances mesurées

| on fait varier | facteur | fréquence | solidité |
|---|---|---|---|
| gains de boucle (`D` ÷3, `P` ÷1,5) | commande moteur ÷5 | **inchangée** | forte (plusieurs vols) |
| régime moteur (fondamentale 235→256 Hz) | ±4 % | **inchangée (±1,5 %)** | forte |
| **masse ajoutée latéralement sur la batterie** | inertie de roulis fortement augmentée | **inchangée (15,47-15,50 Hz)** | **forte** (11 lots, même vol) |
| orientation batterie (rotation 90°) | inertie de roulis fortement modifiée | inchangée | **FAIBLE — un seul lot** (§7.2) |
| vols successifs sur 3 jours | — | 15,44 / 15,83 / 16,22 Hz | moyenne |

Mesure la plus propre (2026-07-29 soir, poids latéraux en place, gyro brut lot par lot) :
`15.50, 15.50, 15.49, 15.49, 15.49, 15.48, 15.48, 15.48, 15.48, 15.47, 15.47 Hz`.

### 5.4bis Intermittence — la caractéristique la plus marquante

À **fréquence rigoureusement constante**, l'amplitude varie d'un facteur **30** à l'intérieur
d'un même vol :

```
t=33,7s   9,2 deg/s    <- calme
t=36,2s   9,0
t=38,3s  79,0          <- le mode « s'accroche »
t=40,5s  74,6
t=42,9s  21,7
t=49,8s   2,8          <- très calme
t=58,9s  52,1          <- reprend juste avant l'atterrissage
```

Le pilote perçoit exactement ces alternances. **Interprétation proposée** (non démontrée) : le
gain de la boucle d'entretien est très proche de 1, si bien que le mode s'établit ou non selon
des conditions marginales. Aucune corrélation propre n'a pu être établie avec la position ou
la vitesse de variation des gaz — l'hypothèse pilote « ça ne vibre que quand le régime est
stable » n'est **ni confirmée ni infirmée**.

Conséquence pratique : **toute comparaison avant/après doit porter sur plusieurs dizaines de
secondes et sur la fraction de temps en résonance**, pas sur une impression ponctuelle ni sur
un seul lot FFT.

### 5.5 Test moteurs coupés (exclut un capteur défectueux)

Amplitude de la raie 13-19 Hz du gyro roulis selon le régime :

| moteurs | amplitude |
|---|---|
| **arrêtés (désarmé)** | **0,013 rad/s** |
| ralenti (~1030-1100 µs) | 0,0007 |
| proche hover (~1250-1300 µs) | **1,292** (×100) |

Un capteur défectueux produirait son artefact indépendamment des moteurs. Et l'accéléro
confirme une rotation **physiquement réelle** via la projection de gravité (§5.2).

Sur un vol instrumenté, la transition est nette : moteurs 1094 µs → amplitude 0,002 ;
moteurs 1194 µs → amplitude 1,60.

### 5.6 Le test du notch — et ce qu'il a renversé

Notch fixe posé sur la fréquence mesurée :
`INS_HNTCH_ENABLE=1, MODE=0 (fixe), FREQ=15, BW=8, ATT=30, HMNCS=1`.

```
                                          avant notch    avec notch
RATE.R à 15,4 Hz (vu par le contrôleur)    26,4 deg/s  →   3,13     (notch efficace)
ROut   à 15,4 Hz (commande moteur)          0,0633     →   0,0063
GYRO BRUT à 15,4 Hz (mouvement physique)   101,0 deg/s →   1,6      ← point clé
nouveau pic (gyro brut)                        —       →   7,72 Hz = 340,7 deg/s
```

Le notch n'agit **que sur ce que le contrôleur voit** ; il ne peut pas toucher à la mécanique.
Si le basculement avait été une résonance purement externe, il serait resté dans le brut.

**→ La boucle de commande participe à l'entretien de l'oscillation.**

Effet secondaire : le notch a mangé trop de phase dans la bande de la boucle et l'a rendue
**instable à 8 Hz, avec une amplitude 3× plus grande**. Configuration abandonnée
(`INS_HNTCH_ENABLE=0`).

Une version adoucie (`BW=4, ATT=15`) a été essayée ensuite : le pilote rapporte « rien
changé », **non vérifié au log** (téléchargement échoué, §7).

### 5.7 Mesure de la réponse du contrôleur (avant notch)

Avec `RATE`/`PIDR` à 400 Hz, sur une fenêtre de 17 s :

```
prédiction à partir des gains P=0,09 / D=0,0009 :
   ROut = -(P + jωD) · R,  ω = 96,9 rad/s
   → amplitude 0,0578,  phase -136°
mesuré :  amplitude 0,0633,  phase -151°
```

À 10 % près, la sortie de la FC est **exactement la réponse linéaire de ses PID** au signal
gyro. Elle n'injecte rien de propre à cette fréquence, et sa phase est majoritairement
**opposée** au mouvement (composante en phase : cos(151°) = −0,87 → amortissante).

Ce résultat et celui du §5.6 sont **en tension** : voir §7.

### 5.8 Modèle retenu

Couplage entre un **mode mécanique** et la **boucle de commande** :

```
la carte bascule sur ses silentblocs (mode propre ~15,4 Hz)
   → le gyro, boulonné dessus, le rapporte comme une rotation de l'appareil
      → la FC commande les moteurs
         → la cellule bouge
            → le mouvement se réinjecte dans le montage de la carte
               → entretient le basculement
```

| observation | expliquée par ce modèle ? |
|---|---|
| fréquence figée malgré ÷3 sur D | ✓ fixée par le mode mécanique |
| amplitude peu sensible au gain | ✓ cycle limite, amplitude fixée par la non-linéarité |
| absente moteurs coupés | ✓ pas d'excitation, boucle ouverte |
| insensible au régime moteur | ✓ |
| axe de rotation à 4 mm de l'IMU | ✓ c'est la carte qui bouge |
| batterie en travers : amplitude ÷10, fréquence inchangée | ✓ couplage/amortissement modifiés, pas la raideur |
| ce même essai était **intermittent** | ✓ gain de boucle juste au seuil de 1 |
| le notch tue la raie dans le gyro **brut** | ✓ il coupe la boucle |
| réponse de la FC purement linéaire et amortissante (§5.7) | ✗ **non expliqué** |

### 5.9 ✅ RÉSOLUTION — le gain de boucle était au-dessus du seuil d'auto-entretien

Test décisif (2026-07-29 nuit) : gains réduits d'un tiers, évalués sur la **fraction de temps
en résonance** (§5.4bis) et non sur l'amplitude des pics.

| | avant `P=0,09 D=0,0009` | **après `P=0,06 D=0,0005`** |
|---|---|---|
| lots gyro bruts | 16 (36 s de vol) | **21 (51 s)** |
| amplitude moyenne de la raie | 43,2 deg/s | **1,7** |
| amplitude max | 109,4 deg/s | **3,5** |
| **temps en résonance (>40 deg/s)** | **44 %** | **0 %** (0/21) |
| rms `RATE.R` en vol | 50,2 deg/s | **10,6** |
| erreur d'assiette roulis (std / moy / max) | 0,71° / −0,31° / 3,0° | **0,40° / −0,02° / 1,0°** |
| erreur d'assiette tangage (std) | 2,40° | **1,76°** |
| mélangeur saturé | 11 % | **0 %** |
| écart instantané moteurs (moy / p95) | 276 / 578 µs | **126 / 153 µs** |

**Aucune contrepartie : le suivi d'attitude s'améliore avec des gains plus faibles.** Signature
classique — l'oscillation consommait l'autorité de commande ; la supprimer libère la boucle.

Gain de boucle à 15,5 Hz : `P + ω·D` passe de `0,090 + 0,0877 = 0,178` à
`0,060 + 0,0487 = 0,109`, soit **−39 %**. C'est ce pas qui franchit le seuil.

**Pourquoi les baisses de gains précédentes semblaient sans effet.** Elles réduisaient le gain
**sans franchir le seuil**, et elles étaient évaluées sur l'*amplitude* — laquelle, dans un
cycle limite, est fixée par la non-linéarité et non par le gain linéaire. À `P=0,09` le système
était exactement **au** seuil, d'où l'intermittence à 44 %.

**L'ordre des opérations était contraint.** Au départ, baisser les gains n'aurait rien donné :
la saturation du mélangeur les annulait mathématiquement (§4.3). Il fallait réparer la plage de
poussée pour que les gains **existent**, puis les régler.

### 5.10 Réserves sur la résolution

- **Le mode mécanique à 15,5 Hz est toujours là**, on a seulement cessé de l'exciter. On est
  sous le seuil, mais pas loin. Rigidifier le montage de la stack (entretoises M3 nylon/alu au
  lieu des silentblocs) reste le remède de fond, et donnerait de la marge.
- **⚠ Ne pas lancer l'Autotune tel quel** : il monte les gains jusqu'à trouver la limite — il
  retrouverait ce cycle limite et calerait dessus. Rigidifier d'abord, ou `AUTOTUNE_AGGR` bas
  avec le doigt sur l'interrupteur.
- **Un seul vol** (51 s, 4 segments). À reconfirmer, idéalement en extérieur.
- Les **poids latéraux** collés sur la batterie sont désormais une variable inutile à tester :
  les retirer et refaire 60 s pour voir si le 0 % tient.

### 5.11 Hypothèses éliminées, avec la mesure qui les élimine

| hypothèse | éliminée par |
|---|---|
| tune / gains PID seuls | fréquence inchangée sur ÷3 en D et ÷1,5 en P |
| gyro ou IMU défectueux | raie absente moteurs coupés (×100) ; rotation confirmée par l'accéléro |
| balourd hélice ou moteur | apparaîtrait à 1× le régime (~250 Hz) ; la fréquence ne suit pas le régime |
| battement entre moteurs | suivrait le régime |
| batterie = masse résonante | rotation à 90° : inertie de roulis fortement modifiée, fréquence inchangée |
| câbles, RX, VTX, antenne | argument de moment cinétique (~50× trop faible) ; antenne déplacée sans effet |
| cellule entière = corps résonant | géométrie de l'axe (4 mm de l'IMU) + 61 g/moteur impossibles |
| visserie de stack manquante | résonance présente **avant** la perte des écrous ; scotchage sans effet |
| bruit moteur passant dans le gyro | seulement 0,8 % de l'énergie gyro en 120-250 Hz |

### 5.12 Ce qui n'a **pas** été testé

- **Montage rigide de la FC** (entretoises nylon/alu au lieu des silentblocs) — c'est le
  remède principal du modèle, **jamais essayé** : les pas de vis de la plaque supérieure du
  cadre sont cassés, l'accès à la stack est impossible. **C'est le blocage actuel.**
- Test de masse ajoutée sur la stack (scotcher quelques grammes → si la fréquence descend,
  c'est bien elle qui résonne). Même blocage.
- Vol en extérieur avec la configuration actuelle.
- AltHold depuis la correction du problème n°1.
- Betaflight comme contrôle indépendant (le phénomène étant mécanique, il devrait s'y voir
  aussi ; utile seulement si le modèle actuel s'effondre).

Un essai modal par pichenettes a été tenté et a **échoué** : l'échantillonneur n'écoute que
45 % du temps (lots de 1,04 s séparés de ~1,2 s) et l'énergie des impacts était ~100× sous
celle de l'oscillation en vol. Pour le refaire : déplacer-et-lâcher plutôt que taper, drone
tenu en l'air et non posé sur une table.

---

## 6. Jeu de paramètres actuel

```
# plage de poussée (problème n°1, résolu)
MOT_SPIN_ARM      0.03
MOT_SPIN_MIN      0.05
MOT_SPIN_MAX      0.95
MOT_THST_EXPO     0.43
MOT_THST_HOVER    0.179          # valeur mesurée
MOT_BAT_VOLT_MAX  16.8
MOT_BAT_VOLT_MIN  13.2

# filtres (posés par MP « Initial Tune Parameters », hélice 3,5")
INS_GYRO_FILTER   101
INS_ACCEL_FILTER  10
ATC_RAT_RLL_FLTD  50.5    ATC_RAT_RLL_FLTT  50.5
ATC_RAT_PIT_FLTD  50.5    ATC_RAT_PIT_FLTT  50.5

# gains (baissés après la levée de la saturation, cf. §4.6)
ATC_RAT_RLL_P  0.06   ATC_RAT_RLL_I  0.06   ATC_RAT_RLL_D  0.0005
ATC_RAT_PIT_P  0.06   ATC_RAT_PIT_I  0.06   ATC_RAT_PIT_D  0.0005
# c'est CE pas (-39 % de gain de boucle a 15,5 Hz) qui a supprime le cycle limite, cf. §5.9

# notch : ANNULÉ (déstabilisait la boucle à 8 Hz)
INS_HNTCH_ENABLE  0

# logging courant (léger, téléchargements fiables)
LOG_BITMASK       136954
INS_LOG_BAT_MASK  0
# pour rediagnostiquer : LOG_BITMASK 136955 + INS_LOG_BAT_MASK 1 + INS_LOG_BAT_OPT 1
# (puce pleine en ~5 min dans ce mode — effacer avant, vol de 60-90 s, télécharger aussitôt)
```

Reliquats connus, non traités :
- `AHRS_TRIM_X/Y` ≈ 0 alors que le drone dérive vers l'avant-droite. `RC7_OPTION=5`
  (`SAVE_TRIM`) est **inutilisable en vol** : le source exige `channel_throttle->get_control_in() == 0`.
  Utiliser **`RC7_OPTION=182`** (`AHRS_AUTO_TRIM`, autotrim en vol, vérifié compilé dans le
  binaire) : voie haute = « AutoTrim running », voler ~30 s en corrigeant aux manches, voie
  basse = « Trim saved ».
- CG reculé : les moteurs arrière tournent **34 µs** plus vite que les avant. Avancer la
  batterie de 5-10 mm, cible < 10 µs d'écart.
- Autotune **jamais lancé**. ⚠ Le mode mécanique à 15,5 Hz existe toujours (§5.10) : l'Autotune
  monte les gains jusqu'à trouver la limite et retrouverait ce cycle limite. Rigidifier d'abord
  le montage de la stack, ou `AUTOTUNE_AGGR` bas avec le doigt sur l'interrupteur.

---

## 7. ⚠ Ce dont je ne suis PAS sûr

Section volontairement explicite. Rien ici n'est établi.

> **Avertissement de méthode.** Au cours de cette enquête j'ai posé **trois conclusions que
> j'ai ensuite dû retirer** : « le wobble vient du tune », « la résonance est purement
> mécanique, la boucle est innocentée », et « ajouter de la masse déplace la fréquence, donc la
> batterie est dans le résonateur ». Les trois venaient de la même faute : conclure à partir
> d'une mesure unique ou d'une fenêtre mal choisie, sur un phénomène dont l'amplitude varie
> d'un facteur 30 spontanément (§5.4bis). **Sur ce système, une mesure isolée ne prouve rien.**

1. **Contradiction non résolue entre §5.6 et §5.7.** Le notch a fait disparaître la raie du
   gyro **brut**, ce qui indique que la boucle entretenait l'oscillation. Mais la mesure de
   phase montre une commande **majoritairement amortissante** et d'amplitude exactement égale
   à la réponse linéaire des PID. Les deux ne se concilient pas proprement. Explication
   possible : quand la mesure « avec notch » a été prise, le drone était dans un état
   violemment différent (8 Hz, 340 deg/s), et la raie à 15,4 Hz n'était peut-être simplement
   plus excitée — d'autant que l'amplitude varie spontanément d'un facteur 30 (§5.4bis).
   **Ce point invalide potentiellement la conclusion « la boucle participe ».**
2. **L'élimination de la batterie par la rotation à 90° était mal étayée.** Dans ce vol-là, la
   résonance n'était présente que 5 % du temps, soit **un seul lot FFT sur 19**. J'avais
   présenté « σ = 0,00 Hz » comme une preuve solide : sur un échantillon unique, σ = 0 ne
   signifie rien. La conclusion est aujourd'hui **soutenue par une mesure bien meilleure** (11
   lots avec masse ajoutée latéralement, fréquence inchangée à 15,47-15,50 Hz), mais elle ne
   l'était pas quand je l'ai écrite.
3. **Un épisode à 9,8 Hz a été mal interprété.** Un vol a montré une oscillation à 6-11 Hz avec
   rms 179 deg/s, que j'ai lue comme un **décalage** de la raie à 15,5 Hz sous l'effet des
   masses ajoutées. Le vol suivant, mêmes masses et instrumentation complète, donne 15,48 Hz
   inchangé. Il s'agissait donc d'un **phénomène distinct** (oscillation lente et ample, peut-être
   induite par le pilote), pas d'un déplacement de fréquence. Ce que cet épisode démontre
   quand même : le drone peut entrer dans d'autres régimes oscillatoires que celui à 15,5 Hz.
4. **Quel corps bascule exactement.** La géométrie (axe à 4 mm de l'IMU) désigne la carte ou
   la stack. Mais **scotcher fermement la FC n'a rien changé** — soit le scotch n'apporte pas
   de raideur en basculement (plausible), soit le corps résonant est autre chose. Non tranché.
5. **L'inertie de roulis (4·10⁻⁴ kg·m²) est calculée, pas mesurée**, à partir d'une masse
   elle-même **jamais pesée** (~280-300 g). L'argument du §5.3 en dépend — il résiste à une
   erreur d'un facteur 3, mais pas davantage. **Peser le drone consoliderait ou casserait une
   des pièces maîtresses du raisonnement, et prend une minute.**
6. **Le notch adouci (`BW=4, ATT=15`) n'a pas été vérifié au log** : téléchargement à 0 octet.
   Le « rien changé » est un ressenti pilote, non mesuré.
7. **Le lien entre la perte des écrous de stack et la fréquence** n'est pas établi. Les vols
   du 27/07 donnaient 15,83 et 16,22 Hz, ceux d'après 15,44 Hz — une baisse de 5 % cohérente
   avec une perte de précharge, mais l'instant exact de la perte est inconnu.
8. **L'intermittence** (le mode « s'accroche » ou non) est *interprétée* comme un gain de
   boucle proche de 1. C'est cohérent avec tout le reste, mais ce n'est pas une mesure.
9. **L'origine du `MOT_THST_HOVER = 0,6864`** n'a jamais été établie. Le séparateur décimal
   FR est le suspect principal (il avait déjà fait planter Mission Planner), mais ce n'est pas
   démontré.
10. **Le lien de causalité de l'essai « batterie en travers »** (amplitude ÷10) est interprété
   comme un changement de couplage/amortissement, sur la seule base que la fréquence n'a pas
   bougé. Non reproductible depuis.
11. **La marge de phase réelle de la boucle** n'a jamais été mesurée. L'instabilité à 8 Hz
   provoquée par le notch suggère qu'elle est faible, mais aucun relevé de réponse
   fréquentielle n'a été fait.

---

## 8. Questions ouvertes (pour un forum ou un expert)

1. Un **mode de basculement du montage souple de la FC vers 15 Hz** sur un multirotor très
   léger sous ArduPilot : est-ce un phénomène connu ? Les silentblocs sont dimensionnés pour
   une masse donnée — sous-chargés, leur fréquence propre descend, et 15 Hz tombe en plein
   dans la bande de la boucle de rate.
2. **Un notch fixe à 15 Hz est-il viable sous ArduCopter**, ou est-ce structurellement trop
   près de la bande passante de la boucle de rate ? Quelle combinaison `BW`/`ATT` minimale
   permet de faire passer un gain de boucle sous 1 sans détruire la marge de phase ?
3. **Montage rigide ou souple** pour une FC sur un multirotor ArduPilot très léger (< 350 g) ?
   La pratique Betaflight (souple + filtrage agressif + RPM filter) est-elle transposable ?
4. Existe-t-il une méthode propre pour **mesurer la fonction de transfert du montage de la
   FC** en vol, sans banc de vibration ?

---

## 9. Prochaines étapes

1. **Débloquer l'accès à la stack** — extraire les vis à pas foiré de la plaque supérieure
   (embout Torx légèrement surdimensionné enfoncé au marteau, ou extracteur, ou perçage de la
   tête). **C'est le chemin critique.**
2. **Monter la stack rigide** : entretoises M3 nylon ou alu de la hauteur des silentblocs.
   Coût ~3 €. Vol de 30 s → mesurer fréquence et amplitude de la raie.
3. Si la raie disparaît : modèle confirmé, dossier clos.
   Si elle persiste à la même fréquence : le corps résonant est ailleurs → **test de masse
   ajoutée** (scotcher quelques grammes sur chaque candidat, chercher la fréquence qui descend).
4. Ensuite seulement : `AHRS_AUTO_TRIM`, recentrage du CG, AltHold en extérieur, puis
   **Autotune**.

---

## 10. Outils

- Analyses en Python + `pymavlink` sur les `.bin` DataFlash.
- `tools/thrust_range.py` (dans ce dépôt) : inverse la courbe de poussée d'ArduPilot pour
  sortir la poussée de hover réelle, le % de saturation du mélangeur et le `MOT_SPIN_MAX` à
  viser. Gère la compensation de tension via `MOTB.LiftMax`.
- `tools/log_quicklook.py` : bilan de santé rapide d'un log (vibrations, équilibre moteurs,
  erreur d'assiette, batterie, erreurs système).
- Historique jour par jour : `docs/journal.md` (entrées 2026-07-20 → 2026-07-29).
