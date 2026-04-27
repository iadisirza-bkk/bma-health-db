#!/usr/bin/env bash
# Install macOS LaunchAgent: daily REFRESH MATERIALIZED VIEW at 03:00 BKK.
#
# Calls public.refresh_all_mvs() which:
#  - refreshes mv_visit_resolved first (other MVs depend on it)
#  - then mv_kpi_tier1, mv_disease_district, mv_demographics, mv_lab_distribution,
#    mv_mental_health, mv_lifestyle, mv_data_dictionary
#  - logs each result to public.mv_refresh_log
#
# Output → /tmp/bma-mv-refresh.log
set -eu

LABEL="com.bma.mv-refresh"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

# Use docker exec so we don't need psql installed on macOS host
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
    <string>/usr/local/bin/docker exec bma-health-db psql -U postgres -d bma_health -c "INSERT INTO public.mv_refresh_log (view_name, status, duration_ms) SELECT view_name, status, duration_ms FROM public.refresh_all_mvs();"</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/bma-mv-refresh.log</string>
  <key>StandardErrorPath</key><string>/tmp/bma-mv-refresh.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed: ${LABEL} — runs daily 03:00 BKK — log: /tmp/bma-mv-refresh.log"
