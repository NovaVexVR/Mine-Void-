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
from .game import BLOCK_GROUPS, short
from .items import CLASSES, matcher

ANIMALS = "cow|pig|sheep|chicken|rabbit|mooshroom"


def gather(g, facts, blocks, item, count, timeout=900):
    """Mine blocks with Baritone until `count` matching items are held."""
    pred, names = matcher(item), BLOCK_GROUPS[blocks]
    end, misses = time.time() + timeout, 0
    last_progress, last_state = time.time(), None
    # Anti-xray scrambles hidden ores, so only chase ore we can actually
    # see; wood and stone are never faked and can use chunk data directly.
    g.b("set legitMine " + ("true" if blocks in ("iron", "coal") else "false"))
    while g.count(pred) < count:
        if time.time() > end:
            g.stop()
            return False
        g.status = f"gathering {blocks} {g.count(pred)}/{count}"
        if not g.baritone.getMineProcess().isActive():
            g.b("mine " + names)
            g.sleep(4)
            if not g.baritone.getMineProcess().isActive():
                # Nothing of that kind known nearby: wander and retry.
                misses += 1
                g.status = f"exploring for {blocks}"
                g.b("explore")
                g.sleep(15 + 10 * min(misses, 4))
                g.b("stop")
        state = ([round(v) for v in g.pos()], g.count(pred))
        if state != last_state:
            last_state, last_progress = state, time.time()
        elif time.time() - last_progress > 45:
            g.log("gather stuck, restarting baritone")
            g.stop()
            last_progress = time.time()
        g.sleep(1)
    g.stop()
    return True


crafting.gather_hook = lambda g, blocks, item, n: gather(g, {}, blocks, item, n)


def craft(g, facts, item, count=1):
    return crafting.craft(g, item, count)


def hunt(g, facts, count=6, timeout=600):
    """Kill passive animals until `count` food items are held."""
    food = CLASSES["food"]
    end, misses = time.time() + timeout, 0
    while g.count(food) < count and time.time() < end:
        g.status = f"hunting {g.count(food)}/{count}"
        prey = m.entities(type=f".*\\.({ANIMALS})$", max_distance=60,
                          sort="nearest", limit=1)
        if not prey:
            misses += 1
            if misses > 12:
                break
            g.b("explore")
            g.sleep(20)
            g.b("stop")
            continue
        target, last_goto, chase_end = prey[0], 0, time.time() + 45
        pos = target.position
        while time.time() < chase_end:
            cur = m.entities(uuid=target.uuid, max_distance=80)
            if not cur:
                break  # dead (or gone)
            pos = cur[0].position
            me = g.pos()
            if math.dist(me, pos) > 3.0:
                if time.time() - last_goto > 1.5:
                    g.b("goto %d %d %d" % tuple(math.floor(v) for v in pos))
                    last_goto = time.time()
                g.sleep(0.3)
            else:
                g.attack(cur[0])
                g.sleep(0.65)
        g.goto(*[math.floor(v) for v in pos], timeout=15, near=1)  # drops
        g.sleep(1)
    g.stop()
    return g.count(food) >= count


def shelter(g, facts):
    """Sit out the night: go to the base if there is one, otherwise dig a
    3-deep hole and seal it."""
    g.status = "sheltering for the night"
    if "base" in facts:
        g.goto(*facts["base"], timeout=180, near=2)
    else:
        x, y, z = g.block_pos()
        g.goto(x, y - 3, z, timeout=60)
        x, y, z = g.block_pos()
        solid = lambda i: (i in ("dirt", "cobblestone", "cobbled_deepslate",
                                 "netherrack", "andesite", "diorite", "granite")
                           or i.endswith("_planks"))
        if g.block(x, y + 2, z) == "air" and g.select(solid):
            # Click the side of a wall block above head height.
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if g.block(x + dx, y + 2, z + dz) != "air":
                    g.use_on(x + 0.5 + dx * 0.5, y + 2.5, z + 0.5 + dz * 0.5)
                    break
    g.wait(lambda: not g.is_night(), 700, 2)
    return True


