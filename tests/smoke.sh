#!/bin/sh
# Smoke test for the Tailarr engine.
#
# Drives create.sh directly with config JSON on stdin (exactly how the web
# controller renders pods), asserts on the generated scripts, and executes
# them against a fake `podman` on PATH. No real containers, network calls,
# or Tailscale connections are made.
#
# Tailscale is mandatory: every pod gets its own sidecar + identity, HTTPS
# via `tailscale serve` whenever it has a port, no published host ports.
# Pass 1: sonarr from the catalog defaults (shared key file)
# Pass 2: :ro volume handling
# Pass 3: funnel render (AllowFunnel opt-in)
# Pass 4: spent/deleted key tolerated when Tailscale state exists
# Pass 5: log init never kills a deploy (invalid / read-only CWD regression)
# Pass 6: nzbget download paths land under the shared /data mount
#         (catalog config_set seeding, applied once per pod)
set -eu

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
TEST_KEY="dummy-test-authkey-1"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

pass() {
    echo "  ok: $1"
}

# The engine wants bash >= 4; macOS ships 3.2.
find_bash() {
    for b in bash /opt/homebrew/bin/bash /usr/local/bin/bash; do
        if command -v "$b" >/dev/null 2>&1; then
            v=$("$b" -c 'echo "${BASH_VERSINFO[0]}"')
            if [ "$v" -ge 4 ]; then
                command -v "$b"
                return 0
            fi
        fi
    done
    return 1
}

BASH_BIN=$(find_bash) || fail "bash >= 4 is required (brew install bash)"
command -v jq >/dev/null 2>&1 || fail "jq is required"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
HOME="$WORK/home"
export HOME
mkdir -p "$HOME/Pods"
cd "$WORK"

# --- Fake podman: logs every invocation, always succeeds. `ps` lists the ---
# --- names of containers previously started, so liveness checks pass.    ---
mkdir -p "$WORK/bin"
PODMAN_LOG="$WORK/podman.log"
export PODMAN_LOG
cat > "$WORK/bin/podman" << 'EOF'
#!/bin/sh
echo "podman $*" >> "${PODMAN_LOG:?}"
case "${1:-}" in
  ps)
    grep -o "run -d --name [^ ]*" "$PODMAN_LOG" 2>/dev/null | awk '{print $4}' | sort -u
    ;;
esac
exit 0
EOF
chmod +x "$WORK/bin/podman"
ln -s "$BASH_BIN" "$WORK/bin/bash"
PATH="$WORK/bin:$PATH"
export PATH

# The shared key file: created once, referenced by every render (the web
# controller writes this the same way on install).
printf '%s\n' "$TEST_KEY" > "$HOME/Pods/.tailscale_authkey"

# render <config-json>: pipe a config into create.sh, as the controller does.
render() {
    if ! printf '%s' "$1" | "$BASH_BIN" "$REPO_DIR/create.sh" \
            > "$WORK/create.log" 2>&1; then
        cat "$WORK/create.log" >&2
        fail "create.sh exited non-zero"
    fi
}

run_generated() {
    # $1: service dir
    if ! (cd "$1" && WAIT=0 sh ./run.sh > "$1/run-test.log" 2>&1 < /dev/null); then
        echo "--- run.sh output ---" >&2
        cat "$1/run-test.log" >&2
        fail "generated run.sh exited non-zero in $1"
    fi
    rm -f "$1/run-test.log"
}

