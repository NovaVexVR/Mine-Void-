#!/usr/bin/env bash
# One command to run the whole thing on your own Linux machine (written for
# Arch / CachyOS; apt and dnf work too):
#
#     ./play.sh            # server + coordinator + 3 bots
#     ./play.sh 5          # ... with 5 bots
#     HEADLESS=1 ./play.sh # bots on a virtual display instead of windows
#
# The first run installs what is missing (Java 21, Python, Xvfb), downloads
# Paper, the plugins and the client mods, and restores the newest world
# snapshot from snapshots/ (the cloud trial's world, base and all). Ctrl+C
# stops the bots, the coordinator and the server. Dashboard:
# http://127.0.0.1:8765/
set -euo pipefail
cd "$(dirname "$0")"
N="${1:-3}"

# -- dependencies ---------------------------------------------------------
java21() {
  for j in /usr/lib/jvm/java-21-openjdk/bin/java /usr/lib/jvm/java-21-openjdk-amd64/bin/java \
           /usr/lib/jvm/java-21/bin/java "$(command -v java 2>/dev/null || true)"; do
    [ -x "$j" ] && "$j" -version 2>&1 | grep -q 'version "2[1-9]' && { echo "$j"; return; }
  done
}
missing=()
[ -n "$(java21)" ] || missing+=(java)
command -v python3 >/dev/null || missing+=(python)
command -v curl >/dev/null || missing+=(curl)
[ -n "${HEADLESS:-}" ] && ! command -v Xvfb >/dev/null && missing+=(xvfb)
[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && ! command -v Xvfb >/dev/null && missing+=(xvfb)
if [ ${#missing[@]} -gt 0 ]; then
  echo "installing: ${missing[*]}"
  if command -v pacman >/dev/null; then
    declare -A pk=([java]=jdk21-openjdk [python]=python [curl]=curl [xvfb]=xorg-server-xvfb)
    sudo pacman -S --needed --noconfirm $(for m in "${missing[@]}"; do echo "${pk[$m]}"; done)
  elif command -v apt-get >/dev/null; then
    declare -A pk=([java]=openjdk-21-jre-headless [python]=python3 [curl]=curl [xvfb]=xvfb)
    sudo apt-get install -y $(for m in "${missing[@]}"; do echo "${pk[$m]}"; done)
  elif command -v dnf >/dev/null; then
    declare -A pk=([java]=java-21-openjdk [python]=python3 [curl]=curl [xvfb]=xorg-x11-server-Xvfb)
    sudo dnf install -y $(for m in "${missing[@]}"; do echo "${pk[$m]}"; done)
  else
    echo "please install: Java 21, Python 3.10+, curl (and Xvfb for headless bots)"; exit 1
  fi
fi
JAVA="$(java21)"
[ -n "$JAVA" ] || { echo "Java 21 not found after installing"; exit 1; }
export PATH="$(dirname "$JAVA"):$PATH"   # (Arch can have several JDKs)

# -- the world --------------------------------------------------------------
if [ ! -d server/world ]; then
  snap=$(ls -1 snapshots/world-*.tar.gz 2>/dev/null | sort | tail -1 || true)
  if [ -n "$snap" ]; then
    echo "restoring the world from $snap"
    tar xzf "$snap"
  else
    echo "no snapshot: a new world will be generated"
  fi
fi

# -- run --------------------------------------------------------------------
mkdir -p run
rm -f server/server.pid run/coordinator.pid client/instances/*/pids run/*.sock
stop() {
  echo; echo "stopping..."
  scripts/stop.sh --server || true
  exit 0
}
trap stop INT TERM
# Small game windows on a desktop; a virtual display with HEADLESS=1 or
# when there is no desktop session at all.
if [ -n "${HEADLESS:-}" ] || [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  export LAUNCH_FLAGS="--headless"
else
  export LAUNCH_FLAGS="--window"
fi
scripts/start.sh "$N"
echo
echo "running: $N bots. Dashboard http://127.0.0.1:8765/ - Ctrl+C to stop everything."
tail -n 5 -F run/coordinator.log &
wait
