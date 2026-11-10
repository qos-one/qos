#!/bin/bash
# Run ON THE ENGINE SERVER as root, once: open the QOS MCP server to remote AI clients (Grok, ChatGPT) at https://<domain>/mcp
#   QOS_MCP_DOMAIN=mcp.example.com bash deploy/mcp-setup.sh
# What the AI may do is chosen in the Terminal (top bar → AI): Read-only (default), Ask first (you approve each order) or Auto.
# Needs: Caddy already set up (deploy/terminal-setup.sh), the DNS A record of the domain pointing to this server.
# Secrets are generated here and stay in /srv/qos/home/.env (mode 600); nothing is printed. Read the token on the server when
# you configure the AI client: sudo grep MCP_BEARER_TOKEN /srv/qos/home/.env
set -euo pipefail
: "${QOS_MCP_DOMAIN:?set QOS_MCP_DOMAIN=mcp.example.com}"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
U=qos; CODE=/srv/qos/engine/bot; HOME_DIR=/srv/qos/home; ENVF="$HOME_DIR/.env"
as_qos() { su - "$U" -c "export XDG_RUNTIME_DIR=/run/user/\$(id -u) PATH=\$HOME/.local/bin:\$PATH QOS_HOME=$HOME_DIR; $*"; }
setenv() {   # set KEY=VALUE in .env without printing it (replace or append)
  local k="$1" v="$2"
  if grep -q "^$k=" "$ENVF"; then sed -i "s|^$k=.*|$k=$v|" "$ENVF"; else echo "$k=$v" >> "$ENVF"; fi
}

echo "== secrets in $ENVF (not shown)"
touch "$ENVF"; chown "$U:$U" "$ENVF"; chmod 600 "$ENVF"
grep -q '^MCP_BEARER_TOKEN=.\+' "$ENVF" || setenv MCP_BEARER_TOKEN "$(openssl rand -hex 32)"
if ! grep -q '^ASSISTANT_PIN=.\+' "$ENVF"; then
  PIN="$(openssl rand -hex 24)"
  PF="$(mktemp)"; printf '%s' "$PIN" > "$PF"; chown "$U" "$PF"; chmod 600 "$PF"      # handed over in a file, not on a command line
  as_qos "cd $CODE && uv run --frozen python -c \"from pathlib import Path; from api import operators as o; pin=Path('$PF').read_text(); ids=[x['id'] for x in o.list_ops()]; (o.update('remote_ai', pin=pin) if 'remote_ai' in ids else o.create('remote_ai', 'assistant', pin, created_by='mcp-setup')); print('operator remote_ai (role assistant) ready')\""
  rm -f "$PF"
  setenv ASSISTANT_OPERATOR_ID remote_ai
  setenv ASSISTANT_PIN "$PIN"
  unset PIN
fi

echo "== MCP service (qos-mcp)"
as_qos "mkdir -p ~/.config/systemd/user && cp $CODE/deploy/systemd/qos-mcp.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable -q qos-mcp.service && systemctl --user restart qos-mcp.service"

echo "== Caddy site $QOS_MCP_DOMAIN"
mkdir -p /etc/caddy/sites /var/log/caddy && chown caddy:caddy /var/log/caddy
sed "s|__DOMAIN__|$QOS_MCP_DOMAIN|" "$CODE/deploy/caddy/mcp.caddy" > /etc/caddy/sites/mcp.caddy
grep -q '^import /etc/caddy/sites/\*.caddy' /etc/caddy/Caddyfile || printf '\nimport /etc/caddy/sites/*.caddy\n' >> /etc/caddy/Caddyfile
( set -a; [ -f /etc/default/caddy ] && . /etc/default/caddy; set +a; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null )
systemctl reload caddy

echo "== check (waits up to 90 s for the HTTPS certificate)"
code=000
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "https://$QOS_MCP_DOMAIN/mcp" -H 'content-type: application/json' -d '{}' || true)
  [ "$code" = 000 ] || break
  sleep 3
done
echo "without token: HTTP $code (401 expected; 000 = no certificate yet: check DNS, then journalctl -u caddy)"
echo
echo "Done. AI clients: server URL https://$QOS_MCP_DOMAIN/mcp, header Authorization: Bearer <MCP_BEARER_TOKEN>"
echo "Read the token here, never paste it into a chat: sudo grep MCP_BEARER_TOKEN $ENVF"
echo "By default the AI can only read the account. Choose AI → Ask first to approve each order, or Auto to let it confirm its own orders."
echo "The AI control is in the top bar of the Terminal. Orders are paper by default, live only if an admin turns it on."
echo "Optional and experimental. AI can make mistakes; you are responsible for every order it places. Account data you share goes to your chosen AI provider. Keep your token secret. Software, not financial advice."