# =========================================================================
echo "=== Pass 1: sonarr (catalog defaults, shared key file) ==="
SONARR=$(jq -c --arg home "$HOME" '
    .[] | select(.name == "sonarr") | {
        container: .name, image: .image, ports: .ports,
        environment: .environment,
        volumes: (.volumes | with_entries(.value = "\($home)/Pods/sonarr/data\(.key)")),
        network_mode: "bridge", restart_policy: "unless-stopped",
        auth_key_file: "\($home)/Pods/.tailscale_authkey",
        base_path: "\($home)/Pods", command: ""
    }' "$REPO_DIR/homelab.js")
render "$SONARR"

SVC_DIR="$HOME/Pods/sonarr"
for f in run.sh stop.sh remove.sh diagnose.sh; do
    [ -f "$SVC_DIR/$f" ] || fail "missing generated file: $SVC_DIR/$f"
done
pass "run.sh, stop.sh, remove.sh, diagnose.sh generated"

grep -q -- "--network container:tailscale-sonarr" "$SVC_DIR/run.sh" \
    || fail "run.sh does not share the Tailscale sidecar network namespace"
pass "run.sh uses --network container:tailscale-sonarr"

if grep -rq "$TEST_KEY" "$SVC_DIR"; then
    fail "auth key leaked into generated files"
fi
pass "auth key not embedded in any generated file"

grep -q "TS_AUTHKEY_FILE=" "$SVC_DIR/run.sh" || fail "run.sh does not read the auth key from a file"
grep -q "Pods/.tailscale_authkey" "$SVC_DIR/run.sh" || fail "run.sh does not reference the key file"
pass "run.sh reads the auth key from a file at runtime"

if grep -q '[^.]ts\.net' "$SVC_DIR/run.sh" && ! grep -q 'DNSName' "$SVC_DIR/run.sh"; then
    fail "run.sh fabricates a .ts.net FQDN instead of querying MagicDNS"
fi
pass "run.sh derives the FQDN from tailscale's DNSName"

grep -q "TS_SERVE_CONFIG" "$SVC_DIR/run.sh" || fail "HTTPS=yes but no TS_SERVE_CONFIG in run.sh"
grep -q '"Proxy": "http://127.0.0.1:8989"' "$SVC_DIR/run.sh" \
    || fail "serve config does not proxy to the service port"
pass "HTTPS via tailscale serve wired into the sidecar"

grep -q "TS_DEBUG_MTU=1280" "$SVC_DIR/run.sh" \
    || fail "sidecar MTU is not 1280 (IPv6 floor — Funnel breaks below it)"
pass "sidecar MTU is 1280 (IPv6/Funnel safe)"

if grep -qE -- "-p [0-9]+:[0-9]+" "$SVC_DIR/run.sh"; then
    fail "run.sh publishes host ports (should be tailnet-only, no -p)"
fi
pass "run.sh publishes no host ports"

grep -q '"funnel": "no"' "$SVC_DIR/.config.json" || fail "funnel not defaulted to no"
grep -q "AllowFunnel" "$SVC_DIR/run.sh" && fail "AllowFunnel present without funnel=yes"
pass "funnel defaults off; no AllowFunnel in a default render"

run_generated "$SVC_DIR"
pass "generated run.sh executes cleanly (stubbed podman, WAIT=0)"

grep -q "run -d --name tailscale-sonarr" "$PODMAN_LOG" || fail "tailscale sidecar was not started"
grep -q "run -d --name sonarr" "$PODMAN_LOG" || fail "service container was not started"
pass "run.sh started tailscale sidecar and service"

# =========================================================================
echo "=== Pass 2: :ro volume handling ==="
: > "$PODMAN_LOG"
render "{
  \"container\": \"rotest\", \"image\": \"docker.io/nginx:latest\",
  \"network_mode\": \"bridge\", \"ports\": {\"80\": \"80\"},
  \"restart_policy\": \"unless-stopped\",
  \"auth_key_file\": \"$HOME/Pods/.tailscale_authkey\",
  \"base_path\": \"$HOME/Pods\", \"environment\": {},
  \"volumes\": {\"/archive\": \"$HOME/media-archive:ro\"}, \"command\": \"\"
}"

SVC_DIR="$HOME/Pods/rotest"
grep -q -- "-v $HOME/media-archive:/archive:ro" "$SVC_DIR/run.sh" \
    || fail "run.sh does not mount the :ro volume as host:container:ro"
[ -d "$HOME/media-archive" ] || fail ":ro volume host directory was not created"
[ ! -d "$HOME/media-archive:ro" ] || fail ":ro suffix leaked into a directory name"
pass ":ro volume mounts read-only; suffix stripped from filesystem paths"

run_generated "$SVC_DIR"
pass "run.sh executes cleanly (stubbed podman, WAIT=0)"

# =========================================================================
echo "=== Pass 3: funnel render (AllowFunnel opt-in) ==="
render "{
  \"container\": \"funtest\", \"image\": \"docker.io/nginx:latest\",
  \"network_mode\": \"bridge\", \"ports\": {\"80\": \"80\"},
  \"restart_policy\": \"unless-stopped\",
  \"auth_key_file\": \"$HOME/Pods/.tailscale_authkey\",
  \"base_path\": \"$HOME/Pods\", \"environment\": {}, \"volumes\": {},
  \"command\": \"\", \"funnel\": \"yes\"
}"

