"""Skills: the things a task in the goal plan can ask a bot to do.

Each skill is `fn(g, facts, **args) -> bool | dict`. Returning a dict means
success plus facts to publish to the team (e.g. the base location).
Skills block until finished; `g.sleep` / `g.wait` keep the heartbeat and
self-defence running and may raise `Interrupt` when the bot is reassigned.
"""
import math
import time

import minescript as m

from . import craft as crafting
from . import game
from .game import BLOCK_GROUPS, short
from .items import CLASSES, matcher

ANIMALS = "cow|pig|sheep|chicken|rabbit|mooshroom"


LEASH = 160  # blocks from home a bot may wander while working


def come_home(g, facts, force=False, leash=LEASH):
    """Keep the team together: if we strayed beyond the leash, walk back
    towards home (the base, or world spawn before there is one)."""
    if not g.overworld():
        return False  # (home is an overworld place)
    home = facts.get("base") or m.world_info().spawn
    here = g.pos()
    if not force and math.dist(here[::2], home[::2]) <= leash:
        return False
    g.stop()
    g.status = "returning to the team area"
    g.b(f"goto {home[0]} {home[2]}")
    g.sleep(2)
    g.wait(lambda: math.dist(g.pos()[::2], home[::2]) < leash / 3
           or not g.busy(), 600, 2)
    g.stop()
    return True


def near_danger(facts, pos, radius=40):
    """Within `radius` of a live danger spot (a teammate's recent death)."""
    now = time.time()
    return any(now - d[3] < (d[4] if len(d) > 4 else 43200)
               and math.dist(d[:3], pos) < radius
               for d in facts.get("danger", []))


def avoid_danger(g, facts, radius=28):
    """Leave the area of a teammate's recent underground death: Baritone
    mines the nearest exposed ore, which kept leading bots back into the
    same dark cave. Moving away makes other ore the nearest."""
    if not g.overworld():
        return False
    here = g.pos()
    spots = [d[:3] for d in facts.get("danger", [])
             if time.time() - d[3] < (d[4] if len(d) > 4 else 43200)
             and math.dist(d[:3], here) < radius
             and not (facts.get("base") and math.dist(d[:3], facts["base"]) < 32)]
    if not spots:
        return False  # (deaths at home are dealt with at home, not avoided)
    d = spots[0]
    # Far enough that the spot's chunks unload: Baritone mines the nearest
    # visible ore in loaded chunks, and from 60 blocks away that was still
    # the deadly cave. Head away from it, or home if that leaves the leash.
    home = facts.get("base") or here
    # Of eight points 130 blocks from the spot (within the leash), the one
    # farthest from every known danger spot: straight away from one spot
    # led a bot to the Drowned's river by another.
    now = time.time()
    live = [s[:3] for s in facts.get("danger", [])
            if now - s[3] < (s[4] if len(s) > 4 else 43200)] or [d]
    cands = [(d[0] + math.cos(a) * 130, d[2] + math.sin(a) * 130)
             for a in (i * math.pi / 4 for i in range(8))]
    cands = [c for c in cands if math.dist(c, (home[0], home[2])) <= LEASH] or \
        [(home[0], home[2])]
    tx, tz = max(cands, key=lambda c: min(math.dist(c, (q[0], q[2])) for q in live))
    g.stop()
    g.status = f"keeping away from where a teammate died at {[round(v) for v in d]}"
    g.b(f"goto {int(tx)} {int(tz)}")
    g.sleep(2)
    g.wait(lambda: math.dist(g.pos(), d) > 120 or not g.busy(), 240, 1)
    g.stop()
    return True


def explore(g, facts, seconds):
    """Baritone's explore for a while, turning away from danger spots on
    the way: exploring for logs walked a bot 165 blocks straight back to
    the lake where the Drowned had just killed it."""
    g.b("explore")
    end = time.time() + seconds
    while time.time() < end:
        g.sleep(1)
        if avoid_danger(g, facts, radius=40):
            return
    g.stop()


def light_the_way(g, last):
    """Underground, drop a torch every ~10 blocks: caves full of mobs were
    killing the miners, and lit tunnels stop new spawns. Returns where the
    last torch went."""
    here = g.block_pos()
    if here[1] > 55 or (last and math.dist(here, last) < 10):
        return last
    if g.count(lambda i: i == "torch") == 0:
        if g.count(CLASSES["coal"]) == 0 or not crafting.craft(g, "torch"):
            return last
    g.b("pause")
    try:
        pos = g.place_block("torch")
    finally:
        g.b("resume")
    return pos or here


MINE_DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
ORES = {"iron": ("iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore"),
        "coal": ("coal_ore", "deepslate_coal_ore", "iron_ore", "deepslate_iron_ore"),
        # deep down: diamonds, plus lapis for enchanting, and whatever iron
        "diamond": ("diamond_ore", "deepslate_diamond_ore", "deepslate_lapis_ore",
                    "lapis_ore", "deepslate_iron_ore", "deepslate_gold_ore")}
HAZARDS = ("lava", "water", "bubble_column")


