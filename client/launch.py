#!/usr/bin/env python3
"""Launch one headless bot client: client/launch.py <BotName> [--fg]

Each bot gets its own game dir (client/instances/<name>) and its own Xvfb
display, shares the downloaded jars/assets, and auto-joins the server. The
Minescript autorun hook starts bots/agent.py once the bot is in the world.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "cache")
SERVER = os.environ.get("MC_SERVER", "127.0.0.1:25565")
HEAP = os.environ.get("CLIENT_HEAP", "1536M")

OPTIONS = """\
renderDistance:{rd}
simulationDistance:5
maxFps:10
graphicsMode:0
graphicsPreset:"fast"
ao:false
renderClouds:"false"
particles:2
entityShadows:false
entityDistanceScaling:0.5
biomeBlendRadius:0
mipmapLevels:0
enableVsync:false
fullscreen:false
pauseOnLostFocus:false
soundCategory_master:0.0
narrator:0
tutorialStep:none
skipMultiplayerWarning:true
joinedFirstServer:true
onboardAccessibility:false
autoJump:false
"""


def offline_uuid(name):
    """Same UUID an offline-mode server derives for this name."""
    d = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode()).digest())
    d[6] = d[6] & 0x0F | 0x30
    d[8] = d[8] & 0x3F | 0x80
    return str(uuid.UUID(bytes=bytes(d)))


def display_for(name):
    """Stable X display number per bot, kept in client/instances/displays.json."""
    path = os.path.join(HERE, "instances", "displays.json")
    table = json.load(open(path)) if os.path.exists(path) else {}
    if name not in table:
        table[name] = 90 + len(table)
        json.dump(table, open(path, "w"))
    return table[name]


def prepare(name):
    inst = os.path.join(HERE, "instances", name)
    os.makedirs(os.path.join(inst, "mods"), exist_ok=True)
    with open(os.path.join(CACHE, "launch.json")) as f:
        launch = json.load(f)
    for jar in os.listdir(launch["mods_dir"]):
        link = os.path.join(inst, "mods", jar)
        if not os.path.lexists(link):
            os.symlink(os.path.join(launch["mods_dir"], jar), link)
    mdst = os.path.join(inst, "minescript", "system", "mappings",
                        launch["mc_version"])
    os.makedirs(mdst, exist_ok=True)
    for f in os.listdir(launch["mappings_dir"]):
        if not os.path.exists(os.path.join(mdst, f)):
            shutil.copy(os.path.join(launch["mappings_dir"], f), mdst)
    opts = os.path.join(inst, "options.txt")
    if not os.path.exists(opts):
        with open(opts, "w") as f:
            f.write(OPTIONS.format(rd=os.environ.get("RENDER_DISTANCE", "6")))
    # Minescript: point it at the system python and the repo's bot scripts.
    ms = os.path.join(inst, "minescript")
    os.makedirs(ms, exist_ok=True)
    with open(os.path.join(ms, "config.txt"), "w") as f:
        f.write(f'python="{shutil.which("python3")}"\n')
        f.write(f'command_path="{os.path.join(ROOT, "bots")}"\n')
        f.write("autorun[*]=agent\n")
    return inst, launch


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    name = args[0]
    inst, launch = prepare(name)
    disp = display_for(name)

    xvfb = subprocess.Popen(
        ["Xvfb", f":{disp}", "-screen", "0", "640x480x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    env = dict(os.environ, DISPLAY=f":{disp}", LIBGL_ALWAYS_SOFTWARE="1",
               GALLIUM_DRIVER="llvmpipe", LP_NUM_THREADS="1",
               MINEVOID_BOT=name)
    cmd = [
        "java", f"-Xmx{HEAP}", "-Xms256M", "-XX:+UseSerialGC",
        "-XX:TieredStopAtLevel=4", "-Dfabric.log.disableAnsi=true",
        "-cp", os.pathsep.join(launch["classpath"]), launch["main_class"],
        "--username", name, "--uuid", offline_uuid(name),
        "--accessToken", "0", "--userType", "legacy",
        "--version", "fabric-" + launch["mc_version"],
        "--gameDir", inst, "--assetsDir", launch["assets_dir"],
        "--assetIndex", launch["asset_index"],
        "--width", "320", "--height", "240",
        "--quickPlayMultiplayer", SERVER,
    ]
    log = open(os.path.join(inst, "client.out"), "w")
    game = subprocess.Popen(cmd, cwd=inst, env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    with open(os.path.join(inst, "pids"), "w") as f:
        f.write(f"{game.pid} {xvfb.pid}\n")
    print(f"{name}: game pid {game.pid}, display :{disp}, log {inst}/client.out")
    if "--fg" in sys.argv:
        try:
            game.wait()
        finally:
            xvfb.terminate()


if __name__ == "__main__":
    main()
