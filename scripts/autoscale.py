#!/usr/bin/env python3
"""Run as many bots as this computer can spare: scripts/autoscale.py

Meant for running Mine-Void on your everyday computer. Every 30 seconds it
looks at free memory, at how much CPU everything *except* Mine-Void is using,
and at whether you are at the keyboard, then starts or stops one bot:

- you are active (keyboard/mouse used recently, or other programs busy):
  at most --active-max bots (default 1), and fewer if memory or CPU runs short;
- you are away: one more bot every few minutes while memory and CPU allow,
  up to --max (default 6).

Stopping a bot just disconnects it: it keeps its items and carries on where it
was when it is started again. The coordinator hands its job to someone else
after 90 seconds. The server and coordinator are never touched.

Override the detection by writing a word into run/autoscale-mode:
  idle    - treat the computer as unused (more bots)
  active  - treat it as in use (fewest bots)
  pause   - change nothing
  auto    - (or no file) decide from the measurements
`scripts/autoscale.py --status` prints what it measures and would do, once.
Decisions are logged to run/autoscale.log.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INST = os.path.join(ROOT, "client", "instances")
MODE_FILE = os.path.join(ROOT, "run", "autoscale-mode")
LOG = os.path.join(ROOT, "run", "autoscale.log")
COORD = os.environ.get("MINEVOID_COORD", "http://127.0.0.1:8765")
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TICKS = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


# -- measurements ------------------------------------------------------------
def mem_available_gb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1048576
    return 0.0


def cpu_total():
    v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    return sum(v) - v[3] - v[4]  # busy ticks (all but idle and iowait)


def ours(pid):
    """Is this process part of Mine-Void (server, coordinator, bots, Xvfb)?"""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if cwd.startswith(ROOT):
            return True
        return ROOT.encode() in open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return False


def our_ticks():
    total = 0
    for p in os.listdir("/proc"):
        if p.isdigit() and ours(p):
            try:
                f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
                total += int(f[11]) + int(f[12])  # utime + stime
            except (OSError, IndexError, ValueError):
                pass
    return total


def sample_cpu(seconds):
    """(cores busy with other programs, cores busy with Mine-Void) over seconds."""
    t0, o0 = cpu_total(), our_ticks()
    time.sleep(seconds)
    t1, o1 = cpu_total(), our_ticks()
    whole = (t1 - t0) / TICKS / seconds
    mine = (o1 - o0) / TICKS / seconds
    return max(0.0, whole - mine), mine


def user_idle_seconds():
    """Seconds since keyboard/mouse input, or None if it can't be told."""
    if os.environ.get("DISPLAY") and shutil.which("xprintidle"):
        try:
            return int(subprocess.check_output(["xprintidle"], timeout=5)) / 1000
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    if shutil.which("loginctl"):  # Wayland (KDE, GNOME) sets IdleHint
        try:
            user = os.environ.get("USER") or os.getlogin()
            out = subprocess.check_output(
                ["loginctl", "list-sessions", "--no-legend"], text=True, timeout=5)
            idle = []
            for row in out.splitlines():
                cols = row.split()
                if len(cols) < 3 or cols[2] != user:
                    continue
                info = dict(l.split("=", 1) for l in subprocess.check_output(
                    ["loginctl", "show-session", cols[0], "-p", "Type", "-p", "IdleHint",
                     "-p", "IdleSinceHintMonotonic", "-p", "Active"],
                    text=True, timeout=5).splitlines() if "=" in l)
                if info.get("Type") not in ("x11", "wayland") or info.get("Active") != "yes":
                    continue
                if info.get("IdleHint") != "yes":
                    idle.append(0)
                else:
                    since = int(info.get("IdleSinceHintMonotonic") or 0) / 1e6
                    idle.append(max(0, time.clock_gettime(time.CLOCK_MONOTONIC) - since)
                                if since else 3600)
            if idle:
                return min(idle)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    return None


# -- bots --------------------------------------------------------------------
def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def running():
    out = []
    for f in glob.glob(os.path.join(INST, "Void*", "pids")):
        pids = open(f).read().split()
        if pids and pid_alive(pids[0]):
            out.append(os.path.basename(os.path.dirname(f)))
    return sorted(out, key=lambda n: int(re.sub(r"\D", "", n) or 0))


def bot_rss_gb(name):
    try:
        pid = open(os.path.join(INST, name, "pids")).read().split()[0]
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1048576
    except (OSError, IndexError):
        pass
    return None


