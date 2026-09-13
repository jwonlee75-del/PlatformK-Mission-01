#!/usr/bin/env bash
# Start the unified two-bot portfolio dashboard (read-only, no live orders).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8790}"
HOST="${HOST:-127.0.0.1}"
# Sibling 091170 bot (override if the operator checkout is elsewhere)
export GRID_BOT_091170_ROOT="${GRID_BOT_091170_ROOT:-/workspace/grid-bot-091170}"
exec python3 "$ROOT/dashboard/multi/server_multi.py" --host "$HOST" --port "$PORT"
