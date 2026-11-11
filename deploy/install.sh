#!/usr/bin/env bash
# QOS self-hosted install / update — run ON THE SERVER as root (Ubuntu 24.04 or newer). Safe to run again: it updates.
#
#   curl -fsSLO https://raw.githubusercontent.com/qos-one/qos/main/deploy/install.sh
#   sudo bash install.sh                                              engine only (use the Terminal through an SSH tunnel)
#   sudo QOS_TERMINAL_DOMAIN=terminal.example.com ACME_EMAIL=you@example.com bash install.sh     + web Terminal on HTTPS
#
# Optional settings (environment):
#   QOS_ALLOW_IPS="203.0.113.7/32"     only these addresses may open the web Terminal
#   QOS_TERMINAL_VERSION=v0.1.0        web Terminal release (default: latest)
#   QOS_REPO / QOS_TERMINAL_REPO       code and release sources (default: github.com/qos-one/qos and qos-terminal)
#   SWAP_GB=4                          swap size
# Nothing here asks for broker credentials: you sign in to Interactive Brokers later, in the Terminal (step "IBKR gateway").
# See docs/SELF_HOSTING.md.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo bash install.sh)"; exit 1; }
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || echo "warning: tested on Ubuntu only (found ${PRETTY_NAME:-unknown})"

U=qos
REPO="${QOS_REPO:-https://github.com/qos-one/qos.git}"
TREPO="${QOS_TERMINAL_REPO:-qos-one/qos-terminal}"
TVER="${QOS_TERMINAL_VERSION:-latest}"
DOMAIN="${QOS_TERMINAL_DOMAIN:-}"
SWAP_GB="${SWAP_GB:-4}"
CODE=/srv/qos/engine/bot            # engine code (folder name kept for the systemd units)
HOME_DIR=/srv/qos/home              # data, configs, strategies, IB Gateway profiles, .env
step() { echo; echo "== $*"; }
as_qos() {  # forward QOS_LOCAL_TOKEN_FILE so later steps chmod the file the engine will actually read
  local extra=""
  if [[ -n "${QOS_LOCAL_TOKEN_FILE:-}" ]]; then extra=" QOS_LOCAL_TOKEN_FILE=$(printf '%q' "$QOS_LOCAL_TOKEN_FILE")"; fi
  su - "$U" -c "export XDG_RUNTIME_DIR=/run/user/\$(id -u) PATH=\$HOME/.local/bin:\$PATH QOS_HOME=$HOME_DIR$extra; $*"
}
if [[ -n "$DOMAIN" && -z "${ACME_EMAIL:-}" ]]; then echo "set ACME_EMAIL (certificate notices) together with QOS_TERMINAL_DOMAIN"; exit 1; fi

step "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates build-essential ufw >/dev/null
if ! command -v docker >/dev/null; then
  apt-get install -y -qq docker.io docker-compose-v2 >/dev/null
  systemctl enable --now docker >/dev/null
fi
docker compose version >/dev/null || { echo "docker compose plugin missing (apt install docker-compose-v2)"; exit 1; }
echo "git $(git --version | cut -d' ' -f3) · $(docker --version | cut -d, -f1) · $(gcc --version | head -1)"

step "user $U (runs the engine; may start IB Gateway containers)"
id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"
usermod -aG docker "$U"
loginctl enable-linger "$U"
install -d -m 750 -o "$U" -g "$U" /srv/qos /srv/qos/engine "$HOME_DIR" /srv/qos/backups /srv/qos/terminal /srv/qos/terminal/releases
if [[ ! -f /srv/qos/deploy.env ]]; then
  printf 'QOS_HOME=%s\nPYTHONUNBUFFERED=1\n' "$HOME_DIR" > /srv/qos/deploy.env
  chown "$U:$U" /srv/qos/deploy.env && chmod 640 /srv/qos/deploy.env
fi

step "swap ${SWAP_GB} GB"
have_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo); want_kb=$((SWAP_GB * 1024 * 1024))
if (( have_kb < want_kb - 102400 )); then
  add_mb=$(( (want_kb - have_kb) / 1024 ))
  fallocate -l "${add_mb}M" /swapfile-qos && chmod 600 /swapfile-qos && mkswap /swapfile-qos >/dev/null && swapon /swapfile-qos
  grep -q '^/swapfile-qos' /etc/fstab || echo '/swapfile-qos none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-qos.conf && sysctl -q -p /etc/sysctl.d/90-qos.conf
free -h | awk 'NR<=3'

step "engine code → $CODE"
if [[ -d "$CODE" && ! -d "$CODE/.git" ]]; then echo "$CODE exists but is not a git checkout (installed with deploy-engine.sh?): update it that way instead"; exit 1; fi
if [[ -d "$CODE/.git" ]]; then as_qos "git -C $CODE pull --ff-only -q"; else as_qos "git clone -q --depth 1 $REPO $CODE"; fi
as_qos "git -C $CODE log -1 --format='%h %s'"
[[ -x /home/$U/.local/bin/uv ]] || as_qos "curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null"
as_qos "cd $CODE && uv sync --frozen --no-dev -q && uv run --frozen python -c 'import nautilus_trader as n; print(\"python ok · nautilus\", n.__version__)'"
as_qos "cd $CODE && uv run --frozen python -m qos.cli home init $HOME_DIR >/dev/null && uv run --frozen python -c 'import os; from qos.local_token import ensure_local_token, token_path; ensure_local_token(); os.chmod(token_path(), 0o600)' && echo 'QOS_HOME ready: $HOME_DIR'"

