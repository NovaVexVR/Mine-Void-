# Mine-Void

A team of Minecraft bots that work together towards a goal you can change at
runtime. The bots are **real Minecraft clients** (Fabric + Meteor + Baritone +
Minescript) running headless, playing on a Paper server protected by an
anti-cheat and anti-xray.

First goal: **beat the game** (kill the Ender Dragon), broken into phases in
[`goals/beat_the_game.json`](goals/beat_the_game.json).

## Quick start

```bash
scripts/start.sh 3        # server + coordinator + 3 bots (downloads on first run)
scripts/status.sh         # what every bot is doing right now
scripts/stop.sh --server  # stop everything
```

Dashboard: <http://127.0.0.1:8765/> (auto-refreshing), JSON at `/state`.
Needs Java 21, Python 3.10+, Xvfb and Mesa (software OpenGL).

## How it fits together

```
 goals/*.json ──> coordinator (HTTP :8765) <── status / inventory every 3 s
                      │ task + shared facts
        ┌─────────────┼─────────────┐
     Void1          Void2         Void3      real clients under Xvfb
   Minescript ── bots/agent.py ── skills ──> Baritone (pathing, mining)
                                        └──> Meteor (auto-eat, armor, respawn)
                      │
              Paper 1.21.11  (GrimAC, anti-xray, LuckPerms, EssentialsX, ...)
```

| Part | Where | Notes |
|---|---|---|
| Server | `server/`, `scripts/setup-server.sh` | Paper 1.21.11. **GrimAC** anti-cheat, Paper **anti-xray engine-mode 2** (hidden blocks become fake ores), LuckPerms, EssentialsX, CoreProtect, Chunky. Offline mode, bound to `127.0.0.1`, whitelist on. |
| Client | `client/install.py`, `client/launch.py` | Vanilla client + Fabric loader fetched from Mojang/Fabric. Mods: Meteor Client, Baritone, Minescript, Fabric API, Sodium, Lithium, FerriteCore. One game dir and one Xvfb display per bot. |
| Bot brain | `bots/agent.py`, `bots/minevoid/` | Minescript autoruns the agent on join. `brain.py` is the report/assign/act loop, `game.py` wraps Minescript/Baritone/Meteor, `craft.py` crafts by clicking real inventory slots, `skills.py` holds the skills. |
| Coordinator | `coordinator/coordinator.py` | Reads the goal plan, assigns each bot the first unfinished task it may take, keeps shared facts (base location, ...). |

### The goal plan

A plan is a list of phases, each a list of tasks:

```json
{"id": "stone_pickaxe", "scope": "each", "skill": "craft",
 "args": {"item": "stone_pickaxe"}, "have": {"stone_pickaxe|iron_pickaxe": 1}}
```

* `scope: each` – every bot does it; `scope: team` – up to `slots` bots.
* `have` / `skip_if_have` – the task counts as done while the bot's inventory
  satisfies it, so a bot that dies and loses its gear automatically drops back
  to the earlier phases.
* `fact` – done once a bot publishes that fact (e.g. `base`).
* `surface: true` – bots shelter at night instead of doing it.

Switch goal while everything is running:

```bash
curl -X POST localhost:8765/goal -d '{"file": "goals/my_goal.json"}'
```

* `team_have` – done once the whole team holds that much **per bot**.
* `requires` / `requires_any` / `needs_fact` – the task only applies to bots
  that hold something (raw iron, wool) or once a fact exists (`base`).
* `repeat` / `cooldown` – repeatable jobs (smelting, harvesting every 20 min).
* `preempt` – worth dropping other work for (placing a bed).

Assignments are sticky: a bot keeps its job until it finishes or fails,
unless a teammate needs help or a `preempt` task applies.

**Teamwork.** The coordinator hands out roles (two hunters, a base builder, a
farmer, two iron miners) and turns the plan's `sharing` rules and `kit` into
jobs between bots: a bot with spare food walks over to a hungry teammate and
hands it over, a bot in trouble gets the nearest teammate sent to `assist`,
and everyone stays within a leash of home.

**The iron kit** (`kit` in the plan) is the gear every bot should own, in
priority order: iron pickaxe, shield, iron sword, then chestplate, helmet,
boots and leggings (30 iron each). Miners keep mining until the whole team's
kit is paid for; spare iron is handed out one tier at a time, so everybody
has a pickaxe before anybody gets a chestplate. Shields go to the off-hand
and are raised against skeletons; Meteor's Auto Armor wears the armour.

**Base, beds and nights.** The base is a 5x5 room dug 8 blocks under world
spawn with a crafting table, furnace and chest, lit with torches. Each bot
brings three wool (from sheep), crafts a bed at the base and places it there,
which moves its respawn point into the base. At night every bot goes home and
sleeps; with `players_sleeping_percentage` at 30 (set by `server/start.sh`)
one sleeper out of three skips the night.

