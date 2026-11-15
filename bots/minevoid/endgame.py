"""Endgame skills, roadmap items 10-16: enchanting, obsidian, the nether
portal, blaze rods, ender pearls, eyes of ender, the stronghold and the
dragon.

Baritone keeps a cache of a few rare blocks it has seen (spawners, end
portal frames, nether portals, enchanting tables), so `goto spawner` finds
a fortress's blaze spawner and `goto end_portal_frame` the portal room once
their chunks have been loaded. Anti-xray only fakes blocks that touch no
air, so neither is hidden from us.
"""
import math
import time

import minescript as m

from . import craft as crafting
from . import skills
from .game import short
from .items import CLASSES
from .skills import (AIR, branch_mine, fill_bucket, gather, height_map,
                     pick_up_drops, store, withdraw)


def has(g, item):
    return g.count(lambda i: i == item)


def cells_named(centre, names, r=4, down=1, up=2):
    """Cells around `centre` holding one of `names` (block ids, any state)."""
    cx, cy, cz = (math.floor(v) for v in centre)
    cells = [[x, y, z] for x in range(cx - r, cx + r + 1)
             for y in range(cy - down, cy + up + 1)
             for z in range(cz - r, cz + r + 1)]
    found = []
    for i in range(0, len(cells), 4096):  # (Minescript caps one request)
        chunk = cells[i:i + 4096]
        found += [(c, k) for c, k in zip(chunk, m.getblocklist(chunk))
                  if k and short(k) in names]
    return found


def wait_arrival(g, goal, near, timeout):
    """After a Baritone goto, wait until within `near` of goal (x, z) or
    Baritone gives up."""
    g.sleep(2)
    g.wait(lambda: math.dist(g.pos()[::2], goal) < near or not g.busy(), timeout, 2)
    return math.dist(g.pos()[::2], goal) < near


# ---------------------------------------------------------------------------
# 10. Enchanting

def enchanting_table(g, facts):
    """Craft the enchanting table (a book, two diamonds, four obsidian) and
    put it in the base room's free corner."""
    if facts.get("enchanting_table"):
        return {"enchanting_table": facts["enchanting_table"]}
    x, y, z = facts["base"]
    spot = (x - 2, y, z - 2)
    for item, n in (("book", 1), ("diamond", 2), ("obsidian", 4)):
        if has(g, item) < n:
            withdraw(g, facts, item, n - has(g, item))
        if has(g, item) < n:
            g.log(f"enchanting: need {n} {item}")
            return False
    if not has(g, "enchanting_table") and not crafting.craft(g, "enchanting_table"):
        return False
    g.goto(x, y, z, timeout=180, near=1)
    if g.block(*spot) not in AIR:
        g.select(lambda i: i.endswith("_pickaxe"))
        g.break_block(*spot, timeout=8)
    if not g.place_at("enchanting_table", *spot, stand=(x - 1, y, z - 1)):
        return False
    store(g, facts)
    return {"enchanting_table": list(spot)}


ENCHANT_ORDER = ("_sword", "_pickaxe", "_chestplate", "bow", "_leggings",
                 "_helmet", "_boots", "_axe")


def is_enchanted(stack):
    return "enchantments" in (stack.nbt or "")


