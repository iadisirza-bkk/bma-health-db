#!/usr/bin/env bash
# Install a macOS LaunchAgent that runs `docker system prune` weekly.
# Sunday 03:00. Output → /tmp/bma-docker-prune.log
#
# Why: the unified-CTE queries spill to disk under heavy load; if the
# Docker overlay fills (Build cache + dangling images), Postgres returns
# `psycopg2.errors.DiskFull`. A weekly prune keeps headroom.
set -eu

LABEL="com.bma.docker-prune"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOCKER_BIN=$(command -v docker || echo /usr/local/bin/docker)

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>${DOCKER_BIN} system prune -f &amp;&amp; ${DOCKER_BIN} builder prune -f</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/bma-docker-prune.log</string>
  <key>StandardErrorPath</key><string>/tmp/bma-docker-prune.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed: ${LABEL} — runs Sun 03:00 — log: /tmp/bma-docker-prune.log"
echo "Uninstall with: make uninstall-prune-cron"
