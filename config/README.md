# Paramètres ArduPilot du drone ARGOS

Sauvegardes de la liste complète de paramètres de la FC (SpeedyBee F405 Mini,
ArduCopter 4.8.0-dev `ArduCopter-ARGOS`, hash `8927564c`).

Fichiers copiés **tels quels** depuis Mission Planner (`Config → Full Parameter List → Save to
file`), sans aucune modification — ils se rechargent directement avec *Load from file*.

| fichier | date | état |
|---|---|---|
| `argos-drone-2026-07-27-avant-corrections.param` | 2026-07-27 | **avant** toute correction — tous les `MOT_*` et `ATC_*` d'usine. Référence historique. |
| `argos-drone-2026-07-29.param` | 2026-07-29 | ✅ **configuration de vol validée** — les deux correctifs appliqués, logging en mode vol |

## Ce que contient la version validée

Les deux correctifs qui ont rendu le drone pilotable (détail complet et preuves dans
[`../docs/diagnostic_complet.md`](../docs/diagnostic_complet.md) §0) :

**FIX 1 — plage de poussée** (gros wobble, dérive, envolée AltHold, manche utilisable à 6 %)

```
MOT_SPIN_ARM     0.03      (defaut 0.10)
MOT_SPIN_MIN     0.05      (defaut 0.15)
MOT_THST_EXPO    0.43      (defaut 0.65)
MOT_THST_HOVER   0.1793    (defaut 0.35)  <- valeur MESUREE
```

**FIX 2 — gain de boucle** (cycle limite de roulis à 15,5 Hz)

```
ATC_RAT_RLL_P = ATC_RAT_RLL_I    0.06      (defaut 0.135)
ATC_RAT_RLL_D                    0.0005    (defaut 0.0036)
ATC_RAT_PIT_P = ATC_RAT_PIT_I    0.06
ATC_RAT_PIT_D                    0.0005
```

Filtres posés par MP *Initial Tune Parameters* (hélice 3,5") : `INS_GYRO_FILTER=101`,
`INS_ACCEL_FILTER=10`, `ATC_RAT_*_FLTD/FLTT=50.5`. Notch désactivé (`INS_HNTCH_ENABLE=0`) —
il déstabilisait la boucle à 8 Hz.

Logging en **mode vol** : `LOG_BITMASK=136954`, `INS_LOG_BAT_MASK=0`.
Pour rediagnostiquer, basculer en **mode diagnostic** : `LOG_BITMASK=136955` +
`INS_LOG_BAT_MASK=1` + `INS_LOG_BAT_OPT=1`, effacer la puce, vol de 60-90 s, télécharger
aussitôt (la puce de 8 Mo se remplit en ~5 min dans ce mode).

## Avertissements avant de recharger

- **Ne jamais retoucher `MOT_THST_HOVER` pour changer le feeling** : c'est une mesure physique
  utilisée en feed-forward par le contrôleur d'altitude. La fausser ramène le problème n°1 —
  c'est exactement ce qui a envoyé le drone au plafond quand elle s'est retrouvée à 0,6864.
  Après tout changement de `MOT_SPIN_MAX`, **re-mesurer** avec `tools/thrust_range.py`.
- **Plafond connu sur cet appareil : `ATC_RAT_*_P ≈ 0,09`.** Au-dessus, le cycle limite à
  15,5 Hz revient. Actuellement à 0,06.
- **⚠ Autotune** : il monte les gains jusqu'à trouver la limite, donc il retrouverait ce cycle
  limite. Rigidifier d'abord le montage de la stack, ou `AUTOTUNE_AGGR` bas, doigt sur
  l'interrupteur.
- `AHRS_TRIM_X/Y` viennent de l'autotrim en vol (`RC7_OPTION=182`). Valeurs propres à cet
  assemblage — à refaire après toute recalibration accéléro ou remontage de la FC.
- `COMPASS_ENABLE=0` (compas défectueux) et pas de GPS monté : vols en Stabilize.