SVC_DIR="$HOME/Pods/funtest"
grep -q '"AllowFunnel": {"${TS_CERT_DOMAIN}:443": true}' "$SVC_DIR/run.sh" \
    || fail "funnel=yes run.sh does not write AllowFunnel into the serve config"
grep -q '"funnel": "yes"' "$SVC_DIR/.config.json" \
    || fail "funnel choice not persisted in .config.json"
pass "funnel=yes renders AllowFunnel and persists the choice"

# =========================================================================
echo "=== Pass 4: spent/deleted key tolerated when state exists ==="
SVC_DIR="$HOME/Pods/sonarr"
grep -q "tailscaled.state" "$SVC_DIR/run.sh" \
    || fail "run.sh does not tolerate a spent/deleted single-use key (no state fallback)"
mkdir -p "$SVC_DIR/tailscale" && touch "$SVC_DIR/tailscale/tailscaled.state"
rm -f "$HOME/Pods/.tailscale_authkey"
run_generated "$SVC_DIR"
pass "run.sh still works after the single-use key file is deleted (state present)"

# =========================================================================
echo "=== Pass 5: log init never kills a deploy (bad-CWD regression) ==="
# Regression for the v0.5.x production failure: LOG_FILE defaulted to the
# relative ./.deployment.log and was touched at source time, so create.sh
# died with "touch: ./.deployment.log: No such file or directory" before
# any other output whenever the caller's CWD was invalid. A deploy must
# now survive any CWD, and the log must land in the service dir.
printf '%s\n' "$TEST_KEY" > "$HOME/Pods/.tailscale_authkey"
LOGCFG() {
    printf '{"container": "%s", "image": "docker.io/nginx:latest",
      "network_mode": "bridge", "ports": {"80": "80"},
      "restart_policy": "unless-stopped",
      "auth_key_file": "%s/Pods/.tailscale_authkey",
      "base_path": "%s/Pods", "environment": {}, "volumes": {},
      "command": ""}' "$1" "$HOME" "$HOME"
}

# 5a: read-only CWD (a /tmp surrogate the deploy may not write into)
RO_DIR="$WORK/read-only-cwd"
mkdir -p "$RO_DIR"
chmod 555 "$RO_DIR"
if ! (cd "$RO_DIR" && LOGCFG logtest-ro | "$BASH_BIN" "$REPO_DIR/create.sh" \
        > "$WORK/create-ro.log" 2>&1); then
    cat "$WORK/create-ro.log" >&2
    chmod 755 "$RO_DIR"
    fail "create.sh died with a read-only CWD (log-init regression)"
fi
chmod 755 "$RO_DIR"
[ -f "$HOME/Pods/logtest-ro/run.sh" ] || fail "read-only-CWD render produced no run.sh"
[ -f "$HOME/Pods/logtest-ro/.deployment.log" ] \
    || fail "deployment log did not land in the service dir"
pass "create.sh survives a read-only CWD; log lands in the service dir"

# 5b: deleted CWD (not every OS allows removing the CWD; skip if this one
# refuses — 5a already exercises the unwritable-CWD path)
GONE_DIR="$WORK/gone-cwd"
mkdir -p "$GONE_DIR"
if (cd "$GONE_DIR" && rmdir "$GONE_DIR" 2>/dev/null); then
    mkdir -p "$GONE_DIR"
    if ! (cd "$GONE_DIR" && rmdir "$GONE_DIR" \
            && LOGCFG logtest-gone | "$BASH_BIN" "$REPO_DIR/create.sh" \
            > "$WORK/create-gone.log" 2>&1); then
        cat "$WORK/create-gone.log" >&2
        fail "create.sh died with a deleted CWD (log-init regression)"
    fi
    [ -f "$HOME/Pods/logtest-gone/run.sh" ] || fail "deleted-CWD render produced no run.sh"
    pass "create.sh survives a deleted CWD"
else
    pass "deleted-CWD sub-test skipped (this OS refuses to rmdir the CWD)"
fi

