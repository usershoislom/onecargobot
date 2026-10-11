#!/usr/bin/env bash
# OneCargo bot launcher for macOS / Linux. Run: ./start.sh   (Ctrl+C to stop)
cd "$(dirname "$0")" || exit 1

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Fill in .env (keys and tokens), then run ./start.sh again."
  exit 1
fi
if [ ! -f service_account.json ]; then
  echo "service_account.json is missing. Put the Google key file next to start.sh."
  exit 1
fi

[ -d .venv ] || { echo "First run: setting up Python environment..."; python3 -m venv .venv; }
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

# On a Mac, keep the computer from sleeping while the bot runs.
KEEP_AWAKE=""
[ "$(uname)" = "Darwin" ] && KEEP_AWAKE="caffeinate -i"

while true; do
  $KEEP_AWAKE .venv/bin/python -m bot.main
  echo "Bot stopped. Restarting in 10 seconds... (Ctrl+C to quit)"
  sleep 10
done
