#!/usr/bin/env bash

# Generate the run.sh script content
generate_run_template() {
    local service="$1"
    local auth_key_file="$2"
    local ts_image="$3"
    local service_image="$4"
    local restart_policy="$5"
    local primary_port="$6"
    local service_info="$7"

    local ports_json command_str memory_limit include_https funnel
    ports_json=$(jq -c '.ports // {}' <<<"$service_info")
    command_str=$(jq -r '.command // ""' <<<"$service_info")
    memory_limit=$(jq -r '.memory_limit // ""' <<<"$service_info")
    # Every pod is a Tailscale node; HTTPS via serve is on whenever there is a
    # port to proxy (parse-service-config derives this from the primary port).
    include_https=$(jq -r '.include_https // "no"' <<<"$service_info")
    # Public exposure (Tailscale Funnel), opt-in per pod.
    funnel=$(jq -r '.funnel // "no"' <<<"$service_info")

    # --- Header and container cleanup ---
    cat << EOF
#!/bin/sh
set -e

# Seconds to wait between startup phases (override with WAIT=0 ./run.sh)
WAIT="\${WAIT:-10}"

# Automatically remove existing containers for this service only
echo "Removing existing $service containers..."
podman rm -f $service 2>/dev/null || true
podman rm -f tailscale-$service 2>/dev/null || true

EOF

    # --- Tailscale sidecar (every pod gets one) ---
    cat << EOF
# The auth key is read from this file at runtime and is never stored in
# this script or in saved configurations. It is only needed for the FIRST
# enrollment: afterwards the pod's identity lives in ./tailscale/ and a
# spent or deleted key file is fine (single-use keys are supported).
TS_AUTHKEY_FILE="$auth_key_file"
if [ ! -f "\$TS_AUTHKEY_FILE" ] && [ ! -f "\$(pwd)/tailscale/tailscaled.state" ]; then
  echo "Error: no auth key file (\$TS_AUTHKEY_FILE) and no existing Tailscale state in \$(pwd)/tailscale" >&2
  exit 1
fi

EOF

        if [[ "$include_https" == "yes" ]]; then
            cat << EOF
# HTTPS via tailscale serve: terminate TLS on 443 with an automatic
# ts.net certificate and proxy to the service. Requires "HTTPS
# Certificates" to be enabled once in the Tailscale admin console (DNS tab).
cat > "\$(pwd)/tailscale-serve.json" << 'SERVEEOF'
{
  "TCP": {"443": {"HTTPS": true}},
EOF
            if [[ "$funnel" == "yes" ]]; then
                cat << 'EOF'
  "AllowFunnel": {"${TS_CERT_DOMAIN}:443": true},
EOF
            fi
            cat << EOF
  "Web": {
    "\${TS_CERT_DOMAIN}:443": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:$primary_port"}}
    }
  }
}
SERVEEOF

EOF
        fi

        cat << EOF
# Start Tailscale first with a unique hostname for this service.
#
# --network podman (the default bridge) gives every sidecar its OWN routable
# IP, so Tailscale nodes discover each other and connect DIRECTLY. Without it
# (slirp4netns default on rootless/nested hosts) every pod thinks it is
# 10.0.2.100 and ALL tailnet traffic relays through DERP at ~14 KB/s.
# TS_USERSPACE=false uses a kernel TUN device (throughput + MTU control).
# TS_DEBUG_MTU=1280: IPv6 refuses to run on links below 1280 bytes, and
# Funnel delivers ingress traffic to the node's tailnet IPv6 — smaller MTUs
# (the old 1200) silently break public exposure. 1280 is the floor that
# keeps IPv6 alive; oversized WireGuard UDP on nested 1280-byte host links
# falls back to DERP/PMTUD per path.
# The mkdir works around a podman 4.x rootless bug: bridge IPAM opens its db
# under a staging /run that is torn down with the last bridge container.
echo "Starting Tailscale..."
mkdir -p /run/libpod/rootless-netns/run/containers/storage/networks 2>/dev/null || true
podman run -d \\
  --name tailscale-$service \\
  --network podman \\
  --cap-add NET_ADMIN --cap-add NET_RAW \\
  --device /dev/net/tun \\
  -v "\$(pwd)/tailscale:/var/lib/tailscale" \\
EOF
        if [[ "$include_https" == "yes" ]]; then
            cat << EOF
  -v "\$(pwd)/tailscale-serve.json:/config/serve.json" \\
  -e TS_SERVE_CONFIG=/config/serve.json \\
EOF
        fi
        cat << EOF
  -e TS_AUTHKEY="\$(cat "\$TS_AUTHKEY_FILE" 2>/dev/null || true)" \\
  -e TS_STATE_DIR=/var/lib/tailscale \\
  -e TS_USERSPACE=false \\
  -e TS_DEBUG_MTU=1280 \\
  -e TS_HOSTNAME="$service" \\
  $ts_image

echo "Waiting for Tailscale..."
sleep "\$WAIT"

