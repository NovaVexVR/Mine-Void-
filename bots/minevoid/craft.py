"""Crafting by clicking real inventory slots, like a player would.

Recipes are (pattern, key, yield). Patterns that fit 2x2 are crafted in the
inventory grid, the rest on a crafting table (found nearby or placed).
Missing intermediate items (planks, sticks, table) are crafted recursively;
missing raw materials are gathered through `gather_hook`.
"""
import math
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
    "shield": (("PIP", "PPP", " P "), {"P": "planks", "I": "iron_ingot"}, 1),
    "iron_helmet": (("III", "I I"), {"I": "iron_ingot"}, 1),
    "iron_chestplate": (("I I", "III", "III"), {"I": "iron_ingot"}, 1),
    "iron_leggings": (("III", "I I", "I I"), {"I": "iron_ingot"}, 1),
    "iron_boots": (("I I", "I I"), {"I": "iron_ingot"}, 1),
}
for _mat, _ing in (("wooden", "planks"), ("stone", "stone"), ("iron", "iron_ingot")):
    for _tool, _pat in TOOL.items():
        RECIPES[f"{_mat}_{_tool}"] = (_pat, {"M": _ing, "S": "stick"}, 1)

# Beds need three wool of one colour.
for _c in ("white orange magenta light_blue yellow lime pink gray light_gray "
           "cyan purple blue brown green red black").split():
    RECIPES[f"{_c}_bed"] = (("WWW", "PPP"), {"W": f"{_c}_wool", "P": "planks"}, 1)

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
    return after > before
