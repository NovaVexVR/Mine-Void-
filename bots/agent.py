"""Mine-Void bot agent. Minescript runs this automatically when the client
joins a world (see `autorun` in client/launch.py).

Loop: report status to the coordinator -> receive a task -> run its skill.
While a skill runs, `tick` keeps reporting, fights nearby hostiles, and
interrupts the skill if the coordinator reassigns us or night falls during
surface work.
"""
import importlib
import json
import os
import socket
import sys
import time
import traceback
import urllib.request

import minescript as m

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minevoid import craft, game, items, skills  # noqa: E402
from minevoid.game import Game                   # noqa: E402

COORD = os.environ.get("MINEVOID_COORD", "http://127.0.0.1:8765")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Agent:
    def __init__(self):
        self.g = Game()
        self.g.on_tick = self.tick
        self.task = None
        self.assigned = None
        self.facts = {}
        self.interruptible = False
        self.last_report = self.last_fight = 0
        self.was_dead = False
        self.debug = self.open_debug_socket()

    # -- coordinator -------------------------------------------------------
    def report(self, **extra):
        g = self.g
        body = dict(name=g.name, pos=[round(v) for v in g.pos()],
                    health=round(g.health(), 1), food=g.hunger(),
                    inventory=g.inv(), status=g.status, night=g.is_night(),
                    **extra)
        req = urllib.request.Request(COORD + "/report", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            reply = json.load(HTTP.open(req, timeout=5))
        except OSError as e:
            g.log("coordinator unreachable:", e)
            return None
        self.last_report = time.time()
        self.facts = reply["facts"]
        return reply["task"]

    # -- background duties, called from every g.sleep() --------------------
    def tick(self):
        g, now = self.g, time.time()
        self.serve_debug()
        dead = g.health() <= 0
        if dead and not self.was_dead:
            self.report(event="died")
        self.was_dead = dead
        if dead:
            return
        if now - self.last_fight > 1 and g.hostiles(4.5):
            # Baritone owns the camera while pathing, so pause it, fight
            # properly, then let it carry on. (Kill Aura helps as well.)
            g.b("pause")
            try:
                g.fight()
            finally:
                g.b("resume")
            self.last_fight = time.time()
        if now - self.last_report > 3:
            task = self.report()
            if self.interruptible and task and self.task:
                if task["id"] != self.assigned:
                    raise game.TaskChanged(task["id"])
                if self.task.get("surface") and g.is_night() \
                        and self.task["skill"] != "shelter":
                    raise game.TaskChanged("night")

    # -- main loop ---------------------------------------------------------
    def run(self):
        g = self.g
        g.setup()
        g.log("agent started")
        while True:
            try:
                self.step()
            except game.TaskChanged as e:
                g.log("interrupted:", e)
                g.stop()
            except Exception:
                err = traceback.format_exc()
                g.log(err)
                g.stop()
                self.report(event="error: " + err.strip().splitlines()[-1])
                time.sleep(5)

    def reload_code(self):
        """Pick up edited skill code without restarting the client."""
        mods = (items, game, craft, skills)
        stamp = max(os.path.getmtime(mod.__file__) for mod in mods)
        if stamp > getattr(self, "code_stamp", stamp):
            for mod in mods:
                importlib.reload(mod)
            self.g.__class__ = game.Game
            self.g.log("reloaded skill code")
        self.code_stamp = stamp

    def step(self):
        g = self.g
        self.reload_code()
        task = self.report()
        if task is None:
            time.sleep(3)
            return
        if g.health() <= 0:
            g.respawn()
            return
        self.assigned = task["id"]  # what the coordinator thinks we do
        if task.get("surface") and g.is_night():
            task = {"id": "shelter", "skill": "shelter", "args": {}}
        elif g.hunger() <= 12 and g.count(items.CLASSES["food"]) == 0 and not g.is_night():
            task = {"id": "emergency_food", "skill": "hunt", "args": {"count": 3},
                    "surface": True}
        self.task = task
        g.status = task["id"]
        self.interruptible = True
        try:
            result = skills.SKILLS[task["skill"]](g, self.facts, **task["args"])
        finally:
            self.interruptible = False
            g.close_screen()
        if result:
            self.report(done=task["id"],
                        facts=result if isinstance(result, dict) else None)
        else:
            self.report(event=f"{task['id']} failed")
            g.sleep(5)

    # -- dev console: scripts/botexec.py <bot> '<python>' ------------------
    def open_debug_socket(self):
        path = os.path.join(ROOT, "run", f"{self.g.name}.sock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(2)
        srv.setblocking(False)
        return srv

    def serve_debug(self):
        try:
            conn, _ = self.debug.accept()
        except BlockingIOError:
            return
        conn.setblocking(True)
        code = b""
        while (chunk := conn.recv(65536)):
            code += chunk
        out = []
        # stdout is Minescript's command channel, so print() is captured.
        env = {"m": m, "g": self.g, "agent": self, "skills": skills,
               "print": lambda *a: out.append(" ".join(map(str, a)))}
        try:
            exec(code.decode(), env)
        except Exception:
            out.append(traceback.format_exc())
        conn.sendall("\n".join(out).encode() + b"\n")
        conn.close()


Agent().run()
