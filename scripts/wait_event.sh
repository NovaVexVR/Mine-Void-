#!/usr/bin/env bash
# Wait until a NEW coordinator log line matches a regex, or a timeout passes.
# scripts/wait_event.sh 'fact |died' 540
cd "$(dirname "$0")/.."
pat="$1"; secs="${2:-540}"
timeout "$secs" bash -c "tail -n0 -F run/coordinator.log 2>/dev/null | grep -m1 -E \"$pat\"" || true
