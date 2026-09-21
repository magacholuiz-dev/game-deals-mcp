#!/usr/bin/env bash
# Install the scheduler as a macOS LaunchAgent, generated for THIS checkout.
#
#   scripts/install.sh --print      show the plist, change nothing
#   scripts/install.sh              write it and load it
#   scripts/install.sh --uninstall  unload and remove it
#
# The old plist had the author's home directory written into it. This one is
# built from the repository path and the current user, so it works anywhere.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.gamedeals.scheduler"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UV="$(command -v uv || true)"

render() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string>
    <string>run</string>
    <string>--quiet</string>
    <string>game-deals-scheduler</string>
    <string>--serve</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <!-- The scheduler is a loop with its own state in the database, so launchd only
       has to keep it alive. After the Mac sleeps it catches up by itself. -->
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$REPO/scheduler.log</string>
  <key>StandardErrorPath</key><string>$REPO/scheduler.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$UV"):/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLIST
}

case "${1:-}" in
  --print) render ;;
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "removed $LABEL" ;;
  "")
    [ -n "$UV" ] || { echo "uv not found on PATH" >&2; exit 1; }
    mkdir -p "$(dirname "$PLIST")"
    render > "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed $LABEL (logs: $REPO/scheduler.log)"
    echo "check it:  uv run game-deals-scheduler --status" ;;
  *) echo "usage: $0 [--print|--uninstall]" >&2; exit 2 ;;
esac
