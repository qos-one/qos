#!/bin/zsh
# Run ON YOUR MAC: connect the Terminal to the engine on the VPS through SSH (no port is opened on the internet).
#   QOS_ENGINE_HOST=qos@<server-ip> deploy/tunnel.sh
# Terminal keeps using http://127.0.0.1:8000 · IB Gateway screen (VNC) at localhost:5901 · Ctrl+C closes the tunnel.
set -euo pipefail
HOST="${QOS_ENGINE_HOST:?set QOS_ENGINE_HOST=qos@<server-ip>}"
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8000 is in use on this Mac (the local bot API?). Stop it first (find its label with: launchctl list | grep -E 'qos|quantx'), then: launchctl bootout gui/\$(id -u)/<label>"
  exit 1
fi
echo "Tunnel to $HOST: API → 127.0.0.1:8000 · VNC → localhost:5901 (Ctrl+C to close)"
exec ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 8000:127.0.0.1:8000 -L 5901:127.0.0.1:5901 "$HOST"
