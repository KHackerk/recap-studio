#!/bin/sh
# Start RecapStudio locally on port 5100 (idempotent: no-op if already running)
if ss -ltn 2>/dev/null | grep -q ":5100 "; then echo "already running on :5100"; exit 0; fi
cd "$(dirname "$0")"
setsid nohup "$HOME/workspace/movie-recap-venv/bin/python" app.py > server.log 2>&1 < /dev/null &
sleep 3
curl -s -o /dev/null -w "RecapStudio: HTTP %{http_code} at http://127.0.0.1:5100\n" http://127.0.0.1:5100/