def enchant(g, facts):
    """Enchant the kit, best item first, with whatever levels and lapis we
    have (no bookshelves: up to level 8 offers)."""
    if not facts.get("enchanting_table"):
        return False
    if has(g, "lapis_lazuli") < 3:
        withdraw(g, facts, "lapis_lazuli", 3 - has(g, "lapis_lazuli"))
    done = 0
    for suffix in ENCHANT_ORDER:
        level = g.mc.player.experienceLevel
        if level < 1 or not has(g, "lapis_lazuli"):
            break
        stack = next((s for s in m.player_inventory()
                      if short(s.item).endswith(suffix) and not is_enchanted(s)
                      and not short(s.item).startswith(("wooden_", "stone_", "golden_"))),
                     None)
        if stack is None:
            continue
        if not g.open_block("enchanting_table", "nchant"):
            g.log("enchanting: could not open the table")
            return done > 0
        g.sync_menu()
        menu = g.menu()
        # slot 0 the item, slot 1 lapis, then the player inventory
        slot = next((s.slot for s in m.player_inventory()
                     if short(s.item) == short(stack.item) and not is_enchanted(s)), None)
        lapis = next((s.slot for s in m.player_inventory()
                      if short(s.item) == "lapis_lazuli"), None)
        if slot is None or lapis is None:
            g.close_screen()
            break
        g.click(g.menu_slot(slot, 2), 0, "QUICK_MOVE")
        g.click(g.menu_slot(lapis, 2), 0, "QUICK_MOVE")
        g.sleep(1)
        costs = list(menu.costs)
        best = max((i for i in range(3)
                    if 0 < costs[i] <= level and i + 1 <= has_lapis_in_menu(menu)),
                   default=None)
        if best is not None:
            g.mc.gameMode.handleInventoryButtonClick(menu.containerId, best)
            g.sleep(1)
            done += 1
            g.log(f"enchanting: {short(stack.item)} for {costs[best]} levels")
        g.click(0, 0, "QUICK_MOVE")
        g.click(1, 0, "QUICK_MOVE")
        g.close_screen()
        if best is None:
            break
    return done > 0


def has_lapis_in_menu(menu):
    st = menu.getSlot(1).getItem()
    return 0 if st.isEmpty() else st.getCount()


# ---------------------------------------------------------------------------
# 11. Obsidian and the nether portal

def obsidian(g, facts, count=10, timeout=1800):
    """Cast obsidian where the deep mine ran into lava: water poured into
    the tunnel's end flows onto the lava (sources turn to obsidian, flowing
    lava to cobble), then a diamond pickaxe mines it out. With no lava known
    yet, extend the deep mine until a branch finds some."""
    if not has(g, "diamond_pickaxe"):
        return False
    if not has(g, "water_bucket") and not fill_bucket(g, facts):
        return False
    end, searches = time.time() + timeout, 0
    while has(g, "obsidian") < count and time.time() < end:
        spots = dict(facts.get("lava") or {}, **g.pending_facts.get("lava", {}))
        spots = {k: v for k, v in spots.items() if v}
        if not spots:
            if searches >= 3:
                g.log("obsidian: no lava found")
                break
            searches += 1
            g.status = "extending the deep mine to find lava"
            branch_mine(g, facts, blocks="diamond", item="diamond", count=99,
                        length=24, timeout=600, level=-58, name="deep_mine")
            continue
        key, spot = next(iter(spots.items()))
        if not cast_obsidian(g, spot, count, end):
            spots[key] = None  # used up (or unreachable): drop it
            g.pending_facts["lava"] = spots
    g.log(f"obsidian: holding {has(g, 'obsidian')}")
    return has(g, "obsidian") >= count


