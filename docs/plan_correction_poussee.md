# ARGOS — Plan de correction : sortir le drone de la saturation permanente du mélangeur

> Fait suite au diagnostic `docs/diagnostic_wobble.md` §9 (2026-07-25).
> Document opérationnel : à suivre dans l'ordre, sur le terrain.

---

## Ce qu'on cherche à obtenir, en une image

ArduPilot ne raisonne pas en microsecondes mais sur une échelle de poussée **0 → 1**, où
`0 = MOT_SPIN_MIN` et `1 = MOT_SPIN_MAX`. Pour corriger une inclinaison, il doit pouvoir
**baisser** deux moteurs et **monter** les deux autres.

Aujourd'hui ton hover est à **0.052** sur cette échelle : il ne reste que 0.052 de marge vers
le bas. Le mélangeur passe son temps à demander plus que ça, donc il rabote la commande
d'assiette et gèle les intégrateurs — **99 % du temps, depuis le premier vol**.

**Objectif : remonter le hover autour de 0.25**, c'est-à-dire au milieu de l'échelle, avec
autant de marge en dessous qu'au-dessus. Tout le reste (wobble, dérive, AltHold, Autotune)
découle de ça.

**Trois leviers**, à utiliser dans cet ordre :
1. **baisser `MOT_SPIN_MIN`** → on descend le plancher, donc le hover remonte sur l'échelle ;
2. **baisser `MOT_SPIN_MAX`** → on descend le plafond, le hover remonte encore (tu as un
   excès de poussée absurde, tu peux en sacrifier sans risque) ;
3. **corriger `MOT_THST_HOVER`** → pour que le contrôleur d'altitude sache enfin où est le
   point d'équilibre.

---

## Étape 0 — Filet de sécurité (5 min, au chaud)

**Pourquoi.** Tu vas changer une dizaine de paramètres. S'il faut revenir en arrière ou
comprendre ce qui a changé, il faut une référence.

**Comment.**
- MP → `CONFIG` → `Full Parameter List` → **Save to file** → `params_2026-07-25_avant.param`
- Range ce fichier dans le repo (`config/`) — c'est aussi de la traçabilité pour l'entretien.
- Refais un « Save to file » **après chaque étape**, avec un nom daté. Tu pourras diff.

**Chiffres de référence à garder sous les yeux** (mesurés sur tes logs actuels) :

| grandeur | avant | cible |
|---|---|---|
| poussée de hover (échelle 0→1) | **0.052** | **~0.25** |
| `PIDR.Flags` bit 0 (`LIMIT`) | **99 %** du temps | **< 10 %** |
| sortie moteur au hover | 1246 µs | ~1250 µs (peu changer) |
| manche des gaz au hover | **6,5 %** | **~50 %** |
| erreur d'assiette moyenne | 6-8° | < 2° |

---

## Étape 1 — Remettre des gains sains, AVANT de toucher aux moteurs

**Pourquoi cette étape en premier, et pas en dernier.** Deux raisons.

1. **Sécurité.** Ton gain d'assiette effectif est aujourd'hui `P × rpy_scale`, avec
   `rpy_scale ≈ 0.1`. Dès que la saturation sera levée, `rpy_scale → 1` : **le gain réel sera
   multiplié par ~8-10 d'un coup.** Si tu voles avec les gains actuels après avoir corrigé
   les moteurs, tu risques une oscillation violente au décollage. Il faut les baisser
   *avant*.
2. **Ordre des opérations.** L'outil de MP qui pose les gains touche aussi à `MOT_THST_EXPO`
   et parfois aux `MOT_SPIN_*`. Si tu le lances *après* ta calibration, il l'écrase. Donc :
   gains d'abord, moteurs ensuite.

**Comment.** MP → `SETUP` → `Mandatory Hardware` → **Initial Tune Parameters** (selon la
version de MP ça peut être ailleurs dans SETUP). Tu renseignes :
- diamètre d'hélice : **3.5 pouces**
- batterie : **4S**

