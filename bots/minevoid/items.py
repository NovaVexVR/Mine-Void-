"""Item classes shared by the coordinator (plan conditions) and the bots."""

FOODS = ("beef", "porkchop", "mutton", "chicken", "rabbit", "cooked_beef",
         "cooked_porkchop", "cooked_mutton", "cooked_chicken", "cooked_rabbit",
         "bread", "apple", "baked_potato", "carrot", "sweet_berries", "cod",
         "salmon", "cooked_cod", "cooked_salmon", "melon_slice")

CLASSES = {
    "log": lambda i: i.endswith(("_log", "_stem")),
    "planks": lambda i: i.endswith("_planks"),
    "stone": lambda i: i in ("cobblestone", "cobbled_deepslate", "blackstone"),
    "coal": lambda i: i in ("coal", "charcoal"),
    "pickaxe": lambda i: i.endswith("_pickaxe"),
    "sword": lambda i: i.endswith("_sword"),
    "food": lambda i: i in FOODS,
    "wool": lambda i: i.endswith("_wool"),
    "bed": lambda i: i.endswith("_bed"),
}


def matcher(spec):
    """Predicate for 'a|b' where each part is an item id or a class name."""
    preds = [CLASSES.get(p, lambda i, p=p: i == p) for p in spec.split("|")]
    return lambda item: any(p(item) for p in preds)


def count(inv, spec):
    pred = matcher(spec)
    return sum(n for item, n in inv.items() if pred(item))
