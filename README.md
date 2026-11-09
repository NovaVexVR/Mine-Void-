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
| Bot brain | `bots/agent.py`, `bots/minevoid/` | Minescript autoruns the agent on join. `game.py` wraps Minescript/Baritone/Meteor, `craft.py` crafts by clicking real inventory slots, `skills.py` holds the skills. |
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

Skills available to plans: `gather`, `craft`, `hunt`, `build_base`,
`build_farm`, `smelt`, `shelter`, `idle` (see `bots/minevoid/skills.py`).
Bots defend themselves against hostile mobs in the background of every skill.

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

See the bottom of this file's history / the PR description for what has been
verified in a live run. Phases after iron (diamonds, Nether, blaze rods, ender
pearls, stronghold, dragon) are listed in the plan as `todo` and have no
skills yet.

## Notes

* The server runs in offline mode because the bots have no Microsoft
  accounts. Keep it bound to localhost / behind the whitelist.
* `scripts/setup-server.sh` writes `eula=true`, i.e. you accept the
  [Minecraft EULA](https://aka.ms/MinecraftEULA) by running it.
* Baritone runs with `legitMine` for ores: with anti-xray on, ore positions
  in chunk data are fake, so bots only go for ore they can actually see.
