#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# ORDERING RULE FOR run.sh: NEVER DESTROY BEFORE YOU KNOW YOU CAN RECREATE.
#
# run.sh runs under `set -e` and used to `podman rm -f $service` as its very
# first act, before a single precondition had been checked. Everything that
# could fail afterwards — a missing auth key, a dead bind source, a slow
# sidecar, a failing `podman run` — therefore left the pod DESTROYED, with its
# Pods dir, run.sh, .config.json and data all intact but no container at all.
#
# That is not theoretical. On 2026-08-05 a controller upgrade / storage-
# appliance repin did it three times on the live box: `jellyfin` twice and
# `lidarr` once. Both are exactly the pods that hold the `media` storage share,
# which is the tell — a storage share renders as `-v /srv/media:/data:rslave`,
# and while the appliance is being repinned /srv/media is a dead NFS mount. The
# containers were already gone by the time podman refused to run against it.
#
# So _emit_authkey_precondition and the WAIT validation are rendered BEFORE the
# removal block, and _emit_run_postcondition is rendered after the main
# `podman run` so a pod that was removed and not recreated says so loudly
# (exit 2) instead of exiting on a generic error the caller reads as "stopped".
# Anything new that can refuse belongs above the removal block too.
#
# ⚠️ WHAT DOES *NOT* BELONG HERE: anything that inspects the HOST's mounts.
# An earlier version of this change added a `mountpoint -q` preflight on every
# `:rslave` (storage-backed) bind source, to catch precisely the repin above.
# It cannot work from here. run.sh is executed by _run_action INSIDE the
# controller container (web/app.py), and that container mounts only PODS_DIR,
# the podman socket and /run/libpod — never /srv. podman still binds the share
# correctly because it is a REMOTE client and the `-v` resolves host-side, but
# `mountpoint` and /proc/self/mounts read the CONTROLLER's namespace, where
# /srv/<vol> is an ordinary empty directory and never a mount point. Measured
# on the live box inside the `tailarr` container: the dir exists, `mountpoint
# -q` says no, and /proc/self/mounts has no /srv line at all. The preflight
# would therefore have refused EVERY controller-driven start of jellyfin and
# lidarr, forever — trading an occasional repin teardown for permanently
# unmanageable storage pods. app.py's status_shares documents the same
# namespace trap for os.path.isdir. A liveness check has to be made from
# somewhere that can actually see the host, not from inside run.sh.
# ---------------------------------------------------------------------------

# The auth key precondition. PURE precondition: it reads two files and decides
# whether enrolment is possible at all. It used to be emitted after the removal
# block, which meant "no auth key" tore the pod down and then told you why.
_emit_authkey_precondition() {
    local auth_key_file="$1"
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
}

# Post-condition on the main service container. Under `set -e` a failed
# `podman run` used to abort the script *after* the removal block had already
# run, so the pod ended up with no container and the caller saw a generic
# non-zero exit — indistinguishable from "the pod is stopped". Exit 2 is
# reserved for exactly this: REMOVED AND NOT RECREATED.
#
# ⚠️ `podman container exists`, NEVER `podman inspect`. This shipped in v0.184.0
# using `podman inspect $service`, which ALSO MATCHES IMAGES — and every arr pod
# is named after its image (`jellyfin`, `lidarr`, `radarr`, `sonarr`, `prowlarr`,
# `nzbget` all resolve to docker.io/linuxserver/<name>). So the guard could not
# fire for any of the six pods it was written to protect: the container was gone
# and `podman inspect jellyfin` cheerfully described the IMAGE.
#
# Observed live on 2026-08-06: the v0.185.0 deploy tore down all six, and
# `podman inspect` reported every one of them healthy while
# `podman container exists` reported GONE. It fooled the deploy's own
# verification script too, which is how it was finally caught.
#
# KNOWN AND ACCEPTED: `podman container exists` exits 0 for present, 1 for
# absent, and 125 on a DAEMON/CONNECTION error — the last of which this
# condition reads as "absent", so a broken podman makes the guard report exit 2
# ("removed and could not be recreated") for a pod that may still be there.
# That is the SAFE direction and is deliberate: it fails toward alarm, exactly
# as the old `inspect` behaviour did, and it leaves the pod's scripts, config
# and data untouched — the only cost is a false alarm the operator can check in
# seconds. Erring the other way (treating 125 as "present") would restore the
# original defect, silently declaring a torn-down pod healthy. Do not "fix"
# this by distinguishing 125; if it ever needs distinguishing, the answer is a
# clearer MESSAGE for 125, never a quieter one.
_emit_run_postcondition() {
    local service="$1"
    cat << EOF
if [ "\${RUN_RC:-0}" -ne 0 ] || ! podman container exists $service; then
  echo "FATAL: $service was removed and could not be recreated" >&2
  echo "       (podman run exited \${RUN_RC:-0}.) The pod's scripts, config and data are" >&2
  echo "       intact — fix the cause and run ./run.sh again, or press Start." >&2
  exit 2
fi

EOF
}

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
    # Tailscale LocalAPI socket exposure (Go identity plane, transport A), opt-in
    # per pod, default off. See parse-service-config.sh + go-increment-2-build-plan
    # §2. Emits ONE extra `-v` on the sidecar; off ⇒ run.sh byte-identical to today.
    local expose_ts_socket
    expose_ts_socket=$(jq -r '.expose_ts_socket // "no"' <<<"$service_info")

    # --- Header ---
    cat << EOF
