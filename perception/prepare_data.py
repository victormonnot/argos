"""prepare_data.py — VisDrone -> dataset YOLO remappé en 2 classes (personne / véhicule).

1) Télécharge + convertit VisDrone via Ultralytics (labels YOLO 10 classes).
2) Sauvegarde les labels d'origine UNE fois, puis remap vers 2 classes EN repartant
   toujours de la sauvegarde => idempotent : change REMAP, relance `make data`,
   ça re-remappe SANS re-télécharger.
3) Génère argos_visdrone.yaml (chemin absolu) prêt pour l'entraînement.

~2 Go de download au 1er run. Lance : make data
"""
import shutil
from pathlib import Path

from ultralytics.data.utils import check_det_dataset

# Ordre des classes VisDrone (tel que converti par Ultralytics, 0-indexé) :
#   0 pedestrian  1 people  2 bicycle  3 car  4 van
#   5 truck       6 tricycle  7 awning-tricycle  8 bus  9 motor
# -> 0 = personne, 1 = véhicule, None = boîte supprimée.
# Ambigus : bicycle supprimé par défaut ; passe-le à 1 si tu le veux "véhicule".
REMAP = {
    0: 0,    # pedestrian      -> personne
    1: 0,    # people          -> personne
    3: 1,    # car             -> véhicule
    4: 1,    # van             -> véhicule
    5: 1,    # truck           -> véhicule
    8: 1,    # bus             -> véhicule
    9: 1,    # motor (moto)    -> véhicule
    6: 1,    # tricycle        -> véhicule
    7: 1,    # awning-tricycle -> véhicule
    2: None,  # bicycle        -> supprimé (ambigu)
}

HERE = Path(__file__).resolve().parent


def main():
    print("== Téléchargement + conversion VisDrone (via Ultralytics) ==")
    info = check_det_dataset("VisDrone.yaml", autodownload=True)
    root = Path(info["path"]).resolve()
    print(f"Dataset : {root}")

    labels_dir = root / "labels"
    backup = root / "labels_visdrone10"          # copie pristine 10 classes (1 seule fois)
    if not backup.exists():
        print("== Sauvegarde des labels d'origine (10 classes) ==")
        shutil.copytree(labels_dir, backup)

    # Remap TOUJOURS depuis la sauvegarde -> idempotent + tu peux changer REMAP.
    print("== Remap des labels en 2 classes (depuis la sauvegarde) ==")
    n_files = kept = dropped = 0
    for src in backup.rglob("*.txt"):
        out = []
        for line in src.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split()
            new = REMAP.get(int(parts[0]))
            if new is None:
                dropped += 1
                continue
            parts[0] = str(new)
            out.append(" ".join(parts))
            kept += 1
        dst = labels_dir / src.relative_to(backup)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("\n".join(out) + ("\n" if out else ""))
        n_files += 1
    print(f"  {n_files} fichiers · {kept} boîtes gardées · {dropped} supprimées")

    # yaml d'entraînement, chemin ABSOLU = zéro ambiguïté de résolution.
    yaml_path = HERE / "argos_visdrone.yaml"
    yaml_path.write_text(
        "# Généré par prepare_data.py — VisDrone remappé en 2 classes.\n"
        f"path: {root}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        "  0: personne\n"
        "  1: vehicule\n"
    )
    print(f"Écrit : {yaml_path}")
    print("Terminé. Entraîne avec : make train")


if __name__ == "__main__":
    main()
