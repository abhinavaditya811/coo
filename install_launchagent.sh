#!/bin/bash
# install_launchagent.sh — keep the kernel running across logins and reboots.
#
# "Text your Mac home" only works if the Mac is actually listening, so the
# kernel shouldn't depend on you remembering to run it in a terminal.
#
#   AGENT_TOKEN=... ./install_launchagent.sh
#
# Re-run it any time you change a setting; it replaces the existing agent.
# Uninstall:  launchctl bootout gui/$(id -u)/com.coo.kernel
#             rm ~/Library/LaunchAgents/com.coo.kernel.plist

set -euo pipefail

LABEL="com.coo.kernel"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
LOG="$HOME/Library/Logs/coo-kernel.log"

: "${AGENT_TOKEN:?AGENT_TOKEN must be set. Generate one: export AGENT_TOKEN=\$(openssl rand -hex 32)}"

# Default to tailnet-only: reachable from your iPhone anywhere, but not from
# whatever Wi-Fi you happen to join. Set AGENT_HOST=127.0.0.1 to keep it local.
AGENT_HOST="${AGENT_HOST:-tailscale}"
AGENT_PORT="${AGENT_PORT:-8765}"
AGENT_PROVIDER="${AGENT_PROVIDER:-openai}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:11434/v1}"
AGENT_MODEL="${AGENT_MODEL:-llama3:8b}"

esc() { python3 -c 'import html,sys; print(html.escape(sys.argv[1]))' "$1"; }

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(esc "$PYTHON")</string>
    <string>$(esc "$REPO/kernel.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(esc "$REPO")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGENT_TOKEN</key><string>$(esc "$AGENT_TOKEN")</string>
    <key>AGENT_HOST</key><string>$(esc "$AGENT_HOST")</string>
    <key>AGENT_PORT</key><string>$(esc "$AGENT_PORT")</string>
    <key>AGENT_PROVIDER</key><string>$(esc "$AGENT_PROVIDER")</string>
    <key>OPENAI_BASE_URL</key><string>$(esc "$OPENAI_BASE_URL")</string>
    <key>AGENT_MODEL</key><string>$(esc "$AGENT_MODEL")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- AGENT_HOST=tailscale fails fast if the tailnet isn't up yet at login;
       KeepAlive plus this throttle retries until Tailscale connects. -->
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$(esc "$LOG")</string>
  <key>StandardErrorPath</key><string>$(esc "$LOG")</string>
</dict>
</plist>
PLISTEOF

# The plist holds AGENT_TOKEN, so keep it owner-readable only.
chmod 600 "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed $LABEL"
echo "  plist:  $PLIST (chmod 600 — contains your token)"
echo "  log:    $LOG"
echo "  host:   $AGENT_HOST:$AGENT_PORT"
echo
echo "Check it:  launchctl print gui/$(id -u)/$LABEL | head -20"
echo "Logs:      tail -f $LOG"
