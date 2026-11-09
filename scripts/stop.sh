#!/usr/bin/env bash
# Stop bots, coordinator and (with --server) the Minecraft server.
cd "$(dirname "$0")/.."
for f in client/instances/*/pids; do [ -f "$f" ] && kill $(cat "$f") 2>/dev/null; rm -f "$f"; done
pkill -f coordinator/coordinator.py
if [ "${1:-}" = "--server" ]; then scripts/mc-cmd.sh stop; fi
