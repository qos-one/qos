#!/bin/zsh
# Run ON YOUR MAC: copy the engine code to the VPS, install Python packages, install/refresh the systemd user services.
# Copies code only: never .env, data/, reports/ or IB Gateway settings (that is deploy/migrate-data.sh, once, at cut-over).
#   QOS_ENGINE_HOST=qos@<server-ip> deploy/deploy-engine.sh            install / update, does not start or restart anything
#   RESTART=1 QOS_ENGINE_HOST=... deploy/deploy-engine.sh              also restart the API if it is running (nodes keep running)
#   TESTS=1 ...                                                         run the test suite on the server after installing
set -euo pipefail
BOT="${0:A:h:h}"
HOST="${QOS_ENGINE_HOST:?set QOS_ENGINE_HOST=qos@<server-ip>}"
DIR=/srv/qos/engine/bot

echo "== copy code to $HOST:$DIR"
ssh "$HOST" "mkdir -p $DIR"
rsync -az --delete --exclude-from="$BOT/deploy/rsync-exclude.txt" "$BOT/" "$HOST:$DIR/"
EX="${BOT:h}/strategies-examples"
[[ -d "$EX" ]] && rsync -az --delete --exclude-from="$BOT/deploy/rsync-exclude.txt" "$EX/" "$HOST:${DIR:h}/strategies-examples/"

echo "== install packages and services"
ssh "$HOST" "RESTART=${RESTART:-0} TESTS=${TESTS:-0} bash -s" <<'EOF'
set -euo pipefail
cd /srv/qos/engine/bot
export PATH="$HOME/.local/bin:$PATH" XDG_RUNTIME_DIR="/run/user/$(id -u)"
uv sync --frozen --no-dev </dev/null >/dev/null
echo "python: $(uv run --frozen python -V) · nautilus $(uv run --frozen python -c 'import nautilus_trader as n; print(n.__version__)')"
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
echo "services installed (enable them at cut-over, see deploy/README.md)"
docker compose -f docker-compose.yml up -d redis </dev/null >/dev/null 2>&1 && echo "redis: $(docker compose -f docker-compose.yml exec -T redis redis-cli ping </dev/null)"
if [ "$TESTS" = 1 ]; then
  if systemctl --user is-active -q qos-api.service; then echo "tests skipped: the API is running here (tests must not run next to live nodes)";
  else uv sync --frozen </dev/null >/dev/null && (QOS_HOME=$(mktemp -d) uv run --frozen pytest -q </dev/null 2>&1 | tail -3) || true; uv sync --frozen --no-dev </dev/null >/dev/null; fi
fi
if systemctl --user is-active -q qos-api.service; then
  if [ "$RESTART" = 1 ]; then systemctl --user restart qos-api.service && echo "api restarted"; else echo "api running (not restarted; RESTART=1 to restart)"; fi
  if systemctl --user is-active -q qos-mcp.service && [ "$RESTART" = 1 ]; then systemctl --user restart qos-mcp.service && echo "mcp restarted"; fi
else
  echo "api not started yet (start it at cut-over: systemctl --user start qos-api.service)"
fi
EOF