def start_bot(name):
    subprocess.call([os.path.join(ROOT, "scripts", "mc-cmd.sh"), f"whitelist add {name}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    flags = os.environ.get("LAUNCH_FLAGS", "--headless").split()
    # (own session: signals meant for the autoscaler must not reach the bot)
    subprocess.call([sys.executable, os.path.join(ROOT, "client", "launch.py"), name] + flags,
                    stdout=subprocess.DEVNULL, start_new_session=True)


def stop_bot(name):
    f = os.path.join(INST, name, "pids")
    for pid in open(f).read().split():
        try:
            os.kill(int(pid), 15)
        except (OSError, ValueError):
            pass
    os.remove(f)


def pick_to_stop(names):
    """The bot whose leaving costs least: one at home doing a short job,
    else the highest-numbered."""
    try:
        st = json.load(HTTP.open(COORD + "/state", timeout=5))
        bots, base = st.get("bots", {}), st.get("facts", {}).get("base")
    except Exception:  # noqa: BLE001
        bots, base = {}, None

    def cost(n):
        b = bots.get(n) or {}
        pos = b.get("pos")
        far = 0
        if base and pos:
            far = sum((a - c) ** 2 for a, c in zip(pos, base)) ** 0.5
        return (far > 40, -int(re.sub(r"\D", "", n) or 0))
    return min(names, key=cost)


# -- policy ------------------------------------------------------------------
def decide(a, state):
    mem = mem_available_gb()
    other, mine = sample_cpu(a.sample)
    idle = user_idle_seconds()
    mode = "auto"
    if os.path.exists(MODE_FILE):
        mode = open(MODE_FILE).read().strip().lower() or "auto"
    bots = running()
    sizes = [s for s in map(bot_rss_gb, bots) if s]
    per_bot = max(a.bot_gb, max(sizes) if sizes else 0)

    busy_other = other / NCPU
    if mode == "idle":
        away = True
    elif mode == "active":
        away = False
    else:
        away = (idle is None or idle >= a.idle_minutes * 60) and busy_other < a.busy
    cap = a.max if away else a.active_max
    why = (f"{len(bots)} bots, {mem:.1f} GB free, other programs {busy_other:.0%} CPU, "
           f"mine-void {mine / NCPU:.0%}, input idle "
           f"{'?' if idle is None else f'{idle / 60:.0f} min'}, mode {mode} -> "
           f"{'away' if away else 'in use'}, cap {cap}")
    now = time.time()
    if mode == "pause":
        return None, why
    if bots and len(bots) > a.min and (
            mem < a.reserve_gb or busy_other > a.busy_stop or len(bots) > cap):
        if now - state.get("stopped", 0) >= 45:
            return ("stop", pick_to_stop(bots)), why
        return None, why
    room = mem - a.reserve_gb - per_bot
    spare_cpu = NCPU - other - mine
    if (len(bots) < max(cap, a.min) and (len(bots) < a.min or (
            room > 0 and spare_cpu > a.bot_cores and busy_other < a.busy))
            and now - state.get("started", 0) >= a.start_every):
        taken = set(bots)
        name = next(f"Void{i}" for i in range(1, 100) if f"Void{i}" not in taken)
        return ("start", name), why
    return None, why


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min", type=int, default=1, help="never fewer bots (default 1)")
    ap.add_argument("--max", type=int, default=6, help="most bots while you're away (6)")
    ap.add_argument("--active-max", type=int, default=1,
                    help="most bots while you use the computer (1)")
    ap.add_argument("--reserve-gb", type=float, default=4.0,
                    help="memory always left free for you (4 GB)")
    ap.add_argument("--bot-gb", type=float, default=2.2,
                    help="memory one bot needs, at least (2.2 GB; measured once running)")
    ap.add_argument("--bot-cores", type=float, default=0.8,
                    help="CPU cores one bot needs (0.8)")
    ap.add_argument("--idle-minutes", type=float, default=5,
                    help="no keyboard/mouse this long = you're away (5)")
    ap.add_argument("--busy", type=float, default=0.25,
                    help="other programs using more than this share of the CPU = in use")
    ap.add_argument("--busy-stop", type=float, default=0.6,
                    help="... more than this = stop a bot even below the cap")
    ap.add_argument("--start-every", type=float, default=180,
                    help="seconds between starting bots (180)")
    ap.add_argument("--sample", type=float, default=30, help="seconds per measurement")
    ap.add_argument("--status", action="store_true", help="measure once, print, change nothing")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)

    state = {}
    if a.status:
        a.sample = min(a.sample, 5)
        act, why = decide(a, state)
        print(why)
        print("would:", " ".join(act) if act else "change nothing")
        return
    pidfile = os.path.join(ROOT, "run", "autoscale.pid")
    if os.path.exists(pidfile) and pid_alive(open(pidfile).read().strip() or 0):
        sys.exit("autoscale is already running (scripts/stop.sh stops it with the bots)")
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    log(f"autoscale on: {a.min}..{a.max} bots ({a.active_max} while in use), "
        f"{a.reserve_gb} GB kept free")
    while True:
        act, why = decide(a, state)
        if act:
            verb, name = act
            log(f"{verb} {name}: {why}")
            if verb == "start":
                start_bot(name)
                state["started"] = time.time()
            else:
                stop_bot(name)
                state["stopped"] = time.time()
        elif int(time.time()) % 600 < a.sample:
            log(why)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
