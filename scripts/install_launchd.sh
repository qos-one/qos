#!/bin/zsh
# Install the QOS LaunchAgents from launchd/templates using this machine's paths
#   scripts/install_launchd.sh [--prefix com.qos] [--port 8000] [--no-catalog] [--no-watchdog] [--load] [--dry-run]
# label = <prefix>-api / <prefix>-catalog / <prefix>-watchdog · QOS_HOME set at run time is written into the plist (unset = legacy layout)
# Existing files are never overwritten — an existing machine can add only the watchdog: --prefix <existing prefix> --no-catalog --load
set -e
BOT="${0:A:h:h}"
PREFIX="com.qos"; PORT=8000; CATALOG=1; WATCHDOG=1; LOAD=0; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-catalog) CATALOG=0; shift ;;
    --no-watchdog) WATCHDOG=0; shift ;;
    --load) LOAD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
DEST="$HOME/Library/LaunchAgents"
ENV=""
[[ -n "$QOS_HOME" ]] && ENV="<key>QOS_HOME</key><string>$QOS_HOME</string>"
PYTHON="$BOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3 || echo /usr/bin/python3)"
render() {
  local tmpl="$1" label="$2"
  sed -e "s|@LABEL@|$label|g" -e "s|@BOT@|$BOT|g" -e "s|@PORT@|$PORT|g" -e "s|@ENV@|$ENV|g" \
      -e "s|@PYTHON@|$PYTHON|g" -e "s|@API_LABEL@|$PREFIX-api|g" "$BOT/launchd/templates/$tmpl"
}
install_one() {
  local tmpl="$1" label="$2" out="$DEST/$2.plist"
  if [[ $DRY -eq 1 ]]; then echo "--- $out"; render "$tmpl" "$label"; return; fi
  mkdir -p "$DEST"
  if [[ -e "$out" ]]; then echo "exists, not overwritten: $out"; else render "$tmpl" "$label" > "$out"; echo "wrote $out"; fi
  command -v plutil >/dev/null && plutil -lint "$out" >/dev/null
  if [[ $LOAD -eq 1 ]]; then launchctl bootstrap "gui/$(id -u)" "$out" 2>/dev/null && echo "loaded $label" || echo "already loaded: $label"; fi
}
install_one api.plist.in "$PREFIX-api"
[[ $CATALOG -eq 1 ]] && install_one catalog.plist.in "$PREFIX-catalog"
[[ $WATCHDOG -eq 1 ]] && install_one watchdog.plist.in "$PREFIX-watchdog"
echo "restart API:  launchctl kickstart -k gui/\$(id -u)/$PREFIX-api"
