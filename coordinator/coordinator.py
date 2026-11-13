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
        self.done_at = {}   # task id -> time last finished (for cooldowns)
        self.failed_at = {}  # (bot, task id) -> time it last failed
        self.animals = {}   # (kind, x//16, z//16) -> [kind, x, y, z, seen_at]
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

    # -- equipment kit -----------------------------------------------------
    # The plan's "kit" lists gear every bot should end up with, in priority
    # order, with its cost in the kit material (iron). Raw ore counts as
    # material too: it only needs smelting.
    def kit_missing(self, inv, upto=None):
        kit = self.goal.get("kit", {}).get("items", [])
        return [(item, cost) for item, cost in kit[:upto]
                if count(inv, item) < 1]

    def kit_held(self, inv, raw=True):
        k = self.goal["kit"]
        return count(inv, k["material"] + ("|" + k["raw"] if raw else ""))

    def kit_need(self, inv, upto=None):
        """Material still needed for the first `upto` kit items (negative
        means spare material)."""
        return sum(c for _, c in self.kit_missing(inv, upto)) - self.kit_held(inv)

    def storage(self):
        return self.facts.get("storage") or {}

    def team_kit_need(self):
        return sum(self.kit_need(b.get("inventory", {}))
                   for b in self.active().values()) - self.kit_held(self.storage())

    def kit_task(self, task, inv):
        """Fill in kit tasks; None if there is nothing to do for this bot."""
        if task.get("kit_mine"):
            need = self.team_kit_need()
            if need <= 0:
                return None
            have = count(inv, task["args"]["item"])
            return dict(task, args=dict(task["args"],
                                        count=have + min(need, task.get("trip", 10))))
        if task.get("kit_craft"):
            missing = self.kit_missing(inv)
            if not missing:
                return None
            item, cost = missing[0]
            held = self.kit_held(inv, raw=False)
            if held >= cost:
                return dict(task, id=f"craft_{item}", args={"item": item})
            stored = count(self.storage(), self.goal["kit"]["material"])
            if held + stored >= cost and "base" in self.facts:
                return dict(task, id=f"craft_{item}", skill="craft_from_storage",
                            at_base=True,
                            args={"item": item, "material": self.goal["kit"]["material"],
                                  "cost": cost})
            return None
        return task

    def kit_delivery(self, name, inv, team, dist):
        """Hand spare kit material to whoever needs it most, one tier at a
        time: everybody gets a pickaxe before anybody gets a chestplate."""
        kit = self.goal.get("kit")
        if not kit:
            return None
        for tier in range(1, len(kit["items"]) + 1):
            needy = {n: self.kit_need(b.get("inventory", {}), tier)
                     for n, b in team.items()
                     if n != name and b.get("health", 0) > 0}
            needy = {n: v for n, v in needy.items() if v > 0}
            if not needy:
                continue
            spare = min(-self.kit_need(inv, tier), self.kit_held(inv))
            if spare <= 0:
                return None  # someone needs this tier, but not from us
            for other in sorted(needy, key=lambda n: dist(team[n])):
                if dist(team[other]) > 400:
                    continue
                # Worth a trip: cover this tier and the next one if we can,
                # and never walk over for less than a few ingots.
                oinv = team[other].get("inventory", {})
                want = self.kit_need(oinv, min(tier + 1, len(kit["items"])))
                give = min(spare, want)
                if give < min(3, needy[other]):
                    continue
                spec = kit["material"] + "|" + kit["raw"]
                return {"id": f"deliver:kit:{other}", "skill": "deliver",
                        "surface": True, "phase": "team",
                        "args": {"to": other, "item": spec, "count": give}}
            return None
        return None

    # -- task selection ----------------------------------------------------
    FAIL_BACKOFF = 300  # seconds a bot leaves a task alone after failing it

    def satisfied(self, task, name, inv):
        if time.time() - self.failed_at.get((name, task["id"]), 0) < self.FAIL_BACKOFF:
            return True  # failed just now: do something else for a while
        if "requires" in task and not has_all(inv, task["requires"]):
            return True  # not applicable to this bot right now
        if "needs_fact" in task and task["needs_fact"] not in self.facts:
            return True  # not possible yet
        if "storage_has" in task and not has_all(self.storage(), task["storage_has"]):
            return True
        if "requires_any" in task and \
                not any(has_all(inv, alt) for alt in task["requires_any"]):
            return True
        if "cooldown" in task and \
                time.time() - self.done_at.get(task["id"], 0) < task["cooldown"]:
            return True
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
        if task.get("repeat") or task.get("kit_mine") or task.get("kit_craft") \
                or "cooldown" in task:
            return False  # decided by `requires` / kit_task()
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
                    and dist(b) < 48 and str(b.get("status", "")).startswith("delivering"):
                return {"id": f"receive:{other}", "skill": "receive",
                        "args": {"frm": other}, "phase": "team"}
        if night:
            return None
        # Kit material is shared through the base chest now (it survives
        # deaths); walking it over by hand cost lives.
        handoff = None if "storage" in self.facts else \
            self.kit_delivery(name, inv, team, dist)
        if handoff and not [h for h in self.holders(handoff["id"], now)
                            if h != name]:
            return handoff
        for rule in self.goal.get("sharing", []):
            item, give = rule["item"], rule["give"]
            floor = rule.get("keep", 0)
            if "giver_done" in rule and \
                    name not in self.done.get(rule["giver_done"], []):
                continue
            if "keep_unless_have" in rule and count(inv, rule["keep_unless_have"]):
                floor = 0
            give = min(give, count(inv, item) - floor)
            if give < rule.get("min_give", rule["give"]):
                continue
            for other, b in sorted(team.items(), key=lambda kv: dist(kv[1])):
                oinv = b.get("inventory", {})
                if (other == name or dist(b) > rule.get("max_dist", 100)
                        or b.get("health", 0) <= 0
                        or count(oinv, rule.get("need_spec", item)) >= rule["low"]
                        or ("max_food" in rule and b.get("food", 20) > rule["max_food"])
                        or ("to_not_done" in rule
                            and other in self.done.get(rule["to_not_done"], []))
                        or ("skip_if_have" in rule
                            and count(oinv, rule["skip_if_have"]))):
                    continue
                tid = f"deliver:{item}:{other}"
                if [h for h in self.holders(tid, now) if h != name]:
                    continue
                if now - self.done_at.get(f"{name}>{tid}", 0) < 120:
                    continue  # just did this one; let it land first
                return {"id": tid, "skill": "deliver", "surface": True,
                        "args": {"to": other, "item": item, "count": give},
                        "phase": "team"}
        return None

    def holders(self, task_id, now):
        return [n for n, b in self.bots.items()
                if b.get("task", {}).get("id") == task_id
                and now - b["seen"] < LEASE_SECONDS]

    def keep(self, name, task, inv, report):
        """A bot keeps working on its plan task until it finishes or fails,
        instead of being reshuffled whenever priorities move (that sent bots
        back and forth across the map). Urgent team jobs still preempt."""
        # Only finishing/failing *this* task ends it; the bot's own detours
        # (shelter at night, digging in, recovering gear) do not.
        ended = report.get("done") == task.get("id") if task else False
        failed = (report.get("event") or "").startswith(f"{task.get('id')} ") \
            if task else False
        if not task or ended or failed \
                or task.get("phase") in ("team", "fallback", None) \
                or task.get("kit_craft"):
            return None
        urgent = self.teamwork(name, inv, report.get("pos"),
                               report.get("night", False))
        if urgent:
            return None  # helping/supplying a teammate comes first
        for p in self.goal["phases"]:
            for t in p["tasks"]:
                if t.get("preempt") and t["id"] != task["id"] \
                        and not self.satisfied(t, name, inv):
                    return None  # something more important came up
        plan = next((t for p in self.goal["phases"] for t in p["tasks"]
                     if t["id"] == task["id"]), None)
        if plan is None or self.satisfied(plan, name, inv):
            return None
        if plan["scope"] == "team":
            others = sorted(h for h in self.holders(plan["id"], time.time())
                            if h != name)
            if len(others) >= plan.get("slots", 1) and others[0] < name:
                return None  # over-subscribed (e.g. after a restart)
        if plan.get("kit_mine") and "count" not in task.get("args", {}):
            return self.kit_task(dict(task), inv)  # restored without its count
        return task

    def pick(self, name, inv, night=False, pos=None):
        """First unfinished task this bot may take. At night, underground
        work from later phases is preferred over surface tasks; if only
        surface work is left the bot gets it anyway and shelters."""
        now = time.time()
        first_surface = self.teamwork(name, inv, pos, night)
        if first_surface:
            return first_surface
        phases = self.goal["phases"]
        ordered = [(p, t) for p in phases for t in p["tasks"] if t.get("preempt")] + \
                  [(p, t) for p in phases for t in p["tasks"] if not t.get("preempt")]
        for phase, task in ordered:
            if self.satisfied(task, name, inv):
                continue
            if task["scope"] == "team":
                others = [h for h in self.holders(task["id"], now) if h != name]
                if len(others) >= task.get("slots", 1):
                    continue
            task = self.kit_task(dict(task, phase=phase["id"]), inv)
            if task is None:
                continue
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
            for tid in r.get("undone") or []:
                if name in self.done.get(tid, []):
                    self.done[tid].remove(name)
                self.facts.pop(f"{tid}_{name}", None)
                self.log(f"{name} lost {tid}; will redo it")
                self.save()
            if r.get("done"):
                self.done_at[r["done"]] = time.time()
                self.done_at[f"{name}>{r['done']}"] = time.time()
                self.done.setdefault(r["done"], [])
                if name not in self.done[r["done"]]:
                    self.done[r["done"]].append(name)
                self.log(f"{name} finished {r['done']}")
            if r.get("event"):
                self.log(f"{name}: {r['event']}")
                if r["event"].endswith(" failed"):
                    self.failed_at[(name, r["event"][:-len(" failed")])] = time.time()
            now = time.time()
            for kind, x, y, z in r.get("animals") or []:
                self.animals[(kind, x // 16, z // 16)] = [kind, x, y, z, now]
            self.animals = {k: v for k, v in self.animals.items()
                            if now - v[4] < 600}
            # Count this bot as part of the team before deciding its task.
            self.bots[name] = dict(r, seen=time.time(), task=prev.get("task", {}))
            inv = r.get("inventory", {})
            current = prev.get("task")
            if not current and r.get("task_id"):
                # Coordinator restarted: carry on with what the bot says it
                # was doing, so roles are not reshuffled.
                current = next((dict(t, phase=p["id"]) for p in self.goal["phases"]
                                for t in p["tasks"] if t["id"] == r["task_id"]), None)
            task = self.keep(name, current, inv, r) or \
                self.pick(name, inv, r.get("night", False), r.get("pos"))
            if task["id"] != prev.get("task", {}).get("id"):
                self.log(f"{name} -> {task['phase']}/{task['id']}")
            self.bots[name] = dict(r, seen=time.time(), task=task)
            if r.get("facts") or r.get("done"):
                self.save()
            return {"task": task, "facts": self.facts,
                    "animals": [v[:4] for v in self.animals.values()],
                    "team": {n: b.get("pos") for n, b in self.active().items()}}

    def state(self):
        with self.lock:
            now = time.time()
            return {"goal": self.goal["name"], "facts": self.facts,
                    "team_kit_need": self.team_kit_need() if "kit" in self.goal else None,
                    "animal_sightings": len(self.animals),
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