# 5c: an absolute LOG_FILE from the environment wins (how the controller calls it)
PINNED="$WORK/pinned.log"
LOGCFG logtest-env | LOG_FILE="$PINNED" "$BASH_BIN" "$REPO_DIR/create.sh" \
    > "$WORK/create-env.log" 2>&1 || {
    cat "$WORK/create-env.log" >&2
    fail "create.sh failed with LOG_FILE pinned via env"
}
[ -f "$PINNED" ] || fail "env-pinned LOG_FILE was not written"
grep -q "Tailarr Deployment Log" "$PINNED" || fail "env-pinned log has no header"
pass "absolute LOG_FILE from the environment is honored"

# =========================================================================
echo "=== Pass 6: nzbget paths under the shared /data mount (config_set) ==="
# Regression for the Debian VM deployment where a fresh nzbget wrote
# completed downloads to the base image's default DestDir (/downloads/…,
# and /Downloads/… on older images) — a path mounted NOWHERE under the
# shared-/data layout, so the *arr apps could never import anything
# without a remote-path-mapping band-aid. The catalog must (a) mount the
# shared /data and (b) seed DestDir/InterDir under it, once per pod.

# 6a: catalog invariant — nzbget's seeded dirs live under a mounted path
NZB_VOLS=$(jq -r '.[] | select(.name == "nzbget") | .volumes | to_entries[].value' "$REPO_DIR/homelab.js")
echo "$NZB_VOLS" | grep -qx "/data" || fail "nzbget catalog entry no longer mounts /data"
for key in DestDir InterDir; do
    val=$(jq -r --arg k "$key" '.[] | select(.name == "nzbget") | .config_set[$k] // ""' "$REPO_DIR/homelab.js")
    case "$val" in
        /data/*) ;;
        *) fail "nzbget catalog $key ('$val') is not under the shared /data mount" ;;
    esac
done
pass "catalog seeds nzbget DestDir/InterDir under the mounted /data"

# 6b: render nzbget from the catalog (as the controller does) and check
# the one-time seeding block in run.sh
: > "$PODMAN_LOG"
printf '%s\n' "$TEST_KEY" > "$HOME/Pods/.tailscale_authkey"
NZBGET=$(jq -c --arg home "$HOME" '
    .[] | select(.name == "nzbget") | {
        container: .name, image: .image, ports: .ports,
        environment: .environment,
        volumes: (.volumes | with_entries(.value = "\($home)/Pods/nzbget/data\(.key)")),
        network_mode: "bridge", restart_policy: "unless-stopped",
        auth_key_file: "\($home)/Pods/.tailscale_authkey",
        base_path: "\($home)/Pods", command: "",
        config_file: .config_file, config_set: .config_set
    }' "$REPO_DIR/homelab.js")
render "$NZBGET"

SVC_DIR="$HOME/Pods/nzbget"
grep -q "sed -i 's|^DestDir=.*|DestDir=/data/downloads/completed|' /config/nzbget.conf" "$SVC_DIR/run.sh" \
    || fail "run.sh does not seed DestDir under /data"
grep -q "sed -i 's|^InterDir=.*|InterDir=/data/downloads/intermediate|' /config/nzbget.conf" "$SVC_DIR/run.sh" \
    || fail "run.sh does not seed InterDir under /data"
grep -q '.config-seeded' "$SVC_DIR/run.sh" \
    || fail "config seeding is not sentinel-gated (would stomp user edits on re-render)"
grep -q '"config_set"' "$SVC_DIR/.config.json" \
    || fail "config_set not persisted in .config.json (re-renders would drop it)"
pass "run.sh seeds nzbget paths under /data, gated by a one-time sentinel"

# 6c: seeding executes once, then never again (stubbed podman)
run_generated "$SVC_DIR"
grep -q "exec nzbget sed -i s|^DestDir=" "$PODMAN_LOG" \
    || fail "first run did not apply the DestDir seed"
grep -q "restart nzbget" "$PODMAN_LOG" || fail "seeding did not restart the service"
[ -f "$SVC_DIR/.config-seeded" ] || fail "sentinel not written after seeding"
: > "$PODMAN_LOG"
run_generated "$SVC_DIR"
if grep -q "sed -i" "$PODMAN_LOG"; then
    fail "second run re-applied the seed (user config edits would be stomped)"
fi
pass "config seeding applied exactly once; re-runs leave user edits alone"

echo ""
echo "SMOKE TEST PASSED"
