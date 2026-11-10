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


LEASH = 160  # blocks from home a bot may wander while working


def come_home(g, facts, force=False, leash=LEASH):
    """Keep the team together: if we strayed beyond the leash, walk back
    towards home (the base, or world spawn before there is one)."""
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
        come_home(g, facts)
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


ARMOR = ("_helmet", "_chestplate", "_leggings", "_boots")


def craft(g, facts, item, count=1):
    ok = crafting.craft(g, item, count)
    if ok and item == "shield":
        g.equip_offhand("shield")
    elif ok and item.endswith(ARMOR):
        g.sleep(1)  # give Meteor's Auto Armor a moment first
        g.equip_armor(item)
    return ok


def hunt(g, facts, count=6, timeout=600, animals=ANIMALS, item="food"):
    """Kill animals until `count` items matching `item` are held. Animals
    near home get used up (they do not respawn), so hunters range twice
    as far as other workers."""
    food = matcher(item)
    end, misses = time.time() + timeout, 0
    while g.count(food) < count and time.time() < end:
        come_home(g, facts, leash=2 * LEASH)
        g.status = f"hunting {animals} for {item} {g.count(food)}/{count}"
        prey = m.entities(type=f".*\\.({animals})$", max_distance=60,
                          sort="nearest", limit=1)
        if not prey:
            misses += 1
            if misses > 20:
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
            if math.dist(me, pos) > 2.8:
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
    """Sit out the night. With a base: go there and sleep in our bed (which
    skips the night once enough of the team is in bed). Without one: dig a
    3-deep hole and seal it."""
    g.status = "sheltering for the night"
    base = facts.get("base")
    if base and math.dist(g.pos(), base) < LEASH \
            and g.goto(*base, timeout=180, near=2):
        bed = facts.get(f"bed_{g.name}")
        last_try = 0

        def morning():
            nonlocal last_try
            if bed and not g.sleeping() and time.time() - last_try > 8 \
                    and not g.hostiles(8):
                last_try = time.time()
                g.status = "going to sleep"
                g.use_on(bed[0] + 0.5, bed[1] + 0.4, bed[2] + 0.5)
            return not g.is_night()
        g.wait(morning, 700, 2)
    else:
        seal_in(g)
        g.wait(lambda: not g.is_night(), 700, 2)
    return True


def seal_in(g):
    """Dig three blocks down and close the hole above our head."""
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
    seal_in(g)
    g.wait(lambda: g.health() <= 0 or (g.health() >= 16 and not g.hostiles(10)),
           240, 2)
    return True


def recover(g, facts, pos):
    """Walk back to where we died and pick the dropped items up."""
    g.status = f"recovering gear at {pos}"
    g.goto_near(*pos, radius=1, timeout=200)
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
    g.b("sel cleararea")
    g.sleep(3)
    g.wait(lambda: not g.baritone.getBuilderProcess().isActive(), 900, 2)
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


def fix_base(g, facts):
    """Fill holes in the base floor (Baritone digs down into the room, and
    coal in the floor gets mined) and put back missing furniture."""
    x, y, z = facts["base"]
    g.status = "repairing base"
    filler = lambda i: i in ("cobblestone", "cobbled_deepslate", "dirt",
                             "andesite", "diorite", "granite", "tuff")
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            if g.block(x + dx, y - 1, z + dz) not in AIR + ("water", "lava"):
                continue
            item = next((i for i in g.inv() if filler(i)), None)
            if item is None:
                return
            # A deeper hole needs its bottom filled first.
            if g.block(x + dx, y - 2, z + dz) in AIR:
                g.place_at(item, x + dx, y - 2, z + dz)
            g.place_at(item, x + dx, y - 1, z + dz)
    for item, dx in FURNITURE:
        if g.block(x + dx, y, z - 2) == item or g.find_block(item, 4):
            continue
        if g.count(lambda i: i == item) or crafting.craft(g, item):
            g.place_at(item, x + dx, y, z - 2, stand=(x, y, z))


BED_SLOTS = (0, -1, 1, -2, 2)


def bed(g, facts):
    """Get a bed (wool from sheep), put it in the base and sleep-click it
    once so it becomes our respawn point."""
    x, y, z = facts["base"]
    wool = lambda: max((n for i, n in g.inv().items() if i.endswith("_wool")),
                       default=0)
    while not g.count(CLASSES["bed"]):
        if wool() >= 3:
            colour = max((i for i in g.inv() if i.endswith("_wool")),
                         key=lambda i: g.inv()[i])[:-len("_wool")]
            if not crafting.craft(g, f"{colour}_bed"):
                return False
        elif not hunt(g, facts, count=g.count(CLASSES["wool"]) + 1,
                      animals="sheep", item="wool", timeout=480):
            return False  # no sheep found this time
    bed_item = next(i for i in g.inv() if i.endswith("_bed"))
    g.status = "placing bed in base"
    if not g.goto(x, y, z, timeout=300, near=1):
        return False
    fix_base(g, facts)
    for dx in BED_SLOTS:
        foot, head, stand = (x + dx, y, z + 1), (x + dx, y, z + 2), (x + dx, y, z - 1)
        if not all(g.block(*c) in AIR for c in (foot, head, stand)):
            continue
        if not g.place_at(bed_item, *foot, stand=stand):
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


def find_water(g, center, radius=24):
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
    if not g.count(rod) and not crafting.craft(g, "fishing_rod"):
        return False
    food = CLASSES["food"]
    home = facts.get("base") or g.block_pos()
    water = None
    for _ in range(4):
        water = find_water(g, g.block_pos()) or find_water(g, home, 40)
        if water:
            break
        g.b("explore")  # loads more chunks around us
        g.sleep(20)
        g.stop()
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
    """Harvest and replant the wheat farm with Baritone, then bake bread."""
    farm = facts["farm"]
    g.status = "harvesting the farm"
    g.goto_near(*farm, radius=2, timeout=180)
    if g.count(lambda i: i == "wheat_seeds") == 0:
        g.log("harvest: no seeds to replant with, keeping the first ones")
    g.b(f"farm {radius}")
    g.sleep(3)
    g.wait(lambda: not g.baritone.getFarmProcess().isActive(), 240, 2)
    g.stop()
    wheat = g.count(lambda i: i == "wheat")
    if wheat >= 3:
        crafting.craft(g, "bread", wheat // 3)
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


def build_farm(g, facts, plots=8):
    """Wheat farm next to natural water: collect seeds, till, plant.
    Harvest later with Baritone's `farm` process."""
    seeds = lambda i: i == "wheat_seeds"
    if g.count(lambda i: i.endswith("_hoe")) < 1 and not crafting.craft(g, "stone_hoe"):
        return False
    if g.count(seeds) < plots and not gather(g, facts, "grass", "wheat_seeds", plots, 420):
        if g.count(seeds) < 2:
            return False
    spots = []
    for attempt in range(8):
        g.status = "looking for water"
        spots = farm_spots(g)
        if len(spots) >= 2:
            break
        g.b("explore")
        g.sleep(25)
        g.stop()
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


def idle(g, facts):
    g.status = "idle"
    g.sleep(10)
    return True


SKILLS = {"gather": gather, "craft": craft, "hunt": hunt, "shelter": shelter,
          "build_base": build_base, "smelt": smelt, "build_farm": build_farm,
          "deliver": deliver, "assist": assist, "idle": idle,
          "dig_in": dig_in, "recover": recover,
          "light_base": light_base, "bed": bed, "fish": fish,
          "harvest": harvest}
