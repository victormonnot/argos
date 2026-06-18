#!/usr/bin/env bash
# Lance la simu 3D complète : Gazebo Harmonic (physique + rendu) <-> ArduCopter SITL.
#
# C'est le "vrai drone en simu" : le firmware ArduPilot tourne pour de vrai, et toute
# la physique (IMU, GPS, moteurs, portance) vient de Gazebo via l'interface FDM JSON.
#   - SITL envoie les sorties servos    -> Gazebo 127.0.0.1:9002
#   - Gazebo renvoie l'etat capteurs    -> SITL (repond a l'adresse source)
#   - MAVLink est expose sur TCP 5760    -> console.py / QGC / pymavlink s'y connectent
#
# Ce qui a debloque le handshake (cf docs/journal.md 2026-06-19) :
#   1. cote SITL : "--model JSON:127.0.0.1"  (PAS "--model gazebo-iris", qui est invalide)
#   2. le plugin ArduPilotPlugin actif est dans models/iris_with_gimbal/model.sdf
#      (le monde iris_runway.sdf charge iris_with_gimbal, pas iris_with_ardupilot)
#   3. en headless "gz sim -s -r" STEP bien la physique (le diag "ca ne step pas" etait faux)
#
# Usage :
#   ./sitl/run_gazebo.sh            # lance tout, MAVLink sur tcp:127.0.0.1:5760
#   GUI=1 ./sitl/run_gazebo.sh      # avec la fenetre Gazebo (sur le PC fixe, display dispo)
#   Ctrl-C                          # arrete proprement Gazebo + SITL
#
# Apres lancement, dans un autre terminal :
#   - console.py se connecte tout seul a tcp:127.0.0.1:5760 (Mode B)
#   - pour QGC sur le Mac :  mavproxy.py --master tcp:127.0.0.1:5760 --out udp:<MAC>:14550
set -euo pipefail

ARDUPILOT_DIR="${ARDUPILOT_DIR:-$HOME/argos-project/ardupilot}"
GAZEBO_DIR="${GAZEBO_DIR:-$HOME/argos-project/ardupilot_gazebo}"
WORLD="${WORLD:-iris_runway.sdf}"

# --- Gazebo : ou trouver modeles, mondes et le plugin compile ---
export GZ_SIM_RESOURCE_PATH="$GAZEBO_DIR/models:$GAZEBO_DIR/worlds:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$GAZEBO_DIR/build:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"
# --- WSL : rendu GPU headless via D3D12 (sinon llvmpipe logiciel, trop lent) ---
export GALLIUM_DRIVER="${GALLIUM_DRIVER:-d3d12}"
export MESA_LOADER_DRIVER_OVERRIDE="${MESA_LOADER_DRIVER_OVERRIDE:-d3d12}"

GZ_LOG=/tmp/argos_gz.log
SITL_LOG=/tmp/argos_sitl.log

cleanup() {
  echo; echo "[run_gazebo] arret..."
  [[ -n "${SITL_PID:-}" ]] && kill "$SITL_PID" 2>/dev/null || true
  [[ -n "${GZ_PID:-}"   ]] && kill "$GZ_PID"   2>/dev/null || true
  pkill -f "[a]rducopter .*JSON:127.0.0.1" 2>/dev/null || true
  pkill -f "[g]z sim .*${WORLD}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[run_gazebo] Gazebo ($WORLD) -> $GZ_LOG"
if [[ "${GUI:-0}" == "1" ]]; then
  gz sim -r -v3 "$WORLD" >"$GZ_LOG" 2>&1 &   # serveur + GUI
else
  gz sim -s -r -v3 "$WORLD" >"$GZ_LOG" 2>&1 & # serveur seul (headless)
fi
GZ_PID=$!

# attendre que le monde publie /stats (physique prete) avant de lancer le SITL
echo -n "[run_gazebo] attente de Gazebo"
for _ in $(seq 1 60); do
  if gz topic -l 2>/dev/null | grep -q "/stats"; then echo " ok"; break; fi
  echo -n "."; sleep 0.5
done

cd "$ARDUPILOT_DIR"
echo "[run_gazebo] ArduCopter SITL (FDM JSON -> Gazebo) -> $SITL_LOG"
build/sitl/bin/arducopter \
  -I0 \
  --model JSON:127.0.0.1 \
  --speedup 1 \
  --defaults Tools/autotest/default_params/copter.parm,Tools/autotest/default_params/gazebo-iris.parm \
  >"$SITL_LOG" 2>&1 &
SITL_PID=$!

cat <<EOF

[run_gazebo] c'est parti.
   Gazebo  PID $GZ_PID   (log: $GZ_LOG)
   SITL    PID $SITL_PID (log: $SITL_LOG)
   MAVLink : tcp:127.0.0.1:5760
   Camera  : gz topic -e -t /world/iris_runway/.../camera/image

   -> lance la console (Mode B) ou QGC dans un autre terminal.
   -> Ctrl-C ici pour tout arreter.
EOF

# rester au premier plan tant que le SITL vit
wait "$SITL_PID"