**Food.** Two hunters range twice as far as other workers (animals near home
do not respawn), raw meat is cooked at the base, a fisher uses Meteor's Auto
Fish once someone has string for a rod, and the wheat farm is harvested and
replanted with Baritone's `farm` and baked into bread.

Skills available to plans: `gather`, `craft`, `hunt`, `fish`, `smelt`,
`build_base`, `light_base`, `bed`, `build_farm`, `harvest`, `shelter`,
`deliver`, `assist`, `idle` (see `bots/minevoid/skills.py`). In the
background of every skill bots eat, fight (backing off from creepers), dig
in when badly hurt or unarmed among mobs, and go back for their gear once
after dying.

### Scaling

`scripts/start.sh N` starts N bots (`Void1..VoidN`). Each client renders on
the CPU, so budget roughly **one core and 1.5 GB RAM per bot** plus the
server. Bots only need to reach the coordinator URL (`MINEVOID_COORD`) and
the server (`MC_SERVER`), so they can be spread over several machines.

### Developing

Skill code hot-reloads: edit `bots/minevoid/*.py` and each bot picks it up at
its next task. Run Python inside a live bot with:

```bash
scripts/botexec.py Void1 'print(g.inv(), g.pos())'
scripts/mc-cmd.sh "time set day"      # server console command
```

## Roadmap

Towards the first goal (kill the Ender Dragon). Done items are verified in
live runs; see Status for details.

**Stage 1 – early game** (mostly working, being stabilised)
1. Wood -> wooden -> stone tools, food, underground base at spawn – done
2. Sustainable food: shared animal sightings (done), cooking (done), wheat
   farm + harvests, fishing once someone has string, breeding animals
3. A bed for every bot in the base (respawn point, sleep through nights) –
   two of three so far; shears to shear sheep instead of killing them
4. Iron kit for every bot: pickaxe, shears, shield, sword, full armour

**Stage 2 – mid game**
5. Diamonds (real tunnels at diamond depth; anti-xray hides ore positions)
6. Enchanting: enchanting table (obsidian, diamonds, a book), bookshelves
   (books from sugar cane paper + leather), lapis, and XP from mining,
   smelting and mob kills; enchant the kit (Protection, Sharpness,
   Efficiency, Unbreaking, Power on a bow). Later an anvil to combine books
   and repair gear
7. Nether portal (obsidian via bucket + lava, or mined with diamond pick)
8. Nether: gold armour for piglins, fortress, ~6 blaze rods
9. ~12 ender pearls (endermen, or bartering gold with piglins)

**Stage 3 – the dragon**
10. Eyes of ender, triangulate the stronghold from two throw points
11. Activate the portal, bring blocks, bows, beds and food
12. Destroy the end crystals, kill the dragon

**Infrastructure** (alongside)
* A small Fabric mod for instant inventory work (crafting via Minescript
  reflection takes 5-20 s)
* Bots on your own machines: coordinator reachable over the network with a
  token, launcher for your OS
* More than three bots; dashboard with death and anti-cheat stats

## Status

Seen working in live runs with three bots:

* server + anti-cheat + anti-xray, headless clients joining, autorun agent
* wood -> wooden tools -> stone tools (about 6-8 minutes per bot from nothing)
* hunting, eating, night shelter in the base, underground base with table,
  furnace and chest
* legit iron mining, smelting, iron pickaxe and sword
* role split, home leash, recovery after death, sticky roles
* a bed crafted at the base and placed, "Respawn point set", the bot
  sleeping in it at night

Written but not yet seen end to end: item hand-offs (`deliver`), `assist`
reaching a rescue, the iron kit hand-outs, shields/armour, fishing, farm
harvests, skipping a night (needs two beds). Phases after iron (diamonds,
Nether, blaze rods, ender pearls, stronghold, dragon) are `todo`.

Known problems:

* **Food is scarce.** Animals near spawn are used up and do not respawn, so
  hunters range far and slowly; fishing needs string first. This is the
  current bottleneck: hungry bots cannot regenerate health.
* **Wool for beds** depends on finding sheep; only one bed so far.
* GrimAC cancels attacks from beyond 3 blocks (`Reach`), so attack range is
  capped at 2.9. Its `Simulation` (movement) check also fired in two bursts
  on one bot early on; the cause was not pinned down.
* Reflection calls from Minescript are slow (a craft takes 5-20 s). A small
  purpose-built Fabric mod would make inventory work instant.

## Notes

* The server runs in offline mode because the bots have no Microsoft
  accounts. Keep it bound to localhost / behind the whitelist.
* `scripts/setup-server.sh` writes `eula=true`, i.e. you accept the
  [Minecraft EULA](https://aka.ms/MinecraftEULA) by running it.
* Baritone runs with `legitMine` for ores: with anti-xray on, ore positions
  in chunk data are fake, so bots only go for ore they can actually see.