# Fail fast if the sidecar died (bad/spent auth key, missing /dev/net/tun...)
if ! podman ps --format '{{.Names}}' | grep -q "^tailscale-$service\$"; then
  echo "Error: Tailscale sidecar failed to start. Recent logs:" >&2
  podman logs --tail 20 tailscale-$service >&2 || true
  exit 1
fi

EOF

    # --- Main service ---
    cat << EOF
# Start main service
echo "Starting $service..."
podman run -d \\
  --name $service \\
EOF

    # Share the Tailscale sidecar's network namespace. The pod publishes no
    # host ports; it is reachable only over the tailnet via its own identity.
    echo "  --network container:tailscale-$service \\"

    # Environment variables
    while IFS= read -r env_pair; do
        echo "  -e $env_pair \\"
    done < <(jq -r '.environment | to_entries[] | "\(.key)=\"\(.value)\""' <<<"$service_info")

    # Volume mounts; a host path ending in :ro becomes a read-only mount
    while IFS= read -r volume_pair; do
        echo "  -v $volume_pair \\"
    done < <(jq -r '.volumes | to_entries[] |
        if (.value | endswith(":ro"))
        then "\(.value | rtrimstr(":ro")):\(.key):ro"
        else "\(.value):\(.key)" end' <<<"$service_info")

    # Complete the service container command
    echo "  --restart $restart_policy \\"
    if [[ -n "$memory_limit" ]]; then
        echo "  -m $memory_limit \\"
    fi
    if [[ -n "$command_str" ]]; then
        echo "  $service_image \\"
        echo "  $command_str"
    else
        echo "  $service_image"
    fi
    cat << EOF

echo "Waiting for $service..."
sleep "\$WAIT"

EOF

    # --- Bind-address fix (Arr-suite style config.xml) ---
    if [[ -n "$primary_port" ]]; then
        cat << EOF
# Services with a config.xml (Sonarr/Radarr/etc.) sometimes bind to
# 127.0.0.1 only, which blocks access from outside the container.
echo "Checking $service binding configuration..."
if podman exec $service sh -c "[ -f /config/config.xml ]" 2>/dev/null; then
  BIND_ADDRESS=\$(podman exec $service grep -oP '(?<=<BindAddress>)[^<]+' /config/config.xml 2>/dev/null || echo "")
  if [ "\$BIND_ADDRESS" = "127.0.0.1" ]; then
    echo "Fixing binding address..."
    podman exec $service sed -i 's/<BindAddress>127.0.0.1</<BindAddress>*</g' /config/config.xml
    echo "Restarting $service..."
    podman restart $service
    sleep "\$WAIT"
  fi
else
  echo "Config file not found - $service may still be initializing"
fi

EOF
    fi

    # --- Results ---
    generate_tailscale_results "$service" "$primary_port" "$ports_json" "$include_https"
}

# Result section for Tailscale-enabled deployments
generate_tailscale_results() {
    local service="$1"
    local primary_port="$2"
    local ports_json="$3"
    local include_https="${4:-no}"

    cat << EOF
# Get Tailscale network information
echo "Getting network information..."
TS_IP=\$(podman exec tailscale-$service tailscale ip -4 2>/dev/null || echo "Not available")

# The full MagicDNS name (host.<tailnet>.ts.net) comes from DNSName;
# the bare hostname alone is NOT a resolvable FQDN.
TS_DNSNAME=\$(podman exec tailscale-$service tailscale status --json --peers=false 2>/dev/null | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
TS_FQDN="\${TS_DNSNAME%.}"
if [ -z "\$TS_FQDN" ]; then
  TS_FQDN="<pending - run: podman exec tailscale-$service tailscale status>"
fi

echo ""
echo "Verifying services..."
EOF

    if [[ -n "$primary_port" ]]; then
        cat << EOF
SERVICE_READY=\$(podman exec tailscale-$service wget -q --spider --timeout=5 http://localhost:$primary_port 2>/dev/null && echo "yes" || echo "no")
EOF
    fi

    cat << EOF

echo ""
echo "========================================"
echo "  $service Deployment Complete"
echo "========================================"
echo ""
echo "Network Information:"
echo "  Tailscale IP: \$TS_IP"
echo "  MagicDNS: \$TS_FQDN"
echo ""
echo "Service Status:"
EOF

    if [[ -n "$primary_port" ]]; then
        cat << EOF
if [ "\$SERVICE_READY" = "yes" ]; then
  echo "  $service: OK"
else
  echo "  $service: not ready"
fi
EOF
    fi

    cat << EOF
echo ""
echo "Access URLs:"
EOF

    if [[ "$include_https" == "yes" ]]; then
        echo "echo \"  $service: https://\$TS_FQDN (HTTPS via tailscale serve)\""
    fi

    if [[ -n "$primary_port" ]]; then
        echo "echo \"  $service: http://\$TS_FQDN:$primary_port\""
        cat << EOF
echo ""
echo "Direct IP Access:"
echo "  http://\$TS_IP:$primary_port ($service)"
EOF
    fi

    cat << EOF
echo ""

if [ "\${SERVICE_READY:-yes}" != "yes" ]; then
  echo "Note: $service is not yet accessible."
  echo "Run './diagnose.sh' if the issue persists."
fi
EOF
}
