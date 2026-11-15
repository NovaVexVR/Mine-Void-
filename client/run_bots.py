#!/usr/bin/env python3
"""Run bots on this machine against a server and coordinator elsewhere.

    python client/run_bots.py --server HOST:25565 --coord http://HOST:8765 \\
        --token SECRET Void4 Void5

Works on Linux, Windows and macOS (needs Java 21 and Python 3.10+). The first
run downloads the game, Fabric and the mods into client/cache. Each bot gets
a small window on a desktop, or a virtual display on a Linux server. The bot
names must be on the server's whitelist (on the server:
scripts/mc-cmd.sh "whitelist add Void4"). Ctrl+C stops them all.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="+", help="bot names, e.g. Void4 Void5")
    ap.add_argument("--server", required=True, help="Minecraft server host:port")
    ap.add_argument("--coord", required=True, help="coordinator URL, e.g. http://host:8765")
    ap.add_argument("--token", default=os.environ.get("MINEVOID_TOKEN"),
                    help="the coordinator's COORD_TOKEN")
    ap.add_argument("--headless", action="store_true",
                    help="Linux only: virtual display instead of windows")
    a = ap.parse_args()

    if not shutil.which("java"):
        sys.exit("Java 21 is needed (https://adoptium.net/) and must be on PATH")
    if not os.path.exists(os.path.join(HERE, "cache", "launch.json")):
        print("first run: downloading the client (a few minutes)")
        subprocess.check_call([sys.executable, os.path.join(HERE, "install.py")])

    env = dict(os.environ, MC_SERVER=a.server, MINEVOID_COORD=a.coord.rstrip("/"))
    if a.token:
        env["MINEVOID_TOKEN"] = a.token
    flags = ["--fg", "--headless" if a.headless else "--window"]
    procs = []
    for i, name in enumerate(a.names):
        if i:
            time.sleep(20)  # client start-up is CPU heavy: stagger it
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(HERE, "launch.py"), name] + flags, env=env))
    print(f"{len(procs)} bot(s) started; Ctrl+C stops them")
    try:
        while any(p.poll() is None for p in procs):
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
