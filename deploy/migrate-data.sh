#!/bin/zsh
# Run ON YOUR MAC, once, at cut-over (FX market closed, Mac API and nodes stopped):
# copies data, strategy configs, installed strategies, models, IB Gateway profiles and .env to the VPS (QOS_HOME layout),
# then fixes Mac paths there. Your own machine to your own server over SSH; nothing goes anywhere else.
#   QOS_ENGINE_HOST=qos@<server-ip> deploy/migrate-data.sh
set -euo pipefail
BOT="${0:A:h:h}"
TRADING="${BOT:h}"
HOST="${QOS_ENGINE_HOST:?set QOS_ENGINE_HOST=qos@<server-ip>}"
H=/srv/qos/home

if curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:8000/health 2>/dev/null && [[ "${FORCE:-0}" != 1 ]]; then
  echo "The bot API on this Mac is still running. Stop the nodes and the API first (deploy/README.md, cut-over step 2)."
  exit 1
fi
if ssh "$HOST" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user is-active -q qos-api.service"; then
  echo "The API on the server is already running: stop it before copying data (systemctl --user stop qos-api.service)."
  exit 1
fi

echo "== copy to $HOST:$H"
R=(rsync -az)
"${R[@]}" --exclude 'logs/*.log' --exclude '*.bak-*' "$BOT/data/" "$HOST:$H/data/"
[[ -d "$BOT/reports" ]] && "${R[@]}" "$BOT/reports/" "$HOST:$H/reports/"
"${R[@]}" "$BOT/configs/" "$HOST:$H/configs/"
[[ -d "$BOT/models" ]] && "${R[@]}" "$BOT/models/" "$HOST:$H/models/"
[[ -d "$TRADING/strategies" ]] && "${R[@]}" "$TRADING/strategies/" "$HOST:$H/strategies/"
[[ -d "$TRADING/ibgw" ]] && "${R[@]}" "$TRADING/ibgw/" "$HOST:$H/ibgw/"
[[ -f "$BOT/.env" ]] && "${R[@]}" "$BOT/.env" "$HOST:$H/.env"
ssh "$HOST" "chmod 700 $H/ibgw; find $H/ibgw -type f -exec chmod 600 {} +; chmod 600 $H/.env 2>/dev/null; du -sh $H/data | cut -f1 | sed 's/^/data on server: /'"

echo "== fix paths on the server"
ssh "$HOST" "cd /srv/qos/engine/bot && python3 deploy/migrate_paths.py --mac-trading '$TRADING' --home $H"
echo
echo "Data is on the server. Next: cut-over steps 4-6 in deploy/README.md (gateway, API, nodes)."
