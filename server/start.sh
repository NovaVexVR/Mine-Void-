#!/usr/bin/env bash
# Starts Paper in the background. Console input goes through server/console.in
# (see scripts/mc-cmd.sh), output to server/logs/latest.log.
set -euo pipefail
cd "$(dirname "$0")"
[ -f paper.jar ] || ../scripts/setup-server.sh
if [ -f server.pid ] && kill -0 "$(cat server.pid)" 2>/dev/null; then
  echo "server already running (pid $(cat server.pid))"; exit 0
fi
rm -f console.in && mkfifo console.in
# Hold the fifo open so the server never sees EOF on stdin.
( exec 3<>console.in; exec java -Xms1G -Xmx"${SERVER_HEAP:-3G}" -XX:+UseG1GC \
    -jar paper.jar --nogui <&3 >server.out 2>&1 ) &
echo $! > server.pid
echo "server starting (pid $!), waiting for 'Done'..."
for _ in $(seq 180); do
  grep -q 'Done (' server.out 2>/dev/null && { echo "server up"; exit 0; }
  kill -0 "$(cat server.pid)" 2>/dev/null || { tail -30 server.out; exit 1; }
  sleep 1
done
echo "timed out waiting for server"; tail -30 server.out; exit 1
