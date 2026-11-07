#!/usr/bin/env bash
# Start the whole demo locally: LiveKit dev server, mock internal API, Claude
# webhook, web page + token endpoint, and the agent worker.
# Logs go to ./logs/. Press Ctrl+C to stop everything.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and fill in your keys." >&2
  exit 1
fi

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Stopping..."
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

start() {  # start <name> <command...>
  local name=$1; shift
  "$@" >"logs/$name.log" 2>&1 &
  pids+=($!)
  echo "  $name (pid $!) -> logs/$name.log"
}

echo "Starting services:"
if lsof -nP -iTCP:7880 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  livekit-server already running on :7880, reusing it"
else
  start livekit livekit-server --dev
fi
start mock-api uv run uvicorn services.mock_api:app --host 127.0.0.1 --port 8001
start webhook uv run uvicorn services.claude_webhook:app --host 127.0.0.1 --port 8002
start web uv run uvicorn services.web:app --host 127.0.0.1 --port 8000
start agent uv run python -m voice_agent dev

echo
echo "Open http://localhost:8000 and press Start call. Ctrl+C to stop."
wait || true
