"""Crafting by clicking real inventory slots, like a player would.

Recipes are (pattern, key, yield). Patterns that fit 2x2 are crafted in the
inventory grid, the rest on a crafting table (found nearby or placed).
Missing intermediate items (planks, sticks, table) are crafted recursively;
missing raw materials are gathered through `gather_hook`.
"""
import math
import re
import time

import minescript as m

from .game import short
from .items import matcher

TOOL = {"pickaxe": ("MMM", " S ", " S "), "axe": ("MM", "MS", " S"),
        "sword": ("M", "M", "S"), "shovel": ("M", "S", "S"),
        "hoe": ("MM", " S", " S")}
RECIPES = {
    "planks": (("L",), {"L": "log"}, 4),
    "stick": (("P", "P"), {"P": "planks"}, 4),
    "crafting_table": (("PP", "PP"), {"P": "planks"}, 1),
    "chest": (("PPP", "P P", "PPP"), {"P": "planks"}, 1),
    "furnace": (("CCC", "C C", "CCC"), {"C": "stone"}, 1),
    "torch": (("C", "S"), {"C": "coal", "S": "stick"}, 4),
    "bucket": (("I I", " I "), {"I": "iron_ingot"}, 1),
    "bread": (("WWW",), {"W": "wheat"}, 1),
    "shears": ((" I", "I "), {"I": "iron_ingot"}, 1),
    "fishing_rod": (("  S", " SX", "S X"), {"S": "stick", "X": "string"}, 1),
    "shield": (("PIP", "PPP", " P "), {"P": "planks", "I": "iron_ingot"}, 1),
    "bow": ((" SX", "S X", " SX"), {"S": "stick", "X": "string"}, 1),
    "arrow": (("F", "S", "E"), {"F": "flint", "S": "stick", "E": "feather"}, 4),
    "paper": (("CCC",), {"C": "sugar_cane"}, 3),
    "book": (("PP", "PL"), {"P": "paper", "L": "leather"}, 1),
    "bookshelf": (("PPP", "BBB", "PPP"), {"P": "planks", "B": "book"}, 1),
    "enchanting_table": ((" B ", "DOD", "OOO"),
                         {"B": "book", "D": "diamond", "O": "obsidian"}, 1),
    "flint_and_steel": (("I", "F"), {"I": "iron_ingot", "F": "flint"}, 1),
    "blaze_powder": (("R",), {"R": "blaze_rod"}, 2),
    "ender_eye": (("PB",), {"P": "ender_pearl", "B": "blaze_powder"}, 1),
    "golden_helmet": (("GGG", "G G"), {"G": "gold_ingot"}, 1),
    "leather_chestplate": (("L L", "LLL", "LLL"), {"L": "leather"}, 1),
    "leather_boots": (("L L", "L L"), {"L": "leather"}, 1),
    "iron_helmet": (("III", "I I"), {"I": "iron_ingot"}, 1),
    "iron_chestplate": (("I I", "III", "III"), {"I": "iron_ingot"}, 1),
    "iron_leggings": (("III", "I I", "I I"), {"I": "iron_ingot"}, 1),
    "iron_boots": (("I I", "I I"), {"I": "iron_ingot"}, 1),
}
for _mat, _ing in (("wooden", "planks"), ("stone", "stone"), ("iron", "iron_ingot"),
                   ("diamond", "diamond")):
    for _tool, _pat in TOOL.items():
        RECIPES[f"{_mat}_{_tool}"] = (_pat, {"M": _ing, "S": "stick"}, 1)

# Beds need three wool of one colour.
for _c in ("white orange magenta light_blue yellow lime pink gray light_gray "
           "cyan purple blue brown green red black").split():
    RECIPES[f"{_c}_bed"] = (("WWW", "PPP"), {"W": f"{_c}_wool", "P": "planks"}, 1)

# Doors need six planks of one wood.
for _w in ("oak spruce birch jungle acacia dark_oak mangrove cherry pale_oak "
           "bamboo crimson warped").split():
    RECIPES[f"{_w}_door"] = (("PP", "PP", "PP"), {"P": f"{_w}_planks"}, 3)
    RECIPES[f"{_w}_fence_gate"] = (("SPS", "SPS"), {"P": f"{_w}_planks", "S": "stick"}, 1)

# Pen walls: cobblestone is what every bot carries plenty of.
RECIPES["cobblestone_wall"] = (("CCC", "CCC"), {"C": "cobblestone"}, 6)

# Raw materials a bot can go and get by itself: class -> (block group, count pad)
RAW = {"log": "logs", "stone": "stone"}
gather_hook = None  # set by skills: gather_hook(g, blocks, item_spec, count)


def needs(item, times):
    pattern, key, _ = RECIPES[item]
    out = {}
    for row in pattern:
        for ch in row:
            if ch != " ":
                out[key[ch]] = out.get(key[ch], 0) + times
    return out


def ensure(g, spec, n):
    """Make sure the inventory holds n items matching spec."""
    have = g.count(matcher(spec))
    if have >= n:
        return True
    if spec in RECIPES:
        return craft(g, spec, math.ceil((n - have) / RECIPES[spec][2]))
    if spec in RAW and gather_hook:
        return gather_hook(g, RAW[spec], spec, n)
    g.log(f"cannot obtain {n} x {spec}")
    return False


def open_table(g):
    if "raft" in (m.screen_name() or ""):
        return True
    if not g.find_block("crafting_table", 6) and not ensure(g, "crafting_table", 1):
        return False
    return g.open_block("crafting_table", "raft")


