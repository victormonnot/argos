# Dialecte MAVLink ARGOS — une source, deux langages (PORTFOLIO §1.3).
#
#   make          génère Python + C
#   make test     banc : collisions d'ID, round-trip d'encodage
#   make clean    efface tout ce qui est généré
#
# Rien de ce qui est produit ici n'est versionné : le dépôt ne contient que
# `argos.xml`. Du code généré committé, c'est deux sources de vérité qui
# divergent en silence le jour où l'une des deux est modifiée à la main.

PY       = ../perception/.venv/bin/python
MAVGEN   = $(PY) ../perception/.venv/bin/mavgen.py
# Les dialectes amont, là où pymavlink les installe. On ne les copie pas dans le
# dépôt : leur version est celle de pymavlink, elle est déjà épinglée par
# requirements.txt.
UPSTREAM = $(shell $(PY) -c "import pymavlink,pathlib;print(pathlib.Path(pymavlink.__file__).parent/'dialects'/'v20')")
BUILD    = .build
GEN      = generated

.PHONY: all stage python c test clean

all: python c
	@echo
	@echo "  Python  -> $(GEN)/python/argos.py"
	@echo "  C       -> $(GEN)/c/argos/"
	@echo "  Meme argos.xml, meme CRC_EXTRA des deux cotes."

# mavgen resout les <include> RELATIVEMENT au fichier qui inclut (mavgen.py,
# expand_includes). Notre argos.xml vit dans le depot, ardupilotmega.xml vit
# dans le venv : ils doivent donc se retrouver dans un meme repertoire le temps
# de la generation. D'ou ce repertoire de montage, jetable.
stage:
	@rm -rf $(BUILD) && mkdir -p $(BUILD)
	@cp $(UPSTREAM)/*.xml $(BUILD)/
	@cp argos.xml $(BUILD)/

python: stage
	@mkdir -p $(GEN)/python          # mavgen ne cree pas l'arborescence de sortie
	$(MAVGEN) --lang=Python3 --wire-protocol=2.0 -o $(GEN)/python/argos.py $(BUILD)/argos.xml

c: stage
	$(MAVGEN) --lang=C --wire-protocol=2.0 -o $(GEN)/c $(BUILD)/argos.xml

test:
	$(PY) test_dialect.py

clean:
	rm -rf $(BUILD) $(GEN)
