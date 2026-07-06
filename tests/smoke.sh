#!/bin/sh
# Smoke test for the Podscale engine.
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

echo ""
echo "SMOKE TEST PASSED"