Il calcule et propose tout le bloc `ATC_RAT_*`, `ATC_ANG_*`, `INS_GYRO_FILTER`,
`MOT_THST_EXPO`. Accepte, puis **Write Params**, puis reboot.

**Si tu ne trouves pas l'écran**, pose-les à la main (valeurs volontairement molles — pour
les vols de mesure on veut *sûr*, pas *performant*) :

```
ATC_RAT_RLL_P  = 0.06     ATC_RAT_PIT_P  = 0.06
ATC_RAT_RLL_I  = 0.06     ATC_RAT_PIT_I  = 0.06     (convention ArduPilot : I = P)
ATC_RAT_RLL_D  = 0.003    ATC_RAT_PIT_D  = 0.003
ATC_RAT_YAW_P  = 0.12     ATC_RAT_YAW_I  = 0.012
INS_GYRO_FILTER = 50      (20 Hz = valeur pour du 10 pouces ; ton 3,5" tourne bien plus vite)
```

Laisse `ATC_ANG_RLL_P` / `ATC_ANG_PIT_P` à 4.5 pour l'instant.

**Ce que tu dois comprendre :** ces valeurs ne sont **pas** le tune final. C'est un point de
départ conservateur. Le vrai tune, c'est l'Autotune, tout à la fin. Là on veut juste un drone
qui vole assez bien pour qu'on puisse le mesurer.

---

## Étape 2 — Calibrer `MOT_SPIN_ARM` et `MOT_SPIN_MIN` (banc, hélices démontées)

**Pourquoi.** `MOT_SPIN_MIN` = 0.15 est la valeur par défaut d'ArduPilot, dimensionnée pour
de gros moteurs lents en 5-10 pouces. Sur tes F1404 3800 KV, **1150 µs fait déjà tourner les
hélices à ~9000 tr/min**, ce qui produit déjà une grosse partie de ta poussée de hover. Le
firmware croit que c'est « poussée zéro ». D'où tout le problème.

**Le mapping à connaître** (vérifié dans le source, `ArduCopter/motor_test.cpp` :
`pwm = pwm_min + (pwm_max - pwm_min) × pourcentage/100`, et tes `MOT_PWM_MIN/MAX` = 1000/2000) :

> **Le pourcentage affiché dans le Motor Test de MP = la valeur de `MOT_SPIN_*` × 100.**
> 5 % dans le motor test ⇔ `MOT_SPIN_MIN = 0.05`. Pas de conversion à faire.

**Comment.**