def cast_obsidian(g, spot, count, end):
    ex, y, ez, dx, dz = spot
    g.status = "going to the lava in the deep mine"
    if not g.goto(ex - dx, y, ez - dz, timeout=500):
        return False
    progress = False
    for _ in range(10):
        if has(g, "obsidian") >= count or time.time() > end:
            return True
        g.status = f"casting obsidian ({has(g, 'obsidian')})"
        if not cells_named((ex, y, ez), ("lava",), r=3, down=1, up=2):
            return progress
        if not has(g, "water_bucket"):
            g.log("obsidian: lost the water")
            return progress
        # 1. water into the tunnel's last cell, standing one cell back
        g.select(lambda i: i == "water_bucket")
        g.use_on(ex + 0.5, y + 0.02, ez + 0.5, hold=0.1)
        g.sleep(2)
        # 2. open the face ahead: the water runs onto whatever lava is there
        cx, cz = ex + dx, ez + dz
        for c in ((cx, y + 1, cz), (cx, y, cz)):
            if g.block(*c) not in AIR + ("water", "lava", "obsidian"):
                g.select(lambda i: i.endswith("_pickaxe"))
                g.break_block(*c, timeout=8)
        g.sleep(3)
        # 3. mine what hardened in reach (about 9 s a block)
        obs = sorted((c for c, _ in cells_named((ex, y, ez), ("obsidian",), r=3, down=1, up=2)),
                     key=lambda c: math.dist(c, g.pos()))
        for c in obs:
            if math.dist((c[0] + .5, c[1] + .5, c[2] + .5),
                         (g.pos()[0], g.pos()[1] + 1.6, g.pos()[2])) > 4.4:
                continue
            g.select(lambda i: i == "diamond_pickaxe")
            g.break_block(*c, timeout=14)
            progress = True
        # 4. the water back into the bucket, then pick the drops up
        g.select(lambda i: i == "bucket")
        g.use_on(ex + 0.5, y + 0.15, ez + 0.5, hold=0.1)
        pick_up_drops(g, (ex, y, ez), 5)
        # 5. one step forward if the way is open and dry
        if all(g.block(*c) in AIR for c in ((cx, y, cz), (cx, y + 1, cz))):
            ex, ez = cx, cz
        g.goto(ex - dx, y, ez - dz, timeout=30)
    return progress


def portal_site(g, facts, hm=None):
    """A flat strip by the stairs: four columns for the frame (in an
    x-row) and two rows in front of it to stand on."""
    ex, ey, ez = facts["base_exit"]
    bx = facts["base"][0]
    hm = hm or height_map(g, ex, ez, ey, 18)
    pen = facts.get("pen") or {}
    px0, pz0 = (pen.get("corner") or (10 ** 6, 10 ** 6))
    best = None
    for x0 in range(ex - 16, ex + 13):
        for z0 in range(ez - 16, ez + 13):
            cols = [(x, z) for x in range(x0 - 1, x0 + 5) for z in range(z0 - 1, z0 + 3)]
            if any(c not in hm for c in cols):
                continue
            if any(abs(z - ez) <= 2 and bx <= x <= ex + 2 for x, z in cols):
                continue  # the stairs
            if any(px0 - 1 <= x <= px0 + 6 and pz0 - 1 <= z <= pz0 + 6 for x, z in cols):
                continue  # the pen
            hs = {hm[c][0] for c in cols}
            if len(hs) != 1 or any(hm[c][1] not in skills.GROUND_OK
                                   or hm[c][2] not in AIR + ("short_grass", "tall_grass", "snow")
                                   for c in cols):
                continue
            d = math.dist((x0 + 2, z0), (ex, ez))
            if 6 <= d and (best is None or d < best[0]):
                best = (d, x0, z0, hs.pop())
    return best and best[1:]