def build_base(g, facts, depth=8, size=5):
    """Dig a room `depth` blocks below the surface and furnish it."""
    for spec, n in (("crafting_table", 1), ("stone", 8), ("planks", 8)):
        if not crafting.ensure(g, spec, n):
            return False
    x, y, z = g.block_pos()
    floor, r = max(y - depth, 12), size // 2
    g.status = "digging base"
    g.b(f"sel 1 {x - r} {floor} {z - r}")
    g.b(f"sel 2 {x + r} {floor + 2} {z + r}")
    g.b("sel cleararea")
    g.sleep(3)
    g.wait(lambda: not g.baritone.getBuilderProcess().isActive(), 900, 2)
    g.b("sel clear")
    g.stop()
    if not g.goto(x, floor, z, timeout=120, near=1):
        g.log("could not reach base room")
        return False
    g.status = "furnishing base"
    # Mining the room gave us plenty of cobble; restock what the dig used.
    for item in ("crafting_table", "furnace", "chest"):
        if g.count(lambda i: i == item) < 1 and not crafting.craft(g, item):
            g.log(f"base: could not craft {item}")
            continue
        g.place_block(item)
    if g.count(CLASSES["coal"]) and crafting.craft(g, "torch"):
        g.place_block("torch")
    return {"base": [x, floor, z]}


def smelt(g, facts, input, output, count):
    """Smelt `count` items in a furnace (nearby, at the base, or placed)."""
    n = min(count, g.count(matcher(input)))
    if n == 0:
        return False
    if not crafting.ensure(g, "planks", math.ceil(n / 1.5) + 1):  # fuel
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
    for spec, slot in ((input, 0), ("planks", 1)):
        stack = next(s for s in m.player_inventory() if matcher(spec)(short(s.item)))
        g.click(g.menu_slot(stack.slot, 3))
        g.click(slot)
        g.click(g.menu_slot(stack.slot, 3))  # leftovers back, if any
    done = lambda: g.menu().getSlot(2).getItem().getCount() >= n
    # Each item takes 10 s. Do not use g.sleep here: defending would close
    # the furnace screen, so smelt somewhere safe (the base).
    end = time.time() + n * 10 + 20
    while time.time() < end and not done():
        time.sleep(2)
    for slot in (2, 0, 1):
        g.click(slot, 0, "QUICK_MOVE")
    g.close_screen()
    return g.count(matcher(output)) >= n


def build_farm(g, facts, plots=8):
    """Wheat farm next to natural water: collect seeds, till, plant.
    Harvest later with Baritone's `farm` process."""
    seeds = lambda i: i == "wheat_seeds"
    if g.count(lambda i: i.endswith("_hoe")) < 1 and not crafting.craft(g, "stone_hoe"):
        return False
    if g.count(seeds) < plots and not gather(g, facts, "grass", "wheat_seeds", plots, 420):
        if g.count(seeds) < 2:
            return False
    g.status = "looking for water"
    g.b("goto water")
    g.sleep(2)
    g.wait(lambda: not g.busy(), 180, 1)
    g.stop()
    px, py, pz = g.block_pos()
    region = m.get_block_region([px - 6, py - 3, pz - 6], [px + 6, py + 2, pz + 6])
    tillable = ("grass_block", "dirt")
    spots = []
    for x in range(px - 6, px + 7):
        for y in range(py - 3, py + 2):
            for z in range(pz - 6, pz + 7):
                if short(region.get_block(x, y, z)) != "water":
                    continue
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0),
                               (0, 2), (0, -2), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                    c = (x + dx, y, z + dz)
                    inside = (abs(c[0] - px) <= 6 and abs(c[2] - pz) <= 6
                              and c[1] + 1 <= py + 2)
                    if (inside and c not in spots
                            and short(region.get_block(*c)) in tillable
                            and short(region.get_block(c[0], c[1] + 1, c[2]))
                            in ("air", "short_grass")):
                        spots.append(c)
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


def idle(g, facts):
    g.status = "idle"
    g.sleep(10)
    return True


SKILLS = {"gather": gather, "craft": craft, "hunt": hunt, "shelter": shelter,
          "build_base": build_base, "smelt": smelt, "build_farm": build_farm,
          "idle": idle}
