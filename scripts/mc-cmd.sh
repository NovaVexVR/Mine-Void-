#!/usr/bin/env bash
# Send a console command to the running server: scripts/mc-cmd.sh "say hi"
cd "$(dirname "$0")/../server" && printf '%s\n' "$*" > console.in
