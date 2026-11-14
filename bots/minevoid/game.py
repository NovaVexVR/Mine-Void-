"""Thin wrapper over Minescript, Baritone and Meteor for one bot client.

Everything that touches the game goes through here so skills stay readable.
Game objects are reached with Minescript's Java reflection (Mojang names);
Baritone is driven through its public API and Meteor through chat commands.
"""
import math
import time

import minescript as m
from java import JavaClass

HOSTILES = ("zombie", "skeleton", "spider", "creeper", "drowned", "husk", "stray",
            "witch", "slime", "phantom", "pillager", "vindicator", "enderman",
            "cave_spider", "silverfish", "zombie_villager", "bogged", "blaze",
            "wither_skeleton", "piglin_brute", "hoglin", "magma_cube", "ghast")
BLOCK_GROUPS = {
    "logs": "oak_log birch_log spruce_log jungle_log acacia_log dark_oak_log "
            "cherry_log mangrove_log pale_oak_log",
    "stone": "stone cobblestone deepslate cobbled_deepslate",
    "iron": "iron_ore deepslate_iron_ore coal_ore deepslate_coal_ore",
    "coal": "coal_ore deepslate_coal_ore",
    "grass": "short_grass tall_grass",
    "dirt": "dirt grass_block",
}
BARITONE_SETTINGS = {
    # Play like a legit player: the server runs GrimAC and anti-xray, so do
    # not trust ore positions in chunk data and avoid twitchy movement.
    "legitMine": "true",
    "legitMineYLevel": "16",
    "allowParkour": "false",
    "allowSprint": "true",
    "antiCheatCompatibility": "true",
    # Turn the real camera, not just the rotation sent to the server: GrimAC
    # (BadPacketsJ) rejects block placements whose look direction differs
    # from the last reported rotation, which left bots stuck retrying.
    "freeLook": "false",
    # Walk (out through the base door and up the stairs) rather than dig
    # new tunnels: every tunnel out of the base is a way in for mobs.
    "blockBreakAdditionalPenalty": "6",
    # Never mine base fixtures while pathing (Baritone broke the beds).
    "blocksToDisallowBreaking": "crafting_table,furnace,chest,torch,wall_torch,farmland,wheat,white_bed,orange_bed,magenta_bed,light_blue_bed,yellow_bed,lime_bed,pink_bed,gray_bed,light_gray_bed,cyan_bed,purple_bed,blue_bed,brown_bed,green_bed,red_bed,black_bed,oak_door,spruce_door,birch_door,jungle_door,acacia_door,dark_oak_door,mangrove_door,cherry_door,pale_oak_door",
    "allowBreak": "true",
    "allowPlace": "true",
    "rightClickContainerOnArrival": "true",
    "mineScanDroppedItems": "true",
    "avoidance": "true",          # path around mobs
    "chatControl": "true",
    "notificationOnPathComplete": "false",
    "renderPath": "false", "renderGoal": "false",
    "renderSelectionBoxes": "false", "renderCachedChunks": "false",
}
METEOR_MODULES = ("kill-aura", "auto-eat", "auto-respawn", "auto-armor",
                  "auto-weapon", "auto-replenish")


class TaskChanged(Exception):
    """Raised inside a skill when the coordinator reassigns the bot."""


def short(item):
    if not item:  # unloaded chunk
        return "unknown"
    return item.split(":", 1)[-1].split("[", 1)[0]


