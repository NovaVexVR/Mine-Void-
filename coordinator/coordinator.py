#!/usr/bin/env python3
"""Mine-Void coordinator: owns the goal plan and hands out tasks to bots.

Bots POST /report with their status and get back the task they should be
doing plus the shared facts (base location, ...). The plan is plain data
(goals/*.json), so the goal can be swapped at runtime:

    curl -X POST localhost:8765/goal -d '{"file": "goals/other.json"}'
    curl localhost:8765/state        # full JSON state
    open http://localhost:8765/      # tiny dashboard
"""
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "run", "coordinator-state.json")
LEASE_SECONDS = 90  # a silent bot loses its team-task slot after this long

sys.path.insert(0, os.path.join(ROOT, "bots"))
from minevoid.items import count  # noqa: E402


def has_all(inv, need):
    return all(count(inv, spec) >= n for spec, n in need.items())


class Coordinator:
    def __init__(self, goal_file):
        self.lock = threading.Lock()
        self.bots = {}      # name -> last report (+ "seen", "task")
        self.facts = {}     # shared knowledge, e.g. {"base": [x, y, z]}
        self.done = {}      # task id -> [bot names] (sticky/team completions)
        self.events = []    # recent log lines for the dashboard
        self.load_goal(goal_file)
        if os.path.exists(STATE_FILE):
            saved = json.load(open(STATE_FILE))
            if saved.get("goal") == self.goal["name"]:
                self.facts, self.done = saved["facts"], saved["done"]

    def load_goal(self, path):
        with open(os.path.join(ROOT, path)) as f:
            self.goal = json.load(f)
        self.goal_file = path
        self.log(f"goal loaded: {self.goal['name']}")

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        self.events = (self.events + [line])[-200:]

    def save(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"goal": self.goal["name"], "facts": self.facts,
                       "done": self.done}, f)

    # -- team view ---------------------------------------------------------
    def active(self):
        now = time.time()
        return {n: b for n, b in self.bots.items()
                if now - b["seen"] < LEASE_SECONDS}

    def team_count(self, spec):
        return sum(count(b.get("inventory", {}), spec)
                   for b in self.active().values())

    # -- task selection ----------------------------------------------------
    def satisfied(self, task, name, inv):
        if "requires" in task and not has_all(inv, task["requires"]):
            return True  # not applicable to this bot right now
        if "fact" in task and task["fact"] in self.facts:
            return True
        if "until_fact" in task and task["until_fact"] in self.facts:
            return True
        if "skip_if_have" in task and has_all(inv, task["skip_if_have"]):
            return True
        if "team_have" in task:  # amounts are per bot, summed over the team
            size = max(1, len(self.active()))
            return all(self.team_count(spec) >= n * size
                       for spec, n in task["team_have"].items())
        if task.get("repeat"):
            return False  # applies whenever its `requires` holds
        marked = self.done.get(task["id"], [])
        if task["scope"] == "team":
            return bool(marked) and "fact" not in task and "until_fact" not in task
        if "have" in task:
            if has_all(inv, task["have"]):
                return True
            return task.get("sticky", False) and name in marked
        if task["skill"] == "gather":
            a = task["args"]
            return count(inv, a["item"]) >= a["count"]
        return name in marked

    def teamwork(self, name, inv, pos, night):
        """Jobs that come from teammates rather than the plan: help someone
        in trouble, hand over spare items, or wait for a hand-over."""
        now, team = time.time(), self.active()
        dist = lambda other: math.dist(pos, other["pos"]) \
            if pos and other.get("pos") else 1e9
        for other, b in team.items():
            if other == name:
                continue
            tid = f"assist:{other}"
            if b.get("danger") and dist(b) < 96 and \
                    not [h for h in self.holders(tid, now) if h != name]:
                return {"id": tid, "skill": "assist", "args": {"to": other},
                        "phase": "team"}
            # Someone is on their way to us with a delivery: stay put.
            t = b.get("task", {})
            if t.get("skill") == "deliver" and t["args"]["to"] == name \
                    and dist(b) < 48:
                return {"id": f"receive:{other}", "skill": "idle", "args": {},
                        "phase": "team"}
        if night:
            return None
        for rule in self.goal.get("sharing", []):
            item, give = rule["item"], rule["give"]
            floor = rule.get("keep", 0)
            if "keep_unless_have" in rule and count(inv, rule["keep_unless_have"]):
                floor = 0
            if count(inv, item) - give < floor:
                continue
            for other, b in sorted(team.items(), key=lambda kv: dist(kv[1])):
                oinv = b.get("inventory", {})
                if (other == name or dist(b) > 400 or b.get("health", 0) <= 0
                        or count(oinv, item) >= rule["low"]
                        or ("skip_if_have" in rule
                            and count(oinv, rule["skip_if_have"]))):
                    continue
                tid = f"deliver:{item}:{other}"
                if [h for h in self.holders(tid, now) if h != name]:
                    continue
                return {"id": tid, "skill": "deliver", "surface": True,
                        "args": {"to": other, "item": item, "count": give},
                        "phase": "team"}
        return None

    def holders(self, task_id, now):
        return [n for n, b in self.bots.items()
                if b.get("task", {}).get("id") == task_id
                and now - b["seen"] < LEASE_SECONDS]

    def pick(self, name, inv, night=False, pos=None):
        """First unfinished task this bot may take. At night, underground
        work from later phases is preferred over surface tasks; if only
        surface work is left the bot gets it anyway and shelters."""
        now = time.time()
        first_surface = self.teamwork(name, inv, pos, night)
        if first_surface:
            return first_surface
        for phase in self.goal["phases"]:
            for task in phase["tasks"]:
                if self.satisfied(task, name, inv):
                    continue
                if task["scope"] == "team":
                    others = [h for h in self.holders(task["id"], now) if h != name]
                    if len(others) >= task.get("slots", 1):
                        continue
                task = dict(task, phase=phase["id"])
                if "count_per_bot" in task:
                    task["args"] = dict(task["args"], count=task["count_per_bot"]
                                        * max(1, len(self.active())))
                if night and task.get("surface"):
                    first_surface = first_surface or task
                    continue
                return task
        return first_surface or dict(self.goal["fallback"], phase="fallback")

    # -- endpoints ---------------------------------------------------------
    def report(self, r):
        with self.lock:
            name = r["name"]
            prev = self.bots.get(name, {})
            for k, v in (r.get("facts") or {}).items():
                self.facts[k] = v
                self.log(f"{name} set fact {k}={v}")
            if r.get("done"):
                self.done.setdefault(r["done"], [])
                if name not in self.done[r["done"]]:
                    self.done[r["done"]].append(name)
                self.log(f"{name} finished {r['done']}")
            if r.get("event"):
                self.log(f"{name}: {r['event']}")
            task = self.pick(name, r.get("inventory", {}), r.get("night", False),
                             r.get("pos"))
            if task["id"] != prev.get("task", {}).get("id"):
                self.log(f"{name} -> {task['phase']}/{task['id']}")
            self.bots[name] = dict(r, seen=time.time(), task=task)
            if r.get("facts") or r.get("done"):
                self.save()
            return {"task": task, "facts": self.facts,
                    "team": {n: b.get("pos") for n, b in self.active().items()}}

    def state(self):
        with self.lock:
            now = time.time()
            return {"goal": self.goal["name"], "facts": self.facts,
                    "done": self.done, "events": self.events[-40:],
                    "bots": {n: {"task": b["task"]["phase"] + "/" + b["task"]["id"],
                                 "age": round(now - b["seen"], 1),
                                 **{k: b.get(k) for k in
                                    ("pos", "health", "food", "status", "inventory")}}
                             for n, b in self.bots.items()}}


