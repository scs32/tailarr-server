#!/usr/bin/env bash
# Bootstrap the Tailarr controller: a Tailscale sidecar plus the controller
# web UI sharing its network namespace - deployed exactly like any other pod.
#
# Usage:
#   TS_AUTHKEY=tskey-... ./bootstrap-tailarr.sh          (preferred: env var)
#   ./bootstrap-tailarr.sh tskey-...                     (or as an argument)
#   ./bootstrap-tailarr.sh                               (reuse existing state/key)
#
# The web UI is then at http://tailarr.<your-tailnet>.ts.net:8080
set -euo pipefail

PODS_DIR="${PODS_DIR:-$HOME/Pods}"
# The controller release this script ships with. Pinned (not :latest) so a
# fresh install right after a release can't catch a stale :latest manifest
# from GHCR. CI enforces that this matches web/app.py's VERSION; bump both
# when cutting a release. HOMEPOD_IMAGE still overrides everything.
TAILARR_VERSION="0.9.1"
IMAGE="${HOMEPOD_IMAGE:-ghcr.io/scs32/tailarr:v${TAILARR_VERSION}}"
TS_IMAGE="docker.io/tailscale/tailscale:stable"
SOCKET="/run/podman/podman.sock"
KEY_FILE="$PODS_DIR/tailarr/.tailscale_authkey"

command -v podman >/dev/null || { echo "Error: podman is required" >&2; exit 1; }

# --- auth key: env var, argument, or existing key file / state ---
key="${TS_AUTHKEY:-${1:-}}"
if [[ -n "$key" ]]; then
    mkdir -p "$(dirname "$KEY_FILE")"
    printf '%s\n' "$key" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
elif [[ ! -f "$KEY_FILE" && ! -f "$PODS_DIR/tailarr/tailscale/tailscaled.state" ]]; then
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
# /dev/shm) and starts sidecars before services. On systemd hosts
# (Debian/Ubuntu — the documented target) it is wired to boot below;
# elsewhere wire it to whatever starts this host at boot (LaunchAgent,
# cron @reboot, etc.).
cat > /root/start-pods.sh << 'STARTEOF'
#!/bin/sh
# Bring up the pod fleet after guest boot (see bootstrap-tailarr.sh).
#
# HAZARD: wiping podman's runroot on a LIVE stack destroys the running
# containers (they drop to Created and need a full re-bootstrap). The wipe
# is only correct after a real reboot, when /run holds stale pre-reboot
# state. The tmpfs sentinel normally gates it, but if this script is run
# by hand on a healthy stack (or the sentinel was cleared), double-check:
# only wipe when podman genuinely can't reach its state AND nothing is Up.
if [ ! -f /dev/shm/pods-booted ]; then
  if podman --url unix:///run/podman/podman.sock info >/dev/null 2>&1 \
     || [ -n "$(podman ps --format '{{.Names}}' 2>/dev/null)" ]; then
    : # API socket answers or containers are Up -> healthy stack, a manual
      # run must NOT wipe; just (re)start anything stopped below.
  else
    # Socket dead AND nothing running: genuine post-reboot stale state.
    rm -rf /run/containers /run/user/0/netns /run/libpod 2>/dev/null || true
  fi
  touch /dev/shm/pods-booted
fi

# podman 4.x rootless bridge bug: IPAM db opens under this staging /run,
# which does not exist after a wipe / full-fleet stop. Must precede any
# bridge-network container start or they fail with an IPAM error.
mkdir -p /run/libpod/rootless-netns/run/containers/storage/networks 2>/dev/null || true

# /run is NOT tmpfs in these guests, so a stale socket FILE survives a VM
# restart while the service behind it is gone — probe the API, not the path.
mkdir -p /run/podman
if ! podman --url unix:///run/podman/podman.sock info >/dev/null 2>&1; then
  rm -f /run/podman/podman.sock
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

