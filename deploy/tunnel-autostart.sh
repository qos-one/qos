#!/bin/zsh
# Run ON YOUR MAC: keep the SSH tunnel to the engine server open in the background (starts at login, reconnects by itself).
#   QOS_ENGINE_HOST=qos@<server-ip> deploy/tunnel-autostart.sh install     install + start
#   deploy/tunnel-autostart.sh status | restart | uninstall
# Same forwards as deploy/tunnel.sh: API → 127.0.0.1:8000, IB Gateway screen (VNC) → localhost:5901.
# Needs SSH key login without a typed password (a key passphrase is fine if it is in the macOS keychain / ssh-agent).
set -euo pipefail
LABEL=one.qos.tunnel
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/qos-tunnel.log"
DOMAIN="gui/$(id -u)"

case "${1:-status}" in
install)
  HOST="${QOS_ENGINE_HOST:?set QOS_ENGINE_HOST=qos@<server-ip>}"
  echo "== check SSH key login to $HOST"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true || { echo "SSH needs a password or failed: set up key login first (ssh-copy-id $HOST)"; exit 1; }
  echo "== keep the old local engine off (it would take port 8000 and fight the server for the IB session)"
  # the engine's old launchd jobs from the time it ran on this Mac (*quantx-api / -catalog / -watchdog)
  OLD=( ${(f)"$( { launchctl list | awk '{print $3}'; ls "$HOME/Library/LaunchAgents" 2>/dev/null | sed 's/\.plist$//'; } | grep -E 'quantx-(api|catalog|watchdog)$' | sort -u || true )"} )
  for l in "${OLD[@]}"; do
    launchctl bootout "$DOMAIN/$l" 2>/dev/null && echo "stopped $l" || true
    launchctl disable "$DOMAIN/$l" 2>/dev/null || true
  done
  if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -nP -iTCP:8000 -sTCP:LISTEN | tail -n +2
    echo "port 8000 is still in use on this Mac: stop that program (a deploy/tunnel.sh window?) and run install again"; exit 1
  fi
  echo "== write $PLIST"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string><string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=20</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ConnectTimeout=15</string>
    <string>-L</string><string>8000:127.0.0.1:8000</string>
    <string>-L</string><string>5901:127.0.0.1:5901</string>
    <string>$HOST</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PL
  plutil -lint "$PLIST" >/dev/null
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  sleep 4
  "$0" status
  ;;
status)
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "tunnel service: $(launchctl print "$DOMAIN/$LABEL" | awk -F'= ' '/^\tstate/ {print $2; exit}')"
  else
    echo "tunnel service: not installed"
  fi
  if curl -fsS --max-time 4 -o /dev/null http://127.0.0.1:8000/health; then echo "engine API via tunnel: ok"; else echo "engine API via tunnel: not reachable (log: $LOG)"; fi
  ;;
restart)
  launchctl kickstart -k "$DOMAIN/$LABEL" && sleep 4 && "$0" status
  ;;
uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "tunnel service removed (use deploy/tunnel.sh to open the tunnel by hand)"
  ;;
*)
  echo "usage: $0 install | status | restart | uninstall"; exit 1
  ;;
esac