def make_bow(g, facts, arrows=16):
    """A bow and arrows: string and feathers come from the chest (spiders
    and chickens filled it), flint from gravel."""
    has = lambda i: g.count(lambda j: j == i)
    if not has("bow"):
        if has("string") < 3:
            withdraw(g, facts, "string", 3)
        if has("string") < 3 or not crafting.craft(g, "bow"):
            g.log("bow: not enough string")
            return False
    sets = max(0, (arrows - has("arrow") + 3) // 4)
    if sets:
        if has("feather") < sets:
            withdraw(g, facts, "feather", sets)
        if has("flint") < sets:
            gather(g, facts, "gravel", "flint", sets, timeout=400, until_dark=True)
        n = min(sets, has("feather"), has("flint"))
        if n:
            crafting.craft(g, "arrow", n)
    g.log(f"bow: {has('bow')} bow, {has('arrow')} arrows")
    return has("bow") > 0 and has("arrow") > 0


def make_books(g, facts, books=3):
    """Books for the enchanting table and bookshelves: leather from the
    chest (hunting piled it up), paper from sugar cane by the water."""
    has = lambda i: g.count(lambda j: j == i)
    need = books - has("book")
    if need <= 0:
        return True
    if has("leather") < need:
        withdraw(g, facts, "leather", need - has("leather"))
    n = min(need, has("leather"))
    if not n:
        g.log("books: no leather")
        return False
    cane = max(0, 3 * n - has("paper") - has("sugar_cane"))
    if cane:
        water = facts.get("water")
        if water and math.dist(g.pos(), water) > 32:
            g.status = "heading for the water to find sugar cane"
            g.goto_near(*water, radius=8, timeout=240)
        gather(g, facts, "sugar_cane", "sugar_cane", has("sugar_cane") + cane,
               timeout=600, until_dark=True)
    if has("sugar_cane") >= 3 and facts.get("base"):
        # (paper needs a crafting table: out by the water none could be placed)
        g.status = "taking the sugar cane home"
        g.goto(*facts["base"], timeout=300, near=2)
    if has("sugar_cane") >= 3:
        crafting.craft(g, "paper", has("sugar_cane") // 3)
    n = min(n, has("paper") // 3, has("leather"))
    if n:
        crafting.craft(g, "book", n)
    g.log(f"books: {has('book')} book(s), {has('paper')} paper left")
    return has("book") > 0


SHAFT_TURNS = ((1, 0), (0, 1), (0, -1), (-1, 0))


def dig_shaft(g, facts, start="base_exit", to_y=16, name="mine", timeout=3600):
    """Dig the team's own staircase down to a mine level, once: 1-wide,
    3-high steps going down one block per step, a torch every ~10 blocks,
    turning aside at lava or water. Walking to the mine through caves got
    a bot killed by a zombie on the way; this way down is lit and known.
    Progress is kept in `<name>_shaft`, the bottom becomes fact `name`."""
    if facts.get(name):
        return {name: facts[name]}
    if not g.count(lambda i: i.endswith("_pickaxe")):
        return False
    if not g.count(lambda i: i == "torch") and not g.count(CLASSES["coal"]):
        make_torches(g, facts, count=8)
    prog = facts.get(f"{name}_shaft")
    if prog:
        cx, cy, cz, dx, dz = prog
    else:
        sx, sy, sz = facts[start]
        dx, dz = 1, 0
        cx, cy, cz = sx, sy, sz
    g.status = f"going to the {name} shaft"
    if not g.goto(cx, cy, cz, timeout=400, near=1):
        g.log(f"shaft: could not reach {[cx, cy, cz]}")
        return False
    end, last_torch, turns = time.time() + timeout, None, 0
    while cy > to_y and time.time() < end:
        g.status = f"digging the {name} shaft (y={cy}, down to {to_y})"
        nx, ny, nz = cx + dx, cy - 1, cz + dz
        dig = [(nx, ny, nz), (nx, ny + 1, nz), (nx, ny + 2, nz)]
        side = [(nx + a, yy, nz + b) for a, b in ((dz, dx), (-dz, -dx))
                for yy in (ny, ny + 1, ny + 2)]
        look = dig + side + [(nx + dx, ny, nz + dz), (nx + dx, ny + 1, nz + dz),
                             (nx, ny - 1, nz), (nx, ny + 3, nz)]
        kinds = [short(k) for k in m.getblocklist([list(c) for c in look])]
        if any(k in HAZARDS for k in kinds):
            # lava or water ahead: try another heading from here
            turns += 1
            if turns > 3:
                g.log(f"shaft: boxed in by liquid at {[cx, cy, cz]}")
                break
            i = SHAFT_TURNS.index((dx, dz)) if (dx, dz) in SHAFT_TURNS else 0
            dx, dz = SHAFT_TURNS[(i + 1) % 4]
            continue
        for c in reversed(dig):  # top first: the lower blocks hide behind it
            for _ in range(4):  # gravel and sand fall in
                if g.block(*c) in AIR:
                    break
                g.select(lambda i: i.endswith("_pickaxe"))
                if not g.break_block(*c, timeout=8):
                    break
        if any(g.block(*c) not in AIR for c in dig):
            g.log(f"shaft: could not dig at {[nx, ny, nz]}")
            break
        if g.block(nx, ny - 1, nz) in AIR + ("water", "lava"):
            # a cave below the step: floor it over
            filler = next((f for f in FILLERS if g.count(lambda j, f=f: j == f)), None)
            if not filler or not g.place_against(filler, nx, ny - 1, nz):
                g.log(f"shaft: open drop at {[nx, ny - 1, nz]}")
                break
        if not g.goto(nx, ny, nz, timeout=15):
            break
        cx, cy, cz, turns = nx, ny, nz, 0
        last_torch = light_the_way(g, last_torch)
        g.pending_facts[f"{name}_shaft"] = [cx, cy, cz, dx, dz]
    g.stop()
    if cy <= to_y:
        g.log(f"shaft: {name} reached at {[cx, cy, cz]}")
        return {name: [cx, cy, cz], f"{name}_shaft": [cx, cy, cz, dx, dz]}
    g.log(f"shaft: {name} shaft down to y={cy}")
    return False


def light_shaft(g, facts, name="mine", start="base_exit", every=8):
    """Torches down a straight shaft dug while nobody had coal: the dark
    stairs and tunnels spawned the skeleton that shot a miner."""
    top, bottom = facts.get(start), facts.get(name)
    if not top or not bottom:
        return False
    if not g.count(lambda i: i == "torch") and not make_torches(g, facts, count=10):
        return False
    steps = top[1] - bottom[1]
    dx = (bottom[0] - top[0]) // steps if steps else 0
    dz = (bottom[2] - top[2]) // steps if steps else 0
    if steps <= 0 or (top[0] + dx * steps, top[2] + dz * steps) != (bottom[0], bottom[2]):
        g.log(f"shaft: not a straight shaft from {top} to {bottom}")
        return False
    lit = 0
    for k in range(every, steps + 1, every):
        if not g.count(lambda i: i == "torch"):
            break
        c = (top[0] + dx * k, top[1] - k, top[2] + dz * k)
        g.status = f"lighting the {name} shaft ({k}/{steps})"
        if not g.goto(*c, timeout=60):
            continue
        g.stop()
        if g.place_block("torch"):
            lit += 1
    g.log(f"shaft: {lit} torches down the {name} shaft")
    return {f"{name}_lit": True} if lit else False


def branch_mine(g, facts, blocks="iron", item="raw_iron|iron_ingot", count=10,
                length=24, timeout=900, level=16, name="mine"):
    """Mine ore from the team's own branch mine instead of wandering into
    caves (where most miners died): a fixed spot at y=16 near the base,
    with straight 1x2 tunnels dug by hand in four directions. Every step
    the freshly exposed walls are checked for ore, which is real once
    exposed (anti-xray only fakes hidden blocks). A torch every ~10 blocks;
    lava or water ahead ends the branch. `level` and `name` give other
    mines (diamonds at y=-58 in "deep_mine")."""
    pred = matcher(item)
    if not g.count(lambda i: i.endswith("_pickaxe")):
        return False
    if not g.count(lambda i: i == "torch") and not g.count(CLASSES["coal"]):
        # A dark mine spawns skeletons (one shot a miner at y=16): torches
        # from charcoal before going down.
        make_torches(g, facts, count=8)
    base = facts["base"]
    mine = facts.get(name) or [base[0] + 6, level, base[2] + 24]
    g.status = "going down to the mine"
    if not g.goto(*mine, timeout=400, near=1):
        g.log(f"mine: could not get down to {mine}")
        return False
    if not facts.get(name):
        g.pending_facts[name] = mine
    branches = dict(facts.get(f"{name}_branches") or {})
    if branches.get("blocked"):
        branches.pop("blocked")
    shaft = facts.get(f"{name}_shaft")
    if shaft:
        # Back the way the shaft came down runs right under its steps: the
        # west branch dug out the staircase's floor.
        back = next((k for k, v in MINE_DIRS.items() if v == (-shaft[3], -shaft[4])), None)
        if back:
            branches[back] = -1
    # Shortest branches first, and each miner its own: two miners both
    # picked the shortest and dug the same tunnel one behind the other.
    open_dirs = sorted((k for k in MINE_DIRS if branches.get(k, 0) >= 0),
                       key=lambda k: (branches.get(k, 0), k))
    digits = "".join(ch for ch in g.name if ch.isdigit()) or "0"
    d = open_dirs[int(digits) % len(open_dirs)] if open_dirs else None
    if d is None:
        g.log("mine: every branch is blocked")
        return False
    dx, dz = MINE_DIRS[d]
    n = branches.get(d, 0)
    x, y, z = mine
    ex, ez = x + dx * n, z + dz * n
    if n and not g.goto(ex, y, ez, timeout=180, near=0):
        g.log(f"mine: could not get to the end of branch {d}")
        return False
    ores = ORES.get(blocks, ORES["iron"])
    side = [(dz, dx), (-dz, -dx)]  # perpendicular offsets (x, z)
    last_torch, end, steps = None, time.time() + timeout, 0
    while g.count(pred) < count and steps < length and time.time() < end:
        g.status = f"branch mining {d} ({n} blocks, {g.count(pred)}/{count})"
        cx, cz = ex + dx, ez + dz
        look = [(cx, y, cz), (cx, y + 1, cz), (cx + dx, y, cz + dz),
                (cx + dx, y + 1, cz + dz), (cx, y + 2, cz), (cx, y - 1, cz)]
        look += [(cx + a, yy, cz + b) for a, b in side for yy in (y, y + 1)]
        kinds = [short(k) for k in m.getblocklist([list(c) for c in look])]
        if any(k in HAZARDS for k in kinds):
            g.log(f"mine: {d} branch hit liquid at {[cx, y, cz]}; stopping it")
            if "lava" in kinds:
                # where the tunnel stops short of lava: obsidian comes from
                # pouring water on it (portal, enchanting table)
                lava = dict(facts.get("lava") or {})
                lava[f"{name}_{d}"] = [ex, y, ez, dx, dz]
                g.pending_facts["lava"] = lava
            branches[d] = -1  # never extend it again
            break
        for c in (look[1], look[0]):  # head height first: it hides the lower one
            for _ in range(4):  # gravel/sand can keep falling in
                if g.block(*c) in AIR:
                    break
                g.select(lambda i: i.endswith("_pickaxe"))
                if not g.break_block(*c, timeout=8):
                    break
        if any(g.block(*c) not in AIR for c in look[:2]):
            g.log(f"mine: could not dig through at {[cx, y, cz]}")
            break
        if not g.goto(cx, y, cz, timeout=15):
            break
        ex, ez, n, steps = cx, cz, n + 1, steps + 1
        # (saved as we go: dusk interrupted runs and their tunnels were lost)
        branches[d] = n
        g.pending_facts[f"{name}_branches"] = dict(branches)
        # Ore showing in the walls, floor or ceiling - of the new cell and
        # of the one before it: anti-xray sends the real blocks a moment
        # after a block breaks, so the new cell's walls can still read as
        # plain stone (30 blocks of tunnel turned up no iron at all).
        walls = []
        for wx, wz in ((ex, ez), (ex - dx, ez - dz)):
            walls += [(wx + a, yy, wz + b) for a, b in side for yy in (y, y + 1)]
            walls += [(wx, y - 1, wz), (wx, y + 2, wz)]
        for c, k in zip(walls, m.getblocklist([list(c) for c in walls])):
            if short(k) in ores:
                g.select(lambda i: i.endswith("_pickaxe"))
                g.break_block(*c, timeout=8)
        last_torch = light_the_way(g, last_torch)
    pick_up_drops(g, g.pos(), 4)
    branches[d] = n if branches.get(d, 0) >= 0 else -1
    g.pending_facts[f"{name}_branches"] = branches
    g.log(f"mine: branch {d} now {n} long, holding {g.count(pred)} {item}")
    return steps > 0 or g.count(pred) >= count


def gather(g, facts, blocks, item, count, timeout=900, until_dark=False):
    """Mine blocks with Baritone until `count` matching items are held."""
    pred, names = matcher(item), BLOCK_GROUPS[blocks]
    if blocks == "logs" and facts.get("tree_farm") and g.overworld():
        tree_harvest(g, facts)  # our own trees first, no wandering
        if g.count(pred) >= count:
            return True
    if blocks == "stone" and facts.get("mine") and facts.get("mine_lit") \
            and g.count(lambda i: i.endswith("_pickaxe")) and g.overworld():
        # Cobble from our own lit tunnels: Baritone went for the nearest
        # exposed stone, often deep in a cave (a skeleton shot a bot at
        # y=-1 while it was only after cobblestone for tools).
        g.status = f"cutting stone in the mine {g.count(pred)}/{count}"
        branch_mine(g, facts, blocks="iron", item=item, count=count,
                    length=count, timeout=min(timeout, 900))
        if g.count(pred) >= count:
            return True
    if blocks in ("iron", "coal") and not g.count(lambda i: i == "torch") \
            and not g.count(CLASSES["coal"]):
        make_torches(g, facts, count=8)  # caves are dark: light them as we go
    end, misses = time.time() + timeout, 0
    last_progress, last_state, stuck = time.time(), None, 0
    last_torch = None
    # Anti-xray scrambles hidden ores, so only chase ore we can actually
    # see; wood and stone are never faked and can use chunk data directly.
    g.b("set legitMine " + ("true" if blocks in ("iron", "coal") else "false"))
    while g.count(pred) < count:
        if time.time() > end:
            g.stop()
            return False
        if until_dark and (g.is_dusk() or g.is_night()):
            # an errand for an indoor job: back inside before dark (a
            # harvester out collecting seeds was killed at nightfall)
            g.stop()
            return False
        come_home(g, facts)
        if g.in_rooms(g.pos()) and facts.get("base_exit"):
            # In the base nothing may be dug: out by the door first (a
            # harvester sat "gathering grass" inside the farm for half an hour)
            g.status = "leaving the base"
            g.via_door(*facts["base_exit"])
        if blocks in ("iron", "coal", "stone"):
            last_torch = light_the_way(g, last_torch)
        avoid_danger(g, facts)
        base = facts.get("base")
        if blocks in ("iron", "coal") and base and math.dist(g.pos(), base) < 20:
            # Ore showing through gaps in the base's walls drew a miner back
            # to dig into the base from outside: mine elsewhere.
            g.stop()
            g.status = "mining away from the base"
            # the way that keeps furthest from teammates' recent deaths (due
            # east led straight into the cave that killed four bots)
            spots = [d[:3] for d in facts.get("danger", [])] or [[base[0], 0, base[2] + 1000]]
            tx, tz = max(((base[0] + dx * 100, base[2] + dz * 100) for dx, dz in
                          ((1, 0), (-1, 0), (0, 1), (0, -1))),
                         key=lambda t: min(math.dist(t, (d[0], d[2])) for d in spots))
            g.via_door(int(tx), base[1] + 10, int(tz))  # out by the door
            g.b(f"goto {int(tx)} {int(tz)}")
            g.sleep(2)
            g.wait(lambda: math.dist(g.pos(), base) > 90 or not g.busy(), 240, 1)
            g.stop()
        base = facts.get("base")
        if base and blocks not in ("logs", "grass") and math.dist(g.pos(), base) < 24:
            # Do not quarry the base itself (bots mined its roof away).
            g.status = "leaving the base to dig"
            g.via_door(base[0] + 40, base[1] + 12, base[2])  # not through a wall
            g.b(f"goto {base[0] + 40} {base[2]}")
            g.wait(lambda: math.dist(g.pos(), base) >= 24 or not g.busy(), 90, 1)
            g.stop()
        g.status = f"gathering {blocks} {g.count(pred)}/{count}"
        if not g.baritone.getMineProcess().isActive():
            g.b("mine " + names)
            g.sleep(4)
            if not g.baritone.getMineProcess().isActive() and blocks == "stone":
                # No stone Baritone can path to (anti-xray hides unexposed
                # rock): just tunnel; every block dug is stone anyway.
                # Dig down first: near the surface a tunnel only finds dirt
                # and gravel.
                g.status = "digging down for stone"
                x, y, z = g.block_pos()
                if y > 50:
                    g.goto(x, y - 10, z, timeout=60)
                g.status = "tunnelling for stone"
                g.b("tunnel")
                g.sleep(20)
                g.b("stop")
                continue
            if not g.baritone.getMineProcess().isActive():
                # Nothing of that kind known nearby: wander and retry.
                misses += 1
                g.status = f"exploring for {blocks}"
                explore(g, facts, 15 + 10 * min(misses, 4))
        # Progress = more items, or really going somewhere. Jittering in
        # place (Baritone fighting a path it cannot finish) is not progress.
        have_now, here = g.count(pred), g.pos()
        if last_state is None or have_now > last_state[0] \
                or math.dist(here, last_state[1]) > 6:
            last_state, last_progress = (have_now, here), time.time()
        elif time.time() - last_progress > 45:
            stuck += 1
            g.log(f"gather stuck ({stuck}), restarting baritone")
            g.stop()
            if stuck >= 2 and blocks == "stone":
                # Stone Baritone keeps failing to reach (a bot gathered 46
                # dirt and no stone in 20 minutes): dig down and tunnel -
                # every block that way is stone.
                x, y, z = g.block_pos()
                g.status = "digging down for stone"
                g.goto(x, max(y - 10, 20), z, timeout=60)
                g.status = "tunnelling for stone"
                g.b("tunnel")
                g.sleep(25)
                g.stop()
            elif stuck >= 2:
                # Same spot keeps failing (often a cramped corner Baritone
                # cannot path out of): go somewhere else first.
                x, y, z = g.block_pos()
                g.b(f"goto {x + 24 * (1 if stuck % 2 else -1)} {z + 24}")
                g.sleep(15)
                g.stop()
            last_progress = time.time()
        g.sleep(1)
    g.stop()
    if blocks == "logs" and not g.is_night():
        # The leaves of the trees just cut drop saplings as they decay:
        # wait a little and pick them up for the tree farm (the trees
        # around the base were all gone and nobody had saplings).
        g.status = "picking up saplings"
        g.sleep(20)
        pick_up_drops(g, g.pos(), 8)
    return True


# Materials a recipe is missing: with the team's facts (danger spots, the
# mine, the tree room) and never after dark - a harvester needing logs for
# a hoe explored into the night and drowned.
crafting.gather_hook = lambda g, blocks, item, n: gather(
    g, getattr(g, "facts", None) or {}, blocks, item, n, until_dark=True)


ARMOR = ("_helmet", "_chestplate", "_leggings", "_boots")


def craft(g, facts, item, count=1):
    ok = crafting.craft(g, item, count)
    if ok and item == "shield":
        g.equip_offhand("shield")
    elif ok and item.endswith(ARMOR):
        g.sleep(1)  # give Meteor's Auto Armor a moment first
        g.equip_armor(item)
    return ok


def spare_from_pen(e, facts):
    """Wild animals are fair game; penned ones only beyond the breeding
    stock (adults only: a calf drops nothing)."""
    pen = facts.get("pen")
    if not pen or not in_pen(e.position, pen):
        return True
    kind = e.type.split(".")[-1].split(":")[-1]
    adults = penned(pen, kind) if kind in PEN_KINDS else []
    return len(adults) > PEN_CULL and any(a.uuid == e.uuid for a in adults)


def hunt(g, facts, count=6, timeout=600, animals=ANIMALS, item="food", fed=None):
    """Kill animals until `count` items matching `item` are held. Animals
    near home get used up (they do not respawn), so hunters range twice
    as far as other workers."""
    food = matcher(item)
    end, misses, visited = time.time() + timeout, 0, set()
    escaped = set()  # animals we chased and could not kill (on a cliff, fenced)
    while g.count(food) < count and time.time() < end:
        if fed and g.hunger() >= fed:
            return True  # an emergency meal is eaten as caught: stop once full
        come_home(g, facts, leash=2 * LEASH)
        avoid_danger(g, facts)
        g.status = f"hunting {animals} for {item} {g.count(food)}/{count}"
        prey = [e for e in m.entities(type=f".*\\.({animals})$", max_distance=60,
                                      sort="nearest", limit=8)
                if (e.position[1] > 55 or e.position[1] > g.pos()[1] - 4)
                and e.uuid not in escaped and spare_from_pen(e, facts)
                # (a hunter chased a swimming animal into a lake and drowned)
                and g.block(*[math.floor(v) for v in e.position]) != "water"
                and not near_danger(facts, e.position)][:1]
        if not prey:
            misses += 1
            if misses > 20:
                break
            # Teammates report the animals they pass: head for the nearest
            # sighting of what we want before wandering blind.
            kinds = animals.split("|")
            home = facts.get("base") or g.pos()
            spots = [a for a in facts.get("_animals", [])
                     if a[0] in kinds and math.dist(a[1:], home) < 2 * LEASH
                     and a[2] > 55
                     and tuple(a[1:]) not in visited
                     # (Void1 went after a sighting by the lake where two
                     # teammates had drowned, and drowned there too)
                     and not near_danger(facts, a[1:])]
            if spots:
                a = min(spots, key=lambda a: math.dist(a[1:], g.pos()))
                visited.add(tuple(a[1:]))
                g.status = f"heading for a {a[0]} seen at {a[1:]}"
                g.goto_near(*a[1:], radius=6, timeout=90)
                continue
            explore(g, facts, 30)
            continue
        target, last_goto, chase_end = prey[0], 0, time.time() + 45
        pos = target.position
        g.select(lambda i: i.endswith(("_sword", "_axe")))
        g.aura_targets(animals.split("|"))  # Kill Aura times the swings
        while time.time() < chase_end:
            cur = m.entities(uuid=target.uuid, max_distance=80)
            if not cur:
                break  # dead (or gone)
            pos = cur[0].position
            if g.mc.player.getAirSupply() < 200:
                escaped.add(target.uuid)  # out of breath: let it go
                break
            if math.dist(g.pos(), pos) > 2.5:
                # A hit animal panics and runs: re-aim the path often.
                if time.time() - last_goto > 0.8:
                    g.b("goto %d %d %d" % tuple(math.floor(v) for v in pos))
                    last_goto = time.time()
                g.sleep(0.25)
            else:
                g.b("stop")
                g.sleep(0.4)
        g.aura_targets([])
        if m.entities(uuid=target.uuid, max_distance=80):
            # Still alive after the whole chase: unreachable. (All three
            # bots spent a morning circling one pig on a ledge.)
            escaped.add(target.uuid)
            continue
        g.goto(*[math.floor(v) for v in pos], timeout=15, near=1)  # drops
        g.sleep(1)
    g.aura_targets([])
    g.stop()
    return g.count(food) >= count


# ---------------------------------------------------------------------------
# Shared storage: the base chest. What is in a chest survives deaths, and
# any bot can take from it, so it is how the team pools iron and loot.

VALUABLES = ("raw_iron", "iron_ingot", "raw_gold", "gold_ingot", "diamond",
             "redstone", "lapis_lazuli", "emerald", "string", "leather",
             "ender_pearl", "blaze_rod", "obsidian", "book", "feather", "ender_eye")


def open_chest(g, facts):
    x, y, z = facts["base"]
    if math.dist(g.pos(), (x, y, z)) > 6:
        g.goto(x, y, z, timeout=240, near=1)
    if not g.open_block("chest", "Chest"):
        return False
    g.sync_menu()
    return True


def chest_contents(g):
    out = {}
    menu = g.menu()
    for i in range(27):
        st = menu.getSlot(i).getItem()
        if not st.isEmpty():
            n, item = str(st.toString()).split(" ", 1)
            out[short(item)] = out.get(short(item), 0) + int(n)
    return out


def store(g, facts):
    """Put valuables in the base chest (and report what it now holds)."""
    g.status = "storing valuables"
    if not open_chest(g, facts):
        return False
    for st in m.player_inventory():
        if st.slot < 36 and short(st.item) in VALUABLES:
            g.click(g.menu_slot(st.slot, 27), 0, "QUICK_MOVE")
    time.sleep(0.4)
    contents = chest_contents(g)
    g.close_screen()
    g.log(f"stored valuables; chest holds {contents}")
    seal_base(g, facts)  # we are home anyway: close the holes bots dug
    return {"storage": contents}


def withdraw(g, facts, item, n):
    """Take n of an item out of the base chest (whole stacks for big n)."""
    if not open_chest(g, facts):
        return None
    menu, got = g.menu(), 0
    for i in range(27):
        need = n - g.count(lambda j: j == item)
        if need <= 0:
            break
        st = menu.getSlot(i).getItem()
        if st.isEmpty() or short(str(st.toString()).split(" ", 1)[1]) != item:
            continue
        free = sorted(set(range(36)) - {s.slot for s in m.player_inventory()})
        if need >= st.getCount() or need > 16 or not free:
            g.click(i, 0, "QUICK_MOVE")  # the whole stack
        else:
            # Just what we need: a whole stack left the rest stranded in
            # one bot's pockets (22 leather when a chestplate needed 8).
            g.click(i)                                  # stack onto the cursor
            for _ in range(need):
                g.click(g.menu_slot(free[0], 27), 1)    # right-click: drop one
            g.click(i)                                  # the rest back
        time.sleep(0.2)
    contents = chest_contents(g)
    g.close_screen()
    g.pending_facts["storage"] = contents  # (a stale record kept re-sending bots)
    return contents


def craft_from_storage(g, facts, item, material, cost):
    """Collect the material from the base chest, craft, put the rest back."""
    g.status = f"fetching {material} to craft {item}"
    contents = withdraw(g, facts, material, cost)
    if contents is None or g.count(lambda j: j == material) < cost:
        if contents is not None:
            g.pending_facts["storage"] = contents  # still correct the record
        return False
    ok = craft(g, facts, item)
    stored = store(g, facts)
    return stored if ok else False


def smelt_storage(g, facts, raw="raw_iron", out="iron_ingot"):
    """Smelt the raw ore waiting in the chest and store the ingots."""
    contents = withdraw(g, facts, raw, 64)
    if contents is None:
        return False
    if g.count(lambda j: j == raw):
        smelt(g, facts, raw, out, 64)
    return store(g, facts)


def shear(g, facts, timeout=300):
    """Shear sheep until we hold three wool of one colour. Unlike killing,
    this keeps the flock (wool grows back), so beds stop depending on
    finding new sheep."""
    wool = lambda: max((n for i, n in g.inv().items() if i.endswith("_wool")),
                       default=0)
    end, tried = time.time() + timeout, set()
    while wool() < 3 and time.time() < end:
        g.status = f"shearing sheep ({wool()}/3 wool)"
        flock = [e for e in m.entities(type=".*\\.sheep$", max_distance=48,
                                       sort="nearest", nbt=True)
                 if e.uuid not in tried and "Sheared:1b" not in (e.nbt or "")
                 and "Age:-" not in (e.nbt or "")]
        if not flock:
            # nothing woolly in sight: head for a reported sheep
            spots = [a for a in facts.get("_animals", []) if a[0] == "sheep"]
            if not spots:
                explore(g, facts, 30)
                continue
            a = min(spots, key=lambda a: math.dist(a[1:], g.pos()))
            g.goto_near(*a[1:], radius=6, timeout=90)
            tried.clear()
            continue
        sheep = flock[0]
        tried.add(sheep.uuid)
        g.select(lambda i: i == "shears")
        if g.use_on_entity(sheep, height=0.8):
            x, y, z = sheep.position
            g.sleep(0.5)
            g.goto_near(math.floor(x), math.floor(y), math.floor(z), radius=0,
                        timeout=8)  # walk over the dropped wool
            g.sleep(1)
    g.stop()
    return wool() >= 3


def shelter(g, facts):
    """Sit out the night. With a base: go there and sleep in our bed (which
    skips the night once enough of the team is in bed). Without one: dig a
    3-deep hole and seal it."""
    g.status = "sheltering for the night"
    base = facts.get("base")
    # At dusk there is time to come home from further out.
    # Before nightfall there is time to come home from far out; once it is
    # dark, walking more than a short way home got bots killed by zombies on
    # the way, so they dig in where they are instead.
    reach = 2 * LEASH if not g.is_night() else 48
    if base and math.dist(g.pos(), base) < reach \
            and g.goto(*base, timeout=300, near=2):
        seal_base(g, facts)
        if any(short(st.item) in VALUABLES for st in m.player_inventory()):
            result = store(g, facts)
            if result:
                g.pending_facts.update(result)
        bed = facts.get(f"bed_{g.name}")
        if bed and not g.block(*bed).endswith("_bed"):
            # Our bed is gone (broken while pathing, before the guard
            # existed): tell the coordinator so the bed task comes back.
            g.log(f"bed at {bed} is missing")
            g.lost.append("bed")
            bed = None
        last_try = 0

        def morning():
            nonlocal last_try
            if bed and not g.sleeping() and time.time() - last_try > 8 \
                    and not g.hostiles(8):
                last_try = time.time()
                g.status = "going to sleep"
                g.stop()  # Baritone must not be steering the camera
                if math.dist(g.pos(), (bed[0] + .5, bed[1], bed[2] + .5)) > 3:
                    g.goto_near(*bed, radius=1, timeout=20)
                if g.aim(bed):  # crosshair really on the bed
                    m.player_press_use(True)
                    time.sleep(0.3)
                    m.player_press_use(False)
                    time.sleep(1)
                g.log(f"sleep attempt: sleeping={g.sleeping()} "
                      f"time={m.world_info().day_ticks % 24000}")
            return not g.is_night() and not g.is_dusk()
        g.wait(morning, 900, 2)
    else:
        seal_in(g)
        g.wait(lambda: not g.is_night(), 700, 2)
    return True


def dry_spot(g, radius=8):
    """A column near us where digging 3 down will not let water or lava in:
    solid all the way down and no liquid next to it."""
    x, y, z = g.block_pos()
    wet = ("water", "lava", "bubble_column", "seagrass", "tall_seagrass", "kelp",
           "kelp_plant")
    best = None
    for cx in range(x - radius, x + radius + 1):
        for cz in range(z - radius, z + radius + 1):
            d = math.dist((cx, cz), (x, z))
            if best and d >= best[0]:
                continue
            col = [(cx, y - k, cz) for k in range(1, 5)]
            around = [(cx + dx, y - k, cz + dz) for k in range(0, 5)
                      for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            kinds = m.getblocklist([list(c) for c in col + around + [(cx, y, cz)]])
            kinds = [short(k) for k in kinds]
            if any(k in wet for k in kinds):
                continue
            if any(k in AIR for k in kinds[:len(col)]):
                continue  # cave or overhang below
            best = (d, (cx, y, cz))
    return best and best[1]


def seal_in(g):
    """Dig three blocks down (somewhere dry) and close the hole above."""
    spot = dry_spot(g)
    if spot is None:
        g.log("no dry spot to dig in nearby, staying put")
        return
    if spot[::2] != tuple(g.block_pos())[::2]:
        g.goto(*spot, timeout=30)
    x, y, z = g.block_pos()
    g.goto(x, y - 3, z, timeout=60)
    x, y, z = g.block_pos()
    solid = lambda i: (i in ("dirt", "cobblestone", "cobbled_deepslate",
                             "netherrack", "andesite", "diorite", "granite",
                             "tuff", "sandstone")
                       or i.endswith("_planks"))
    if g.block(x, y + 2, z) == "air" and g.select(solid):
        # Click the side of a wall block above head height.
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if g.block(x + dx, y + 2, z + dz) != "air":
                g.use_on(x + 0.5 + dx * 0.5, y + 2.5, z + 0.5 + dz * 0.5)
                break


def dig_in(g, facts):
    """Badly hurt with mobs about: hide underground, eat, heal up."""
    g.status = "hurt - digging in to recover"
    if not g.in_rooms(g.pos()):
        seal_in(g)  # (in the base already: no digging - the guard forbids it
        #              and the bot stood stuck in the doorway for minutes)
    g.wait(lambda: g.health() <= 0 or (g.health() >= 16 and not g.hostiles(10)),
           240, 2)
    return True


def recover(g, facts, pos):
    """Walk back to where we died and pick the dropped items up."""
    g.status = f"recovering gear at {pos}"
    x, y, z = pos
    g.b(f"goto {x} {y} {z}")
    g.sleep(2)  # (Baritone needs a moment to start pathing)
    end = time.time() + 200
    while time.time() < end and math.dist(g.pos(), pos) > 1.5 and g.busy():
        g.sleep(1)
        if math.dist(g.pos(), pos) < 24:
            cells = [[x + a, y + b, z + c] for a in (-2, 0, 2) for b in (-1, 0, 1)
                     for c in (-2, 0, 2)]
            if sum(short(k) == "water" for k in m.getblocklist(cells)) >= 3:
                # Gear in the water: the Drowned there killed Void2 a second
                # time when it came back for it.
                g.stop()
                g.log(f"recover: the drop at {pos} is in water; leaving it")
                return True
            guard = [e for e in g.hostiles(48, visible=False)
                     if math.dist(e.position, pos) < 20]  # (pillagers shoot from afar)
            if guard:
                # The mob that killed us is still there: Void3 died twice
                # to the same Drowned walking back for its things.
                g.stop()
                g.log(f"recover: a {guard[0].type.rsplit('.', 1)[-1]} waits by "
                      f"the drop at {pos}; leaving the gear")
                return True
    g.stop()
    g.goto_near(*pos, radius=1, timeout=20)
    g.sleep(4)
    return True


def build_base(g, facts, depth=8, size=5):
    """Dig a room `depth` blocks below the surface and furnish it."""
    for spec, n in (("crafting_table", 1), ("stone", 8), ("planks", 8)):
        if not crafting.ensure(g, spec, n):
            return False
    # Build close to world spawn: that is where bots reappear after dying.
    spawn = m.world_info().spawn
    if math.dist(g.pos()[::2], spawn[::2]) > 48:
        g.status = "walking to base site"
        g.b(f"goto {spawn[0] + 12} {spawn[2] + 12}")
        g.sleep(2)
        g.wait(lambda: not g.busy(), 600, 2)
        g.stop()
    x, y, z = g.block_pos()
    floor, r = max(y - depth, 12), size // 2
    g.status = "digging base"
    g.b(f"sel 1 {x - r} {floor} {z - r}")
    g.b(f"sel 2 {x + r} {floor + 2} {z + r}")
    g.dig_ok = True  # digging the base on purpose (see Game.base_guard)
    g.b("sel cleararea")
    g.sleep(3)
    g.wait(lambda: not g.baritone.getBuilderProcess().isActive(), 900, 2)
    g.dig_ok = False
    g.b("sel clear")
    g.stop()
    if not g.goto(x, floor, z, timeout=120, near=1):
        g.log("could not reach base room")
        return False
    g.status = "furnishing base"
    # Fixed layout (room is x-2..x+2, z-2..z+2):
    #   north row z-2: crafting table, furnace, chest
    #   row z:         torches on the east/west walls (light_base)
    #   rows z+1/z+2:  beds, foot at z+1, head at z+2
    for item, dx in (("crafting_table", -1), ("furnace", 0), ("chest", 1)):
        if g.count(lambda i: i == item) < 1 and not crafting.craft(g, item):
            g.log(f"base: could not craft {item}")
            continue
        if not g.place_at(item, x + dx, floor, z - 2, stand=(x, floor, z)):
            g.place_block(item)
    fix_base(g, {"base": [x, floor, z]})
    return {"base": [x, floor, z]}


def light_base(g, facts, torches=4):
    """Light the base so no mobs spawn inside it."""
    x, y, z = facts["base"]
    if g.count(lambda i: i == "torch") < 2:
        if not g.count(CLASSES["coal"]) and \
                not gather(g, facts, "coal", "coal|charcoal", 1, timeout=600):
            return False
        if not crafting.craft(g, "torch"):
            return False
    if not g.goto(x, y, z, timeout=240, near=1):
        return False
    fix_base(g, facts)
    g.status = "lighting the base"
    lit = [g.place_at("torch", x + dx, y, z, stand=(x, y, z)) for dx in (-2, 2)]
    return {"base_lit": True} if any(lit) else False


AIR = ("air", "cave_air")
FURNITURE = (("crafting_table", -1), ("furnace", 0), ("chest", 1))


def clear_clutter(g, facts):
    """Baritone pillars and bridges with dirt/cobble and leaves it behind:
    the room filled up with scaffolding that blocked walking, beds and the
    view of holes in the ceiling that needed sealing."""
    x, y, z = facts["base"]
    clutter = [(x + dx, py, z + dz) for dx in range(-2, 3) for dz in range(-2, 3)
               for py in (y, y + 1, y + 2)]
    clutter += [(x - 3, py, z) for py in (y, y + 1, y + 2)]   # farm doorway
    if facts.get("farm_room"):  # above the soil only
        (x0, x1), (z0, z1) = farm_xz(facts["base"])
        clutter += [(cx, py, cz) for cx in range(x0, x1 + 1)
                    for cz in range(z0, z1 + 1) for py in (y + 1, y + 2)]
    kinds = dict(zip(clutter, (short(k) for k in
                               m.getblocklist([list(c) for c in clutter]))))
    clutter = [c for c in clutter if kinds[c] in CLUTTER
               or kinds[c].endswith(("_planks", "_log", "_wood"))]
    # the step up to the (raised) door is not clutter
    inside = g.base_door()
    if inside and inside[1] > y:
        clutter = [c for c in clutter if c != (inside[0], y, inside[2])]
    while clutter:
        c = min(clutter, key=lambda c: math.dist(c, g.pos()))
        clutter.remove(c)
        g.status = f"clearing scaffolding from the base ({len(clutter)} left)"
        if math.dist(g.pos(), (c[0] + .5, c[1] + .5, c[2] + .5)) > 4:
            g.goto_near(c[0], y, c[2], radius=2, timeout=20)
        g.stop()
        g.select(lambda i: i.endswith("_pickaxe"))
        g.break_block(*c, timeout=6)


def restore_door_step(g, facts):
    """The block in front of the raised door is what lets bots walk level
    into it; when it went missing the whole team stood jammed at the door
    (Baritone will not jump up into a door's block)."""
    x, y, z = facts["base"]
    inside = g.base_door()
    if not inside or inside[1] <= y:
        return
    step = (inside[0], y, inside[2])
    if g.block(*step) not in AIR:
        return
    planks = next((s.item for s in m.player_inventory()
                   if short(s.item).endswith("_planks")), None)
    if planks is None and not (g.count(CLASSES["log"]) and crafting.craft(g, "planks")):
        return  # (no wood on us: no trip out for it from inside the base)
    planks = planks and short(planks) or next(
        i for i in g.inv() if i.endswith("_planks"))
    if g.place_against(planks, *step):
        g.log(f"base: put the door step back at {list(step)}")


def fix_base(g, facts):
    """Put back missing base furniture. (Pits in the floor, from Baritone
    digging down or coal being mined out, are left alone: they are harmless
    and bed placement skips cells without a floor.)"""
    x, y, z = facts["base"]
    g.status = "repairing base"
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            for py in (y - 2, y - 1):  # a pit's bottom first
                if g.block(x + dx, py, z + dz) not in AIR:
                    continue
                filler = max(FILLERS, key=lambda i: g.count(lambda j, i=i: j == i))
                if not g.count(lambda j: j == filler):
                    break
                g.place_against(filler, x + dx, py, z + dz)
    clear_clutter(g, facts)
    for item, dx in FURNITURE:
        if g.block(x + dx, y, z - 2) == item or g.find_block(item, 4):
            continue
        if g.count(lambda i: i == item) or crafting.craft(g, item):
            g.place_at(item, x + dx, y, z - 2, stand=(x, y, z))


def seal_filler(g):
    """Block to patch the base with: planks first (Baritone may not break
    them), then the most plentiful stone-like block, dirt last (the farm
    needs it)."""
    planks = [s.item for s in m.player_inventory() if short(s.item).endswith("_planks")]
    if planks:
        return short(planks[0])
    return max(FILLERS, key=lambda i: (g.count(lambda j, i=i: j == i) > 0 and i != "dirt",
                                       g.count(lambda j, i=i: j == i)))


FILLERS = ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite",
           "granite", "tuff", "netherrack")
CLUTTER = FILLERS + ("gravel", "sand", "stone", "deepslate", "grass_block")


TREE_FARM = {"on": False}  # set from the team's facts (brain.report)


def tree_layout(base):
    """The tree room north of the base, a level below it: a 7x7 room with
    the walking floor at base-7 and air up to base-2 (all within reach
    from the floor: Baritone may not dig inside the base, so it is dug by
    hand), four saplings on dirt set into the floor, torches in the
    corners and the middle, and a 1-wide stair down from the base room's
    north wall. (The ground over the base is only 5-7 blocks up: no room
    for trees at the base's own level.)"""
    x, y, z = base
    fy = y - 7                                   # walking level (feet)
    # Column by column outward from the corridor, each top to bottom: from
    # the floor already cleared, the next column is in reach all the way up.
    sx0 = x + 2  # the stair column: x and its neighbours are the furniture
    cols = sorted(((cx, cz) for cz in range(z - 11, z - 18, -1)
                   for cx in range(x - 3, x + 4)),
                  key=lambda c: (abs(c[0] - sx0) + abs(c[1] - (z - 10)), c))
    room = [(cx, cy, cz) for cx, cz in cols for cy in range(y - 2, fy - 1, -1)]
    stairs = [(sx0, y - k, z - 2 - k) for k in range(1, 8)]   # stand cells
    entry = [(sx0, y + 1, z - 2), (sx0, y, z - 2)]  # the base room in front of it
    stair_cells = entry + [(sx, sy + d, sz) for sx, sy, sz in stairs for d in (2, 1, 0)]
    corridor = [(sx0, fy + d, z - 10) for d in (2, 1, 0)]
    spots = [(x - 2, fy, z - 16), (x + 2, fy, z - 16), (x - 2, fy, z - 12), (x + 2, fy, z - 12)]
    torches = [(x - 3, fy, z - 17), (x + 3, fy, z - 17), (x - 3, fy, z - 11),
               (x + 3, fy, z - 11), (x, fy, z - 14)]
    return {"room": room, "stairs": stairs, "dig": stair_cells + corridor + room,
            "spots": spots, "torches": torches, "centre": (x, fy, z - 14)}


def base_interior(base, farm_room=False):
    """Cells of the rooms themselves: the base room, and the farm room and
    its doorway west of it."""
    x, y, z = base
    cells = {(cx, cy, cz) for cx in range(x - 2, x + 3) for cz in range(z - 2, z + 3)
             for cy in range(y, y + 3)}
    if farm_room:
        (x0, x1), (z0, z1) = farm_xz(base)
        cells |= {(cx, cy, cz) for cx in range(x0, x1 + 1)
                  for cz in range(z0, z1 + 1) for cy in range(y, y + 3)}
        cells |= {(x - 3, cy, z) for cy in range(y, y + 3)}
    if TREE_FARM["on"]:
        cells |= set(tree_layout(base)["dig"])
    return cells


def base_shell(g, base, farm_room=False):
    """Air cells in the one-block shell around the rooms (walls, floor,
    ceiling), skipping the door. Nearest-to-the-floor first."""
    x, y, z = base
    inside = base_interior(base, farm_room)
    # face neighbours only: nothing gets through a diagonal gap, and edge
    # cells cannot be reached from inside anyway
    shell = {(cx + dx, cy + dy, cz + dz) for cx, cy, cz in inside
             for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                                (0, 0, 1), (0, 0, -1))} - inside
    shell = sorted(shell, key=lambda c: (c[1], c[0], c[2]))
    kinds = m.getblocklist([list(c) for c in shell])
    if any(k is None or "void_air" in k for k in kinds):
        raise ValueError("base not loaded")
    below = m.getblocklist([[c[0], c[1] - 1, c[2]] for c in shell])
    # The air above the door stays open: stepping down from the stairs
    # through the doorway needs the head room (Baritone dug it out again).
    # The block above that head room closes it instead.
    headroom = [c for c, k, b in zip(shell, kinds, below)
                if "_door" in (b or "") and "_door" not in (k or "")]
    caps = [(c[0], c[1] + 1, c[2]) for c in headroom]
    cap_kinds = m.getblocklist([list(c) for c in caps]) if caps else []
    holes = [c for c, k in zip(shell, kinds)
             if short(k) in AIR + ("water", "lava") and "door" not in k
             and c not in headroom]
    # An air pocket walled in on all six sides leaks nowhere (and has no
    # face to click): leave it.
    def open_side(c):
        nb = [[c[0] + d[0], c[1] + d[1], c[2] + d[2]] for d in
              ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))]
        return any(short(k) in AIR + ("water", "lava") for k in m.getblocklist(nb))
    holes = [c for c in holes if open_side(c)]
    # Only gaps the rooms' air reaches: one behind the crafting table is
    # shut off by the table (and cannot be clicked from inside anyway).
    def touches_room(c):
        nb = [(c[0] + d[0], c[1] + d[1], c[2] + d[2]) for d in
              ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))]
        nb = [p for p in nb if p in inside]
        return any(short(k) in AIR for k in m.getblocklist([list(p) for p in nb])) if nb else False
    holes = [c for c in holes if touches_room(c) or c in caps]
    return holes + [c for c, k in zip(caps, cap_kinds) if short(k) in AIR]


