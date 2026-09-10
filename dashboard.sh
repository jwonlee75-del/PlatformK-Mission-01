#!/usr/bin/env bash
# Start the grid-bot local dashboard (no live orders).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"
exec python3 "$ROOT/dashboard/server.py" --host "$HOST" --port "$PORT"