step "Redis (Docker, 127.0.0.1:6379)"
as_qos "cd $CODE && docker compose up -d redis >/dev/null 2>&1 && sleep 2 && echo \"redis: \$(docker compose exec -T redis redis-cli ping </dev/null)\""

step "web Terminal"
if [[ -n "$DOMAIN" ]]; then
  base="https://github.com/$TREPO/releases"
  url="$base/latest/download/qos-terminal-web.tgz"; [[ "$TVER" != latest ]] && url="$base/download/$TVER/qos-terminal-web.tgz"
  tmp=$(mktemp -d)
  curl -fsSL "$url" -o "$tmp/qos-terminal-web.tgz"
  curl -fsSL "${url%.tgz}.sha256" -o "$tmp/qos-terminal-web.sha256"
  (cd "$tmp" && sha256sum -c qos-terminal-web.sha256 >/dev/null) || { echo "checksum mismatch: $url"; exit 1; }
  rel=$(date -u +%Y%m%d-%H%M%S)
  install -d -o "$U" -g "$U" "/srv/qos/terminal/releases/$rel"
  tar xzf "$tmp/qos-terminal-web.tgz" -C "/srv/qos/terminal/releases/$rel" && chown -R "$U:$U" "/srv/qos/terminal/releases/$rel"
  ln -sfn "/srv/qos/terminal/releases/$rel" /srv/qos/terminal/current
  rm -rf "$tmp"
  for kv in "QOS_PUBLIC_TERMINAL=1" "QOS_TRUSTED_ORIGINS=https://$DOMAIN"; do
    k=${kv%%=*}; grep -q "^$k=" /srv/qos/deploy.env && sed -i "s#^$k=.*#$kv#" /srv/qos/deploy.env || echo "$kv" >> /srv/qos/deploy.env
  done
  echo "release $(basename "$url" .tgz) ($TVER) installed"
else
  echo "skipped (no QOS_TERMINAL_DOMAIN): use the Terminal on your computer through an SSH tunnel, see docs/SELF_HOSTING.md"
fi

step "services"
as_qos "mkdir -p ~/.config/systemd/user && cp $CODE/deploy/systemd/*.service $CODE/deploy/systemd/*.timer ~/.config/systemd/user/ && systemctl --user daemon-reload"
as_qos "systemctl --user enable -q qos-catalog.timer qos-watchdog.timer qos-api.service && systemctl --user restart qos-api.service && systemctl --user start qos-catalog.timer qos-watchdog.timer"
if [[ -n "$DOMAIN" ]]; then as_qos "systemctl --user enable -q qos-terminal.service && systemctl --user restart qos-terminal.service"; fi
# qos-mcp is optional (mcp-setup). Restart it when it is already running so an update does not leave the old process up.
as_qos "if systemctl --user is-active -q qos-mcp.service; then systemctl --user restart qos-mcp.service && echo mcp restarted; fi"
for _ in $(seq 1 30); do curl -fsS -o /dev/null 127.0.0.1:8000/health 2>/dev/null && break; sleep 2; done
echo "api: $(curl -fsS 127.0.0.1:8000/health 2>/dev/null | head -c 60 || echo 'not answering yet — journalctl --user -u qos-api (as qos)')"

step "firewall"
ufw allow OpenSSH >/dev/null
if [[ -n "$DOMAIN" ]]; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
if ! ufw status | grep -q "Status: active"; then
  if ss -tln | grep -qE '[:.]22\s'; then ufw --force enable >/dev/null
  else echo "SSH is not on port 22: firewall NOT enabled (allow your SSH port, then: ufw enable)"; fi
fi
ufw status | sed -n '1,12p'

if [[ -n "$DOMAIN" ]]; then
  step "HTTPS for $DOMAIN (Caddy)"
  QOS_TERMINAL_DOMAIN="$DOMAIN" ACME_EMAIL="$ACME_EMAIL" QOS_ALLOW_IPS="${QOS_ALLOW_IPS:-}" bash "$CODE/deploy/terminal-setup.sh" | grep -v "^Next\|^  QOS_" || true
fi

step "first admin"
if as_qos "cd $CODE && uv run --frozen python -m qos.cli operator list" | grep -q "no operators"; then
  if [[ -t 0 ]]; then
    read -r -p "Admin name: " NAME
    su - "$U" -c "cd $CODE && QOS_HOME=$HOME_DIR ${DOMAIN:+QOS_PUBLIC_TERMINAL=1} ~/.local/bin/uv run --frozen python -m qos.cli operator add '$NAME' --role admin"
  else
    echo "no terminal attached: create the admin with"
    echo "  sudo -u $U bash -c 'cd $CODE && QOS_HOME=$HOME_DIR ~/.local/bin/uv run --frozen python -m qos.cli operator add <name> --role admin'"
  fi
else
  as_qos "cd $CODE && uv run --frozen python -m qos.cli operator list"
fi

echo
echo "QOS is installed."
if [[ -n "$DOMAIN" ]]; then
  echo "  Open https://$DOMAIN · sign in with the admin PIN · then step 2 \"IBKR gateway\" (paper account first)."
else
  echo "  On your computer: ssh -N -L 8000:127.0.0.1:8000 -L 5901:127.0.0.1:5901 <your-ssh-user>@<this-server>, then start the Terminal."
fi
echo "  Update later: run this script again. Guide: $CODE/docs/SELF_HOSTING.md"
