#!/usr/bin/env bash

# Generate the run.sh script content
generate_run_template() {
    local service="$1"
    local auth_key_file="$2"
    local ts_image="$3"
    local npm_image="$4"
    local service_image="$5"
    local restart_policy="$6"
    local include_ts="$7"
    local include_npm="$8"
    local primary_port="$9"
    local service_info="${10}"
    local include_https="${11:-no}"

    local ports_json
    ports_json=$(jq -c '.ports // {}' <<<"$service_info")

    # HTTPS needs a port to proxy to and a Tailscale sidecar to serve from
    if [[ "$include_https" == "yes" && ( -z "$primary_port" || "$include_ts" != "yes" ) ]]; then
        include_https="no"
    fi

    # --- Header and container cleanup ---
    cat << EOF
#!/bin/sh
set -e

# Seconds to wait between startup phases (override with WAIT=0 ./run.sh)
WAIT="\${WAIT:-10}"

# Automatically remove existing containers for this service only
echo "Removing existing $service containers..."
podman rm -f $service 2>/dev/null || true
podman rm -f npm-$service 2>/dev/null || true
podman rm -f tailscale-$service 2>/dev/null || true

EOF

    # --- Tailscale sidecar ---
    if [[ "$include_ts" == "yes" ]]; then
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
# Start Tailscale first with a unique hostname for this service
echo "Starting Tailscale..."
podman run -d \\
  --name tailscale-$service \\
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
    fi

    # --- Nginx Proxy Manager ---
    if [[ "$include_npm" == "yes" ]]; then
        cat << EOF
# Start NPM
echo "Starting Nginx Proxy Manager..."
podman run -d \\
  --name npm-$service \\
EOF
        if [[ "$include_ts" == "yes" ]]; then
            echo "  --network container:tailscale-$service \\"
        else
            echo "  -p 80:80 -p 81:81 -p 443:443 \\"
        fi
        cat << EOF
  -e DB_SQLITE_FILE="/data/database.sqlite" \\
  -v "\$(pwd)/npm/data:/data" \\
  -v "\$(pwd)/npm/letsencrypt:/etc/letsencrypt" \\
  $npm_image

echo "Waiting for NPM..."
sleep "\$WAIT"

EOF
    fi

    # --- Main service ---
    cat << EOF
# Start main service
echo "Starting $service..."
podman run -d \\
  --name $service \\
EOF

    # Network configuration: share the Tailscale sidecar's namespace, or
    # publish ports directly when running without Tailscale.
    if [[ "$include_ts" == "yes" ]]; then
        echo "  --network container:tailscale-$service \\"
    else
        local port_pair
        while IFS= read -r port_pair; do
            echo "  -p $port_pair \\"
        done < <(jq -r 'to_entries[] | "\(.key):\(.value)"' <<<"$ports_json")
    fi

    # Environment variables
    while IFS= read -r env_pair; do
        echo "  -e $env_pair \\"
    done < <(jq -r '.environment | to_entries[] | "\(.key)=\"\(.value)\""' <<<"$service_info")

    # Volume mounts
    while IFS= read -r volume_pair; do
        echo "  -v $volume_pair \\"
    done < <(jq -r '.volumes | to_entries[] | "\(.value):\(.key)"' <<<"$service_info")

    # Complete the service container command
    cat << EOF
  --restart $restart_policy \\
  $service_image

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
    if [[ "$include_ts" == "yes" ]]; then
        generate_tailscale_results "$service" "$include_npm" "$primary_port" "$ports_json" "$include_https"
    else
        generate_local_results "$service" "$include_npm" "$primary_port" "$ports_json"
    fi
}

# Result section for Tailscale-enabled deployments
generate_tailscale_results() {
    local service="$1"
    local include_npm="$2"
    local primary_port="$3"
    local ports_json="$4"
    local include_https="${5:-no}"

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

    if [[ "$include_npm" == "yes" ]]; then
        cat << EOF
NPM_READY=\$(podman exec tailscale-$service wget -q --spider --timeout=5 http://localhost:81 2>/dev/null && echo "yes" || echo "no")
EOF
    fi

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

    if [[ "$include_npm" == "yes" ]]; then
        cat << EOF
if [ "\$NPM_READY" = "yes" ]; then
  echo "  Nginx Proxy Manager: OK"
else
  echo "  Nginx Proxy Manager: not ready"
fi
EOF
    fi

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

    if [[ "$include_npm" == "yes" ]]; then
        echo "echo \"  NPM Admin: http://\$TS_FQDN:81\""
    fi

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

# Result section for deployments without Tailscale (locally published ports)
generate_local_results() {
    local service="$1"
    local include_npm="$2"
    local primary_port="$3"
    local ports_json="$4"

    cat << EOF
echo ""
echo "========================================"
echo "  $service Deployment Complete"
echo "========================================"
echo ""
echo "Access URLs (local network):"
EOF

    if [[ "$include_npm" == "yes" ]]; then
        echo "echo \"  NPM Admin: http://localhost:81\""
    fi

    local host_port
    while IFS= read -r host_port; do
        echo "echo \"  $service: http://localhost:$host_port\""
    done < <(jq -r 'keys[]' <<<"$ports_json")

    cat << EOF
echo ""
echo "Run './diagnose.sh' if the service is not reachable."
EOF
}