def build_portal(g, facts):
    """A nether portal by the stairs: obsidian frame with cobblestone
    corners, built with Baritone's builder (it climbs and scaffolds as it
    needs), lit with flint and steel."""
    if facts.get("portal"):
        return {"portal": facts["portal"]}
    if has(g, "obsidian") < 10:
        withdraw(g, facts, "obsidian", 10 - has(g, "obsidian"))
    if has(g, "obsidian") < 10:
        g.log("portal: need 10 obsidian")
        return False
    if not has(g, "flint_and_steel"):
        if not has(g, "iron_ingot"):
            withdraw(g, facts, "iron_ingot", 1)
        if not has(g, "flint"):
            gather(g, facts, "gravel", "flint", 1, timeout=400, until_dark=True)
        if not crafting.craft(g, "flint_and_steel"):
            g.log("portal: no flint and steel")
            return False
    if has(g, "cobblestone") < 8:
        gather(g, facts, "stone", "cobblestone", 16, timeout=400)
    ex, ey, ez = facts["base_exit"]
    g.status = "choosing a spot for the nether portal"
    if not g.goto(ex, ey, ez, timeout=300, near=2):
        return False
    site = portal_site(g, facts)
    if not site:
        g.log("portal: no flat spot near the stairs")
        return False
    x0, z0, h = site
    y0 = h + 1
    g.log(f"portal: building at {[x0, y0, z0]}")
    passes = [
        ("cobblestone", [((x0, y0), (x0, y0)), ((x0 + 3, y0), (x0 + 3, y0))]),
        ("obsidian", [((x0 + 1, y0), (x0 + 2, y0)), ((x0, y0 + 1), (x0, y0 + 3)),
                      ((x0 + 3, y0 + 1), (x0 + 3, y0 + 3))]),
        ("cobblestone", [((x0, y0 + 4), (x0, y0 + 4)), ((x0 + 3, y0 + 4), (x0 + 3, y0 + 4))]),
        ("obsidian", [((x0 + 1, y0 + 4), (x0 + 2, y0 + 4))]),
    ]
    for block, boxes in passes:
        g.status = f"building the nether portal ({block})"
        g.b("sel clear")
        for (ax, ay), (bx_, by) in boxes:
            g.b(f"sel 1 {ax} {ay} {z0}")
            g.b(f"sel 2 {bx_} {by} {z0}")
        g.b(f"sel set {block}")
        g.sleep(3)
        g.wait(lambda: not g.baritone.getBuilderProcess().isActive(), 300, 2)
        g.b("sel clear")
        g.stop()
    frame = [(x, y0, z0) for x in (x0 + 1, x0 + 2)] + \
        [(x, y, z0) for x in (x0, x0 + 3) for y in range(y0 + 1, y0 + 4)] + \
        [(x, y0 + 4, z0) for x in (x0 + 1, x0 + 2)]
    missing = [c for c in frame if g.block(*c) != "obsidian"]
    for c in missing:
        # the builder stopped early: place what is left by hand
        g.place_against("obsidian", *c, stand=(c[0], y0, z0 + 1))
    missing = [c for c in frame if g.block(*c) != "obsidian"]
    if missing:
        g.log(f"portal: frame incomplete at {missing}")
        return False
    # clear the inside (the builder may have left scaffolding)
    for x in (x0 + 1, x0 + 2):
        for y in range(y0 + 1, y0 + 4):
            if g.block(x, y, z0) not in AIR:
                g.break_block(x, y, z0, timeout=8)
    g.goto(x0 + 1, y0, z0 + 2, timeout=30)
    g.select(lambda i: i == "flint_and_steel")
    g.use_on(x0 + 1.5, y0 + 1.0, z0 + 0.5, hold=0.1)
    g.sleep(1)
    if g.block(x0 + 1, y0 + 1, z0) != "nether_portal":
        g.log("portal: would not light")
        return False
    g.log(f"portal: lit at {[x0 + 1, y0 + 1, z0]}")
    return {"portal": [x0 + 1, y0 + 1, z0]}


def through_portal(g, cell, want_dim, timeout=30):
    """Walk into the portal block `cell` and wait for the dimension
    change. (Baritone will not path into a portal; walk the last step.)"""
    if g.dim() == want_dim:
        return True
    x, y, z = cell
    for front in ((x, y, z + 1), (x, y, z - 1), (x + 1, y, z), (x - 1, y, z)):
        if g.block(*front) in AIR and g.block(front[0], front[1] + 1, front[2]) in AIR:
            break
    g.goto(*front, timeout=120)
    g.stop()
    end = time.time() + timeout
    while time.time() < end and g.dim() != want_dim:
        here = g.block_pos()
        if g.block(*here) not in ("nether_portal", "end_portal"):
            m.player_look_at(x + 0.5, y + 1.0, z + 0.5)
            m.player_press_forward(True)
            time.sleep(0.25)
            m.player_press_forward(False)
        time.sleep(0.5)
        g._dim_at = 0
    return g.dim() == want_dim