#!/bin/sh
set -e

# Seconds to wait between startup phases (override with WAIT=0 ./run.sh)
WAIT="\${WAIT:-10}"

# Validate WAIT HERE, above the removal block, because it is used by a bare
# \`sleep "\$WAIT"\` further down — and \`sleep\` under set -e is one more way to
# abort AFTER the containers are gone. WAIT=bogus or WAIT=-1 would remove the
# pod and then die on the settle sleep, never reaching the post-condition that
# is supposed to report it. Same ordering rule as everything else up here.
case "\$WAIT" in
  ''|*[!0-9]*)
    echo "Error: WAIT must be a non-negative whole number of seconds (got '\$WAIT')." >&2
    echo "       Nothing has been changed." >&2
    exit 1 ;;
esac
# An absurd value is a typo, not an intent: it would stall the deploy for
# hours (and the ceiling below multiplies it by six) while holding the pod's
# operation claim. 600s is well past any real sidecar enrolment.
if [ "\$WAIT" -gt 600 ]; then
  echo "Error: WAIT=\$WAIT is unreasonably large (max 600). Nothing has been changed." >&2
  exit 1
fi

# Private-registry credentials (Settings -> Private registries): the
# controller renders a standard containers authfile one level up in the
# Pods dir; podman reads it through this env var for every pull below.
if [ -f "\$(dirname "\$(pwd)")/.registry-auth.json" ]; then
  export REGISTRY_AUTH_FILE="\$(dirname "\$(pwd)")/.registry-auth.json"
fi

EOF

    # --- PRECONDITIONS: everything that can refuse runs BEFORE the removal
    #     block below. See the ORDERING RULE at the top of this file. ---
    _emit_authkey_precondition "$auth_key_file"          # RED-MUTATE:authkey

    # --- Container cleanup (the point of no return) ---
    cat << EOF
# Automatically remove existing containers for this service only
echo "Removing existing $service containers..."
podman rm -f $service 2>/dev/null || true
podman rm -f tailscale-$service 2>/dev/null || true

EOF
    # RED-MUTATE:post-removal-anchor

    # --- Tailscale sidecar (every pod gets one) ---
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
        # Tailscale LocalAPI socket exposure (transport A, opt-in per pod). When
        # on, bind-mount the sidecar's socket DIR onto a host path ($(pwd)/ts-sock)
        # so a sibling container can reach this sidecar's tailscaled.sock. The dir
        # (not just the file) is mounted because tailscaled creates the socket at
        # runtime, and TS_SOCKET is set explicitly to the default path for clarity.
        # SECURITY: the LocalAPI is full read+write over that daemon, so this host
        # path MUST stay controller-only (root:root 0600); it grants nothing the
        # controller lacks via its existing `podman exec … tailscale`. A `:ro`
        # mount is deliberately NOT used — connect(2) on an AF_UNIX socket needs
        # write, and :ro would not make it read-only anyway (build-plan §1.4.ii).
        if [[ "$expose_ts_socket" == "yes" ]]; then
            cat << EOF
  -v "\$(pwd)/ts-sock:/var/run/tailscale" \\
  -e TS_SOCKET=/var/run/tailscale/tailscaled.sock \\
EOF
        fi
        if [[ "$include_https" == "yes" ]]; then
            cat << EOF
  -v "\$(pwd)/tailscale-serve.json:/config/serve.json" \\
  -e TS_SERVE_CONFIG=/config/serve.json \\
