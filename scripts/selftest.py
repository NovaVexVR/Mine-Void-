#!/usr/bin/env python3
"""Health check and quick live tests for a running setup: scripts/selftest.py

Checks that the server, the coordinator and every bot are up, then asks each
bot a few harmless questions through its debug socket (nothing in the world
is changed): is the base sealed, is the farm lit and watered, can it read
the recipe book. Exit code 0 when everything passed. Meant to be run by a
person or by Claude watching the bots: `scripts/selftest.py --json` prints a
machine-readable summary.
"""
import json
import os
import socket
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COORD = os.environ.get("MINEVOID_COORD", "http://127.0.0.1:8765")
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def botexec(bot, code, timeout=90):
    path = os.path.join(ROOT, "run", f"{bot}.sock")
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(path)
    s.sendall(code.encode())
    s.shutdown(socket.SHUT_WR)
    out = b""
    while True:
        d = s.recv(65536)
        if not d:
            break
        out += d
    return out.decode().strip()


def pid_alive(path):
    try:
        os.kill(int(open(path).read().split()[0]), 0)
        return True
    except (OSError, ValueError):
        return False


BOT_CHECKS = r'''
import json, math
from minevoid import skills, craft
f = agent.facts
out = {"pos": [round(v) for v in g.pos()], "health": g.health(), "dim": g.dim()}
base = f.get("base")
if base and math.dist(g.pos(), base) < 48 and g.overworld():
    try:
        out["openings"] = len(skills.base_openings(g, base, bool(f.get("farm_room"))))
    except ValueError:
        out["openings"] = "not loaded"
    lit, water = {}, {}
    for w in skills.farms(f):
        centre, torches, soil = skills.farm_room_cells(base, w)
        lit[w] = sum(g.block(*c) in ("torch", "wall_torch") for c in torches)
        water[w] = g.block(*centre) == "water"
    out["farm_torches"], out["farm_water"] = lit, water
out["recipe_book"] = len([k for k in craft._book if k != "_built"]) or len(craft.book_ids(g, "stick"))
print(json.dumps(out))
'''


def main():
    as_json = "--json" in sys.argv
    check("server running", pid_alive(os.path.join(ROOT, "server", "server.pid")))
    try:
        st = json.load(HTTP.open(COORD + "/state", timeout=5))
        check("coordinator answering", True, st.get("goal"))
    except Exception as e:  # noqa: BLE001
        check("coordinator answering", False, e)
        st = {"bots": {}, "facts": {}}
    bots = {n: b for n, b in st.get("bots", {}).items() if n.startswith("Void")}
    check("bots reporting", bots and all(b["age"] < 60 for b in bots.values()),
          ", ".join(f"{n} {b['age']}s ago" for n, b in sorted(bots.items())))
    for n in sorted(bots):
        try:
            info = json.loads(botexec(n, BOT_CHECKS).splitlines()[-1])
        except Exception as e:  # noqa: BLE001
            check(f"{n} answers", False, e)
            continue
        check(f"{n} answers", True, f"at {info['pos']} in {info['dim']}, hp {info['health']}")
        check(f"{n} reads the recipe book", info["recipe_book"] > 0,
              f"{info['recipe_book']} recipes")
        if "openings" in info:
            check(f"base sealed (seen by {n})", info["openings"] == 0, info["openings"])
            for w, t in info["farm_torches"].items():
                check(f"{w} lit (seen by {n})", t >= 4, f"{t}/4 torches")
                check(f"{w} watered (seen by {n})", info["farm_water"][w])
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed  ({time.strftime('%H:%M:%S')})")
    if as_json:
        print(json.dumps({"passed": not failed, "results": results}))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
