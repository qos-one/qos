#!/bin/zsh
# Common launcher for launchd: sets PATH/folders like the Terminal and writes its own log (errors show even if uv fails to start)
# Usage: scripts/launchd_run.sh <log name> <command...> · no machine-specific path is embedded — the bot folder comes from this script's location
name="$1"; shift
BOT="${0:A:h:h}"
export HOME="${HOME:-/Users/$(id -un)}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
if [[ -n "$QOS_DATA_DIR" ]]; then
  LOGS="$QOS_DATA_DIR/logs"
elif [[ -n "$QOS_HOME" ]]; then
  LOGS="$QOS_HOME/data/logs"
else
  LOGS="$BOT/data/logs"
fi
mkdir -p "$LOGS"
exec >>"$LOGS/$name.launchd.log" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') start: $* (bot=$BOT qos_home=${QOS_HOME:-legacy})"
cd "$BOT" || { echo "cannot cd $BOT"; exit 1; }
exec "$@"