EOF
        fi
        cat << EOF
  -e TS_AUTHKEY="\$(cat "\$TS_AUTHKEY_FILE" 2>/dev/null || true)" \\
  -e TS_AUTH_ONCE=true \\
  -e TS_STATE_DIR=/var/lib/tailscale \\
  -e TS_USERSPACE=false \\
  -e TS_DEBUG_MTU=1280 \\
  -e TS_HOSTNAME="$service" \\
EOF

        # Uptime Kuma probes its monitor targets by MagicDNS name
        # (https://<service>.<tailnet>.ts.net) — so its sidecar, uniquely, needs
        # MagicDNS/accept-dns ON to resolve them. Every OTHER sidecar deliberately
        # runs accept-dns OFF (services address each other by IP, not name; see
        # web/app.py:_storage_addr), so this is scoped to uptime-kuma alone and
        # does not change DNS behavior for any other pod.
        if [[ "$service" == "uptime-kuma" ]]; then
            cat << EOF
  -e TS_ACCEPT_DNS=true \\
EOF
        fi

        cat << EOF
  $ts_image

echo "Waiting for Tailscale..."
# The settle sleep is KEPT, deliberately. podman ps lists the sidecar the
# moment it is created, so a poll alone would fall through in milliseconds and
# start the main service before tailscaled had enrolled — strictly earlier than
# the old code, which is a behaviour change this fix has no business making.
# The poll below only ADDS tolerance past this point; it does not replace it.
# (No backticks in this comment: it is inside an unquoted heredoc, where they
# would be command substitution and their OUTPUT would land in run.sh.)
sleep "\$WAIT"

# Then poll, instead of looking exactly once.
#
# The one-shot check turned SLOWNESS into DESTRUCTION: the containers are
# already removed by the time we get here, so a sidecar that needed 11 seconds
# instead of 10 — routine when a post-upgrade fleet rerender starts ten pods
# while the storage appliance is being repinned — exited non-zero under set -e
# and left the pod with no container at all. Polling costs nothing when the
# sidecar is quick and only fails after a real ceiling.
#
# Ceiling is WAIT-derived so the default (10) gives 60s more on top of the
# settle sleep, and WAIT=0 (used by the test harnesses) checks ONCE, never
# sleeps, and never blocks.
TS_CEILING=\$((WAIT * 6))
TS_WAITED=0
while ! podman ps --format '{{.Names}}' | grep -q "^tailscale-$service\$"; do
  if [ "\$TS_WAITED" -ge "\$TS_CEILING" ]; then
    echo "Error: Tailscale sidecar failed to start within \${TS_CEILING}s. Recent logs:" >&2
    podman logs --tail 20 tailscale-$service >&2 || true
    exit 1
  fi
  sleep 1
  TS_WAITED=\$((TS_WAITED + 1))
done

EOF

    # --- Main service ---
    # RUN_RC captures the exit status instead of letting `set -e` abort here.
    # The abort was the destructive path: the containers are already removed by
    # this point, so aborting left the pod with NO container and an exit code
    # the caller could not distinguish from an ordinary start failure. We keep
    # going exactly far enough to run _emit_run_postcondition, which reports it.
    cat << EOF
# Start main service
echo "Starting $service..."
RUN_RC=0
podman run -d \\
  --name $service \\