def clear_grid(g):
    """Shift-click anything left in the 2x2 inventory grid back out. (A
    crafting table returns its grid by itself when the screen closes.)"""
    for slot in (1, 2, 3, 4):
        g.click(slot, 0, "QUICK_MOVE")


_book = {}  # item -> recipe display ids, from the client's recipe book


def book_ids(g, item):
    """Recipe-book entries whose result is `item` (cached per session)."""
    # Built once, refreshed every 10 minutes (new recipes unlock as items
    # turn up) - not on every miss: a full scan takes ~6 s, and doing it for
    # each craft of something not in the book froze the bots.
    if time.time() - _book.get("_built", 0) > 600:
        _book.clear()
        _book["_built"] = time.time()
        try:
            cols = g.mc.player.getRecipeBook().getCollections()
            for i in range(cols.size()):
                rs = cols.get(i).getRecipes()
                for j in range(rs.size()):
                    e = rs.get(j)
                    text = str(e)
                    k = text.find("result=")
                    hit = re.search(r"minecraft:([a-z0-9_]+)", text[k:]) if k >= 0 else None
                    if hit:
                        _book.setdefault(hit.group(1), []).append(e.id())
        except Exception as ex:
            g.log(f"recipe book unreadable: {ex}")
    return _book.get(item, []) if item != "_built" else []


def book_craft(g, item, times, small):
    """Craft through the recipe book: one request places the whole recipe
    in the grid (the server checks the ingredients), then the result is
    taken - about a second instead of 5-20 s of slot clicks. Falls back
    (False) when the recipe is not in our book or nothing comes out."""
    ids = book_ids(g, item)
    if not ids:
        return False
    if small:
        g.close_screen()
    elif not open_table(g):
        return False
    g.sync_menu()
    menu = g.menu()
    pred = lambda i: i == item or (item == "planks" and i.endswith("_planks"))
    before = g.count(pred)
    made = 0
    for _ in range(times):
        placed = False
        for rid in ids:  # (several recipes can make the same thing)
            g.mc.gameMode.handlePlaceRecipe(menu.containerId, rid, False)
            if g.wait_plain(lambda: not menu.getSlot(0).getItem().isEmpty(), 1.5):
                placed = True
                break
        if not placed:
            break
        g.click(0, 0, "QUICK_MOVE")
        time.sleep(0.3)
        made += 1
    if small:
        clear_grid(g)
    else:
        g.close_screen()
    after = g.count(pred)
    if after > before:
        g.log(f"crafted {item} (recipe book): {before} -> {after}")
    return after > before and made >= times


def craft(g, item, times=1, retries=3):
    """Craft `item` `times` times. Returns True if the output arrived."""
    pattern, key, _ = RECIPES[item]
    g.status = f"crafting {item}"
    g.fight()  # never start UI work with a mob in reach
    width = max(len(r) for r in pattern)
    small = width <= 2 and len(pattern) <= 2
    if not small and not g.find_block("crafting_table", 6) \
            and not ensure(g, "crafting_table", 1):
        return False
    # Making one ingredient can eat another (sticks and tables use planks),
    # so repeat until a whole pass finds everything in place.
    want = needs(item, times)
    for _ in range(4):
        if all(g.count(matcher(spec)) >= n for spec, n in want.items()):
            break
        for spec, n in want.items():
            if not ensure(g, spec, n):
                return False
    else:
        return False
    if book_craft(g, item, times, small):
        return True
    if times > 1:
        # Keep every grid cell fed from a single stack: a cell cannot mix
        # e.g. oak and birch planks. Split into batches the first stack covers.
        batch = times
        for spec, n in needs(item, 1).items():
            first = next((s.count for s in m.player_inventory()
                          if matcher(spec)(short(s.item))), 0)
            batch = min(batch, max(1, first // n))
        if batch < times:
            return craft(g, item, batch) and craft(g, item, times - batch)
    if small:
        g.close_screen()
        grid_w, top = 2, None
        clear_grid(g)
    else:
        if not open_table(g):
            g.log("could not open a crafting table")
            return False
        grid_w, top = 3, 10
    before = g.count(lambda i: i == item or (item == "planks" and i.endswith("_planks")))
    for r, row in enumerate(pattern):
        for c, ch in enumerate(row):
            if ch == " ":
                continue
            if g.hostiles(5):
                # Attacked mid-craft: drop the UI, deal with it, start over.
                clear_grid(g) if small else g.close_screen()
                g.fight()
                return retries > 0 and craft(g, item, times, retries - 1)
            pred, left = matcher(key[ch]), times
            while left > 0:
                stack = next((s for s in m.player_inventory()
                              if s.slot < 36 and pred(short(s.item))), None)
                if stack is None:
                    g.log(f"ran out of {key[ch]} while crafting {item}")
                    clear_grid(g) if small else g.close_screen()
                    return False
                src = g.menu_slot(stack.slot, top)
                g.click(src)                       # pick the stack up
                put = min(left, stack.count)
                for _ in range(put):
                    g.click(1 + r * grid_w + c, 1)  # right-click: drop one
                g.click(src)                       # put the rest back
                left -= put
    # The server computes the result; wait for it to show up.
    menu = g.menu()
    g.wait_plain(lambda: not menu.getSlot(0).getItem().isEmpty(), 4)
    g.click(0, 0, "QUICK_MOVE")                    # shift-click the result
    time.sleep(0.4)
    if not small:
        g.close_screen()
    after = g.count(lambda i: i == item or (item == "planks" and i.endswith("_planks")))
    g.log(f"crafted {item}: {before} -> {after}")
    if after <= before and retries > 0:
        g.close_screen()  # the table hands back what is left in its grid
        return craft(g, item, times, retries - 1)
    return after > before