def scaffold_place(g, filler, c, base, farm_room):
    """Fill shell cell `c` whose only solid neighbour faces away from us:
    put a temporary block in the room cell next to it, place against that,
    then take the temporary block out again."""
    inside = base_interior(base, farm_room)
    # sideways only: under a ceiling hole the helper hides the face we need
    for d in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
        t = (c[0] + d[0], c[1] + d[1], c[2] + d[2])
        if t not in inside or g.block(*t) not in AIR or t[1] == base[1]:
            continue
        if not g.place_against(filler, *t, stand=(t[0], base[1], t[2])):
            continue
        ok = g.place_against(filler, *c, stand=(t[0], base[1], t[2]))
        g.select(lambda i: i.endswith("_pickaxe"))
        g.break_block(*t, timeout=6)
        if ok:
            return True
    return False


def inside_spot(base, c, facts):
    """A floor cell inside the rooms close to `c`, to place it from."""
    inside = [p for p in base_interior(base, bool(facts.get("farm_room")))
              if p[1] == base[1]]
    return min(inside, key=lambda p: math.dist(p, c))


def base_openings(g, base, farm_room=False):
    """Air cells of the base's enclosure that connect to the outside: flood
    fill the air from the room centre inside a box one block larger than
    the room (plus head room), and report cells with air beyond the box."""
    x, y, z = base
    lo, hi = (x - 3, y - 2, z - 3), (x + 3, y + 4, z + 3)  # closed floor pits are fine
    if farm_room:  # the farm room (and its doorway) west of the base
        lo = (x - 11, y - 2, z - 4)
        hi = (hi[0], hi[1], z + 4)
    if TREE_FARM["on"]:  # the tree room and its stair, north and below
        lo = (min(lo[0], x - 4), y - 9, z - 18)
    # (get_block_region returns empty entries away from the player;
    # getblocklist reads the same positions reliably in one call.)
    cells = [(cx, cy, cz) for cx in range(lo[0] - 1, hi[0] + 2)
             for cy in range(lo[1] - 1, hi[1] + 2)
             for cz in range(lo[2] - 1, hi[2] + 2)]
    kinds = {}
    for i in range(0, len(cells), 4096):  # (Minescript caps one request)
        part = cells[i:i + 4096]
        kinds.update(zip(part, m.getblocklist([list(c) for c in part])))
    if any(k is None or "void_air" in k for k in kinds.values()):
        raise ValueError("base not loaded")  # unknown, not "sealed"
    # (the head room kept open above the door counts as closed)
    air = lambda c: short(kinds.get(c)) in AIR and \
        "_door" not in (kinds.get((c[0], c[1] - 1, c[2])) or "")
    inside = lambda c: all(lo[i] <= c[i] <= hi[i] for i in range(3))
    seen, todo, leaks = {(x, y, z)}, [(x, y, z)], []
    while todo:
        c = todo.pop()
        for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            n = (c[0] + d[0], c[1] + d[1], c[2] + d[2])
            if not air(n):
                continue
            if not inside(n):
                if c not in leaks and c[1] >= y:
                    leaks.append(c)
            elif n not in seen:
                seen.add(n)
                todo.append(n)
    return leaks


