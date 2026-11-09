#!/usr/bin/env bash
# One-screen status: coordinator events + each bot's state.
cd "$(dirname "$0")/.."
curl -s localhost:8765/state | python3 -c "
import json,sys
st=json.load(sys.stdin)
print('goal:',st['goal'],' facts:',st['facts'])
for n,b in sorted(st['bots'].items()):
    print(f\"{n:7} {b['task']:28} hp={b['health']} food={b['food']} pos={b['pos']} [{b['status']}] {b['age']}s\")
    print('        ',b['inventory'])
print(*st['events'][-${1:-12}:],sep='\n')"
