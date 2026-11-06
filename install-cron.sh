#!/usr/bin/env bash
# Hands-off mode: run `whispersync watch` every 20 minutes from cron.
# Run this once from the production copy: ~/aiscripts/whispersync/install-cron.sh
# Remove later with:  crontab -l | grep -v 'whispersync watch' | crontab -
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVERY="${1:-20}"

case "$DIR" in
  */projects/*) echo "Note: this is the development copy. For production, run from a separate installed copy." ;;
esac

mkdir -p "$DIR/work" "$DIR/inbox"
"$DIR/whispersync" selftest > /dev/null && echo "-- self-test ok"
command -v ffmpeg > /dev/null || echo "Warning: ffmpeg not found; install it (sudo apt install ffmpeg) before the first episode."

LINE="*/$EVERY * * * * $DIR/whispersync watch >> $DIR/work/watch.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'whispersync watch' ; echo "$LINE" ) | crontab -
echo "-- cron installed: $LINE"

echo "-- first round now. Existing episodes are marked as done, so it won't work through the back catalog."
"$DIR/whispersync" watch
echo
echo "Hands-off from here. New episodes and inbox requests are picked up within $EVERY minutes."
echo "Log: $DIR/work/watch.log"