1. **HÉLICES DÉMONTÉES.** Les 4. Batterie branchée (l'USB ne suffit pas pour les moteurs).
2. MP → `SETUP` (ou `Actions`) → **Motor Test**. Durée : 2-3 s par test.
3. **Trouver `MOT_SPIN_ARM`** : lance « Test all motors » à **3 %**. Est-ce que les 4
   démarrent, à chaque fois, sans hésiter ? Sinon, monte : 4 %, 5 %… Note le premier
   pourcentage où **les 4 démarrent de façon fiable, 3 essais d'affilée**.
   → `MOT_SPIN_ARM` = ce pourcentage / 100, **+ 0.01 de marge**.
4. **Trouver `MOT_SPIN_MIN`** : continue à monter jusqu'au pourcentage où les 4 tournent
   **rond, sans à-coup, et répondent immédiatement** quand tu changes la valeur.
   → `MOT_SPIN_MIN` = ce pourcentage / 100, **+ 0.01 de marge**.
5. Règle de sécurité : garde **`MOT_SPIN_MIN` ≥ `MOT_SPIN_ARM` + 0.02**.

**Ordre de grandeur attendu :** `MOT_SPIN_ARM` ≈ 0.02-0.04, `MOT_SPIN_MIN` ≈ 0.04-0.06.
Si tu tombes bien au-dessus de 0.08, remesure — c'est suspect.

**⚠ Le piège BLHeli_S.** Tes ESC sont en BLHeli_S stock (`J-H-40`). À très bas régime, ces
ESC peuvent **désynchroniser** (le moteur décroche, fait un bruit de grésillement, perd le
couple). C'est LE risque de cette étape. D'où la marge de +0.01, et la vérification en vol à
l'étape 4. Si en vol tu entends un moteur grésiller ou que le drone a un à-coup : remonte
`MOT_SPIN_MIN` de 0.01 et refais.

**Critère de réussite :** les 4 moteurs démarrent et tournent proprement, `MOT_SPIN_MIN`
nettement en dessous de 0.15.

---

## Étape 3 — `MOT_THST_EXPO`

**Pourquoi.** Ce paramètre décrit la forme de la courbe « commande → poussée » de ton
ensemble moteur+hélice. La valeur par défaut 0.65 correspond à de grosses hélices. Les
petites hélices sont plus linéaires.

**Comment.** Si l'étape 1 (Initial Tune Parameters) l'a déjà posé, ne touche à rien. Sinon :
`MOT_THST_EXPO = 0.55`.

---

## Étape 4 — VOL DE MESURE #1 (Stabilize, 60 s)

**Pourquoi.** On ne peut pas calculer `MOT_SPIN_MAX` et `MOT_THST_HOVER` sans savoir à
combien de µs le drone plane **avec les nouveaux réglages**. Ce vol ne sert qu'à mesurer.

**Avant de décoller :**
- Hélices remontées, **les 8 vis**.
- **Efface la puce de log** (MP → `DataFlash Logs` → `Erase`). 8 Mo, ça se remplit vite.
- Mode **Stabilize uniquement**. Pas d'AltHold — il est encore cassé à ce stade.
- ⚠ **Le manche des gaz au hover va bouger.** Avant : 6,5 % de course. Après les étapes 2-3 :
  autour de **20 %**. Ne sois pas surpris, monte doucement.
- Zone dégagée, herbe, pas de vent, pouce sur le kill (voie 8).

**Le vol.** Décollage doux, **stationnaire à 2-3 m pendant 45-60 s, manches le plus au centre
possible**. C'est tout. Plus tu bouges les manches, moins la mesure est propre. Puis
atterrissage, disarm.

**Après.** Télécharge le log, puis :

```bash
tools/thrust_range.py "/mnt/c/Users/victo/Documents/Mission Planner/logs/QUADROTOR/1/<log>.bin"
```

**Ce que tu regardes dans la sortie :**
- `POUSSÉE DE HOVER RÉELLE` → c'est LE chiffre. Il devrait avoir grimpé de 0.052 à ~0.10-0.15.
- `mélangeur saturé (LIMIT)` → il devrait avoir baissé, sans forcément être bon encore.
- `pour ramener le hover à 0.25` → le script te donne directement le `MOT_SPIN_MAX` à poser.

---

## Étape 5 — `MOT_SPIN_MAX` et `MOT_THST_HOVER`

**Pourquoi `MOT_SPIN_MAX`.** Même avec `MOT_SPIN_MIN` corrigé, ton hover sera sans doute
encore autour de 0.10-0.15 — mieux, mais pas idéal, et surtout **toujours sous le plancher
dur de 0.125** du firmware pour `MOT_THST_HOVER`. Baisser le plafond remonte mécaniquement le
hover sur l'échelle.

**Ce que ça coûte.** Tu plafonnes la sortie moteur (attendu : ~1570-1600 µs au lieu de 1950).
Tu perds de la poussée max. **Tu t'en fiches** : il te restera un rapport poussée/poids
d'environ 4, alors qu'ArduPilot est content à partir de 2. Tu voles avec un excès absurde
aujourd'hui — c'est justement le problème.

**Comment.**
1. Pose le `MOT_SPIN_MAX` que `thrust_range.py` a calculé (attendu ~0.57-0.60).
2. Pose `MOT_THST_HOVER = 0.25`.
3. Vérifie que `MOT_HOVER_LEARN = 2` (il est déjà à 2). À partir de maintenant, le firmware
   pourra affiner cette valeur tout seul — **mais seulement en AltHold**, jamais en
   Stabilize (`Copter::update_throttle_hover()` sort tôt si le mode est à gaz manuels).
   C'était exactement le cercle vicieux : AltHold cassé → jamais d'apprentissage → AltHold
   reste cassé.

---

## Étape 6 — VOL DE VALIDATION #2 (Stabilize, 60 s)

Même protocole que l'étape 4 (chip effacée, Stabilize, stationnaire 45-60 s).

⚠ **Le manche des gaz au hover va ENCORE bouger** : cette fois il doit se retrouver **autour
de 50 %**, c'est-à-dire au milieu. C'est le signe que la configuration est saine.

**Critères de réussite — c'est le moment de vérité :**

| indicateur | verdict |
|---|---|
| `POUSSÉE DE HOVER RÉELLE` ≈ 0.20-0.30 | ✅ c'est gagné |
| `LIMIT` < 10 % | ✅ le mélangeur respire |
| manche des gaz au hover ≈ 50 % | ✅ |
| dérive nettement réduite, drone qui « tient » mieux | ✅ les intégrateurs travaillent enfin |

Si `LIMIT` est toujours > 50 %, ne passe pas à la suite : reviens à l'étape 5 et baisse
encore `MOT_SPIN_MAX`.

**Sur le wobble :** il peut avoir disparu, diminué, ou changé de nature. Les trois sont des
informations. Ne conclus rien avant l'étape 9.

---

## Étape 7 — La mousse sur le baromètre (10 min, banc)

**Pourquoi.** Mesure faite sur 5 de tes logs, drone au sol, altitude vraie constante :
dès que les hélices tournent au ralenti, **le baro indique 0,86 à 1,68 m de moins**. Il n'est
pas bruité (0,12 m de bruit statique), il est **biaisé par le débit d'air**. Ce n'était pas
la cause de l'envolée AltHold, mais ça rendra AltHold instable une fois le reste corrigé.

**Comment.** Un petit morceau de **mousse à cellules ouvertes** (mousse noire de packaging
électronique, ou de la mousse de filtre) posé sur le DPS310 de la FC. Il faut que l'air
puisse passer lentement (donc *cellules ouvertes*, pas de la mousse étanche) mais que les
rafales soient amorties. Ne bouche pas hermétiquement.

---

## Étape 8 — Premier AltHold (le mode qui t'a fait tomber)

**Pourquoi maintenant.** Avec `MOT_THST_HOVER` correct, la cause de l'envolée (feed-forward
de gaz faux d'un facteur 7) a disparu. Mais il faut le vérifier **prudemment**.

**Protocole.**
1. Décolle en **Stabilize**, stabilise-toi en stationnaire à **au moins 5 m** (de la marge
   sous toi si ça descend, du temps si ça monte).
2. **Manche des gaz au milieu**, drone stable, puis bascule en AltHold.
3. **Le doigt sur l'interrupteur de mode, prêt à revenir en Stabilize instantanément.**
   Le kill (voie 8) en dernier recours seulement.
4. Ce que tu dois voir : le drone **tient son altitude**. Pas de montée, pas de descente.

**Si ça remonte encore** : retour Stabilize immédiat, atterris, et relance `thrust_range.py`
sur ce log — `MOT_THST_HOVER` est encore trop haut par rapport à la réalité.

**Si ça tient** : reste 30-60 s en AltHold, manche au centre. `MOT_HOVER_LEARN` va affiner
`MOT_THST_HOVER` tout seul pendant ce temps. Atterris, et vérifie dans les paramètres que
`MOT_THST_HOVER` a bougé — c'est la preuve que la boucle d'apprentissage est enfin fermée.

---

## Étape 9 — Requalifier le wobble résiduel (seulement s'il en reste)

**Pourquoi.** Le fameux « 0,7 Hz » du diagnostic était un **artefact d'échantillonnage** :
tes messages ATT/RATE sont loggés à 10 Hz, donc tout ce qui dépasse 5 Hz est replié
(aliasing) et apparaît à une fausse fréquence. Il reste une vraie oscillation roulis haute
fréquence (gyro roulis à 118 °/s de valeur efficace contre 38 pour le tangage), mais **on ne
peut pas la mesurer avec ce réglage de log**.

**⚠ Le piège que j'ai vérifié dans le source :** activer `ATTITUDE_FAST` fait passer ATT,
RATE et les PID à la cadence de boucle (400 Hz) — **et** déplace le log de l'EKF de 10 Hz à
25 Hz (`Copter::twentyfive_hz_logging`). Les messages `XKF*` ne sont pilotés par **aucun**
bit de `LOG_BITMASK` : tu ne peux pas les couper. Résultat : environ **115 ko/s**, soit
**~70 secondes de vol** avant de remplir les 8 Mo.

**Donc :** ne fais ça que pour ce vol-là, et fais-le court.

**Réglages, juste pour ce vol :**
```
LOG_BITMASK = 180223        (= 180222 + 1, le bit 0 = ATTITUDE_FAST)
```
Et pour une vraie analyse fréquentielle des vibrations (échantillonneur par lots, ~1 kHz brut) :
```
INS_LOG_BAT_MASK = 1        (IMU n°1)
INS_LOG_BAT_CNT  = 1024     (déjà à 1024)
INS_LOG_BAT_OPT  = 0        (post-filtre ; mettre 2 pour comparer pré/post)
```

**Le vol :** chip effacée, arme **juste avant** de décoller, stationnaire 45 s, atterris,
disarm tout de suite. Ne laisse pas le drone armé au sol.

**Ensuite** on relit le log ensemble : cette fois la FFT sera valide, et on saura si le
résidu est une résonance mécanique (batterie sur son strap, plots de la FC, condensateur qui
pendouille) ou du bruit amplifié par le terme D. Selon le cas → filtre notch `INS_HNTCH`.

---

## Étape 10 — Autotune

**Seulement quand** : `LIMIT` < 10 %, AltHold tient l'altitude, pas d'oscillation franche.

Rappel de la procédure, avec ce qui avait échoué la dernière fois :
- `RC7_OPTION = 17` (Autotune sur la voie 7), `AUTOTUNE_AXES = 7` (roulis + tangage + lacet ;
  il est actuellement à 3 = roulis+tangage seulement).
- Décollage **en AltHold**, 3-4 m, zone dégagée, air calme.
- Bascule voie 7 → le drone fait des à-coups automatiques pendant 5-10 min.
- **Atterris en gardant la voie 7 active** pour que le tune soit sauvegardé.
- Pouce prêt à repasser en Stabilize.

La dernière fois Autotune refusait de démarrer (« init failed ») — c'est cohérent avec le
reste : il exige AltHold, qui était inutilisable.

---

## Récapitulatif : l'ordre, et pourquoi

| # | action | pourquoi à cette place |
|---|---|---|
| 0 | sauvegarder les params | pouvoir revenir en arrière |
| 1 | gains conservateurs (MP Initial Tune) | **avant** les moteurs, sinon écrasé ; et le gain réel va être ×8-10 |
| 2 | calibrer `MOT_SPIN_ARM`/`MIN` | le levier principal |
| 3 | `MOT_THST_EXPO` | forme de courbe, cohérent avec 3,5" |
| 4 | **vol de mesure** | on ne peut pas calculer sans mesurer |
| 5 | `MOT_SPIN_MAX` + `MOT_THST_HOVER` | calculés à partir de la mesure |
| 6 | **vol de validation** | vérifier `LIMIT` < 10 % |
| 7 | mousse baro | avant de dépendre du baro |
| 8 | **AltHold** | possible seulement une fois 5 fait |
| 9 | log rapide + FFT | requalifier ce qui reste |
| 10 | **Autotune** | exige AltHold sain |

**La règle générale :** ne passe jamais à l'étape suivante si le critère de réussite de
l'étape en cours n'est pas atteint. C'est exactement ce qui nous a fait tourner en rond
pendant trois jours — on empilait des correctifs sans jamais vérifier qu'un seul avait porté.