# --- boot persistence: on systemd hosts, run start-pods.sh at boot so the
# stack self-heals after a reboot with no manual wiring. Ordering lives in
# the script itself (sidecars, then services). Harmless to re-run; skipped
# on non-systemd hosts (e.g. apple/container guests with a minimal init).
if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null; then
    cat > /etc/systemd/system/tailarr-pods.service << UNITEOF
# Managed by Tailarr (bootstrap-tailarr.sh). Re-running the bootstrap
# OVERWRITES this file - put customizations in a drop-in instead:
#   systemctl edit tailarr-pods     (override.conf survives re-runs)
# The controller maintains 50-tailarr-mounts.conf in the drop-in dir:
# RequiresMountsFor for every registered share, so the fleet waits for
# media disks (nofail mounts!) instead of bind-mounting an empty dir.
[Unit]
Description=Tailarr pod fleet (Tailscale sidecars, then services)
Wants=network-online.target
After=network-online.target
RequiresMountsFor=${PODS_DIR}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/root/start-pods.sh

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
    systemctl enable tailarr-pods.service >/dev/null 2>&1 || true
    echo "Enabled tailarr-pods.service (fleet starts at boot)."
    echo "  (customize via: systemctl edit tailarr-pods - re-runs overwrite the unit itself)"
fi

mkdir -p "$PODS_DIR/tailarr/tailscale"

# HTTPS via tailscale serve: TLS on 443 with an automatic ts.net cert,
# proxying to the web UI. Requires "HTTPS Certificates" enabled once in
# the Tailscale admin console (DNS tab).
cat > "$PODS_DIR/tailarr/tailscale-serve.json" << 'SERVEEOF'
{
  "TCP": {"443": {"HTTPS": true}},
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}
    }
  }
}
SERVEEOF

echo "Removing existing tailarr containers..."
podman rm -f tailarr 2>/dev/null || true
podman rm -f tailscale-tailarr 2>/dev/null || true

echo "Starting Tailscale sidecar..."
# --network podman + kernel TUN + MTU 1200: direct (non-DERP) tailnet paths
# on rootless/nested hosts; see the sidecar notes in generate-run-template.sh.
mkdir -p /run/libpod/rootless-netns/run/containers/storage/networks 2>/dev/null || true
podman run -d \
  --name tailscale-tailarr \
  --network podman \
  --cap-add NET_ADMIN --cap-add NET_RAW \
  --device /dev/net/tun \
  -v "$PODS_DIR/tailarr/tailscale:/var/lib/tailscale" \
  -v "$PODS_DIR/tailarr/tailscale-serve.json:/config/serve.json" \
  -e TS_SERVE_CONFIG=/config/serve.json \
  -e TS_AUTHKEY="$(cat "$KEY_FILE" 2>/dev/null || true)" \
  -e TS_STATE_DIR=/var/lib/tailscale \
  -e TS_USERSPACE=false \
  -e TS_DEBUG_MTU=1280 \
  -e TS_HOSTNAME="tailarr" \
  "$TS_IMAGE"

sleep "${WAIT:-10}"
if ! podman ps --format '{{.Names}}' | grep -q '^tailscale-tailarr$'; then
    echo "Error: Tailscale sidecar failed to start. Recent logs:" >&2
    podman logs --tail 20 tailscale-tailarr >&2 || true
    exit 1
fi

echo "Starting Tailarr controller..."
# /run/libpod is mounted so run.sh's IPAM-staging mkdir (see the sidecar
# notes above) lands on the HOST when the controller drives pod starts.
podman run -d \
  --name tailarr \
  --network container:tailscale-tailarr \
  -v "$PODS_DIR:$PODS_DIR" \
  -v "$SOCKET:$SOCKET" \
  -v /run/libpod:/run/libpod \
  -e CONTAINER_HOST="unix://$SOCKET" \
  -e PODS_DIR="$PODS_DIR" \
  --restart unless-stopped \
  "$IMAGE"

sleep 3
FQDN=$(podman exec tailscale-tailarr tailscale status --json --peers=false 2>/dev/null \
    | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
echo ""
echo "Tailarr controller is up."
echo "  Web UI: https://${FQDN%.}  (or http://${FQDN%.}:8080)"