def nearest_portal_block(g, r=6):
    found = cells_named(g.block_pos(), ("nether_portal",), r=r, down=3, up=3)
    return min((c for c, _ in found), key=lambda c: math.dist(c, g.pos()), default=None)


def to_nether(g, facts):
    if g.dim() == "the_nether":
        return True
    portal = facts.get("portal")
    if not portal:
        return False
    g.status = "going through the nether portal"
    m.chat(".toggle auto-armor off")  # (it swapped the gold helmet off)
    wear_gold(g)
    if not through_portal(g, portal, "the_nether"):
        return False
    g.sleep(2)
    if not facts.get("nether_portal"):
        cell = nearest_portal_block(g) or g.block_pos()
        g.pending_facts["nether_portal"] = list(cell)
    return True


def to_overworld(g, facts):
    if g.dim() == "overworld":
        return True
    np = facts.get("nether_portal") or g.pending_facts.get("nether_portal")
    g.status = "heading back to the portal"
    if np:
        g.goto(*np, timeout=600, near=3)
    else:
        g.b("goto nether_portal")
        g.sleep(2)
        g.wait(lambda: not g.busy(), 600, 2)
    cell = nearest_portal_block(g)
    if not cell:
        g.log("nether: cannot find the way home")
        return False
    ok = through_portal(g, cell, "overworld")
    m.chat(".toggle auto-armor on")
    return ok


def wear_gold(g):
    """Piglins leave a player in gold armour alone: swap a golden helmet
    in for the trip."""
    inv = m.player_inventory()
    if any(s.slot == 39 and short(s.item) == "golden_helmet" for s in inv):
        return True
    st = next((s for s in inv if short(s.item) == "golden_helmet"), None)
    if st is None:
        return False
    g.close_screen()
    g.sync_menu()
    g.click(g.menu_slot(st.slot, None))  # pick it up
    g.click(5)                           # head slot (swaps with what is there)
    g.click(g.menu_slot(st.slot, None))  # put the old helmet down
    time.sleep(0.3)
    return True


# ---------------------------------------------------------------------------
# 12. Blaze rods

def blaze_rods(g, facts, count=6, timeout=2400):
    """Through the portal, find a fortress's blaze spawner (Baritone's
    block cache knows spawners in every chunk it has loaded), kill blazes
    there, come back with the rods."""
    rods = lambda: has(g, "blaze_rod")
    if not to_nether(g, facts):
        return False
    end, explored = time.time() + timeout, 0
    try:
        while rods() < count and time.time() < end:
            if g.health() < 8 and not g.count(CLASSES["food"]):
                g.log("nether: too hurt to go on")
                break
            spawner = facts.get("blaze_spawner") or g.pending_facts.get("blaze_spawner")
            if not spawner:
                g.status = "looking for a nether fortress"
                g.b("goto spawner")
                g.sleep(3)
                if g.busy():
                    g.wait(lambda: not g.busy(), 300, 2)
                found = cells_named(g.block_pos(), ("spawner",), r=6, down=4, up=4)
                if found:
                    g.pending_facts["blaze_spawner"] = list(found[0][0])
                    continue
                explored += 1
                g.status = "exploring the nether for a fortress"
                g.b("explore")
                g.sleep(45)
                g.stop()
                continue
            g.status = f"hunting blazes ({rods()}/{count} rods)"
            if math.dist(g.pos(), spawner) > 6:
                g.goto_near(*spawner, radius=3, timeout=240)
            hunt_end = time.time() + 90
            while time.time() < hunt_end and rods() < count:
                blazes = m.entities(type=".*\\.blaze$", max_distance=20, sort="nearest")
                if not blazes:
                    g.sleep(2)
                    continue
                b = blazes[0]
                d = math.dist(b.position, g.pos())
                if d > 5 and g.shoot(b):
                    continue
                g.fight(radius=8, timeout=10)
                pick_up_drops(g, b.position, 4)
            pick_up_drops(g, spawner, 8)
    finally:
        g.stop()
        to_overworld(g, facts)
    g.log(f"nether: back with {rods()} blaze rods")
    return rods() > 0


