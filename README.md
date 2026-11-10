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
* `requires` / `repeat` – only for bots that hold something (e.g. raw iron).

The plan's `sharing` rules make it a team effort: a bot holding spare food or
iron walks to a teammate who is short and hands it over, a bot in trouble
gets the nearest teammate sent to `assist`, and everyone stays within a
leash of home so they can reach each other.

Skills available to plans: `gather`, `craft`, `hunt`, `build_base`,
`build_farm`, `smelt`, `shelter`, `deliver`, `assist`, `idle` (see
`bots/minevoid/skills.py`). Bots eat, defend themselves, dig in when badly
hurt and recover their gear after dying in the background of every skill.

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

## Status

Verified in live runs with three bots:

* server + anti-cheat + anti-xray, headless clients joining, autorun agent
* wood -> wooden tools -> stone tools (about 6-8 minutes per bot from nothing)
* hunting, eating, night shelter, underground base room with table + chest
* legit iron mining, smelting, iron pickaxe and sword (one bot, ~15 minutes)
* role split by the coordinator (hunter / base builder / farmer / miners),
  home leash, recovery after death

Written but not yet seen working end to end: item hand-offs (`deliver`),
`assist`, and the wheat farm. Phases after iron (diamonds, Nether, blaze
rods, ender pearls, stronghold, dragon) are `todo` in the plan with no skills.

Known problems:

* **Nights kill bots.** Unarmoured bots die to mob packs, and mobs camp the
  world spawn so a respawning bot can die repeatedly. Next steps: beds (skip
  the night, move the spawn point into the base), shields and iron armour.
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
