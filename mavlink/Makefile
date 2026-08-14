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

.PHONY: all stage python c cxx listen listen-cpp test clean

all: python c cxx listen listen-cpp
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

# C : ce que le FIRMWARE embarque. ArduPilot est en C++ mais inclut ces
# en-tetes-la — pas de STL, pas d'allocation, ca tourne sur un MCU.
c: stage
	$(MAVGEN) --lang=C --wire-protocol=2.0 -o $(GEN)/c $(BUILD)/argos.xml

# C++11 : l'API des couches HAUTES (MAVSDK, MAVROS, une appli companion).
# Classes, namespaces, constantes constexpr, introspection to_yaml().
# Meme XML, autre etage. La sortie C++11 embarque aussi les en-tetes C, parce
# que la machine a etats du parseur reste celle du C.
cxx: stage
	$(MAVGEN) --lang=C++11 --wire-protocol=2.0 -o $(GEN)/cxx $(BUILD)/argos.xml

# Les deux consommateurs. Une seule chose a retenir dans ces regles : -I.
# C'est la SEULE dependance — pas de bibliotheque a installer, pas d'edition de
# liens. MAVLink est entierement en en-tetes, et elles sont generees.
# -Wno-address-of-packed-member : bruit venant des en-tetes AMONT (common.xml
# packe ses structures), pas de notre code. -Wall -Wextra restent pour le notre.
WARN = -Wall -Wextra -Wno-address-of-packed-member

listen: c
	$(CC) -O2 $(WARN) -std=c11 -I$(GEN)/c \
	      -o consumers/argos_listen consumers/argos_listen.c

listen-cpp: cxx
	$(CXX) -O2 $(WARN) -std=c++11 -I$(GEN)/cxx \
	       -o consumers/argos_listen_cpp consumers/argos_listen.cpp

test:
	$(PY) test_dialect.py

clean:
	rm -rf $(BUILD) $(GEN) consumers/argos_listen consumers/argos_listen_cpp