# ---------------------------------------------------------------------------
# 14. Eyes of ender and the stronghold

def make_eyes(g, facts, count=14):
    """Blaze powder from rods, eyes from powder and pearls (both from the
    chest, where hunters and the nether party leave them)."""
    for item in ("ender_pearl", "blaze_rod"):
        withdraw(g, facts, item, 64)
    want = max(0, count - has(g, "ender_eye"))
    n = min(want, has(g, "ender_pearl"))
    powder = max(0, n - has(g, "blaze_powder"))
    if powder:
        crafting.craft(g, "blaze_powder", min(has(g, "blaze_rod"), math.ceil(powder / 2)))
    n = min(n, has(g, "blaze_powder"))
    if n:
        crafting.craft(g, "ender_eye", n)
    g.log(f"eyes: {has(g, 'ender_eye')} eyes of ender")
    store(g, facts)  # spare rods and pearls back in the chest
    return has(g, "ender_eye") > 0


def throw_eye(g):
    """Throw an eye of ender and watch where it flies. Returns
    (x, z, dx, dz) from where we stood, or (x, z, 0, 0) if it rose straight
    up (we are on top of the stronghold). The eye usually drops back down:
    pick it up again."""
    if not g.select(lambda i: i == "ender_eye"):
        return None
    g.stop()
    x0, _, z0 = g.pos()
    m.player_set_orientation(m.player_orientation()[0], -20)
    m.player_press_use(True)
    time.sleep(0.15)
    m.player_press_use(False)
    track, end = [], time.time() + 3
    while time.time() < end:
        eyes = m.entities(type=".*eye_of_ender$", max_distance=30, sort="nearest", limit=1)
        if eyes:
            track.append(eyes[0].position)
        time.sleep(0.2)
    if len(track) < 3:
        return None
    dx, dz = track[-1][0] - track[0][0], track[-1][2] - track[0][2]
    g.sleep(2)
    pick_up_drops(g, track[-1], 6)
    if math.hypot(dx, dz) < 1:
        return (x0, z0, 0.0, 0.0)
    n = math.hypot(dx, dz)
    return (x0, z0, dx / n, dz / n)


def intersect(a, b):
    """Where two (x, z, dx, dz) rays cross, or None."""
    x1, z1, dx1, dz1 = a
    x2, z2, dx2, dz2 = b
    den = dx1 * dz2 - dz1 * dx2
    if abs(den) < 0.05:
        return None  # (near parallel: no useful fix)
    t = ((x2 - x1) * dz2 - (z2 - z1) * dx2) / den
    s = ((x2 - x1) * dz1 - (z2 - z1) * dx1) / den
    if t < 0 or s < 0:
        return None
    return (x1 + t * dx1, z1 + t * dz1)


def find_stronghold(g, facts):
    """Two eye throws ~300 blocks apart; the stronghold is roughly where the
    lines cross (a degree of error is a few hundred blocks out there, so
    open_end_portal refines the fix on the way)."""
    if facts.get("stronghold"):
        return {"stronghold": facts["stronghold"]}
    if has(g, "ender_eye") < 2:
        withdraw(g, facts, "ender_eye", 2)
    start = facts.get("base_exit") or g.block_pos()
    g.status = "throwing an eye of ender"
    g.goto(*start, timeout=300, near=2)
    a = throw_eye(g)
    if not a or not (a[2] or a[3]):
        g.log(f"stronghold: first throw gave {a}")
        return False
    px, pz = -a[3], a[2]  # perpendicular
    tx, tz = int(a[0] + px * 300), int(a[1] + pz * 300)
    g.status = "walking off to throw from a second spot"
    g.b(f"goto {tx} {tz}")
    if not wait_arrival(g, (tx, tz), 24, 600):
        g.log("stronghold: could not reach the second throw spot")
    g.stop()
    b = throw_eye(g)
    fix = b and (b[2] or b[3]) and intersect(a, b)
    if not fix:
        g.log(f"stronghold: no fix from {a} and {b}")
        return False
    sx, sz = round(fix[0]), round(fix[1])
    g.log(f"stronghold: lines cross at {[sx, sz]} ({round(math.dist(start[::2], fix))} blocks out)")
    return {"stronghold": [sx, sz]}


