#!/usr/bin/env bash
# Lance le SITL ArduCopter, démarrage à Toulouse, avec les deux sorties MAVLink :
#   - :14550 -> QGroundControl sur le Mac
#   - :14551 -> les scripts pymavlink (sitl/mission_basic.py)
#
# Usage :
#   ./sitl/run_sitl.sh                       # Mac à l'IP par défaut ci-dessous
#   MAC_IP=192.168.1.42 ./sitl/run_sitl.sh   # si l'IP du Mac a changé
#   ./sitl/run_sitl.sh -w                     # args en plus passés tels quels (ex: reset params)
set -euo pipefail

MAC_IP="${MAC_IP:-192.168.1.18}"
ARDUPILOT_DIR="${ARDUPILOT_DIR:-$HOME/argos-project/ardupilot}"

# garantit que mavproxy.py (dans le venv) est trouvable par sim_vehicle.py
export PATH="$HOME/venv-ardupilot/bin:$PATH"

cd "$ARDUPILOT_DIR"
exec Tools/autotest/sim_vehicle.py -v ArduCopter \
  --location Toulouse \
  --out "udp:${MAC_IP}:14550" \
  --out udp:127.0.0.1:14551 \
  "$@"
