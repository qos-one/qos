#!/bin/bash
# Run ON THE ENGINE SERVER as root, once: Caddy (HTTPS) + firewall for the web Terminal.
#   QOS_TERMINAL_DOMAIN=terminal.example.com ACME_EMAIL=you@example.com bash terminal-setup.sh
#   optional: QOS_ALLOW_IPS="203.0.113.7/32 198.51.100.0/24"   only these addresses may open the Terminal
# The DNS A record of the domain must already point to this server (Caddy gets the certificate on first start).
set -euo pipefail
: "${QOS_TERMINAL_DOMAIN:?set QOS_TERMINAL_DOMAIN=terminal.example.com}"
: "${ACME_EMAIL:?set ACME_EMAIL=you@example.com (certificate expiry notices)}"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

echo "== Caddy"
command -v caddy >/dev/null || { apt-get update -qq && apt-get install -y -qq caddy; }
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy
install -m 644 /srv/qos/engine/bot/deploy/caddy/Caddyfile.terminal /etc/caddy/Caddyfile
{
  echo "QOS_TERMINAL_DOMAIN=$QOS_TERMINAL_DOMAIN"
  echo "ACME_EMAIL=$ACME_EMAIL"
  [ -n "${QOS_ALLOW_IPS:-}" ] && echo "QOS_ALLOW_IPS=$QOS_ALLOW_IPS"
} > /etc/default/caddy
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/default/caddy\n' > /etc/systemd/system/caddy.service.d/qos.conf
systemctl daemon-reload
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || {
  set -a; . /etc/default/caddy; set +a; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; }
systemctl enable --now caddy && systemctl reload caddy

echo "== firewall: 80/443 for HTTPS (SSH stays open)"
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw status | grep -E "80|443"; fi

echo "== folders"
install -d -o qos -g qos /srv/qos/terminal /srv/qos/terminal/releases
echo
echo "Next (as qos): add the API settings to /srv/qos/deploy.env, then deploy the Terminal from your computer:"
echo "  QOS_PUBLIC_TERMINAL=1"
echo "  QOS_TRUSTED_ORIGINS=https://$QOS_TERMINAL_DOMAIN"
