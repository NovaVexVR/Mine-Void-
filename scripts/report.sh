#!/usr/bin/env bash
# Progress report since a time of day: scripts/report.sh [HH:MM] (default: last 15 min)
# Shows bot status, deaths, GrimAC flags by check and coordinator events.
cd "$(dirname "$0")/.."
SINCE="${1:-$(date -d '15 min ago' +%H:%M)}"
strip() { sed 's/\x1b\[[0-9;]*m//g'; }
after() { awk -v t="$SINCE" '{ s=$0; sub(/^\[/, "", s); if (substr(s,1,5) >= t) print }'; }

scripts/status.sh 0 2>/dev/null | cut -c1-220
echo
echo "== deaths since $SINCE"
grep -v Grim server/server.out | strip | after |
  grep -E ' (was |drowned|died|starved|fell|blew up|hit the ground|burned|suffocated|withered|froze)' |
  grep -v -E 'joined|left' | sed 's/ INFO\]://' | tail -12
echo "== GrimAC flags since $SINCE (check: count)"
grep 'Grim.*failed' server/server.out | strip | after |
  awk '{for(i=1;i<=NF;i++) if($i=="failed"){print $(i-2)" "$(i+1)}}' | sort | uniq -c | sort -rn | head
echo "== coordinator events since $SINCE"
# only this coordinator run (its log is appended across restarts and days)
start=$(grep -n 'goal loaded' run/coordinator.log | tail -1 | cut -d: -f1)
tail -n +"${start:-1}" run/coordinator.log | after |
  grep -v -E ' -> (wood|wooden_tools|stone_tools)/' | tail -25
curl -s localhost:8765/state | python3 -c "
import json,sys; st=json.load(sys.stdin)
print('facts:', st['facts']); print('animal sightings:', st.get('animal_sightings'), ' team kit need:', st.get('team_kit_need'))" 2>/dev/null
