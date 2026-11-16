#!/usr/bin/env bash
# Live summary of a running setup: scripts/watch.sh [seconds between status lines]
# Deaths, failed jobs, errors and roadmap milestones as they happen, plus a
# one-line status per bot every so often. Ctrl+C to stop watching (the bots
# keep running).
cd "$(dirname "$0")/.."
EVERY="${1:-120}"
tail -n0 -F run/coordinator.log 2>/dev/null |
  grep --line-buffered -v 'mine_shaft=\|mine_branches=' |
  grep --line-buffered -E 'died|failed|error|base_open=[1-9]|set fact (farm_big|farm2_room|tree_farm|exit_lit|mine|deep_mine|portal|stronghold|enchanting_table|end_portal|dragon_dead)' &
for log in client/instances/Void*/logs/latest.log; do
  bot=$(basename "$(dirname "$(dirname "$log")")")
  tail -n0 -F "$log" 2>/dev/null |
    grep --line-buffered -a -E "minevoid:$bot\].*(Traceback|crafted (iron|diamond|shield|bow)|tree farm:|farm room:|shaft:|portal:|nether:|stronghold:|DRAGON)" |
    sed -u "s/^.*\[minevoid:/[/" &
done
while sleep "$EVERY"; do
  curl -s localhost:8765/state | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('--', __import__('time').strftime('%H:%M:%S'))
for n,b in sorted(d['bots'].items()):
    if n.startswith('Void'):
        print(f\"   {n}: {b['task']:28} hp={b['health']} food={b['food']} {b['status']}\")" 2>/dev/null
done &
trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
wait
