"""The bot's main loop: report to the coordinator, run the assigned skill.

While a skill runs, `tick` keeps reporting, eats, fights nearby hostiles,
and interrupts the skill if the coordinator reassigns the bot or night falls
during surface work.
"""
import importlib
import json
import math
import os
import socket
import sys
import time
import traceback
import urllib.request

import minescript as m

from . import craft, game, items, skills
from .game import Game

COORD = os.environ.get("MINEVOID_COORD", "http://127.0.0.1:8765")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Agent:
    def __init__(self):
        self.g = Game()
        self.g.on_tick = self.tick
        self.task = None
        self.assigned = None
        self.facts = {}
        self.interruptible = False
        self.last_report = self.last_fight = self.last_eat = 0
        self.was_dead = False
        self.debug = self.open_debug_socket()

    # -- coordinator -------------------------------------------------------
    def report(self, **extra):
        g = self.g
        body = dict(name=g.name, pos=[round(v) for v in g.pos()],
                    health=round(g.health(), 1), food=g.hunger(),
                    inventory=g.inv(), status=g.status, night=g.is_night(),
                    danger=g.health() < 10 and bool(g.hostiles(8)),
                    task_id=getattr(self, "assigned", None),
                    animals=self.sightings(),
                    **extra)
        req = urllib.request.Request(COORD + "/report", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            reply = json.load(HTTP.open(req, timeout=5))
        except OSError as e:
            g.log("coordinator unreachable:", e)
            return None
        self.last_report = time.time()
        # In place: running skills hold this dict and see teammates move.
        self.facts.clear()
        self.facts.update(reply["facts"], _team=reply["team"],
                          _animals=reply.get("animals", []))
        return reply["task"]

    def sightings(self):
        """Passive animals we can see, shared so hunters know where to go.
        Throttled: entity scans are not free."""
        now = time.time()
        if now - getattr(self, "last_sighting", 0) < 15:
            return []
        self.last_sighting = now
        try:
            seen = m.entities(type=".*\\.(cow|pig|sheep|chicken|rabbit|mooshroom)$",
                              max_distance=64, limit=12)
        except Exception:
            return []
        return [[e.type.rsplit(".", 1)[-1]] + [round(v) for v in e.position]
                for e in seen]

    # -- background duties, called from every g.sleep() --------------------
    def tick(self):
        g, now = self.g, time.time()
        self.serve_debug()
        dead = g.health() <= 0
        if dead and not self.was_dead:
            self.death = (getattr(self, "last_alive_pos", None), time.time())
            self.report(event=f"died at {self.death[0]}")
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
        hp = g.health()
        if not dead:
            self.last_alive_pos = g.block_pos()
        # Health only regenerates at 18+ food, so eat sooner when hurt.
        hungry = g.hunger() <= (17 if hp < 16 else 14)
        edible = g.count(items.CLASSES["food"]) or (
            g.hunger() <= 8 and g.count(lambda i: i == "rotten_flesh"))
        if now - getattr(self, "last_eat", 0) > 6 and hungry and edible \
                and not m.screen_name():
            self.last_eat = now
            g.b("pause")
            try:
                g.eat()
            finally:
                g.b("resume")
        if now - self.last_report > 3:
            task = self.report()
            special = self.task and self.task["skill"] in ("shelter", "dig_in",
                                                           "recover")
            if self.interruptible and task and self.task and not special:
                if task["id"] != self.assigned:
                    raise game.TaskChanged(task["id"])
                if g.is_night() and (self.task.get("surface")
                                     or ("base" in self.facts
                                         and not self.task.get("at_base"))):
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
        mods = (items, game, craft, skills, sys.modules[__name__])
        stamp = max(os.path.getmtime(mod.__file__) for mod in mods)
        if stamp > getattr(self, "code_stamp", stamp):
            for mod in mods:
                importlib.reload(mod)
            self.g.__class__ = game.Game
            self.__class__ = sys.modules[__name__].Agent
            self.g.on_tick = self.tick
            self.g.log("reloaded bot code")
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
        death = getattr(self, "death", None)
        unarmed = g.count(items.CLASSES["sword"]) == 0
        if death and death[0] and time.time() - death[1] < 240 \
                and not g.is_night() and not getattr(self, "recovering", False):
            # Dropped items last five minutes: go and pick our gear back up,
            # once. If that kills us too, the spot is camped: leave it.
            self.death = None
            self.recovering = True
            task = {"id": "recover_gear", "skill": "recover",
                    "args": {"pos": death[0]}}
        elif (g.health() < 8 or unarmed) and (g.hostiles(10) or g.is_night()) \
                and task["skill"] != "shelter":
            # Badly hurt, or empty-handed after a respawn, with mobs about:
            # run for the (sealed) base if it is close, else dig in here.
            base = self.facts.get("base")
            if base and math.dist(g.pos(), base) < 48:
                task = {"id": "shelter", "skill": "shelter", "args": {}}
            else:
                task = {"id": "dig_in", "skill": "dig_in", "args": {}}
        else:
            self.recovering = False
        if task["skill"] in ("recover", "dig_in"):
            pass
        elif g.is_night() and (task.get("surface") or unarmed
                               or ("base" in self.facts and not task.get("at_base"))):
            # With a base, the whole team goes home to sleep: mining at
            # night got bots killed, and sleeping skips the night anyway.
            # Surface work waits for daylight, and so does a bot that just
            # respawned with nothing: it would only feed the zombies.
            task = {"id": "shelter", "skill": "shelter", "args": {}}
        elif g.hunger() <= 6 and g.count(items.CLASSES["food"]) == 0 and not g.is_night():
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
        try:
            conn.sendall("\n".join(out).encode() + b"\n")
        except OSError:
            pass  # the caller gave up waiting
        finally:
            conn.close()