def seal_base(g, facts):
    """Close every opening between the base and the outside (our own
    tunnels and shafts) so mobs cannot get in at night. Baritone digs a new
    way out in the morning; whoever shelters next seals it again."""
    x, y, z = facts["base"]
    filler_count = lambda: max([g.count(lambda j, i=i: j == i) for i in FILLERS]
                               + [g.count(lambda j: j.endswith("_planks"))])
    # Go there first: from far away the base's chunks are not loaded and it
    # looked sealed (a bot 200 blocks out reported 0 openings).
    if math.dist(g.pos(), (x, y, z)) > 10 and not g.goto(x, y, z, timeout=300, near=1):
        return False
    clear_clutter(g, facts)  # scaffolding hid the holes from view
    restore_door_step(g, facts)
    # Rebuild the whole shell (floor, walls, ceiling) around the rooms:
    # Baritone had tunnelled through it in a dozen places, and patching only
    # the holes the flood fill found never caught up.
    farm = bool(facts.get("farm_room"))
    holes = base_shell(g, (x, y, z), farm)
    planks = g.count(lambda i: i.endswith("_planks"))
    if holes and planks < len(holes) and g.count(CLASSES["log"]):
        # Seal with planks: Baritone never breaks them (they are on its
        # do-not-break list), so a patched hole cannot be dug open again,
        # from inside or out.
        crafting.craft(g, "planks", min(g.count(CLASSES["log"]),
                                        (len(holes) - planks + 3) // 4))
    for _ in range(3):
        holes = base_shell(g, (x, y, z), farm)
        if not holes or filler_count() == 0:
            break
        for i, c in enumerate(holes):
            if filler_count() == 0:
                g.log("not enough blocks to seal the base")
                break
            g.status = f"rebuilding the base walls ({len(holes) - i} holes)"
            filler = seal_filler(g)
            # Work from inside: walking round to a hole from outside took a
            # bot over the surface at nightfall, and a zombie got it.
            # (A temporary helper block next to the hole never worked from
            # inside - the face it offers points away from us - and it hid
            # the hole from view; place_against jumps for head-height gaps.)
            stand = inside_spot((x, y, z), c, facts)
            if g.block(*stand) not in AIR:
                # (in the farm room the floor cell is the soil itself:
                # stand on top of it - the bot never got under the hole)
                stand = (stand[0], stand[1] + 1, stand[2])
            g.place_against(filler, *c, stand=stand)
    # (With the shell whole, the flood fill only confirms it.)
    left = base_openings(g, (x, y, z), bool(facts.get("farm_room")))
    g.log(f"base sealing: {len(left)} openings left")
    g.pending_facts["base_open"] = len(left)
    return not left


def surface_y(g, x, z, start):
    """Height of the topmost solid block in a column (scanning down)."""
    ys = list(range(start + 40, start - 1, -1))
    kinds = m.getblocklist([[x, yy, z] for yy in ys])
    for yy, k in zip(ys, kinds):
        k = short(k)
        if k not in AIR and k not in ("short_grass", "tall_grass", "snow") \
                and not k.endswith(("_leaves", "_log")):
            return yy
    return start


def build_exit(g, facts):
    """Give the base a walkable way out: a door in the east wall and a
    staircase up to the surface. Climbing our own vertical shafts needs
    block placing (pillaring) and kept getting bots stuck; stairs need
    nothing, and the door keeps mobs out (zombies cannot open it)."""
    x, y, z = facts["base"]
    dz = z - 1
    g.status = "digging the base stairs"
    if not g.goto(x, y, z, timeout=120, near=1):
        return False
    # Column k=1 is the doorway (level with the floor); each next column is
    # one higher, cleared 3 high so there is head room to step up.
    columns, k = [], 1
    while k < 40:
        cx = x + 2 + k
        stand = y if k == 1 else y + k - 1
        columns.append((cx, stand))
        if k > 1 and stand > surface_y(g, cx, dz, y):
            break
        k += 1
    g.b("sel clear")
    for cx, stand in columns:
        g.b(f"sel 1 {cx} {stand} {dz}")
        g.b(f"sel 2 {cx} {stand + (1 if cx == x + 3 else 2)} {dz}")
    g.dig_ok = True  # digging the base on purpose (see Game.base_guard)
    g.b("sel cleararea")
    g.sleep(3)
    g.wait(lambda: not g.baritone.getBuilderProcess().isActive(), 600, 2)
    g.dig_ok = False
    g.b("sel clear")
    g.stop()
    g.goto(x, y, z, timeout=120, near=1)
    door = next((i for i in g.inv() if i.endswith("_door")), None)
    if door is None:
        wood = max((i for i in g.inv() if i.endswith("_planks")),
                   key=lambda i: g.inv()[i], default=None)
        if wood is None or g.inv()[wood] < 6:
            log = max((i for i in g.inv() if CLASSES["log"](i)),
                      key=lambda i: g.inv()[i], default=None)
            if log is None and not gather(g, facts, "logs", "log", 2, timeout=300):
                return False
            crafting.craft(g, "planks", 2)
            wood = max((i for i in g.inv() if i.endswith("_planks")),
                       key=lambda i: g.inv()[i])
        door = wood.replace("_planks", "_door")
        if not crafting.craft(g, door):
            return False
    if not g.place_at(door, x + 3, y, dz, stand=(x + 2, y, dz)):
        g.log("could not hang the base door")
    top = columns[-1]
    g.log(f"base exit: door at {[x + 3, y, dz]}, stairs to {[top[0], top[1], dz]}")
    return {"base_exit": [top[0], top[1], dz]}


BED_SLOTS = (0, -1, 1, -2, 2)


def bed(g, facts):
    """Get a bed (wool from sheep), put it in the base and sleep-click it
    once so it becomes our respawn point."""
    x, y, z = facts["base"]
    wool = lambda: max((n for i, n in g.inv().items() if i.endswith("_wool")),
                       default=0)
    while not g.count(CLASSES["bed"]) and wool() < 3:
        if g.count(lambda i: i == "shears"):
            if not shear(g, facts, timeout=480):
                return False
        elif not hunt(g, facts, count=g.count(CLASSES["wool"]) + 1,
                      animals="sheep", item="wool", timeout=480):
            return False  # no sheep found this time
    # Craft at the base: it has a crafting table, and we may be standing
    # somewhere with no room to put one down (e.g. a night-time hole).
    g.status = "taking wool home for a bed"
    if not g.goto(x, y, z, timeout=400, near=1):
        return False
    fix_base(g, facts)
    if not g.count(CLASSES["bed"]):
        colour = max((i for i in g.inv() if i.endswith("_wool")),
                     key=lambda i: g.inv()[i])[:-len("_wool")]
        if not crafting.craft(g, f"{colour}_bed"):
            return False
    bed_item = next(i for i in g.inv() if i.endswith("_bed"))
    g.status = "placing bed in base"
    floor_ok = lambda c: g.block(c[0], y - 1, c[2]) not in AIR
    free = lambda c: g.block(*c) in AIR and floor_ok(c)
    inside = lambda c: abs(c[0] - x) <= 2 and abs(c[2] - z) <= 2
    # Preferred slots first (south-facing row), then any free pair of cells:
    # the bed's head goes where we face, so we stand behind its foot.
    slots = [((x + dx, y, z + 1), (0, 1)) for dx in BED_SLOTS]
    slots += [((x + dx, y, z + dz), d) for dx in range(-2, 3) for dz in range(-2, 3)
              for d in ((0, 1), (0, -1), (1, 0), (-1, 0))]
    for foot, (ddx, ddz) in slots:
        head = (foot[0] + ddx, y, foot[2] + ddz)
        stand = (foot[0] - ddx, y, foot[2] - ddz)
        if not all(inside(c) and free(c) for c in (foot, head, stand)):
            continue
        if (x, y, z) in (foot, head):
            continue  # keep the room centre clear: bots path to it
        g.goto(*stand, timeout=40)
        if g.block_pos()[::2] != [stand[0], stand[2]]:
            continue
        if not g.place_at(bed_item, *foot):
            continue
        g.use_on(foot[0] + 0.5, foot[1] + 0.4, foot[2] + 0.5)  # set spawn
        g.close_screen()
        g.log(f"bed placed at {list(foot)}, respawn point set")
        return {f"bed_{g.name}": list(foot)}
    g.log("no free bed slot in the base")
    return False


def smelt(g, facts, input, output, count):
    """Smelt (or cook) up to `count` items from one stack in a furnace
    (nearby, at the base, or placed). Fuel: coal if we have it, else planks."""
    stacks = [st for st in m.player_inventory()
              if st.slot < 36 and matcher(input)(short(st.item))]
    if not stacks:
        return False
    n = min(count, max(st.count for st in stacks))
    fuel = "coal|charcoal" if g.count(CLASSES["coal"]) * 8 >= n else "planks"
    if fuel == "planks" and not crafting.ensure(g, "planks", math.ceil(n / 1.5) + 1):
        return False
    g.status = f"smelting {input}"
    g.fight()
    if "base" in facts and math.dist(g.pos(), facts["base"]) < 150:
        g.goto(*facts["base"], timeout=240, near=2)
    if (not g.find_block("furnace", 6) and g.count(lambda i: i == "furnace") < 1
            and not crafting.craft(g, "furnace")):
        return False
    if not g.open_block("furnace", "urnace"):
        return False
    for spec, slot in ((input, 0), (fuel, 1)):
        stack = max((st for st in m.player_inventory()
                     if st.slot < 36 and matcher(spec)(short(st.item))),
                    key=lambda st: st.count)
        g.click(g.menu_slot(stack.slot, 3))
        g.click(slot)
        g.click(g.menu_slot(stack.slot, 3))  # leftovers back, if any
    n = min(n, g.menu().getSlot(0).getItem().getCount())
    done = lambda: g.menu().getSlot(0).getItem().isEmpty()
    # Each item takes 10 s. Do not use g.sleep here: defending would close
    # the furnace screen, so smelt somewhere safe (the base).
    end = time.time() + n * 10 + 20
    while time.time() < end and not done():
        time.sleep(2)
    for slot in (2, 0, 1):
        g.click(slot, 0, "QUICK_MOVE")
    g.close_screen()
    return g.count(matcher(output)) >= 1


def find_water(g, center, radius=8):
    """Nearest open water surface block (water with air above) to center."""
    cx, cy, cz = center
    lo, hi = [cx - radius, cy - 4, cz - radius], [cx + radius, cy + 12, cz + radius]
    region = m.get_block_region(lo, hi)
    best = None
    for x in range(lo[0], hi[0] + 1):
        for z in range(lo[2], hi[2] + 1):
            for y in range(lo[1], hi[1]):
                if short(region.get_block(x, y, z)) == "water" and \
                        short(region.get_block(x, y + 1, z)) == "air":
                    d = math.dist((x, y, z), center)
                    if best is None or d < best[0]:
                        best = (d, [x, y, z])
    return best and best[1]


def fish(g, facts, count=20, timeout=900):
    """Fish with Meteor's Auto Fish until `count` food items are held:
    endless food from any water, close to home."""
    rod = lambda i: i == "fishing_rod"
    if not g.count(rod) and g.count(lambda i: i == "string") < 2:
        withdraw(g, facts, "string", 2)  # spiders' string piles up in the chest
    if not g.count(rod) and not crafting.craft(g, "fishing_rod"):
        return False
    food = CLASSES["food"]
    home = facts.get("base") or g.block_pos()
    water = None
    farm = facts.get("farm")
    if facts.get("farm_room") and farm and g.overworld() and g.block(*farm) == "water":
        # The farm room's water hole: fishing from inside the sealed base
        # (fishers at lakes kept meeting the Drowned).
        x, y, z = facts["base"]
        g.status = "going to fish in the farm room"
        if g.goto(x - 4, y + 1, z, timeout=180, near=1):
            water = list(farm)
    water = water or find_water(g, g.block_pos(), 8)
    if not water:
        # The team's known water, or a sparse scan of the loaded chunks.
        # (Baritone's "goto water" wandered 250 blocks off into the dark
        # and the fisher died there.)
        come_home(g, facts, leash=LEASH // 2)
        spot = facts.get("water") or scan_for_water(g)
        if spot:
            g.pending_facts["water"] = spot
            g.status = "walking to the water"
            g.goto_near(*spot, radius=3, timeout=240)
            g.stop()
            water = find_water(g, g.block_pos(), 8)
    if not water:
        g.log("no water to fish in")
        return False
    g.status = "walking to the fishing spot"
    g.goto_near(*water, radius=3, timeout=120)
    m.chat(".toggle auto-fish on")
    end, last_cast = time.time() + timeout, 0
    try:
        while g.count(food) < count and time.time() < end:
            g.status = f"fishing {g.count(food)}/{count}"
            if g.mc.player.fishing is None and time.time() - last_cast > 4:
                # (Re)cast: after a fight the sword is in hand, and Auto
                # Fish only recasts its own catches.
                g.select(rod)
                m.player_look_at(water[0] + 0.5, water[1] + 0.9, water[2] + 0.5)
                g.use_on(water[0] + 0.5, water[1] + 0.9, water[2] + 0.5, hold=0.1)
                last_cast = time.time()
            g.sleep(2)
    finally:
        m.chat(".toggle auto-fish off")
    return g.count(food) >= count


def harvest(g, facts, radius=12):
    """Harvest ripe wheat in the farm room by hand, pick up the drops,
    repair and replant the plots, bake bread. (Baritone's farm mode left the
    drops on the floor and dug out soil, letting the water spread.)"""
    if not facts.get("farm_room"):
        return harvest_outdoor(g, facts, radius)
    base = facts["base"]
    x, y, z = base
    centre, corners, soil = farm_room_cells(base)
    g.status = "harvesting the farm"
    if not g.goto(x - 4, y + 1, z, timeout=180, near=1):
        if not g.goto(*base, timeout=180, near=1):
            g.log(f"harvest: could not get to the farm (at {g.block_pos()})")
            return False
    # Only plots that are dug out (open above): the rest of a room still
    # being enlarged is solid rock, and trying to soil it cost minutes.
    # (Read here, at the farm: from afar the chunks are not loaded.)
    soil = [c for c in soil if g.block(c[0], y + 1, c[2]) in AIR + ("wheat",)]
    seeds = lambda i: i == "wheat_seeds"
    ripe = [c for c in soil if (m.getblock(c[0], y + 1, c[2]) or "").startswith(
        "minecraft:wheat[age=7")]
    def ripe_at(pos):
        return (m.getblock(*pos) or "").startswith("minecraft:wheat[age=7")

    for c in ripe:
        g.status = f"harvesting the farm ({len(ripe)} ripe)"
        target = [c[0], y + 1, c[2]]
        for attempt in range(4):
            if not ripe_at(target):
                break  # (cut already, as another plot's neighbour)
            if attempt == 2 or math.dist(g.pos(), (c[0] + .5, y + 1.5, c[2] + .5)) > 3.5:
                # Across the field the crosshair lands on the plants in
                # front: from right next to it nothing is in the way.
                g.goto_near(c[0], y + 1, c[2], radius=1, timeout=20)
                g.stop()
            m.player_look_at(c[0] + 0.5, y + 1.25, c[2] + 0.5)
            time.sleep(0.25)
            hit = m.player_get_targeted_block(4.5)
            pos = list(hit.position) if hit else None
            if not pos or not (pos == target or (pos[1] == y + 1 and ripe_at(pos))):
                continue  # never swing at soil, walls or unripe wheat
            m.player_press_attack(True)
            time.sleep(0.15)  # (no background tick while attack is held)
            m.player_press_attack(False)
            time.sleep(0.2)
    pick_up_drops(g, (centre[0], y + 1, centre[2]), 5)
    # Bread first: the team may be starving while the rest (soil, seeds,
    # a new hoe) takes minutes. Some wheat stays unbaked once there is a
    # pen: it is the breeding feed.
    spare = g.count(lambda i: i == "wheat") - (6 if facts.get("pen") else 0)
    if spare >= 3:
        crafting.craft(g, "bread", spare // 3)
    if g.block(*centre) != "water":
        # without it the farmland dries out and the crops pop off
        if g.count(lambda i: i == "water_bucket") and pour_into_hole(g, centre, y):
            g.log("harvest: poured the farm's water back")
        else:
            g.log("harvest: the farm has no water")
            g.pending_facts["farm_dry"] = True
    # Soil lost to digging or a flood comes back; till and plant every plot.
    # Lost soil (a hole, or a block Baritone dropped) is put back here;
    # natural rock is a plot not yet made (farm_expand's job: soiling it
    # from here took the harvester an hour).
    ROCK = ("stone", "deepslate", "andesite", "diorite", "granite", "tuff", "calcite")
    soil = [c for c in soil if g.block(*c) not in ROCK]
    missing = [c for c in soil if g.block(*c) not in ("dirt", "farmland", "grass_block")]
    daylight = not g.is_night() and not g.is_dusk()
    if missing and g.count(lambda i: i == "dirt") < len(missing) and daylight:
        gather(g, facts, "dirt", "dirt", len(missing), timeout=300, until_dark=True)
        g.goto(x - 4, y + 1, z, timeout=180, near=1)
    # Seeds for every empty plot (grass drops them), so the farm grows to
    # its full 20 plots instead of replanting only what it harvested.
    empty = [c for c in soil if g.block(c[0], y + 1, c[2]) != "wheat"]
    if len(empty) > g.count(seeds) and daylight:
        gather(g, facts, "grass", "wheat_seeds", len(empty), timeout=300, until_dark=True)
        g.goto(x - 4, y + 1, z, timeout=180, near=1)
    laid = 0
    for c in soil:
        if laid >= 8:
            break  # (a few per harvest: soiling a whole new room took ages)
        if g.block(*c) not in ("dirt", "farmland", "grass_block") and \
                g.count(lambda i: i == "dirt"):
            laid += 1
            if g.block(*c) not in AIR + ("water",):
                # a stray block in the plot (netherrack Baritone dropped)
                if math.dist(g.pos(), (c[0] + .5, y + .5, c[2] + .5)) > 3.5:
                    g.goto_near(c[0], y + 1, c[2], radius=2, timeout=20)
                    g.stop()
                g.select(lambda i: i.endswith("_pickaxe"))
                g.break_block(*c, timeout=6)
            lay_soil(g, c, centre, (farm_xz(facts["base"])[0][0], x - 3), y)
    for c in corners:
        # Crops need light: without the corner torches every plant died.
        if g.count(lambda i: i == "torch") and g.block(*c) not in ("torch", "wall_torch"):
            if math.dist(g.pos(), (c[0] + .5, y + .5, c[2] + .5)) > 3.5:
                g.goto_near(c[0], y + 1, c[2], radius=2, timeout=20)
                g.stop()
            if g.block(*c) not in AIR:
                g.select(lambda i: i.endswith(("_pickaxe", "_shovel")))
                g.break_block(*c, timeout=6)
            g.place_at("torch", *c)
    if not g.count(lambda i: i.endswith("_hoe")):
        # no hoe, no farmland: 21 seeds sat unplanted (wooden unless we
        # carry stone: digging for it took the harvester out of the base)
        if g.count(CLASSES["stone"]) >= 2:
            crafting.craft(g, "stone_hoe")
        else:
            crafting.craft(g, "wooden_hoe")
    planted = 0
    for c in soil:
        above = g.block(c[0], y + 1, c[2])
        if above == "wheat":
            planted += 1
            continue
        if above not in AIR or not g.count(seeds):
            continue
        if math.dist(g.pos(), (c[0] + .5, y + 1, c[2] + .5)) > 3.5:
            g.goto_near(c[0], y + 1, c[2], radius=2, timeout=20)
            g.stop()
        if g.block(*c) in ("dirt", "grass_block") and \
                g.select(lambda i: i.endswith("_hoe")):
            g.use_on(c[0] + 0.5, y + 0.99, c[2] + 0.5)
        if g.block(*c) == "farmland" and g.select(seeds):
            g.use_on(c[0] + 0.5, y + 0.93, c[2] + 0.5)
            if g.block(c[0], y + 1, c[2]) == "wheat":
                planted += 1
    g.stop()
    # Some wheat stays unbaked once there is a pen: it is the breeding feed.
    spare = g.count(lambda i: i == "wheat") - (6 if facts.get("pen") else 0)
    if spare >= 3:
        crafting.craft(g, "bread", spare // 3)
    g.log(f"harvest: {len(ripe)} ripe, {planted} plots growing, "
          f"{g.count(lambda i: i == 'bread')} bread")
    return {"farm_plots": planted}


def pick_up_drops(g, centre, radius):
    """Walk over the item drops near `centre` so they get picked up."""
    for _ in range(3):
        drops = m.entities(type=".*item", position=list(centre), max_distance=radius)
        if not drops:
            return
        for e in sorted(drops, key=lambda e: math.dist(e.position, g.pos())):
            px, py, pz = e.position
            if math.dist((px, py, pz), g.pos()) > 1.2:
                g.goto_near(math.floor(px), math.floor(py), math.floor(pz),
                            radius=0, timeout=10)
        g.stop()
        g.sleep(0.5)


def harvest_outdoor(g, facts, radius=12):
    """Harvest and replant an outdoor wheat farm with Baritone."""
    farm = facts["farm"]
    g.status = "harvesting the farm"
    g.goto_near(*farm, radius=2, timeout=180)
    guard = game.BARITONE_SETTINGS["blocksToDisallowBreaking"]
    g.b("set blocksToDisallowBreaking " + guard.replace(",wheat", ""))
    try:
        g.b(f"farm {radius}")
        g.sleep(3)
        g.wait(lambda: not g.baritone.getFarmProcess().isActive(), 240, 2)
    finally:
        g.stop()
        g.b("set blocksToDisallowBreaking " + guard)
    # Some wheat stays unbaked once there is a pen: it is the breeding feed.
    spare = g.count(lambda i: i == "wheat") - (6 if facts.get("pen") else 0)
    if spare >= 3:
        crafting.craft(g, "bread", spare // 3)
    return True


def farm_spots(g, radius=20):
    """Tillable blocks level with, and within 3 blocks of, open water near
    the bot (farmland stays hydrated up to 4 blocks from water). Returns the
    cluster around the spot nearest to the bot."""
    px, py, pz = g.block_pos()
    lo = [px - radius, py - 10, pz - radius]
    hi = [px + radius, py + 5, pz + radius]
    region = m.get_block_region(lo, hi)
    at = lambda x, y, z: short(region.get_block(x, y, z))
    inside = lambda x, z: lo[0] <= x <= hi[0] and lo[2] <= z <= hi[2]
    found = set()
    for x in range(lo[0], hi[0] + 1):
        for z in range(lo[2], hi[2] + 1):
            for y in range(lo[1], hi[1]):
                if at(x, y, z) != "water" or at(x, y + 1, z) != "air":
                    continue
                for dx in range(-3, 4):
                    for dz in range(-3, 4):
                        c = (x + dx, y, z + dz)
                        if (inside(c[0], c[2]) and c not in found
                                and at(*c) in ("grass_block", "dirt")
                                and at(c[0], y + 1, c[2]) in ("air", "short_grass")):
                            found.add(c)
    if not found:
        return []
    anchor = min(found, key=lambda c: math.dist(c, (px, py, pz)))
    return sorted((c for c in found if math.dist(c, anchor) <= 7),
                  key=lambda c: math.dist(c, anchor))


def get_bucket(g, facts):
    """A bucket from our own iron, or from the base chest (smelting stored
    raw iron if need be)."""
    has = lambda i: g.count(lambda j: j == i)
    if has("bucket") or has("water_bucket"):
        return True
    if has("iron_ingot") < 3 and "base" in facts:
        withdraw(g, facts, "iron_ingot", 3)
        if has("iron_ingot") < 3:
            withdraw(g, facts, "raw_iron", 3 - has("iron_ingot"))
        if has("iron_ingot") < 3 and has("raw_iron"):
            smelt(g, facts, "raw_iron", "iron_ingot",
                  min(has("raw_iron"), 3 - has("iron_ingot")))
    if has("iron_ingot") < 3:
        g.log(f"bucket: only {has('iron_ingot')} iron ingots available")
        return False
    return crafting.craft(g, "bucket")


def fetch_water(g, facts):
    """Make a bucket, fill it at the nearest water and bring it home: a
    water source next to the base door hydrates a farm there (the nearest
    natural water is far away and full of Drowned)."""
    if not fill_bucket(g, facts):
        return False
    return pour_by_door(g, facts)


def scan_for_water(g, radius=96, step=4):
    """Nearest surface-level water in the loaded chunks, or None."""
    x0, y0, z0 = g.block_pos()
    cells = [[x, y, z] for x in range(x0 - radius, x0 + radius + 1, step)
             for z in range(z0 - radius, z0 + radius + 1, step)
             for y in range(58, 80)]
    best = None
    for i in range(0, len(cells), 2000):
        chunk = cells[i:i + 2000]
        for c, k in zip(chunk, m.getblocklist(chunk)):
            if k and k.startswith("minecraft:water"):
                d = math.dist(c, (x0, y0, z0))
                if (best is None or d < best[0]) and \
                        g.block(c[0], c[1] + 1, c[2]) in AIR:
                    best = (d, c)
    return best and best[1]


def fill_bucket(g, facts):
    """Make a bucket if needed and fill it at the nearest water."""
    if not get_bucket(g, facts):
        return False
    has = lambda i: g.count(lambda j: j == i)
    if not has("water_bucket"):
        g.status = "fetching water"
        come_home(g, facts, leash=LEASH // 2)
        # Baritone's "goto water" only knows blocks in its cache and gave up
        # with water 56 blocks away, so look for it ourselves (a sparse scan
        # of the loaded chunks takes ~3 s) and remember it for the team.
        spot = facts.get("water") or scan_for_water(g)
        if spot:
            g.pending_facts["water"] = spot
            g.goto_near(*spot, radius=3, timeout=240)
        else:
            g.b("goto water")
            g.sleep(2)
            g.wait(lambda: not g.busy(), 240, 1)
        g.stop()
        water = None
        for _ in range(6):
            # A source block: still water (level 0), open to the air.
            here = g.block_pos()
            cells = [[x, y, z] for x in range(here[0] - 4, here[0] + 5)
                     for y in range(here[1] - 3, here[1] + 2)
                     for z in range(here[2] - 4, here[2] + 5)]
            for c, k in zip(cells, m.getblocklist(cells)):
                if k and k.startswith("minecraft:water[level=0") and \
                        g.block(c[0], c[1] + 1, c[2]) == "air":
                    if water is None or math.dist(c, here) < math.dist(water, here):
                        water = c
            if water:
                break
            g.b("goto water")
            g.sleep(2)
            g.wait(lambda: not g.busy(), 60, 1)
            g.stop()
        if not water:
            g.log("water: no source block found")
            return False
        g.select(lambda i: i == "bucket")
        g.goto_near(*water, radius=2, timeout=30)
        g.select(lambda i: i == "bucket")
        g.use_on(water[0] + 0.5, water[1] + 0.9, water[2] + 0.5, hold=0.2)
        if not has("water_bucket"):
            g.log("water: could not fill the bucket")
            return False
        g.log(f"water: filled the bucket at {water}")
    return True


def pour_by_door(g, facts):
    """Surface variant: dig a hole by the base door and pour the water in."""
    ex = facts.get("base_exit") or facts["base"]
    g.status = "bringing water home"
    g.goto_near(*ex, radius=2, timeout=300)
    spots = dry_farm_spots(g, ex, radius=5)
    for x, y, z in spots[4:]:  # leave the cells right at the door free
        if math.dist((x, y, z), ex) < 3:
            continue
        g.goto_near(x, y, z, radius=2, timeout=30)
        if not g.break_block(x, y, z, timeout=8):
            continue
        g.select(lambda i: i == "water_bucket")
        g.use_on(x + 0.5, y, z + 0.5, hold=0.2)  # click the hole's floor
        if g.block(x, y, z) == "water":
            g.log(f"water: placed a source by the base at {[x, y, z]}")
            return {"farm_water": [x, y, z]}
    g.log("water: could not place the water by the base")
    return False


def farm_xz(base):
    """The farm room's floor: x from base-8 to base-4, z +-2 (5x5, water
    in the middle). (A 7x7 room was tried: enlarging it picked up the
    corner torches, and without light - no coal yet - all the wheat died.
    Bigger again once torches are plentiful.)"""
    x, _, z = base
    return (x - 8, x - 4), (z - 2, z + 2)


def farm_room_cells(base):
    """Layout of the farm room west of the base room: (centre, corner
    cells for torches, soil cells). Soil sits one block above the base
    floor with the water in the centre cell, level with the soil. 44 plots
    (the first room had 20: not enough bread for three bots)."""
    x, y, z = base
    (x0, x1), (z0, z1) = farm_xz(base)
    centre = (x - 6, y, z)
    corners = [(cx, y, cz) for cx in (x0, x1) for cz in (z0, z1)]
    soil = [(cx, y, cz) for cx in range(x0, x1 + 1) for cz in range(z0, z1 + 1)
            if (cx, y, cz) != centre and (cx, y, cz) not in corners]
    # far side first, so the bot works its way back towards the doorway
    soil.sort(key=lambda c: (c[0], abs(c[2] - z)))
    return centre, corners, soil


def lay_soil(g, c, centre, xs, y):
    """Put a dirt block in floor cell `c` of the farm room. Clicking the face
    of any visible solid neighbour (the floor, or soil laid next to it), from
    wherever we are first and then from a few spots around it: fixed standing
    spots failed (Baritone parked the bot inside the cell, short of the spot,
    or behind laid soil that hid the floor)."""
    def ok(st):
        return (xs[0] <= st[0] <= xs[1] and (st[0], st[2]) != (c[0], c[2])
                and (st[0], st[2]) != (centre[0], centre[2]))
    spots = [None] + [s for s in (
        (c[0] + 2, y, c[2]), (c[0] - 2, y + 1, c[2]), (c[0], y + 1, c[2] - 2),
        (c[0], y + 1, c[2] + 2), (c[0] + 1, y + 1, c[2] + 1),
        (c[0] - 1, y + 1, c[2] - 1)) if ok(s)]
    for st in spots:
        if st:
            g.goto(*st, timeout=15)
            g.stop()
        bx, by, bz = g.block_pos()
        if (bx, bz) == (c[0], c[2]):
            continue  # standing in it
        if math.dist(g.pos(), (c[0] + .5, c[1] + .5, c[2] + .5)) > 4.2:
            continue
        if g.place_against("dirt", *c, stand=(bx, by, bz)):
            return True
    g.log(f"could not place dirt at {list(c)}")
    return False


def reclaim_water(g, cells):
    """Scoop a misplaced water source in `cells` back into an empty bucket."""
    if not g.count(lambda i: i == "bucket"):
        return False
    for c, k in zip(cells, m.getblocklist([list(c) for c in cells])):
        if k and k.startswith("minecraft:water[level=0"):
            g.status = "taking back misplaced water"
            if math.dist(g.pos(), (c[0] + .5, c[1], c[2] + .5)) > 3:
                g.goto_near(*c, radius=2, timeout=20)
                g.stop()
            g.select(lambda i: i == "bucket")
            g.use_on(c[0] + 0.5, c[1] + 0.85, c[2] + 0.5, hold=0.2)
            if g.count(lambda i: i == "water_bucket"):
                g.log(f"farm room: took back the water at {list(c)}")
                return True
    return False


def dig_out(g, cells, tries=2, keep=()):
    """Break every non-air block of `cells` (in order), walking up to each.
    Returns the cells still solid."""
    for _ in range(tries):
        kinds = dict(zip(cells, (short(k) for k in
                                 m.getblocklist([list(c) for c in cells]))))
        todo = [c for c in cells if kinds[c] not in AIR
                and kinds[c] not in ("torch", "wall_torch", "water")
                and not (c in keep and kinds[c] in ("dirt", "farmland"))]
        if not todo:
            return []
        for i, c in enumerate(todo):
            if g.block(*c) in AIR:
                continue
            g.status = f"digging ({len(todo) - i} blocks left)"
            if not g.count(lambda j: j.endswith("_pickaxe")) and \
                    g.count(CLASSES["stone"]) >= 3:
                # (a big dig wears out pickaxes: the cobble makes the next)
                crafting.craft(g, "stone_pickaxe")
            if math.dist(g.pos(), (c[0] + .5, c[1] + .5, c[2] + .5)) > 4:
                g.goto_near(c[0], c[1], c[2], radius=2, timeout=25)
                g.stop()
            g.select(lambda i: i.endswith("_pickaxe"))
            g.break_block(*c, timeout=6)
    return [c for c in cells if g.block(*c) not in AIR + ("torch", "wall_torch", "water")
            and not (c in keep and g.block(*c) in ("dirt", "farmland"))]


def pour_into_hole(g, centre, y):
    """Pour the water bucket into the farm's centre hole: from on top of the
    soil next to it, looking straight down, and only with the crosshair
    really on the hole's floor (from floor level the soil was in the way,
    and an unchecked pour flooded the base)."""
    # (two rounds: clearing a block out of the hole can take a spot or two)
    for st in [(centre[0] + dx, y + 1, centre[2] + dz)
               for dx, dz in ((0, 1), (0, -1), (1, 0), (-1, 0))] * 2:
        if g.block(*centre) == "water":
            break
        g.goto(*st, timeout=20)
        g.stop()
        if g.block(*centre) not in AIR + ("water",):
            g.select(lambda i: i.endswith("_pickaxe") or i.endswith("_shovel"))
            g.break_block(*centre, timeout=6)
        if g.block(centre[0], y - 1, centre[2]) in AIR:
            g.place_against("dirt", centre[0], y - 1, centre[2])  # a floor
        m.player_look_at(centre[0] + 0.5, y - 0.01, centre[2] + 0.5)
        g.sleep(0.4)
        hit = m.player_get_targeted_block(4.5)
        if hit and list(hit.position) == [centre[0], y - 1, centre[2]] \
                and hit.side == "up":
            g.select(lambda i: i == "water_bucket")
            g.use_on(centre[0] + 0.5, y - 0.01, centre[2] + 0.5, hold=0.2)
            break
    return g.block(*centre) == "water"


def water_farm(g, facts):
    """The farm's water is gone (Baritone dropped a block into the hole to
    walk across): get a bucket of water and pour it back."""
    x, y, z = facts["base"]
    centre, corners, soil = farm_room_cells((x, y, z))
    if g.block(*centre) == "water":
        return {"farm_dry": False}
    if not g.count(lambda i: i == "water_bucket"):
        if g.is_night() or g.is_dusk():
            return False
        if not fill_bucket(g, facts):
            g.log("farm: no water to bring back")
            return False
    if not g.goto(x - 4, y + 1, z, timeout=300, near=1):
        return False
    if not pour_into_hole(g, centre, y):
        g.log("farm: could not pour the water back")
        return False
    g.log("farm: water is back")
    return {"farm_dry": False}


def build_farm_room(g, facts, plots=20):
    """An underground wheat farm inside the base: a 5x5 room off the west
    wall, soil carried in (the base is solid stone), torches in the
    corners (crops need light), and a bucket of water in the middle so
    every plot is hydrated. Safe from mobs, and nobody has to work next to
    the Drowned-infested water outside."""
    x, y, z = facts["base"]
    centre, corners, soil = farm_room_cells((x, y, z))
    has = lambda i: g.count(lambda j: j == i)
    seeds = lambda i: i == "wheat_seeds"
    # -- materials (surface errands; the skill resumes after a night) ------
    short_of = (not g.count(lambda i: i.endswith("_hoe")) or has("dirt") < 16
                or g.count(seeds) < 2 or (has("torch") < 2 and not g.count(CLASSES["coal"])))
    if short_of and (g.is_night() or g.is_dusk()):
        shelter(g, facts)  # the task is marked at_base: do not gather in the dark
    if not has("stone_hoe") and not g.count(lambda i: i.endswith("_hoe")) \
            and not crafting.craft(g, "stone_hoe"):
        g.log("farm room: could not make a hoe")
        return False
    todo_soil = [c for c in soil if g.block(*c) not in ("dirt", "farmland")] \
        if math.dist(g.pos(), (x, y, z)) < 40 else soil
    if has("dirt") < len(todo_soil) and \
            not gather(g, facts, "dirt", "dirt", len(todo_soil), timeout=420):
        g.log(f"farm room: only {has('dirt')} dirt")
        return False
    if g.count(seeds) < 8 and not gather(g, facts, "grass", "wheat_seeds", 8, 420) \
            and g.count(seeds) < 2:
        g.log(f"farm room: only {g.count(seeds)} seeds")
        return False
    if has("torch") < 2:
        if not g.count(CLASSES["coal"]):
            gather(g, facts, "coal", "coal|charcoal", 1, timeout=420)
        if not g.count(CLASSES["coal"]) or not crafting.craft(g, "torch"):
            g.log("farm room: no torches (crops need light)")
            return False
    # -- dig ---------------------------------------------------------------
    g.status = "digging the farm room"
    if not g.goto(x, y, z, timeout=300, near=1):
        g.log("farm room: could not get to the base")
        return False
    # Tell the team first: the half-dug doorway otherwise looks like a hole
    # in the base, and the sealing job walled it up again.
    g.pending_facts["farm_room"] = True
    g.wait(lambda: facts.get("farm_room"), 15, 1)
    (fx0, fx1), (fz0, fz1) = farm_xz((x, y, z))
    room = [(cx, cy, cz) for cx in range(x - 3, fx0 - 1, -1)  # door side first
            for cz in range(fz0, fz1 + 1) for cy in range(y, y + 3)
            if cx != x - 3 or cz == z]                      # doorway: one wide
    solid = [c for c, k in zip(room, m.getblocklist([list(c) for c in room]))
             if short(k) not in AIR]
    if len(solid) > len(room) // 2:
        # Only for a fresh room: the builder also digs out soil already laid.
        g.b("sel clear")
        g.b(f"sel 1 {fx0} {y} {fz0}")
        g.b(f"sel 2 {fx1} {y + 2} {fz1}")
        g.b(f"sel 1 {x - 3} {y} {z}")          # doorway through the west wall
        g.b(f"sel 2 {x - 3} {y + 2} {z}")
        g.dig_ok = True  # digging the base on purpose (see Game.base_guard)
        g.b("sel cleararea")
        g.sleep(3)
        g.wait(lambda: not g.baritone.getBuilderProcess().isActive(), 300, 2)
        g.dig_ok = False
        g.b("sel clear")
        g.stop()
    # Baritone's builder stopped early and left most of the room solid
    # (nowhere to put the soil): break whatever is left ourselves.
    left = dig_out(g, room, keep=set(soil))
    if left:
        g.log(f"farm room: {len(left)} blocks left undug, e.g. {left[:3]}")
        return False
    # -- light, soil, water --------------------------------------------------
    g.status = "building the farm"
    for c in corners:
        if has("torch") and g.block(*c) in AIR:
            g.place_at("torch", *c)
    for c in soil:
        if g.block(*c) in ("dirt", "farmland") or not has("dirt"):
            continue
        if g.block(*c) in ("torch", "wall_torch"):
            # left by an earlier attempt; get close, laid soil blocks the view
            g.goto_near(*c, radius=2, timeout=20)
            g.stop()
            g.break_torch(*c)
        lay_soil(g, c, centre, (farm_xz(facts["base"])[0][0], x - 3), y)
    for c in corners:  # (again: torches of the old, smaller room came back up)
        if has("torch") and g.block(*c) in AIR:
            g.place_at("torch", *c)
    if g.block(*centre) != "water" and not g.count(lambda i: i == "water_bucket"):
        reclaim_water(g, [(cx, y, cz) for cx in range(fx0, fx1 + 1)
                          for cz in range(fz0, fz1 + 1) if (cx, y, cz) != centre])
    if g.block(*centre) != "water" and not g.count(lambda i: i == "water_bucket"):
        # The only surface errand left: wait for daylight in bed (all of
        # the above is underground, so it gets done at night too).
        if g.is_night() or g.head_home(max(LEASH // 2, math.dist(g.pos(), facts.get("water") or g.pos()))):
            g.log("farm room: dug and soiled; fetching water in daylight")
            shelter(g, facts)
        if not fill_bucket(g, facts):
            g.log("farm room: no water")
            return False
        if not g.goto(x, y, z, timeout=300, near=1):
            g.log("farm room: could not get back to the base with the water")
            return False
    if g.block(*centre) != "water":
        ring = [(centre[0] + dx, y, centre[2] + dz)
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))]
        if not all(g.block(*c) in ("dirt", "farmland") for c in ring):
            g.log("farm room: soil around the water cell is incomplete")
            return False
        pour_into_hole(g, centre, y)
        if g.block(*centre) != "water":
            g.log("farm room: could not pour the water")
            return False
    # -- till and plant ------------------------------------------------------
    planted = 0
    for c in soil:
        if g.block(c[0], y + 1, c[2]) == "wheat":
            planted += 1
            continue
        if g.block(*c) not in ("dirt", "farmland") or not g.count(seeds):
            continue
        g.status = f"planting {planted}/{len(soil)}"
        if math.dist(g.pos(), (c[0] + .5, y + 1, c[2] + .5)) > 3.5:
            g.goto_near(c[0], y + 1, c[2], radius=2, timeout=30)
        if g.block(*c) == "dirt":
            g.select(lambda i: i.endswith("_hoe"))
            g.use_on(c[0] + 0.5, y + 0.99, c[2] + 0.5)
        if g.block(*c) != "farmland":
            continue
        g.select(seeds)
        g.use_on(c[0] + 0.5, y + 0.93, c[2] + 0.5)
        if g.block(c[0], y + 1, c[2]) == "wheat":
            planted += 1
    g.log(f"farm room: {planted} plots planted around water at {list(centre)}")
    if planted < 2:
        return False
    soiled = sum(g.block(*c) in ("dirt", "farmland") for c in soil)
    out = {"farm": list(centre), "farm_plots": planted, "farm_room": True}
    if soiled >= len(soil) - 2:
        out["farm_big"] = True  # the 7x7 room is dug and soiled
    return out


def dry_farm_spots(g, center, radius=7):
    """Grass/dirt blocks with open air above near `center` (surface level)."""
    cx, cy, cz = center
    cells = [(x, y, z) for x in range(cx - radius, cx + radius + 1)
             for z in range(cz - radius, cz + radius + 1)
             for y in range(cy - 3, cy + 3)]
    kinds = dict(zip(cells, (short(k) for k in m.getblocklist([list(c) for c in cells]))))
    above_ok = lambda c: kinds.get((c[0], c[1] + 1, c[2])) in ("air", "short_grass")
    spots = [c for c in cells if kinds[c] in ("grass_block", "dirt") and above_ok(c)
             and c[1] + 1 < cy + 3]
    return sorted(spots, key=lambda c: math.dist(c, center))


def build_farm(g, facts, plots=8):
    """Wheat farm next to natural water: collect seeds, till, plant.
    Harvest later with Baritone's `farm` process."""
    seeds = lambda i: i == "wheat_seeds"
    if g.count(lambda i: i.endswith("_hoe")) < 1 and not crafting.craft(g, "stone_hoe"):
        g.log("farm: could not make a hoe")
        return False
    if g.count(seeds) < plots and not gather(g, facts, "grass", "wheat_seeds", plots, 420):
        if g.count(seeds) < 2:
            g.log(f"farm: only {g.count(seeds)} seeds after searching grass")
            return False
    spots = []
    home = facts.get("base") or m.world_info().spawn
    if facts.get("farm_water"):
        # Our own water source by the base: farm around it.
        g.goto_near(*facts["farm_water"], radius=2, timeout=200)
        spots = farm_spots(g, radius=8)
    for attempt in range(0 if len(spots) >= 2 else 3):
        # Close to home: the farm has to be tended (and defended) for good.
        come_home(g, facts, leash=LEASH // 2)
        g.status = "looking for water"
        spots = farm_spots(g)
        if len(spots) >= 2:
            break
        explore(g, facts, 25)
    if len(spots) < 2 and facts.get("base_exit"):
        # No water close to home. Crops grow on dry farmland too, only
        # slower, and next to the base door the farm is easy to tend.
        ex = facts["base_exit"]
        g.status = "making a dry farm by the base"
        g.goto_near(*ex, radius=2, timeout=120)
        spots = dry_farm_spots(g, ex)
    if len(spots) < 2:
        g.log("farm: no tillable ground found")
        return False
    g.log(f"farm: {len(spots)} candidate plots near water at {spots[0]}")
    planted = []
    for x, y, z in spots:
        if len(planted) >= plots or g.count(seeds) == 0:
            break
        g.status = f"planting {len(planted)}/{plots}"
        if not g.goto(x, y + 1, z, timeout=30, near=2):
            continue
        if g.block(x, y + 1, z) == "short_grass":
            g.break_block(x, y + 1, z, 5)
        g.select(lambda i: i.endswith("_hoe"))
        g.use_on(x + 0.5, y + 1.0, z + 0.5)
        if g.block(x, y, z) != "farmland":
            continue
        g.select(seeds)
        g.use_on(x + 0.5, y + 0.95, z + 0.5)
        if g.block(x, y + 1, z) == "wheat":
            planted.append([x, y, z])
    g.log(f"farm: planted {len(planted)} of {len(spots)} candidate spots")
    if len(planted) < 2:
        return False
    return {"farm": planted[0], "farm_plots": len(planted)}


def teammate_pos(g, facts, name):
    return (facts.get("_team") or {}).get(name)


def deliver(g, facts, to, item, count, timeout=420):
    """Walk to a teammate and hand over items by throwing them at their
    feet. The coordinator keeps the receiver waiting once we are close."""
    pred, end = matcher(item), time.time() + timeout
    while time.time() < end:
        pos = teammate_pos(g, facts, to)
        if not pos:
            return False
        d = math.dist(g.pos(), pos)
        g.status = f"delivering {count} {item} to {to} ({round(d)} blocks)"
        if d > 3.5:
            g.goto_near(*pos, radius=2, timeout=20)  # re-aim every 20 s
            continue
        m.player_look_at(pos[0] + 0.5, pos[1] + 0.3, pos[2] + 0.5)
        time.sleep(0.4)
        n = g.toss(pred, count)
        g.log(f"handed {n} {item} to {to}")
        # Step away so the receiver, not us, picks the items up.
        x, y, z = g.block_pos()
        dx, dz = x - pos[0], z - pos[2]
        norm = max(1.0, math.hypot(dx, dz))
        g.goto_near(round(x + 5 * dx / norm), y, round(z + 5 * dz / norm),
                    radius=1, timeout=10)
        g.sleep(3)
        return n > 0
    return False


def receive(g, facts, frm, timeout=90):
    """A teammate is bringing us something: stay close, and walk over to
    any items lying nearby so the hand-over actually lands in our pockets
    (items thrown into water drift; standing still is not enough)."""
    end = time.time() + timeout
    while time.time() < end:
        g.status = f"receiving from {frm}"
        drops = m.entities(type=".*\\.item$", max_distance=8, sort="nearest", limit=1)
        if drops:
            x, y, z = drops[0].position
            g.goto_near(math.floor(x), math.floor(y), math.floor(z), radius=0, timeout=8)
        g.sleep(1)
        team = (facts.get("_team") or {})
        if frm not in team:
            break
    return True


def assist(g, facts, to):
    """Run to a teammate who is hurt and under attack; Kill Aura and the
    fight routine do the rest once we are next to them."""
    end = time.time() + 60
    while time.time() < end:
        pos = teammate_pos(g, facts, to)
        if not pos:
            break
        g.status = f"assisting {to}"
        if math.dist(g.pos(), pos) > 3:
            g.goto_near(*pos, radius=2, timeout=10)
        else:
            g.fight(radius=8, timeout=5)
            g.sleep(1)
    return True


def make_torches(g, facts, count=12):
    """Torches without coal: logs smelted to charcoal in the base furnace
    (planks for fuel), then charcoal and sticks. The farm room dies
    without light, and the team had found no coal at all."""
    has = lambda i: g.count(lambda j: j == i)
    if has("torch") >= count:
        light_farm(g, facts)
        return True
    need = math.ceil((count - has("torch")) / 4)
    if g.count(CLASSES["coal"]) < need:
        logs = need - g.count(CLASSES["coal"]) + 2  # (+2: planks for fuel)
        if g.count(CLASSES["log"]) < logs and \
                not gather(g, facts, "logs", "log", logs, timeout=600, until_dark=True):
            return False
        smelt(g, facts, "log", "charcoal", need - g.count(CLASSES["coal"]))
    n = min(need, g.count(CLASSES["coal"]))
    if n:
        crafting.craft(g, "torch", n)
    g.log(f"torches: {has('torch')} made from charcoal")
    light_farm(g, facts)
    return has("torch") > 0


def light_farm(g, facts):
    """Torches on the farm room's corners (the wheat dies in the dark)."""
    if not facts.get("farm_room") or not g.count(lambda i: i == "torch"):
        return
    x, y, z = facts["base"]
    _, corners, _ = farm_room_cells((x, y, z))
    dark = [c for c in corners if g.block(*c) not in ("torch", "wall_torch")]
    if not dark:
        return
    g.status = "lighting the farm room"
    g.goto(x - 4, y + 1, z, timeout=180, near=1)
    for c in dark:
        if not g.count(lambda i: i == "torch"):
            break
        if math.dist(g.pos(), (c[0] + .5, y + .5, c[2] + .5)) > 3.5:
            g.goto_near(c[0], y + 1, c[2], radius=2, timeout=20)
            g.stop()
        if g.block(*c) not in AIR:
            g.select(lambda i: i.endswith(("_pickaxe", "_shovel")))
            g.break_block(*c, timeout=6)
        g.place_at("torch", *c)
    g.log(f"farm lit: {sum(g.block(*c) in ('torch', 'wall_torch') for c in corners)}/4 corners")


def light_exit(g, facts, radius=12, spacing=5):
    """Torches on the ground around the top of the base stairs: mobs
    spawned and waited there in the dark, and 15 of 54 deaths in a day
    happened within 12 blocks of it. (Monsters only spawn in total
    darkness, so a torch every few blocks stops them.)"""
    ex, ey, ez = facts["base_exit"]
    if g.count(lambda i: i == "torch") < 4 and not make_torches(g, facts, count=16):
        return False
    if not g.goto(ex, ey, ez, timeout=300, near=2):
        return False
    hm = height_map(g, ex, ez, ey, radius + 1)
    spots = []
    for x in range(ex - radius, ex + radius + 1, spacing):
        for z in range(ez - radius, ez + radius + 1, spacing):
            h = hm.get((x, z))
            if not h or h[2] not in AIR or h[1] in ("water", "lava") or h[1].endswith("_leaves"):
                continue
            spots.append((x, h[0] + 1, z))
    spots.sort(key=lambda c: math.dist(c, (ex, ey, ez)))
    lit = 0
    for c in spots:
        if not g.count(lambda i: i == "torch"):
            break
        if g.block(*c) in ("torch", "wall_torch"):
            lit += 1
            continue
        g.status = f"lighting the base exit ({lit}/{len(spots)})"
        if g.place_at("torch", *c):
            lit += 1
    # Count every torch around the exit, wherever it stands (a torch one
    # block off its grid point, or on a block that took none, still lights).
    area = [[cx, cy, cz] for cx in range(ex - radius, ex + radius + 1)
            for cz in range(ez - radius, ez + radius + 1) for cy in range(ey - 6, ey + 7)]
    total = 0
    for i in range(0, len(area), 4096):
        total += sum(1 for k in m.getblocklist(area[i:i + 4096])
                     if k and short(k) in ("torch", "wall_torch"))
    g.log(f"exit: placed {lit}, {total} torches within {radius} of the top of the stairs")
    return {"exit_lit": True} if total >= 8 else False


def build_tree_farm(g, facts):
    """Dig the tree room (see tree_layout), light it, set dirt into the
    floor and plant what saplings we have."""
    if facts.get("tree_farm"):
        return {"tree_farm": True}
    base = facts["base"]
    x, y, z = base
    lay = tree_layout(base)
    # rock over the room: at least 4 blocks below the ground everywhere
    hm = height_map(g, x, z - 14, y, 6) if math.dist(g.pos(), base) < 40 else {}
    if hm and min(hm.get((cx, cz), (y + 20,))[0] for cx in range(x - 4, x + 5)
                  for cz in range(z - 18, z - 9)) < y + 2:
        g.log("tree farm: too little rock over the room")
        return False
    if g.count(lambda i: i == "torch") < 5:
        make_torches(g, facts, count=8)
    if g.count(lambda i: i == "dirt") < 4:
        gather(g, facts, "dirt", "dirt", 4, timeout=300, until_dark=True)
    g.status = "digging the tree room"
    if not g.goto(x, y, z, timeout=300, near=1):
        return False
    # Tell the team first, or the sealing job walls the new stair up.
    TREE_FARM["on"] = True
    g.pending_facts["tree_farm_digging"] = True
    left = dig_out(g, lay["dig"], tries=3)
    if left:
        g.log(f"tree farm: {len(left)} blocks left undug, e.g. {left[:3]}")
        return False
    cx, fy, cz = lay["centre"]
    g.goto(cx, fy, cz, timeout=60)
    for c in lay["torches"]:
        if g.count(lambda i: i == "torch") and g.block(*c) in AIR:
            g.place_at("torch", *c)
    for sx, sy, sz in lay["spots"]:
        soil = (sx, sy - 1, sz)
        if g.block(*soil) not in ("dirt", "grass_block", "podzol") and \
                g.count(lambda i: i == "dirt"):
            if math.dist(g.pos(), (sx + .5, sy, sz + .5)) > 3.5:
                g.goto_near(sx, sy, sz, radius=2, timeout=20)
                g.stop()
            g.select(lambda i: i.endswith("_pickaxe"))
            g.break_block(*soil, timeout=6)
            g.place_against("dirt", *soil)
    plant_tree_spots(g, lay)
    g.log(f"tree farm: room dug, {sum(g.block(*c) in ('torch', 'wall_torch') for c in lay['torches'])} torches")
    return {"tree_farm": True, "tree_farm_digging": None}


def plant_tree_spots(g, lay):
    planted = 0
    for sx, sy, sz in lay["spots"]:
        sap = next((i for i in g.inv() if i.endswith("_sapling")), None)
        if sap is None:
            break
        if g.block(sx, sy, sz) not in AIR or \
                g.block(sx, sy - 1, sz) not in ("dirt", "grass_block", "podzol"):
            continue
        if math.dist(g.pos(), (sx + .5, sy, sz + .5)) > 3.5:
            g.goto_near(sx, sy, sz, radius=2, timeout=20)
            g.stop()
        if g.place_at(sap, sx, sy, sz):
            planted += 1
    return planted


def chop(g, c, tries=5):
    """Break the block at c, cutting through leaves in the way."""
    for _ in range(tries):
        if g.block(*c) in AIR:
            return True
        if math.dist(g.pos(), (c[0] + .5, c[1] + .5, c[2] + .5)) > 4:
            g.goto_near(c[0], c[1], c[2], radius=2, timeout=15)
            g.stop()
        m.player_look_at(c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)
        time.sleep(0.25)
        hit = m.player_get_targeted_block(4.5)
        if not hit:
            continue
        if list(hit.position) == list(c):
            g.select(lambda i: i.endswith("_axe"))
            g.break_block(*c, timeout=8)
        elif short(hit.type).endswith("_leaves"):
            g.break_block(*hit.position, timeout=4)
        else:
            return False
    return g.block(*c) in AIR


def tree_harvest(g, facts):
    """Chop the grown trees in the tree room, pick up what the leaves drop
    (saplings, sticks, apples) and replant."""
    if not facts.get("tree_farm"):
        return False
    lay = tree_layout(facts["base"])
    cx, fy, cz = lay["centre"]
    g.status = "going to the tree room"
    if not g.goto(cx, fy, cz, timeout=300, near=1):
        return False
    before = g.count(CLASSES["log"])
    for sx, sy, sz in lay["spots"]:
        trunk = [(sx, cy, sz) for cy in range(sy, sy + 7)
                 if g.block(sx, cy, sz).endswith(("_log", "_wood"))]
        for c in trunk:  # bottom up: the trunk stays put
            g.status = f"chopping trees ({g.count(CLASSES['log']) - before} logs)"
            chop(g, c)
    cut = g.count(CLASSES["log"]) - before
    if cut:
        g.status = "waiting for the leaves to drop saplings"
        g.sleep(45)
        pick_up_drops(g, (cx, fy, cz), 6)
    planted = plant_tree_spots(g, lay)
    g.log(f"tree farm: {cut} logs, {planted} saplings planted")
    return {"tree_logs": cut} if cut or planted else True


def plant_trees(g, facts, max_trees=12):
    """Saplings in a grid on open grass near the top of the stairs: the
    trees around the base were all cut down, and wood runs went further
    and further out (one bot spent its whole morning looking for logs)."""
    saplings = [i for i in g.inv() if i.endswith("_sapling")]
    if not saplings or not facts.get("base_exit"):
        return False
    ex, ey, ez = facts["base_exit"]
    if not g.goto(ex, ey, ez, timeout=300, near=2):
        return False
    hm = height_map(g, ex, ez, ey, 16)
    pen = facts.get("pen") or {}
    px, pz = pen.get("corner") or (10 ** 6, 10 ** 6)
    bx = facts["base"][0]
    spots = []
    for x in range(ex - 15, ex + 16, 3):
        for z in range(ez - 15, ez + 16, 3):
            h = hm.get((x, z))
            if not h or h[1] not in ("grass_block", "dirt", "podzol") or h[2] not in AIR:
                continue
            if math.dist((x, z), (ex, ez)) < 5 or (abs(z - ez) <= 2 and bx <= x <= ex + 2):
                continue  # not on the stairs or the doorstep
            if px - 2 <= x <= px + 7 and pz - 2 <= z <= pz + 7:
                continue  # not in the pen
            if any(k not in AIR for k in (short(b) for b in m.getblocklist(
                    [[x, h[0] + d, z] for d in range(1, 6)]))):
                continue  # room to grow
            spots.append((x, h[0] + 1, z))
    spots.sort(key=lambda c: math.dist(c, (ex, ey, ez)))
    planted = 0
    for c in spots[:max_trees]:
        sap = next((i for i in g.inv() if i.endswith("_sapling")), None)
        if sap is None:
            break
        g.status = f"planting trees ({planted})"
        if g.place_at(sap, *c):
            planted += 1
    g.log(f"trees: planted {planted} saplings near the base")
    return planted > 0


def wait_for_food(g, facts, timeout=180):
    """Starving and nearly dead: stay in the base room until a teammate
    brings bread or the farm has ripe wheat, then eat."""
    g.status = "waiting in the base for food"
    x, y, z = facts["base"]
    if not g.in_rooms(g.pos()) and not g.goto(x, y, z, timeout=240, near=1):
        return False
    food = CLASSES["food"]
    g.wait(lambda: g.count(food) > 0, timeout, 2)
    return g.count(food) > 0


def idle(g, facts):
    g.status = "idle"
    g.sleep(10)
    return True



# ---------------------------------------------------------------------------
# Animal pen: a 6x6 cobblestone-wall ring (4x4 inside) next to the top of
# the base stairs, with a fence gate. Animals are lured in with wheat and
# bred there, so meat, wool and leather stop depending on finding wild herds.

PEN = 6
PEN_KINDS = ("cow", "sheep")   # both follow and breed with wheat
PEN_MIN, PEN_MAX, PEN_CULL = 2, 8, 4
GROUND_OK = ("grass_block", "dirt", "coarse_dirt", "podzol", "rooted_dirt",
             "sand", "gravel", "stone", "mud", "moss_block")


def height_map(g, cx, cz, y, r):
    """Ground height for every column within r of (cx, cz), scanning a
    band around y (one block-list call per row)."""
    ys = list(range(y + 14, y - 7, -1))
    out = {}
    for x in range(cx - r, cx + r + 1):
        cells = [[x, yy, z] for z in range(cz - r, cz + r + 1) for yy in ys]
        kinds = [short(k) for k in m.getblocklist(cells)]
        for i, z in enumerate(range(cz - r, cz + r + 1)):
            col = kinds[i * len(ys):(i + 1) * len(ys)]
            for j, k in enumerate(col):
                if k in AIR or k in ("short_grass", "tall_grass", "fern", "snow",
                                     "large_fern", "leaf_litter") or k.endswith(
                                         ("_flower", "_tulip", "dandelion", "poppy")):
                    continue
                out[(x, z)] = (ys[j], k, col[j - 1] if j else "air",
                               col[j - 2] if j > 1 else "air")
                break
    return out


def pen_ring(x0, z0):
    cells = []
    for i in range(PEN):
        for c in ((x0 + i, z0), (x0 + i, z0 + PEN - 1), (x0, z0 + i), (x0 + PEN - 1, z0 + i)):
            if c not in cells:
                cells.append(c)
    return cells


def pen_site(g, facts):
    """A flat, open 6x6 patch 5-16 blocks from the top of the stairs."""
    ex, ey, ez = facts["base_exit"]
    bx = facts["base"][0]
    hm = height_map(g, ex, ez, ey, 18)
    best = None
    for x0 in range(ex - 17, ex + 12):
        for z0 in range(ez - 17, ez + 12):
            cx, cz = x0 + PEN / 2, z0 + PEN / 2
            d = math.dist((cx, cz), (ex + .5, ez + .5))
            if not 6 <= d <= 16:
                continue
            cols = [(x, z) for x in range(x0 - 1, x0 + PEN + 1)
                    for z in range(z0 - 1, z0 + PEN + 1)]
            if any(c not in hm for c in cols):
                continue
            # keep clear of the stairs and the exit
            if any(abs(z - ez) <= 2 and bx <= x <= ex + 2 for x, z in cols):
                continue
            hs = [hm[c][0] for c in cols]
            if max(hs) - min(hs) > 1:
                continue
            if any(hm[c][1] not in GROUND_OK or hm[c][2] not in AIR + (
                    "short_grass", "tall_grass", "fern", "snow")
                   for c in cols):
                continue
            score = d + 3 * (max(hs) - min(hs))
            if best is None or score < best[0]:
                best = (score, x0, z0)
    if not best:
        return None
    _, x0, z0 = best
    # the gate goes in the middle of the side facing the exit
    sides = {(0, -1): (x0 + 2, z0), (0, 1): (x0 + 2, z0 + PEN - 1),
             (-1, 0): (x0, z0 + 2), (1, 0): (x0 + PEN - 1, z0 + 2)}
    (nx, nz), (gx, gz) = min(sides.items(),
                             key=lambda s: math.dist(s[1], (ex, ez)))
    gy = hm[(gx, gz)][0] + 1
    out = (gx + nx, hm[(gx + nx, gz + nz)][0] + 1, gz + nz)
    inside = (gx - nx, hm[(gx - nx, gz - nz)][0] + 1, gz - nz)
    far = (gx - 3 * nx, hm[(gx - 3 * nx, gz - 3 * nz)][0] + 1, gz - 3 * nz)
    return {"corner": [x0, z0], "gate": [gx, gy, gz], "out": list(out),
            "in": list(inside), "far": list(far), "y": gy}, hm


def in_pen(pos, pen):
    x0, z0 = pen["corner"]
    return (x0 + 1 <= pos[0] < x0 + PEN - 1 and z0 + 1 <= pos[2] < z0 + PEN - 1
            and abs(pos[1] - pen["y"]) < 4)


def animal_age(e):
    import re
    hit = re.search(r"Age:(-?\d+)", e.nbt or "")
    return int(hit.group(1)) if hit else 0


def penned(pen, kind=None, adults=True):
    kinds = kind or "|".join(PEN_KINDS)
    out = [e for e in m.entities(type=f".*\\.({kinds})$", max_distance=96, nbt=True)
           if in_pen(e.position, pen)]
    return [e for e in out if animal_age(e) >= 0] if adults else out


def build_pen(g, facts):
    """Fence a 4x4 pen next to the stairs with cobblestone walls and a gate,
    placing every wall from outside so the builder never shuts itself in."""
    if facts.get("pen"):
        return {"pen": facts["pen"]}
    ex, ey, ez = facts["base_exit"]
    g.status = "looking for a spot for the animal pen"
    if not g.goto(ex, ey, ez, timeout=240, near=2):
        return False
    found = pen_site(g, facts)
    if not found:
        g.log("pen: no flat open spot near the stairs")
        return False
    pen, hm = found
    x0, z0 = pen["corner"]
    ring = [c for c in pen_ring(x0, z0) if c != (pen["gate"][0], pen["gate"][2])]
    need = sum(1 for x, z in ring if g.block(x, hm[(x, z)][0] + 1, z) != "cobblestone_wall")
    walls = g.count(lambda i: i == "cobblestone_wall")
    if walls < need and not crafting.craft(g, "cobblestone_wall", math.ceil((need - walls) / 6)):
        g.log("pen: could not craft the walls")
        return False
    gate = next((i for i in g.inv() if i.endswith("_fence_gate")), None)
    if gate is None and g.count(lambda i: i == "stick") < 4:
        # sticks first: made afterwards they ate the gate's planks
        crafting.ensure(g, "stick", 4)
    if gate is None:
        planks = max((i for i, n in g.inv().items() if i.endswith("_planks") and n >= 2),
                     key=lambda i: g.inv()[i], default=None)
        if planks is None:
            if not g.count(CLASSES["log"]) and not gather(g, facts, "logs", "log", 2, timeout=300):
                return False
            crafting.craft(g, "planks", 1)
            planks = max((i for i in g.inv() if i.endswith("_planks")),
                         key=lambda i: g.inv()[i])
        gate = planks.replace("_planks", "_fence_gate")
        if not crafting.craft(g, gate):
            g.log(f"pen: could not craft a {gate}")
            return False
    g.log(f"pen: building at {[x0, pen['y'], z0]}, gate at {pen['gate']}")
    missed = 0
    for x, z in ring:
        g.status = f"building the animal pen ({ring.index((x, z)) + 1}/{len(ring)})"
        y = hm[(x, z)][0] + 1
        if g.block(x, y, z) == "cobblestone_wall":
            continue
        # stand just outside the ring, never inside it
        sx = x + (1 if x == x0 + PEN - 1 else -1 if x == x0 else 0)
        sz = z + (1 if z == z0 + PEN - 1 else -1 if z == z0 else 0)
        stand = (sx, hm.get((sx, sz), (y - 1,))[0] + 1, sz)
        if g.block(x, y, z) not in AIR:
            g.break_block(x, y, z, timeout=6)  # grass, a flower
        if not g.place_at("cobblestone_wall", x, y, z, stand=stand):
            missed += 1
    gx, gy, gz = pen["gate"]
    if not g.block(gx, gy, gz).endswith("_fence_gate"):
        if g.block(gx, gy, gz) not in AIR:
            g.break_block(gx, gy, gz, timeout=6)
        if not g.place_at(gate, gx, gy, gz, stand=tuple(pen["out"])):
            missed += 1
    g.stop()
    if missed > 1:
        g.log(f"pen: {missed} gaps left")
        return False
    g.log("pen: done")
    return {"pen": pen}


def set_gate(g, pen, open_it):
    gx, gy, gz = pen["gate"]
    state = m.getblock(gx, gy, gz) or ""
    if "fence_gate" not in state:
        return False
    if ("open=true" in state) == open_it:
        return True
    if math.dist(g.pos(), (gx + .5, gy + .5, gz + .5)) > 3.5:
        g.goto(*pen["out"], timeout=30)
    g.use_on(gx + 0.5, gy + 0.6, gz + 0.5)
    return ("open=true" in (m.getblock(gx, gy, gz) or "")) == open_it


def leave_pen(g, pen):
    """Out through the gate and shut it, wheat out of hand so nothing
    follows."""
    g.select(lambda i: i.endswith("_sword")) or g.select(lambda i: i != "wheat")
    g.sleep(1.5)
    set_gate(g, pen, True)
    g.goto(*pen["out"], timeout=20)
    g.stop()
    set_gate(g, pen, False)


def lure(g, facts, pen, kind, timeout=300):
    """Walk a wild animal into the pen, holding wheat so it follows."""
    wheat = lambda i: i == "wheat"
    wild = lambda r: [e for e in m.entities(type=f".*\\.{kind}$", max_distance=r,
                                            sort="nearest", nbt=True)
                      if not in_pen(e.position, pen) and animal_age(e) >= 0
                      and e.position[1] > 55]
    herd = wild(64)
    if not herd:
        spots = [a for a in facts.get("_animals", []) if a[0] == kind
                 and a[2] > 55 and math.dist(a[1:], pen["out"]) < 120]
        if not spots:
            return False
        a = min(spots, key=lambda a: math.dist(a[1:], g.pos()))
        g.status = f"looking for a {kind} to bring to the pen"
        g.goto_near(*a[1:], radius=6, timeout=120)
        herd = wild(32)
    if not herd:
        return False
    ox, oy, oz = pen["out"]
    end = time.time() + timeout
    g.b("set allowSprint false")  # animals cannot keep up with a sprint
    try:
        x, y, z = (math.floor(v) for v in herd[0].position)
        g.status = f"luring a {kind} to the pen"
        g.select(wheat)
        g.goto_near(x, y, z, radius=2, timeout=60)
        g.stop()
        while time.time() < end:
            g.select(wheat)
            here = g.pos()
            near = [e for e in wild(8)]
            if not near:
                g.log(f"lure: lost the {kind}")
                return False
            if math.dist(here, near[0].position) > 4:
                g.b("stop")
                g.sleep(1.5)  # let it catch up
                continue
            d = math.dist((here[0], here[2]), (ox + .5, oz + .5))
            if d < 1.3:
                break
            if d < 6:
                g.b(f"goto {ox} {oy} {oz}")
            else:
                k = 5 / d
                g.b("goto %d %d" % (math.floor(here[0] + (ox + .5 - here[0]) * k),
                                    math.floor(here[2] + (oz + .5 - here[2]) * k)))
            g.sleep(1.2)
        else:
            return False
        g.stop()
        before = len(penned(pen, kind, adults=False))
        set_gate(g, pen, True)
        g.select(wheat)
        g.goto(*pen["far"], timeout=20)
        g.stop()
        g.wait(lambda: len(penned(pen, kind, adults=False)) > before, 15, 1)
        leave_pen(g, pen)
        got = len(penned(pen, kind, adults=False)) > before
        g.log(f"lure: {'brought' if got else 'failed to bring'} a {kind} into the pen")
        return got
    finally:
        g.b("set allowSprint " + game.BARITONE_SETTINGS["allowSprint"])
        g.stop()


def ranch(g, facts):
    """Keep two or more of each kind in the pen and breed them with wheat."""
    pen = facts.get("pen")
    wheat = lambda i: i == "wheat"
    if not pen or g.count(wheat) < 2:
        return False
    g.status = "going to the animal pen"
    if not g.goto(*pen["out"], timeout=240, near=1):
        return False
    for kind in PEN_KINDS:
        if len(penned(pen, kind)) < PEN_MIN and g.count(wheat) >= 2:
            lure(g, facts, pen, kind)
            g.goto(*pen["out"], timeout=240, near=1)
    pairs = {}
    for kind in PEN_KINDS:
        everyone = penned(pen, kind, adults=False)
        ready = [e for e in everyone if animal_age(e) == 0]
        if len(everyone) < PEN_MAX and len(ready) >= 2:
            pairs[kind] = ready[:2]
    bred = 0
    if pairs and g.count(wheat) >= 2:
        set_gate(g, pen, True)
        g.goto(*pen["in"], timeout=20)
        for kind, two in pairs.items():
            if g.count(wheat) < 2:
                break
            g.status = f"breeding {kind}s"
            fed = 0
            for e in two:
                g.select(wheat)
                fed += bool(g.use_on_entity(e, height=0.8))
            bred += fed == 2
        leave_pen(g, pen)
    counts = {k: len(penned(pen, k, adults=False)) for k in PEN_KINDS}
    g.log(f"ranch: pen holds {counts}, bred {bred} pair(s)")
    return {"pen_animals": counts}


SKILLS = {"gather": gather, "craft": craft, "hunt": hunt, "shelter": shelter,
          "build_base": build_base, "smelt": smelt, "build_farm": build_farm,
          "deliver": deliver, "assist": assist, "idle": idle, "receive": receive,
          "dig_in": dig_in, "recover": recover,
          "light_base": light_base, "bed": bed, "fish": fish,
          "seal_base": seal_base, "build_exit": build_exit,
          "store": store, "craft_from_storage": craft_from_storage,
          "smelt_storage": smelt_storage, "fetch_water": fetch_water,
          "build_farm_room": build_farm_room,
    "branch_mine": branch_mine,
    "make_bow": make_bow,
    "water_farm": water_farm,
    "build_pen": build_pen,
    "dig_shaft": dig_shaft,
    "light_shaft": light_shaft,
    "wait_for_food": wait_for_food,
    "make_torches": make_torches,
    "plant_trees": plant_trees,
    "light_exit": light_exit,
    "build_tree_farm": build_tree_farm,
    "tree_harvest": tree_harvest,
    "make_books": make_books,
    "ranch": ranch,
          "harvest": harvest}

from . import endgame  # noqa: E402,F401  (registers its skills in SKILLS)
