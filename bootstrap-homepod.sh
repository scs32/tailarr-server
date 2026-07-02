#!/usr/bin/env bash
# Bootstrap the HomePod controller: a Tailscale sidecar plus the controller
# web UI sharing its network namespace - deployed exactly like any other pod.
#
# Usage:
#   TS_AUTHKEY=tskey-... ./bootstrap-homepod.sh          (preferred: env var)
#   ./bootstrap-homepod.sh tskey-...                     (or as an argument)
#   ./bootstrap-homepod.sh                               (reuse existing state/key)
#
# The web UI is then at http://homepod.<your-tailnet>.ts.net:8080
set -euo pipefail

PODS_DIR="${PODS_DIR:-$HOME/Pods}"
IMAGE="${HOMEPOD_IMAGE:-localhost/homepod:dev}"
TS_IMAGE="docker.io/tailscale/tailscale:stable"
SOCKET="/run/podman/podman.sock"
KEY_FILE="$PODS_DIR/homepod/.tailscale_authkey"

command -v podman >/dev/null || { echo "Error: podman is required" >&2; exit 1; }

# --- auth key: env var, argument, or existing key file / state ---
key="${TS_AUTHKEY:-${1:-}}"
if [[ -n "$key" ]]; then
    mkdir -p "$(dirname "$KEY_FILE")"
    printf '%s\n' "$key" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
elif [[ ! -f "$KEY_FILE" && ! -f "$PODS_DIR/homepod/tailscale/tailscaled.state" ]]; then
    echo "Error: no auth key given and no existing state." >&2
    echo "Usage: TS_AUTHKEY=tskey-... $0" >&2
    exit 1
fi

# --- container network MTU (nested VMs, e.g. apple/container guests, have
# MTU 1280; containers defaulting to larger MTUs silently blackhole TLS) ---
iface=$(awk '$2=="00000000" {print $1; exit}' /proc/net/route 2>/dev/null || true)
host_mtu=$(cat "/sys/class/net/${iface:-eth0}/mtu" 2>/dev/null || echo 1500)
if [[ "$host_mtu" -lt 1500 ]] && ! grep -qs "network_cmd_options" /etc/containers/containers.conf 2>/dev/null; then
    echo "Host MTU is $host_mtu - matching container network MTU..."
    printf '[engine]\nnetwork_cmd_options=["mtu=%s"]\n' "$host_mtu" >> /etc/containers/containers.conf
fi

# --- podman API socket (the controller drives the host through it) ---
if [[ ! -S "$SOCKET" ]]; then
    echo "Starting podman API socket..."
    mkdir -p "$(dirname "$SOCKET")"
    nohup podman system service --time=0 "unix://$SOCKET" \
        >/var/log/podman-api.log 2>&1 &
    sleep 2
    [[ -S "$SOCKET" ]] || { echo "Error: could not start podman API socket" >&2; exit 1; }
fi

# --- boot recovery script: this host may keep /run on disk (not tmpfs),
# so podman cannot detect reboots and wedges on stale state. Install a
# start script that wipes the runroot once per boot (tmpfs sentinel in
# /dev/shm) and starts sidecars before services. Wire it to whatever
# starts this host at boot (LaunchAgent, cron @reboot, etc.).
cat > /root/start-pods.sh << 'STARTEOF'
#!/bin/sh
# Bring up the pod fleet after guest boot (see bootstrap-homepod.sh).
if [ ! -f /dev/shm/pods-booted ]; then
  rm -rf /run/containers /run/user/0/netns /run/libpod 2>/dev/null || true
  touch /dev/shm/pods-booted
fi

mkdir -p /run/podman
if [ ! -S /run/podman/podman.sock ]; then
  nohup podman system service --time=0 unix:///run/podman/podman.sock >/var/log/podman-api.log 2>&1 &
  sleep 2
fi

for c in $(podman ps -a --format "{{.Names}}" | grep "^tailscale-"); do
  podman start "$c" >/dev/null 2>&1 || true
done
sleep 5
for c in $(podman ps -a --format "{{.Names}}" | grep -v "^tailscale-"); do
  podman start "$c" >/dev/null 2>&1 || { sleep 3; podman start "$c" >/dev/null 2>&1; } || true
done
podman ps --format "{{.Names}}"
STARTEOF
chmod +x /root/start-pods.sh

mkdir -p "$PODS_DIR/homepod/tailscale"

# HTTPS via tailscale serve: TLS on 443 with an automatic ts.net cert,
# proxying to the web UI. Requires "HTTPS Certificates" enabled once in
# the Tailscale admin console (DNS tab).
cat > "$PODS_DIR/homepod/tailscale-serve.json" << 'SERVEEOF'
{
  "TCP": {"443": {"HTTPS": true}},
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}
    }
  }
}
SERVEEOF

echo "Removing existing homepod containers..."
podman rm -f homepod 2>/dev/null || true
podman rm -f tailscale-homepod 2>/dev/null || true

echo "Starting Tailscale sidecar..."
podman run -d \
  --name tailscale-homepod \
  --cap-add NET_ADMIN --cap-add NET_RAW \
  --device /dev/net/tun \
  -v "$PODS_DIR/homepod/tailscale:/var/lib/tailscale" \
  -v "$PODS_DIR/homepod/tailscale-serve.json:/config/serve.json" \
  -e TS_SERVE_CONFIG=/config/serve.json \
  -e TS_AUTHKEY="$(cat "$KEY_FILE" 2>/dev/null || true)" \
  -e TS_STATE_DIR=/var/lib/tailscale \
  -e TS_HOSTNAME="homepod" \
  "$TS_IMAGE"

sleep "${WAIT:-10}"
if ! podman ps --format '{{.Names}}' | grep -q '^tailscale-homepod$'; then
    echo "Error: Tailscale sidecar failed to start. Recent logs:" >&2
    podman logs --tail 20 tailscale-homepod >&2 || true
    exit 1
fi

echo "Starting HomePod controller..."
podman run -d \
  --name homepod \
  --network container:tailscale-homepod \
  -v "$PODS_DIR:$PODS_DIR" \
  -v "$SOCKET:$SOCKET" \
  -e CONTAINER_HOST="unix://$SOCKET" \
  -e PODS_DIR="$PODS_DIR" \
  --restart unless-stopped \
  "$IMAGE"

sleep 3
FQDN=$(podman exec tailscale-homepod tailscale status --json --peers=false 2>/dev/null \
    | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
echo ""
echo "HomePod controller is up."
echo "  Web UI: https://${FQDN%.}  (or http://${FQDN%.}:8080)"
