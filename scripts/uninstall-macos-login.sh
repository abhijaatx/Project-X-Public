#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This uninstaller is only for macOS." >&2
  exit 1
fi

LABEL="com.abhijaat.project-x-host"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || \
  launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
launchctl disable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"

echo "Project X launch-at-login has been stopped and removed."
echo "The source checkout and logs were left in place."