# ---------------------------------------------------------------------------
# 15. Opening the end portal

def frames_near(g, r=10):
    return cells_named(g.block_pos(), ("end_portal_frame",), r=r, down=6, up=4)


def open_end_portal(g, facts, timeout=3600):
    """Travel to the stronghold, find the portal room, fill the frame."""
    if facts.get("end_portal"):
        return {"end_portal": facts["end_portal"]}
    sx, sz = facts["stronghold"]
    if has(g, "ender_eye") < 12:
        withdraw(g, facts, "ender_eye", 64)
    end = time.time() + timeout
    for rnd in range(4):
        g.status = f"travelling to the stronghold at {[sx, sz]}"
        while math.dist(g.pos()[::2], (sx, sz)) > 32 and time.time() < end:
            g.b(f"goto {sx} {sz}")
            wait_arrival(g, (sx, sz), 32, 900)
            g.stop()
        # Refine: closer throws give a much better fix. An eye that drops
        # straight down means we are standing over it.
        if has(g, "ender_eye") <= 12:
            break
        a = throw_eye(g)
        if not a or not (a[2] or a[3]):
            break
        px, pz = -a[3], a[2]
        g.b(f"goto {int(a[0] + px * 80)} {int(a[1] + pz * 80)}")
        wait_arrival(g, (a[0] + px * 80, a[1] + pz * 80), 8, 300)
        g.stop()
        b = throw_eye(g)
        fix = b and (b[2] or b[3]) and intersect(a, b)
        if not fix:
            sx, sz = int(a[0] + a[2] * 64), int(a[1] + a[3] * 64)
        else:
            sx, sz = round(fix[0]), round(fix[1])
        g.log(f"stronghold: refined to {[sx, sz]} (round {rnd + 1})")
        g.pending_facts["stronghold"] = [sx, sz]
    tries = 0
    while not frames_near(g) and time.time() < end and tries < 8:
        tries += 1
        g.status = "looking for the portal room"
        g.b("goto end_portal_frame")
        g.sleep(3)
        if g.busy():
            g.wait(lambda: not g.busy(), 600, 2)
            continue
        if g.block_pos()[1] > 40:
            g.goto(math.floor(g.pos()[0]), 20, math.floor(g.pos()[2]), timeout=300)
            continue
        r = throw_eye(g) if has(g, "ender_eye") > 12 else None
        if r and (r[2] or r[3]):
            tx, tz = int(r[0] + r[2] * 40), int(r[1] + r[3] * 40)
        else:
            tx, tz = sx + (tries % 3 - 1) * 48, sz + (tries // 3 - 1) * 48
        g.goto(tx, 20, tz, timeout=300)
    frames = frames_near(g)
    if not frames:
        g.log("stronghold: no portal room found")
        return False
    for c, k in sorted(frames, key=lambda f: math.dist(f[0], g.pos())):
        if "eye=true" in k:
            continue
        if not has(g, "ender_eye"):
            break
        g.status = "filling the end portal frame"
        if math.dist(g.pos(), c) > 3.5:
            g.goto_near(*c, radius=2, timeout=30)
            g.stop()
        g.select(lambda i: i == "ender_eye")
        g.use_on(c[0] + 0.5, c[1] + 0.82, c[2] + 0.5, hold=0.1)
    g.sleep(1)
    lit = cells_named(g.block_pos(), ("end_portal",), r=10, down=6, up=4)
    empty = sum(1 for _, k in frames_near(g) if "eye=false" in k)
    if not lit:
        g.log(f"end portal: {empty} frames still empty")
        g.pending_facts["end_portal_missing"] = empty
        return False
    cx = round(sum(c[0] for c, _ in lit) / len(lit))
    cz = round(sum(c[2] for c, _ in lit) / len(lit))
    g.log(f"end portal: open at {[cx, lit[0][0][1], cz]}")
    return {"end_portal": [cx, lit[0][0][1], cz]}


# ---------------------------------------------------------------------------
# 16. The dragon

def dragon_dead():
    """The egg sits on the exit portal once the dragon has died."""
    cells = [[x, y, z] for x in (-1, 0, 1) for z in (-1, 0, 1) for y in range(58, 80)]
    return any(k and short(k) == "dragon_egg" for k in m.getblocklist(cells))


def end_fight(g, facts, timeout=3600):
    """Into the End: shoot the end crystals off the pillars, then the
    dragon while it flies, and hit it when it perches on the portal."""
    if facts.get("dragon_dead"):
        return {"dragon_dead": True}
    if g.dim() != "the_end":
        portal = facts.get("end_portal")
        if not portal:
            return False
        g.status = "going to the end portal"
        x, y, z = portal
        g.goto_near(x, y + 1, z + 2, radius=2, timeout=3600)
        g.stop()
        end = time.time() + 20
        while g.dim() != "the_end" and time.time() < end:
            m.player_look_at(x + 0.5, y, z + 0.5)
            m.player_press_forward(True)
            time.sleep(0.3)
            m.player_press_forward(False)
            time.sleep(0.7)
            g._dim_at = 0
        if g.dim() != "the_end":
            return False
        g.sleep(3)
    end = time.time() + timeout
    seen_dragon = False
    while time.time() < end:
        if dragon_dead():
            g.log("THE DRAGON IS DEAD")
            return {"dragon_dead": True}
        crystals = m.entities(type=".*end_crystal$", max_distance=160, sort="nearest")
        dragons = m.entities(type=".*ender_dragon$", max_distance=300)
        seen_dragon = seen_dragon or bool(dragons)
        if math.dist(g.pos()[::2], (0, 0)) > 30:
            # off the spawn platform and onto the island (Baritone bridges)
            g.status = "crossing to the island"
            g.b("goto 0 6")
            g.sleep(4)
        if crystals and has(g, "arrow"):
            g.status = f"shooting end crystals ({len(crystals)} left)"
            g.b("pause")
            try:
                g.shoot(crystals[0])
            finally:
                g.b("resume")
            g.sleep(0.5)
            continue
        if dragons:
            d = dragons[0]
            dx, dy, dz = d.position
            perched = math.dist((dx, dz), (0, 0)) < 8 and dy < 72
            g.status = "fighting the dragon" + (" (perched)" if perched else "")
            if perched:
                g.goto_near(0, 64, 3, radius=2, timeout=8)
                g.stop()
                m.player_look_at(dx, dy + 1.5, dz)
                target = m.player_get_targeted_entity(4.5)
                if target and "dragon" in (target.type or ""):
                    g.select(lambda i: i.endswith("_sword"))
                    m.player_press_attack(True)
                    time.sleep(0.1)
                    m.player_press_attack(False)
                    time.sleep(0.5)
                continue
            if has(g, "arrow") and math.dist(d.position, g.pos()) < 60:
                g.b("pause")
                try:
                    g.shoot(d)
                finally:
                    g.b("resume")
                continue
        g.sleep(2)
    return False


# Registered with the other skills (this module is loaded after them).
skills.SKILLS.update({
    "enchanting_table": enchanting_table, "enchant": enchant,
    "obsidian": obsidian, "build_portal": build_portal,
    "blaze_rods": blaze_rods, "make_eyes": make_eyes,
    "find_stronghold": find_stronghold, "open_end_portal": open_end_portal,
    "end_fight": end_fight,
})