class Game:
    def __init__(self):
        self.name = m.player_name()
        self.mc = JavaClass("net.minecraft.client.Minecraft").getInstance()
        self.ClickType = JavaClass("net.minecraft.world.inventory.ClickType")
        api = JavaClass("baritone.api.BaritoneAPI")
        self.baritone = api.getProvider().getPrimaryBaritone()
        self.on_tick = None  # set by the agent: heartbeat + survival
        self.status = "starting"
        self.lost = []  # finished tasks whose result has since been lost
        self.pending_facts = {}  # facts learned mid-skill, sent with the next report
        self._click = None

    def setup(self):
        for k, v in BARITONE_SETTINGS.items():
            self.b(f"set {k} {v}")
        self.aura_targets([], force=True)
        # GrimAC cancels hits from beyond vanilla reach (3 blocks), so a
        # longer aura range just wastes swings.
        m.chat(".settings kill-aura range 2.6")
        m.chat(".settings kill-aura walls-range 2.6")
        for mod in METEOR_MODULES:
            m.chat(f".toggle {mod} on")

    def aura_targets(self, extra, force=False):
        """Point Meteor's Kill Aura at monsters (never players), plus `extra`
        entity types, e.g. the animal being hunted."""
        aura = [h for h in HOSTILES if h not in ("enderman", "piglin_brute")]
        wanted = ",".join(aura + list(extra))
        if force or wanted != getattr(self, "_aura", None):
            m.chat(".settings kill-aura entities " + wanted)
            self._aura = wanted

    def respawn(self):
        """Leave the death screen (Meteor's auto-respawn only catches deaths
        that happen after it was switched on)."""
        self.mc.player.respawn()
        time.sleep(1)
        if m.screen_name():
            self.mc.setScreen(None)
        self._click = None

    # -- state -------------------------------------------------------------
    def pos(self):
        return m.player_position()

    def block_pos(self):
        return [math.floor(v) for v in m.player_position()]

    def stacks(self):
        return m.player_inventory()

    def inv(self):
        out = {}
        for s in m.player_inventory():
            out[short(s.item)] = out.get(short(s.item), 0) + s.count
        return out

    def count(self, pred):
        return sum(n for item, n in self.inv().items() if pred(item))

    def health(self):
        return m.player_health()

    def hunger(self):
        return self.mc.player.getFoodData().getFoodLevel()

    def is_dusk(self):
        """The last in-game hour or so before night: time to head home."""
        return 11000 <= m.world_info().day_ticks % 24000 < 12800

    def head_home(self, dist):
        """Time to start walking home, given how far away home is: dusk, or
        earlier for a bot far out (about 1.7 blocks a second with detours,
        plus a margin)."""
        t = m.world_info().day_ticks % 24000
        return t < 12800 and 12800 - t < dist * 12 + 1200

    def is_night(self):
        return 12800 < m.world_info().day_ticks % 24000 < 23200

    def block(self, x, y, z):
        return short(m.getblock(x, y, z))

    def log(self, *msg):
        m.log(f"[minevoid:{self.name}]", *msg)

    # -- waiting -----------------------------------------------------------
    def sleep(self, seconds):
        """Sleep while keeping heartbeat/survival alive."""
        end = time.time() + seconds
        while True:
            if self.on_tick:
                self.on_tick()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    def wait(self, cond, timeout, poll=0.5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            self.sleep(poll)
        return bool(cond())

    # -- baritone ----------------------------------------------------------
    def b(self, command):
        return self.baritone.getCommandManager().execute(command)

    def busy(self):
        bt = self.baritone
        return bool(bt.getPathingBehavior().isPathing()
                    or bt.getMineProcess().isActive()
                    or bt.getCustomGoalProcess().isActive()
                    or bt.getBuilderProcess().isActive()
                    or bt.getGetToBlockProcess().isActive()
                    or bt.getExploreProcess().isActive()
                    or bt.getFarmProcess().isActive())

    def stop(self):
        self.b("stop")
        for key in ("forward", "attack", "use", "jump", "sneak"):
            getattr(m, f"player_press_{key}")(False)

    def inside_base(self, pos):
        base = getattr(self, "facts", {}).get("base")
        # the room, its farm room to the west and the bottom of the stairs
        return bool(base) and abs(pos[0] - base[0] - .5) < 10 \
            and abs(pos[2] - base[2] - .5) < 8 and abs(pos[1] - base[1]) < 4

    def via_door(self, x, y, z):
        """Going into or out of the base: take the stairs and the door instead
        of letting Baritone tunnel straight through the walls (bots kept
        digging two-high holes that let mobs in and cost a day of sealing)."""
        exit_ = getattr(self, "facts", {}).get("base_exit")
        if not exit_ or self.inside_base(self.pos()) == self.inside_base((x + .5, y, z + .5)):
            return
        if math.dist(self.pos(), exit_) > 3 and not self.inside_base(self.pos()):
            # coming in: the top of the stairs first, then down them
            self.b(f"goto {exit_[0]} {exit_[1]} {exit_[2]}")
            self.sleep(1)
            self.wait(lambda: math.dist(self.pos(), exit_) < 3 or not self.busy(), 300)
        # Through the door itself: aim for the floor just inside (or the top
        # of the stairs on the way out), so walking through the doorway is
        # Baritone's cheapest route. (With digging off it would not step down
        # through the door at all and bots were locked out.)
        door = self.base_door()
        going_out = self.inside_base(self.pos())
        target = exit_ if going_out or not door else door
        self.b(f"goto {target[0]} {target[1]} {target[2]}")
        self.sleep(1)
        self.wait(lambda: math.dist(self.pos(), target) < 2 or not self.busy(), 120)
        self.b("stop")

    def base_door(self):
        """The floor cell just inside the base door (cached)."""
        base = getattr(self, "facts", {}).get("base")
        if not base:
            return None
        if getattr(self, "_door", (None,))[0] != tuple(base) or \
                getattr(self, "_door_block", None) is None:
            x, y, z = base
            cells = [[cx, y, cz] for cx in range(x - 4, x + 5) for cz in range(z - 4, z + 5)]
            door = next((c for c, k in zip(cells, m.getblocklist(cells))
                         if k and "_door" in k), None)
            inside = None
            self._door_block = tuple(door) if door else None
            if door:
                inside = min(((door[0] + dx, y, door[2] + dz) for dx, dz in
                              ((1, 0), (-1, 0), (0, 1), (0, -1))),
                             key=lambda c: math.dist(c, base))
            self._door = (tuple(base), inside)
        return self._door[1]

    dig_ok = False  # set by skills that dig inside the base on purpose

    def in_rooms(self, pos):
        """Inside the base rooms (base room, farm room, door), not the stairs."""
        base = getattr(self, "facts", {}).get("base")
        if not base:
            return False
        x, y, z = base
        return (x - 9 <= pos[0] < x + 4 and z - 3 <= pos[2] < z + 4
                and y - 1 <= pos[1] < y + 5)

    def base_guard(self):
        """Called every tick: no digging while inside the rooms, whatever is
        moving us (pathing, fighting, a skill). Bots kept breaking the base
        walls from inside in ways no single fix caught."""
        now = time.time()
        if now - getattr(self, "_guard_at", 0) < 1:
            return
        self._guard_at = now
        try:
            want = self.dig_ok or not self.in_rooms(self.pos())
            setting = JavaClass("baritone.api.BaritoneAPI").getSettings().allowBreak
            if bool(setting.value) != want:
                setting.value = want
        except Exception:
            pass

    def close_door(self):
        """Close the base door behind us: Baritone opens it to walk through
        and leaves it open, and a zombie walked in and killed a bot."""
        now = time.time()
        if now - getattr(self, "_door_at", 0) < 3:
            return
        self._door_at = now
        try:
            if self.base_door() is None:
                return  # (from either side: also behind a bot that left)
            d = getattr(self, "_door_block", None)
            state = m.getblock(*d) if d else ""
            if "open=true" not in (state or ""):
                return
            cell = lambda q: (math.floor(q[0]), math.floor(q[2]))
            if cell(self.pos()) == (d[0], d[2]) or \
                    math.dist(self.pos(), (d[0] + .5, d[1], d[2] + .5)) > 4:
                return  # in the doorway, or too far to reach
            if any(cell(p.position) == (d[0], d[2]) for p in m.players()
                   if p.name != self.name):
                return  # a teammate is passing through
            # An open door is a thin panel on one edge of its cell: try
            # points across the cell until the crosshair lands on it.
            for ox, oz, oy in [(a, b, h) for h in (0.5, 1.5)
                               for a in (0.15, 0.5, 0.85) for b in (0.15, 0.5, 0.85)]:
                m.player_look_at(d[0] + ox, d[1] + oy, d[2] + oz)
                time.sleep(0.08)
                hit = m.player_get_targeted_block(4.5)
                if hit and list(hit.position) in (list(d), [d[0], d[1] + 1, d[2]]):
                    m.player_press_use(True)
                    time.sleep(0.1)
                    m.player_press_use(False)
                    break
        except Exception:
            pass

    def walk_only(self, x, y, z):
        """Inside the base, Baritone may not dig: it tunnelled through the
        walls to reach spots in the room and the base never stayed sealed.
        Returns whether breaking was turned off (turn it back on after)."""
        if self.inside_base(self.pos()) and self.inside_base((x + .5, y, z + .5)):
            self.b("set allowBreak false")
            return True
        return False

    def goto(self, x, y, z, timeout=120, near=0):
        """Walk (digging/bridging as needed) to a block. True on arrival."""
        self.via_door(x, y, z)
        nobreak = self.walk_only(x, y, z)
        try:
            return self._goto(x, y, z, timeout, near)
        finally:
            if nobreak:
                self.b("set allowBreak true")

    def _goto(self, x, y, z, timeout, near):
        self.b(f"goto {x} {y} {z}")
        self.sleep(1)

        def arrived():
            px, py, pz = self.pos()
            return (abs(px - x - 0.5) <= near + 0.8 and abs(pz - z - 0.5) <= near + 0.8
                    and abs(py - y) <= near + 1.2)
        ok = self.wait(lambda: arrived() or not self.busy(), timeout)
        self.b("stop")
        return ok and arrived()

    def goto_near(self, x, y, z, radius=2, timeout=60):
        """Path to within `radius` blocks of a position."""
        self.via_door(x, y, z)
        nobreak = self.walk_only(x, y, z)
        try:
            return self._goto_near(x, y, z, radius, timeout)
        finally:
            if nobreak:
                self.b("set allowBreak true")

    def _goto_near(self, x, y, z, radius, timeout):
        goal = JavaClass("baritone.api.pathing.goals.GoalNear")(
            JavaClass("net.minecraft.core.BlockPos")(x, y, z), radius)
        self.baritone.getCustomGoalProcess().setGoalAndPath(goal)
        self.sleep(0.5)
        near = lambda: math.dist(self.pos(), (x + .5, y, z + .5)) <= radius + 1
        ok = self.wait(lambda: near() or not self.busy(), timeout)
        self.b("stop")
        return ok and near()

    def goto_block(self, block, timeout=60):
        """Go to the nearest known block of a type; opens it if a container."""
        self.b(f"goto {block}")
        self.sleep(1.5)
        self.wait(lambda: m.screen_name() or not self.busy(), timeout)
        self.b("stop")

    # -- inventory / container clicks -------------------------------------
    def menu(self):
        return self.mc.player.containerMenu

    def click(self, slot, button=0, kind="PICKUP"):
        """Click a slot of the currently open menu. Every reflection call is
        a round trip to the client, so the lookups are cached and refreshed
        with `sync_menu()` whenever a screen opens or closes."""
        if self._click is None:
            self.sync_menu()
        gm, player, cid, kinds = self._click
        gm.handleInventoryMouseClick(cid, slot, button, kinds[kind], player)

    def sync_menu(self):
        p = self.mc.player
        kinds = {k: getattr(self.ClickType, k) for k in ("PICKUP", "QUICK_MOVE", "THROW")}
        self._click = (self.mc.gameMode, p, p.containerMenu.containerId, kinds)

    def menu_slot(self, inv_slot, top_slots):
        """Map a player-inventory slot to a slot id in the open menu, given
        how many container slots precede the player inventory (crafting
        table 10, furnace 3, chest 27). top_slots=None is the inventory
        screen itself."""
        if top_slots is None:
            return inv_slot + 36 if inv_slot < 9 else inv_slot
        return top_slots + (27 + inv_slot if inv_slot < 9 else inv_slot - 9)

    def close_screen(self):
        if m.screen_name():
            self.mc.player.closeContainer()
            time.sleep(0.3)
        self._click = None

    def select(self, pred):
        """Put an item matching pred in hand. Returns False if absent."""
        for s in m.player_inventory():
            if pred(short(s.item)):
                slot = s.slot if s.slot < 9 else m.player_inventory_slot_to_hotbar(s.slot)
                m.player_inventory_select_slot(slot)
                time.sleep(0.2)
                return True
        return False

    # -- world interaction -------------------------------------------------
    def use_on(self, x, y, z, hold=0.4):
        """Look at a point and right-click."""
        m.player_look_at(x, y, z)
        time.sleep(0.35)
        m.player_press_use(True)
        time.sleep(hold)
        m.player_press_use(False)
        time.sleep(0.4)

    TORCH_AIM = {"east": (0.2, 0.5), "west": (0.8, 0.5),
                 "south": (0.5, 0.2), "north": (0.5, 0.8)}

    def break_torch(self, x, y, z):
        """Knock out a torch. A wall torch hangs against one side of its
        cell, so aiming at the cell's centre hit the wall behind it."""
        state = m.getblock(x, y, z) or ""
        facing = state.split("facing=")[1].split(",")[0].rstrip("]") \
            if "facing=" in state else None
        ox, oz = self.TORCH_AIM.get(facing, (0.5, 0.5))
        m.player_look_at(x + ox, y + (0.6 if facing else 0.45), z + oz)
        time.sleep(0.3)
        hit = m.player_get_targeted_block(4.5)
        if not hit or list(hit.position) != [x, y, z]:
            return False  # not on the torch: do not dig the wall instead
        m.player_press_attack(True)
        time.sleep(0.15)
        m.player_press_attack(False)
        return self.wait_plain(lambda: self.block(x, y, z) in ("air", "cave_air"), 1.0)

    def break_block(self, x, y, z, timeout=15):
        """Break the block at (x, y, z), but only while the crosshair is on
        it: holding attack at a cell hidden behind something else broke the
        base walls instead."""
        gone = lambda: self.block(x, y, z) in ("air", "cave_air")
        if gone():
            return True
        m.player_look_at(x + 0.5, y + 0.5, z + 0.5)
        time.sleep(0.2)
        end = time.time() + timeout
        try:
            while time.time() < end:
                hit = m.player_get_targeted_block(4.5)
                if not hit or list(hit.position) != [x, y, z]:
                    m.player_press_attack(False)
                    return gone()
                m.player_press_attack(True)
                # Let go the moment it breaks: still holding, the swing went
                # on into the block behind it (the base ceiling).
                stop = time.time() + 0.5
                while time.time() < stop and not gone():
                    time.sleep(0.03)
                if gone():
                    m.player_press_attack(False)
                    return True
                self.sleep(0.01)
            return gone()
        finally:
            m.player_press_attack(False)

    def find_block(self, name, radius=4):
        """Nearest block of a type within a small cube, or None."""
        px, py, pz = self.block_pos()
        lo = [px - radius, py - radius, pz - radius]
        region = m.get_block_region(lo, [px + radius, py + radius, pz + radius])
        best, n = None, 2 * radius + 1
        for x in range(lo[0], lo[0] + n):
            for y in range(lo[1], lo[1] + n):
                for z in range(lo[2], lo[2] + n):
                    if short(region.get_block(x, y, z)) == name:
                        d = math.dist((x, y, z), (px, py, pz))
                        if best is None or d < best[0]:
                            best = (d, [x, y, z])
        return best and best[1]

    def place_block(self, name):
        """Place a block from the inventory on the ground next to the bot.
        Returns where it ended up, or None."""
        if not self.select(lambda i: i == name):
            return None
        px, py, pz = self.block_pos()
        soft = ("air", "cave_air", "short_grass", "tall_grass", "snow",
                "leaf_litter", "fern", "dandelion", "poppy")
        bad_floor = soft + ("water", "lava", "crafting_table", "furnace",
                            "chest", "unknown")
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1),
                       (1, -1), (-1, 1), (2, 0), (-2, 0), (0, 2), (0, -2)):
            x, z = px + dx, pz + dz
            for y in (py, py - 1, py + 1):
                if (self.block(x, y, z) in soft
                        and self.block(x, y - 1, z) not in bad_floor):
                    # Aim just inside the top face of the floor block. The
                    # ray can still clip something else, so look for where
                    # the block really went instead of assuming.
                    self.use_on(x + 0.5, y - 0.05, z + 0.5)
                    pos = self.find_block(name)
                    if pos:
                        return pos
        return None

    def open_block(self, name, screen):
        """Open a crafting table / furnace / chest: use one within reach,
        walk to one nearby, or place one from the inventory. `screen` is a
        substring of the expected screen title."""
        is_open = lambda: screen in (m.screen_name() or "")
        if is_open():
            return True
        self.close_screen()
        pos = self.find_block(name, 6)
        if pos and math.dist(pos, self.block_pos()) > 3:
            self.goto_near(*pos, radius=2, timeout=40)
        if not pos:
            pos = self.place_block(name)
        if not pos:
            self.log(f"no {name} nearby and could not place one")
            return False
        for _ in range(5):
            if not self.aim(pos):
                continue
            m.player_press_use(True)
            time.sleep(0.4)
            m.player_press_use(False)
            if self.wait_plain(is_open, 1.5):
                self._click = None
                return True
        self.log(f"could not open {name} at {pos}")
        return False

    def place_at(self, name, x, y, z, stand=None):
        """Place block `name` at exactly (x, y, z) by clicking the top of
        the block below it, standing at `stand` (or anywhere in reach).
        A bed's head goes one block further in the direction we face."""
        placed = lambda: self.block(x, y, z) == name
        if placed():
            return True
        for attempt in range(3):
            if not self.select(lambda i: i == name):
                return False
            if stand and attempt == 0:
                self.goto(*stand, timeout=25)
            if math.dist(self.pos(), (x + .5, y, z + .5)) > 3.5:
                self.goto_near(x, y, z, radius=2, timeout=30)
            m.player_look_at(x + 0.5, y - 0.01, z + 0.5)
            time.sleep(0.4)
            hit = m.player_get_targeted_block(4.5)
            if not hit or list(hit.position) != [x, y - 1, z] or hit.side != "up":
                kind = short(hit.type) if hit else ""
                if hit and (kind.endswith("_leaves") or kind in (
                        "short_grass", "tall_grass", "fern", "snow")):
                    self.break_block(*hit.position, timeout=6)
                else:
                    self.goto_near(x, y, z, radius=1, timeout=20)
                continue
            m.player_press_use(True)
            time.sleep(0.3)
            m.player_press_use(False)
            if self.wait_plain(placed, 1.5):
                return True
        self.log(f"could not place {name} at {[x, y, z]}")
        return False

    SOFT = ("air", "cave_air", "water", "lava", "short_grass", "tall_grass",
            "fern", "snow", "torch", "wall_torch", "vine", "leaf_litter")
    FACES = {(0, -1, 0): "up", (0, 1, 0): "down", (1, 0, 0): "west",
             (-1, 0, 0): "east", (0, 0, 1): "north", (0, 0, -1): "south"}

    def place_against(self, name, x, y, z, stand=None):
        """Place `name` at (x, y, z) by clicking whichever face of a solid
        neighbour we can see: works for wall gaps and shaft openings where
        there is no floor to click."""
        placed = lambda: self.block(x, y, z) == name
        for attempt in range(2):
            for (dx, dy, dz), side in self.FACES.items():
                n = (x + dx, y + dy, z + dz)
                if self.block(*n) in self.SOFT or self.block(*n).endswith("_bed"):
                    continue
                if not self.select(lambda i: i == name):
                    return False
                m.player_look_at(x + 0.5 + dx * 0.5, y + 0.5 + dy * 0.5,
                                 z + 0.5 + dz * 0.5)
                time.sleep(0.3)
                hit = m.player_get_targeted_block(4.5)
                if hit and list(hit.position) == list(n) and hit.side == side:
                    m.player_press_use(True)
                    time.sleep(0.25)
                    m.player_press_use(False)
                    if self.wait_plain(placed, 1.0):
                        return True
            if attempt == 0:
                self.goto_near(*(stand or (x, y, z)), radius=1 if stand else 2,
                               timeout=20)
        # Last try: jump and click at the top of the jump. A wall gap at
        # head height has only the top of the block below it to click, and
        # that is above our eyes while standing.
        for (dx, dy, dz), side in self.FACES.items():
            n = (x + dx, y + dy, z + dz)
            if side != "up" or self.block(*n) in self.SOFT:
                continue
            if not self.select(lambda i: i == name):
                return False
            for _ in range(2):
                m.player_press_jump(True)
                time.sleep(0.25)
                m.player_look_at(x + 0.5, y + 0.02, z + 0.5)
                time.sleep(0.05)
                hit = m.player_get_targeted_block(4.5)
                if hit and list(hit.position) == list(n) and hit.side == "up":
                    m.player_press_use(True)
                    time.sleep(0.1)
                    m.player_press_use(False)
                m.player_press_jump(False)
                time.sleep(0.6)
                if placed():
                    return True
        return False

    def equip_offhand(self, name):
        """Move an item (a shield) into the off-hand slot."""
        stack = next((s for s in m.player_inventory()
                      if short(s.item) == name), None)
        if stack is None or stack.slot == 40:
            return stack is not None
        self.close_screen()
        self.sync_menu()
        self.click(self.menu_slot(stack.slot, None))  # pick it up
        self.click(45)                                 # off-hand slot
        self.click(self.menu_slot(stack.slot, None))  # put back what was there
        time.sleep(0.3)
        return any(s.slot == 40 and short(s.item) == name
                   for s in m.player_inventory())

    def equip_armor(self, name):
        """Shift-click a piece of armour into its slot (Meteor's Auto Armor
        normally beats us to it)."""
        stack = next((s for s in m.player_inventory()
                      if short(s.item) == name), None)
        if stack is None or stack.slot >= 36:
            return stack is not None
        self.close_screen()
        self.sync_menu()
        self.click(self.menu_slot(stack.slot, None), 0, "QUICK_MOVE")
        return True

    def block_with_shield(self, foe, seconds=1.2):
        """Raise the shield towards an archer."""
        if not any(s.slot == 40 and short(s.item) == "shield"
                   for s in m.player_inventory()):
            return False
        x, y, z = foe.position
        m.player_look_at(x, y + 1.4, z)
        m.player_press_use(True)
        time.sleep(seconds)
        m.player_press_use(False)
        return True

    def sleeping(self):
        return bool(self.mc.player.isSleeping())

    def aim(self, pos):
        """Look at a block and confirm the crosshair is really on it. If
        something is in the way, clear it (leaves, grass) or step closer."""
        blocker = None
        for dy in (0.5, 0.95, 0.15):
            m.player_look_at(pos[0] + 0.5, pos[1] + dy, pos[2] + 0.5)
            time.sleep(0.35)
            hit = m.player_get_targeted_block(4.5)
            if hit and list(hit.position) == list(pos):
                return True
            blocker = hit or blocker
        kind = short(blocker.type) if blocker else ""
        if kind.endswith("_leaves") or kind in ("short_grass", "tall_grass", "fern",
                                                "leaf_litter", "vine", "snow"):
            self.break_block(*blocker.position, timeout=6)
        else:
            self.goto_near(*pos, radius=1, timeout=20)
        return False

    def wait_plain(self, cond, timeout):
        """Wait without ticking (no heartbeat/defence), for UI sequences."""
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return bool(cond())

    def attack(self, entity):
        """Hit `entity`, but only with it under the crosshair: swinging at a
        zombie seen through a gap in the base wall hit the wall instead and
        widened the gap block by block."""
        x, y, z = entity.position
        m.player_look_at(x, y + 0.6, z)
        time.sleep(0.1)
        try:
            target = m.player_get_targeted_entity(4.5)
        except Exception:
            target = None
        if target is None or target.id != entity.id:
            return False
        m.player_press_attack(True)
        time.sleep(0.1)
        m.player_press_attack(False)
        return True

    def use_on_entity(self, entity, height=0.7):
        """Right-click an entity (shear, feed, breed). Vanilla/Grim reach
        for entities is 3 blocks, so get close first."""
        x, y, z = entity.position
        if math.dist((x, y, z), self.pos()) > 2.6:
            self.goto_near(math.floor(x), math.floor(y), math.floor(z),
                           radius=1, timeout=20)
            fresh = m.entities(uuid=entity.uuid, max_distance=10)
            if not fresh:
                return False
            x, y, z = fresh[0].position
        m.player_look_at(x, y + height, z)
        time.sleep(0.3)
        target = m.player_get_targeted_entity(3.0)
        if not target or target.uuid != entity.uuid:
            return False
        m.player_press_use(True)
        time.sleep(0.2)
        m.player_press_use(False)
        time.sleep(0.3)
        return True

    def toss(self, pred, n):
        """Throw up to n matching items out in front of the bot."""
        self.close_screen()
        self.sync_menu()
        thrown = 0
        stacks = [s for s in m.player_inventory() if s.slot < 36 and pred(short(s.item))]
        totals = {}
        for s in stacks:
            totals[short(s.item)] = totals.get(short(s.item), 0) + s.count
        # Most plentiful kind first, so e.g. wool arrives as one colour.
        stacks.sort(key=lambda s: -totals[short(s.item)])
        for s in stacks:
            if thrown >= n:
                break
            for _ in range(min(n - thrown, s.count)):
                self.click(self.menu_slot(s.slot, None), 0, "THROW")
                thrown += 1
        return thrown

    def eat(self):
        """Eat one food item (Meteor's Auto Eat only sees the hotbar)."""
        from .items import CLASSES
        cooked = lambda i: CLASSES["food"](i) and i not in ("chicken", "rabbit")
        if not (self.select(lambda i: i.startswith("cooked_") or i == "bread")
                or self.select(cooked) or self.select(CLASSES["food"])
                # Last resort: rotten flesh (may cause Hunger, but starving
                # means no health regeneration at all).
                or (self.hunger() <= 8
                    and self.select(lambda i: i == "rotten_flesh"))):
            return False
        m.player_set_orientation(m.player_orientation()[0], -30)  # not at a block
        m.player_press_use(True)
        time.sleep(2.2)
        m.player_press_use(False)
        return True

    def fight(self, radius=6, timeout=25):
        """Stand and fight until no hostile is within `radius`."""
        end = time.time() + timeout
        fought, armed = False, False
        while time.time() < end:
            foes = self.hostiles(radius)
            if not foes:
                break
            if not fought:
                self.close_screen()
                armed = self.select(lambda i: i.endswith("_sword") or i.endswith("_axe"))
                fought = True
            d = math.dist(foes[0].position, self.pos())
            if foes[0].type.endswith("creeper") and d <= 3.5:
                # Hit once for knockback, then back off before it blows.
                self.attack(foes[0])
                m.player_press_backward(True)
                time.sleep(0.9)
                m.player_press_backward(False)
            elif d <= 2.9:
                self.attack(foes[0])
                time.sleep(0.55)
            elif armed and d <= radius:
                # Close in: standing still lets archers shoot us to death
                # (that is how bots died inside their own base).
                if foes[0].type.endswith(("skeleton", "stray", "bogged")):
                    self.block_with_shield(foes[0], 0.5)
                x, y, z = foes[0].position
                self.b("resume")  # the caller may have paused Baritone
                self.goto_near(math.floor(x), math.floor(y), math.floor(z),
                               radius=1, timeout=3)
                self.b("pause")
            elif foes[0].type.endswith(("skeleton", "stray", "bogged")):
                self.block_with_shield(foes[0], 0.8) or time.sleep(0.2)
            else:
                time.sleep(0.2)
        return fought

    def hostiles(self, radius=5, visible=True):
        """Hostile mobs nearby; by default only ones we can see. A zombie
        on the other side of the base wall made bots dig through the wall
        to reach it (and hit the wall blocks in between)."""
        out = []
        for e in m.entities(max_distance=radius, sort="nearest"):
            if e.type.rsplit(".", 1)[-1] not in HOSTILES:
                continue
            if visible:
                try:
                    je = self.mc.level.getEntity(e.id)
                    if je is None or not self.mc.player.hasLineOfSight(je):
                        continue
                except Exception:
                    pass
            out.append(e)
        return out