EOF

    # Share the Tailscale sidecar's network namespace. The pod publishes no
    # host ports; it is reachable only over the tailnet via its own identity.
    echo "  --network container:tailscale-$service \\"

    # Privileged container flags for the storage appliance ONLY. The
    # deploy_class gate + allowlist are enforced upstream in
    # parse-service-config.sh, so by the time these reach here they are always
    # the fixed set (--device /dev/fuse, --cap-add SYS_ADMIN, optionally
    # --security-opt apparmor=unconfined). @sh-quote each value via jq exactly
    # like the env/volume emission below (S1) so nothing can break out of run.sh
    # into the host shell. Ordinary pods carry empty lists → nothing emitted.
    while IFS= read -r _dev; do
        echo "  --device $_dev \\"
    done < <(jq -r '(.devices // [])[] | @sh' <<<"$service_info")
    while IFS= read -r _cap; do
        echo "  --cap-add $_cap \\"
    done < <(jq -r '(.cap_add // [])[] | @sh' <<<"$service_info")
    while IFS= read -r _secopt; do
        echo "  --security-opt $_secopt \\"
    done < <(jq -r '(.security_opt // [])[] | @sh' <<<"$service_info")

    # Environment variables. @sh-quote each KEY=VALUE so a value containing
    # shell metacharacters ($, ", `, ;) can neither break out into the host
    # shell that runs run.sh nor silently corrupt it (S1 — also a real bug for
    # any legitimate password containing those characters).
    while IFS= read -r env_pair; do
        echo "  -e $env_pair \\"
    done < <(jq -r '.environment | to_entries[] | (.key + "=" + (.value|tostring)) | @sh' <<<"$service_info")

    # Volume mounts; a host path ending in :ro becomes a read-only mount.
    # @sh-quote the whole host:container[:ro] token so a crafted host path
    # can't break out of run.sh into the host shell (S1).
    while IFS= read -r volume_pair; do
        echo "  -v $volume_pair \\"
    done < <(jq -r '.volumes | to_entries[] |
        (if (.value | endswith(":ro"))
        then "\(.value | rtrimstr(":ro")):\(.key):ro"
        elif (.value | endswith(":rslave"))
        then "\(.value | rtrimstr(":rslave")):\(.key):rslave"
        else "\(.value):\(.key)" end) | @sh' <<<"$service_info")

    # Complete the service container command
    echo "  --restart $restart_policy \\"
    if [[ -n "$memory_limit" ]]; then
        echo "  -m $memory_limit \\"
    fi
    if [[ -n "$command_str" ]]; then
        echo "  $service_image \\"
        # Word-split the command into podman args and POSIX single-quote each,
        # so metacharacters in a catalog-supplied command can't execute in the
        # host shell that runs run.sh (S1). Normal multi-arg commands are
        # preserved; a rare arg with embedded spaces would be split (accepted).
        _cmd_line="  "
        for _cw in $command_str; do
            _cesc=$(printf "%s" "$_cw" | sed "s/'/'\\\\''/g")
            _cmd_line="$_cmd_line'$_cesc' "
        done
        echo "$_cmd_line || RUN_RC=\$?"
    else
        echo "  $service_image || RUN_RC=\$?"
    fi
    echo ""

    _emit_run_postcondition "$service"   # RED-MUTATE:postcondition

    cat << EOF
echo "Waiting for $service..."
sleep "\$WAIT"

EOF

    # --- One-time app-config seeding (catalog config_file/config_set) ---
    local config_file config_set_json
    config_file=$(jq -r '.config_file // ""' <<<"$service_info")
    config_set_json=$(jq -c '.config_set // {}' <<<"$service_info")
    if [[ -n "$config_file" && "$config_set_json" != "{}" ]]; then
        cat << EOF
# Seed catalog-defined defaults into the app's own config file (e.g.
# nzbget's DestDir/InterDir, which the base image points at /downloads —
# a path mounted nowhere under the shared-/data layout). Applied ONCE per
# pod: the .config-seeded sentinel keeps re-renders (updates,
# reconfigures) from stomping values the user has since changed in the
# app itself. If the app has not written its config file yet, skip
# WITHOUT the sentinel so the next run seeds it.
if [ ! -f "\$(pwd)/.config-seeded" ]; then
  if podman exec $service sh -c "[ -f $config_file ]" 2>/dev/null; then
    echo "Seeding $service config defaults..."
EOF
        local ck cv
        while IFS=$'\t' read -r ck cv; do
            # Rendered into a sed program below: refuse key/value shapes
            # that could escape it (catalog entries are trusted the same
            # as their image/command fields, this guards against typos).
            if [[ "$ck" =~ ^[A-Za-z0-9_.-]+$ && "$cv" != *'|'* && "$cv" != *"'"* ]]; then
                echo "    podman exec $service sed -i 's|^${ck}=.*|${ck}=${cv}|' $config_file"
            else
                log_warn "config_set: skipping unsafe key/value pair for '$ck'"
            fi
        done < <(jq -r '.config_set | to_entries[] | "\(.key)\t\(.value)"' <<<"$service_info")
        cat << EOF
    touch "\$(pwd)/.config-seeded"
    echo "Restarting $service to pick up seeded config..."
    podman restart $service
    sleep "\$WAIT"
  else
    echo "Note: $config_file not present yet - defaults will be seeded on the next run"
  fi
fi

EOF
    fi

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