PAGE = """<!doctype html><title>Mine-Void</title><meta http-equiv=refresh content=5>
<body style="font:14px monospace;background:#111;color:#ddd"><h2>Mine-Void: %s</h2>
<pre>%s</pre>"""


def make_handler(co):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            st = co.state()
            if self.path == "/state":
                return self.send(st)
            lines = [f"facts: {st['facts']}", ""]
            for n, b in sorted(st["bots"].items()):
                lines.append(f"{n:8} {b['task']:28} hp={b['health']} food={b['food']}"
                             f" pos={b['pos']} [{b['status']}] ({b['age']}s ago)")
                lines.append(f"         {b['inventory']}")
            lines += [""] + st["events"]
            self.send((PAGE % (st["goal"], "\n".join(lines))).encode(), "text/html")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/report":
                return self.send(co.report(body))
            if self.path == "/goal":
                with co.lock:
                    co.load_goal(body["file"])
                    co.done = {}
                return self.send({"ok": True})
            self.send_error(404)
    return Handler


if __name__ == "__main__":
    goal = sys.argv[1] if len(sys.argv) > 1 else "goals/beat_the_game.json"
    port = int(os.environ.get("COORD_PORT", "8765"))
    co = Coordinator(goal)
    # Local only by default; set COORD_HOST when bots run on other machines.
    host = os.environ.get("COORD_HOST", "127.0.0.1")
    co.log(f"listening on {host}:{port}")
    ThreadingHTTPServer((host, port), make_handler(co)).serve_forever()
