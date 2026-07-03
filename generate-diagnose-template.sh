#!/usr/bin/env bash

# Generate the diagnose.sh script content
generate_diagnose_template() {
    local service="$1"
    local primary_port="$2"
    
    cat << EOF
#!/bin/sh
set -e

SERVICE_NAME="$service"

echo "=== \$SERVICE_NAME Diagnostic Tool ==="
echo ""

# 1. Container status
echo "Container Status:"
echo "----------------"
CONTAINERS=\$(podman ps -a --format '{{.Names}} {{.Status}}' | grep -E "(\$SERVICE_NAME|tailscale-\$SERVICE_NAME)")
if [ -n "\$CONTAINERS" ]; then
  echo "\$CONTAINERS"
else
  echo "No \$SERVICE_NAME containers found"
fi
echo ""

# 2. Tailscale status
if podman ps --format '{{.Names}}' | grep -q "^tailscale-\$SERVICE_NAME\$"; then
  echo "Tailscale Status:"
  echo "----------------"
  TS_IP=\$(podman exec tailscale-\$SERVICE_NAME tailscale ip -4 2>/dev/null || echo "Not available")
  echo "IP: \$TS_IP"
  
  # The full MagicDNS name (host.<tailnet>.ts.net) comes from DNSName;
  # the bare hostname alone is NOT a resolvable FQDN.
  TS_DNSNAME=\$(podman exec tailscale-\$SERVICE_NAME tailscale status --json --peers=false 2>/dev/null | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
  if [ -n "\$TS_DNSNAME" ]; then
    echo "MagicDNS: \${TS_DNSNAME%.}"
  fi
  echo ""
fi

# 3. Service logs
if podman ps --format '{{.Names}}' | grep -q "^\$SERVICE_NAME\$"; then
  echo "Recent \$SERVICE_NAME logs:"
  echo "------------------------"
  podman logs --tail 10 \$SERVICE_NAME
  echo ""
fi

# 4. Binding check
if podman ps --format '{{.Names}}' | grep -q "^\$SERVICE_NAME\$"; then
  echo "Binding Configuration:"
  echo "---------------------"
  if podman exec \$SERVICE_NAME sh -c "[ -f /config/config.xml ]" 2>/dev/null; then
    BIND_ADDRESS=\$(podman exec \$SERVICE_NAME grep -oP '(?<=<BindAddress>)[^<]+' /config/config.xml 2>/dev/null || echo "Not found")
    echo "Bind Address: \$BIND_ADDRESS"
    
    if [ "\$BIND_ADDRESS" = "127.0.0.1" ]; then
      echo ""
      echo "⚠️  Warning: Service is binding to localhost only"
      echo "This will prevent Tailscale access"
      echo ""
      echo "Fix by running:"
      echo "podman exec \$SERVICE_NAME sed -i 's/<BindAddress>127.0.0.1</<BindAddress>*</g' /config/config.xml"
      echo "podman restart \$SERVICE_NAME"
    fi
  else
    echo "No config.xml found"
  fi
  echo ""
fi

# 5. Connectivity test
if podman ps --format '{{.Names}}' | grep -q "^tailscale-\$SERVICE_NAME\$"; then
  echo "Connectivity Test:"
  echo "-----------------"

  # Test service if port is defined
  if [ -n "$primary_port" ]; then
    SVC_TEST=\$(podman exec tailscale-\$SERVICE_NAME wget -q --spider --timeout=5 http://localhost:$primary_port 2>/dev/null && echo "\$SERVICE_NAME: ✓ Accessible" || echo "\$SERVICE_NAME: × Not accessible")
    echo "\$SVC_TEST"
  fi
fi
echo ""

echo "Troubleshooting Tips:"
echo "--------------------"
echo "1. Restart services: ./stop.sh && ./run.sh"
echo "2. Check logs: podman logs \$SERVICE_NAME"
echo "3. Remove and recreate: ./remove.sh && ./run.sh"
echo ""

echo "Volume Information:"
echo "------------------"
echo "Service directory: \$(pwd)"
if [ -d "./tailscale" ]; then
  echo "Tailscale state: ./tailscale"
fi
echo ""

echo "Advanced Diagnostics:"
echo "--------------------"
echo "Check container resource usage: podman stats --no-stream \$SERVICE_NAME"
echo "Full logs: podman logs \$SERVICE_NAME"
echo "Container inspect: podman inspect \$SERVICE_NAME"
echo "Network inspect: podman network ls"
echo "Volume inspect: podman volume ls"
EOF
}
