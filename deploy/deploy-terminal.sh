#!/bin/zsh
# Run ON YOUR COMPUTER: install a web Terminal build on the engine server and (re)start it.
# The build comes from GitHub Actions (workflow "Web Terminal" in the dashboard repo), it is Linux-only:
#   gh run download -R <owner>/<dashboard-repo> -n qos-terminal-web -D /tmp/qos-terminal-web
#   QOS_ENGINE_HOST=qos@<server-ip> deploy/deploy-terminal.sh /tmp/qos-terminal-web/qos-terminal-web.tgz
set -euo pipefail
BOT="${0:A:h:h}"
HOST="${QOS_ENGINE_HOST:?set QOS_ENGINE_HOST=qos@<server-ip>}"
TGZ="${1:?path to qos-terminal-web.tgz}"
[[ -f "$TGZ" ]] || { echo "not found: $TGZ"; exit 1; }
REL=$(date -u +%Y%m%d-%H%M%S)

echo "== upload $(du -h "$TGZ" | cut -f1) → $HOST (release $REL)"
scp -q "$TGZ" "$HOST:/srv/qos/terminal/releases/$REL.tgz"
scp -q "$BOT/deploy/systemd/qos-terminal.service" "$HOST:/tmp/qos-terminal.service"
ssh "$HOST" "REL=$REL bash -s" <<'REMOTE'
set -euo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
cd /srv/qos/terminal/releases
mkdir "$REL" && tar xzf "$REL.tgz" -C "$REL" && rm "$REL.tgz"
[ -x "$REL/server" ] && [ -d "$REL/public" ] || { echo "bad build: expected server + public/"; exit 1; }
ln -sfn "/srv/qos/terminal/releases/$REL" /srv/qos/terminal/current
mkdir -p ~/.config/systemd/user && mv /tmp/qos-terminal.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable -q qos-terminal.service
systemctl --user restart qos-terminal.service
sleep 3
systemctl --user is-active qos-terminal.service
curl -s -o /dev/null -w "terminal: HTTP %{http_code}\n" http://127.0.0.1:8080/ </dev/null
ls -1dt /srv/qos/terminal/releases/*/ | tail -n +4 | xargs -r rm -rf          # keep the 3 newest releases
REMOTE
