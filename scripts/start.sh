#!/usr/bin/env bash
# Bring the whole trial up: server, coordinator, N bots.  scripts/start.sh [N=3] [goal.json]
set -euo pipefail
cd "$(dirname "$0")/.."
N="${1:-3}"; GOAL="${2:-goals/beat_the_game.json}"
mkdir -p run client/instances
[ -f client/cache/launch.json ] || python3 client/install.py
server/start.sh
if ! { [ -f run/coordinator.pid ] && kill -0 "$(cat run/coordinator.pid)" 2>/dev/null; }; then
  nohup python3 coordinator/coordinator.py "$GOAL" >> run/coordinator.log 2>&1 &
  echo $! > run/coordinator.pid
  echo "coordinator started (http://127.0.0.1:8765/)"
fi
for i in $(seq "$N"); do
  name="Void$i"
  if [ -f "client/instances/$name/pids" ] &&
     kill -0 "$(cut -d' ' -f1 "client/instances/$name/pids")" 2>/dev/null; then
    echo "$name already running"; continue
  fi
  scripts/mc-cmd.sh "whitelist add $name"
  python3 client/launch.py "$name"
  sleep 20   # stagger client start-up, it is CPU heavy
done
