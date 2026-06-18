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
#   ./sitl/run_gazebo.sh            # Gazebo + SITL + pont MAVLink vers QGC (Mac) et vers :14551
#   GUI=1 ./sitl/run_gazebo.sh      # avec la fenetre Gazebo (sur le PC fixe, display dispo)
#   MAC_IP="" ./sitl/run_gazebo.sh  # headless pur, sans pont QGC (MAVLink seulement sur tcp:5760)
#   Ctrl-C                          # arrete proprement Gazebo + SITL + mavproxy
#
# Quand le pont QGC est actif (MAC_IP non vide), les clients se branchent ainsi :
#   - QGroundControl sur le Mac : se connecte tout seul (ecoute UDP 14550)
#   - scripts pymavlink / smoke-test : CONN=udp:127.0.0.1:14551
set -euo pipefail

# IP Tailscale du Mac (stable quel que soit le WiFi). Vider pour desactiver le pont QGC.
MAC_IP="${MAC_IP-100.114.183.96}"
ARGOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # racine du repo argos
ARDUPILOT_DIR="${ARDUPILOT_DIR:-$HOME/argos-project/ardupilot}"
GAZEBO_DIR="${GAZEBO_DIR:-$HOME/argos-project/ardupilot_gazebo}"
WORLD="${WORLD:-argos_demo.sdf}"               # monde ARGOS (drone + cibles), cf sitl/gazebo/
export PATH="$HOME/venv-ardupilot/bin:$PATH"   # pour trouver mavproxy.py

# --- Gazebo : mondes/modeles ARGOS d'abord, puis ceux d'ardupilot_gazebo + le plugin ---
export GZ_SIM_RESOURCE_PATH="$ARGOS_DIR/sitl/gazebo/worlds:$ARGOS_DIR/sitl/gazebo/models:$GAZEBO_DIR/models:$GAZEBO_DIR/worlds:${GZ_SIM_RESOURCE_PATH:-}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$GAZEBO_DIR/build:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"
# --- WSL : rendu GPU headless via D3D12 (sinon llvmpipe logiciel, trop lent) ---
export GALLIUM_DRIVER="${GALLIUM_DRIVER:-d3d12}"
export MESA_LOADER_DRIVER_OVERRIDE="${MESA_LOADER_DRIVER_OVERRIDE:-d3d12}"

GZ_LOG=/tmp/argos_gz.log
SITL_LOG=/tmp/argos_sitl.log

cleanup() {
  echo; echo "[run_gazebo] arret..."
  [[ -n "${MP_PID:-}"   ]] && kill "$MP_PID"   2>/dev/null || true
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

# Camera ISR : pitch le gimbal vers l'avant-bas (~ -0.8 rad) pour cadrer le sol ahead.
# Tenu par le JointPositionController -> rigide avec le corps (yaw drone = yaw camera).
GIMBAL_PITCH="${GIMBAL_PITCH:--0.8}"
gz topic -t /gimbal/cmd_pitch -m gz.msgs.Double -p "data: $GIMBAL_PITCH" 2>/dev/null || true

cd "$ARDUPILOT_DIR"
echo "[run_gazebo] ArduCopter SITL (FDM JSON -> Gazebo) -> $SITL_LOG"
build/sitl/bin/arducopter \
  -I0 \
  --model JSON:127.0.0.1 \
  --speedup 1 \
  --defaults Tools/autotest/default_params/copter.parm,Tools/autotest/default_params/gazebo-iris.parm,$GAZEBO_DIR/config/gazebo-iris-gimbal.parm \
  >"$SITL_LOG" 2>&1 &
SITL_PID=$!

# --- pont MAVLink : tcp:5760 (SITL) -> QGC sur le Mac (14550) + scripts locaux (14551) ---
# mavproxy se connectant au master 5760 debloque aussi la boucle FDM du SITL.
MP_LOG=/tmp/argos_mavproxy.log
if [[ -n "$MAC_IP" ]]; then
  echo "[run_gazebo] pont MAVLink -> QGC udp:${MAC_IP}:14550 (+ local :14551) -> $MP_LOG"
  mavproxy.py --master tcp:127.0.0.1:5760 \
    --out "udp:${MAC_IP}:14550" \
    --out udp:127.0.0.1:14551 \
    --daemon >"$MP_LOG" 2>&1 &
  MP_PID=$!
fi

cat <<EOF

[run_gazebo] c'est parti.
   Gazebo  PID $GZ_PID   (log: $GZ_LOG)
   SITL    PID $SITL_PID (log: $SITL_LOG)${MAC_IP:+
   mavproxy PID ${MP_PID:-?} (log: $MP_LOG)}
   MAVLink : tcp:127.0.0.1:5760${MAC_IP:+   |   QGC(Mac): udp:${MAC_IP}:14550   |   scripts: udp:127.0.0.1:14551}
   Camera  : gz topic -e -t /world/iris_runway/model/iris_with_gimbal/.../camera/image

   -> Sur le Mac : ouvre QGroundControl, il se connecte tout seul.
   -> Preuve du vol : CONN=udp:127.0.0.1:14551 ~/venv-ardupilot/bin/python sitl/gazebo_takeoff_test.py
   -> Ctrl-C ici pour tout arreter.
EOF

# rester au premier plan tant que le SITL vit
wait "$SITL_PID"
