#!/usr/bin/env bash
# Run ON THE VPS as root, once, AFTER the base setup (cloud/deploy/server-setup.sh: Docker, firewall, user qos).
# Prepares the engine: folders, uv for the qos user, user services that keep running without a login, swap headroom.
#   sudo bash engine-setup.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
U="${DEPLOY_USER:-qos}"
SWAP_GB="${SWAP_GB:-4}"
id "$U" >/dev/null 2>&1 || { echo "user $U not found: run server-setup.sh first"; exit 1; }
command -v docker >/dev/null || { echo "docker not found: run server-setup.sh first"; exit 1; }

echo "== folders"
install -d -m 750 -o "$U" -g "$U" /srv/qos /srv/qos/engine /srv/qos/home /srv/qos/home/data /srv/qos/home/ibgw /srv/qos/backups
if [[ ! -f /srv/qos/deploy.env ]]; then
  printf 'QOS_HOME=/srv/qos/home\nPYTHONUNBUFFERED=1\n' > /srv/qos/deploy.env
  chown "$U:$U" /srv/qos/deploy.env && chmod 640 /srv/qos/deploy.env
fi

echo "== build tools (C compiler for compiled agents)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q build-essential >/dev/null
gcc --version | head -1

echo "== user services survive logout and reboot"
loginctl enable-linger "$U"

echo "== uv for $U"
if [[ ! -x /home/$U/.local/bin/uv ]]; then
  su - "$U" -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi
su - "$U" -c '~/.local/bin/uv --version'

echo "== swap (${SWAP_GB} GB total)"
have_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
want_kb=$((SWAP_GB * 1024 * 1024))
if (( have_kb < want_kb - 102400 )); then
  add_mb=$(( (want_kb - have_kb) / 1024 ))
  fallocate -l "${add_mb}M" /swapfile-qos && chmod 600 /swapfile-qos && mkswap /swapfile-qos >/dev/null && swapon /swapfile-qos
  grep -q '^/swapfile-qos' /etc/fstab || echo '/swapfile-qos none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-qos.conf
sysctl -q -p /etc/sysctl.d/90-qos.conf
free -h | head -3

echo
echo "Done. Next, from your computer:  QOS_ENGINE_HOST=$U@<server-ip> deploy/deploy-engine.sh"
