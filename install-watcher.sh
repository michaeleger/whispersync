#!/usr/bin/env bash
# Idle job watcher: whispersync wakes only when a request lands in inbox/, does the job, and goes back to sleep.
# No timer, no polling loop. Run once from the production copy:  ~/aiscripts/whispersync/install-watcher.sh
# Remove later with:  systemctl --user disable --now whispersync-inbox.path
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$DIR/inbox" "$DIR/work" "$UNIT_DIR"

case "$DIR" in
  */projects/*) echo "Note: this is the development copy. For production, run from a separate installed copy." ;;
esac

"$DIR/whispersync" selftest > /dev/null && echo "-- self-test ok"

cat > "$UNIT_DIR/whispersync-inbox.path" <<UNIT
[Unit]
Description=whispersync: wake when a job lands in the inbox

[Path]
PathExistsGlob=$DIR/inbox/*.json
Unit=whispersync-inbox.service

[Install]
WantedBy=default.target
UNIT

cat > "$UNIT_DIR/whispersync-inbox.service" <<UNIT
[Unit]
Description=whispersync: run the jobs waiting in the inbox

[Service]
Type=oneshot
Nice=10
ExecStart=$DIR/whispersync watch --no-feed --wait-if-busy 300
StandardOutput=append:$DIR/work/watch.log
StandardError=append:$DIR/work/watch.log
UNIT

systemctl --user daemon-reload
systemctl --user enable --now whispersync-inbox.path
echo "-- watcher installed and listening on $DIR/inbox"

# Keep it working when nobody is logged in (otherwise it sleeps until you log in)
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
  echo "-- runs without a login session: yes"
elif loginctl enable-linger "$USER" 2>/dev/null; then
  echo "-- runs without a login session: turned on"
else
  echo "-- To keep it running when you're logged out, run once:  sudo loginctl enable-linger $USER"
fi

# Remove the old 20-minute cron line if an earlier version installed one
if crontab -l 2>/dev/null | grep -q 'whispersync watch'; then
  crontab -l | grep -v 'whispersync watch' | crontab -
  echo "-- removed the old cron timer"
fi
echo "Log: $DIR/work/watch.log"
