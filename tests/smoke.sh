#!/bin/sh
# Smoke test for HomePod Creator.
#
# Drives the interactive wizard end-to-end with canned answers and a fake
# `podman` on PATH, then asserts on the generated scripts and executes them.
# No real containers, network calls, or Tailscale connections are made.
#
# Pass 1: sonarr with NPM=yes, Tailscale=yes, shared key mode
# Pass 2: radarr with NPM=no,  Tailscale=no  (ports must be published via -p)
# Pass 3: lidarr with NPM=no,  Tailscale=yes, per-service key mode
set -eu

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
TEST_KEY="tskey-test-12345"
TEST_KEY2="tskey-test-67890"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

pass() {
    echo "  ok: $1"
}

# The wizard needs bash >= 4 (mapfile, declare -gA); macOS ships 3.2.
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
mkdir -p "$HOME"
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
# The wizard's deploy step invokes `bash`; make sure it resolves to bash >= 4.
ln -s "$BASH_BIN" "$WORK/bin/bash"
PATH="$WORK/bin:$PATH"
export PATH

# Emit N blank lines (accept defaults for env vars / volume prompts)
blanks() {
    i=0
    while [ "$i" -lt "$1" ]; do
        printf '\n'
        i=$((i+1))
    done
}

service_counts() {
    ENV_COUNT=$(jq -r --arg n "$1" '.[] | select(.name == $n).environment | length' "$REPO_DIR/homelab.js")
    VOL_COUNT=$(jq -r --arg n "$1" '.[] | select(.name == $n).volumes | length' "$REPO_DIR/homelab.js")
}

run_wizard() {
    # $1: log file; remaining input arrives on stdin
    if ! "$BASH_BIN" "$REPO_DIR/homelab-orchestrator.sh" > "$1" 2>&1; then
        echo "--- wizard output ($1) ---" >&2
        cat "$1" >&2
        fail "wizard exited non-zero"
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
echo "=== Pass 1: sonarr (NPM=yes, Tailscale=yes, shared key) ==="
service_counts sonarr
{
    printf '1\n'                 # select sonarr
    printf 'yes\n'               # NPM
    printf 'yes\n'               # Tailscale
    printf '\n'                  # HTTPS via tailscale serve (default yes)
    printf '\n'                  # base path (default)
    printf '1\n'                 # key mode: shared reusable key
    printf '%s\n' "$TEST_KEY"    # auth key (no key file exists yet)
    blanks "$ENV_COUNT"          # env var defaults
    blanks "$VOL_COUNT"          # volume defaults
    printf '\n'                  # no more volumes
    printf 'yes\n'               # confirm
    printf 'yes\nyes\n'          # slack for any re-prompt
} | run_wizard "$WORK/wizard-pass1.log"

SVC_DIR="$HOME/Pods/sonarr"
for f in run.sh stop.sh remove.sh diagnose.sh; do
    [ -f "$SVC_DIR/$f" ] || fail "missing generated file: $SVC_DIR/$f"
done
pass "run.sh, stop.sh, remove.sh, diagnose.sh generated"

grep -q -- "--network container:tailscale-sonarr" "$SVC_DIR/run.sh" \
    || fail "run.sh does not share the Tailscale sidecar network namespace"
pass "run.sh uses --network container:tailscale-sonarr"

[ -f "$HOME/Pods/.tailscale_authkey" ] || fail "wizard did not store the shared auth key file"
grep -q "$TEST_KEY" "$HOME/Pods/.tailscale_authkey" || fail "auth key file has wrong contents"
grep -q "shared" "$HOME/Pods/.tailscale_keymode" || fail "key mode choice was not persisted"
pass "shared key stored; key mode persisted"

if grep -rq "$TEST_KEY" "$SVC_DIR" "$HOME/Pods/.configs" "$WORK/.last-config.json"; then
    fail "auth key leaked into generated files or saved configs"
fi
pass "auth key not embedded in any generated file or saved config"

grep -q "TS_AUTHKEY_FILE=" "$SVC_DIR/run.sh" || fail "run.sh does not read the auth key from a file"
pass "run.sh reads the auth key from a file at runtime"

if grep -q '[^.]ts\.net' "$SVC_DIR/run.sh" && ! grep -q 'DNSName' "$SVC_DIR/run.sh"; then
    fail "run.sh fabricates a .ts.net FQDN instead of querying MagicDNS"
fi
pass "run.sh derives the FQDN from tailscale's DNSName"

grep -q "TS_SERVE_CONFIG" "$SVC_DIR/run.sh" || fail "HTTPS=yes but no TS_SERVE_CONFIG in run.sh"
grep -q '"Proxy": "http://127.0.0.1:8989"' "$SVC_DIR/run.sh" \
    || fail "serve config does not proxy to the service port"
pass "HTTPS via tailscale serve wired into the sidecar"

run_generated "$SVC_DIR"
pass "generated run.sh executes cleanly (stubbed podman, WAIT=0)"

grep -q "run -d --name tailscale-sonarr" "$PODMAN_LOG" || fail "tailscale sidecar was not started"
grep -q "run -d --name npm-sonarr" "$PODMAN_LOG" || fail "NPM container was not started"
grep -q "run -d --name sonarr" "$PODMAN_LOG" || fail "service container was not started"
pass "run.sh started tailscale sidecar, NPM, and service"

# =========================================================================
echo "=== Pass 2: radarr (NPM=no, Tailscale=no) ==="
: > "$PODMAN_LOG"
service_counts radarr
{
    printf '2\n'                 # select radarr
    printf 'no\n'                # NPM
    printf 'no\n'                # Tailscale (no key prompts follow)
    printf '\n'                  # base path (default)
    blanks "$ENV_COUNT"          # env var defaults
    blanks "$VOL_COUNT"          # volume defaults
    printf '\n'                  # no more volumes
    printf 'yes\n'               # confirm
    printf 'yes\nyes\n'          # slack for any re-prompt
} | run_wizard "$WORK/wizard-pass2.log"

SVC_DIR="$HOME/Pods/radarr"
[ -f "$SVC_DIR/run.sh" ] || fail "missing generated file: $SVC_DIR/run.sh"

grep -q -- "-p 7878:7878" "$SVC_DIR/run.sh" \
    || fail "no-Tailscale run.sh does not publish ports with -p"
pass "no-Tailscale run.sh publishes ports via -p"

if grep -q "podman exec tailscale" "$SVC_DIR/run.sh"; then
    fail "no-Tailscale run.sh still calls podman exec tailscale-*"
fi
if grep -q "tailscale" "$SVC_DIR/run.sh" ; then
    grep "tailscale" "$SVC_DIR/run.sh" | grep -vq "rm -f tailscale-radarr" \
        && fail "no-Tailscale run.sh references tailscale beyond cleanup"
fi
pass "no-Tailscale run.sh contains no tailscale sidecar usage"

run_generated "$SVC_DIR"
pass "no-Tailscale run.sh executes cleanly (stubbed podman, WAIT=0)"

grep -q "run -d --name radarr" "$PODMAN_LOG" || fail "radarr container was not started"
if grep -q "run -d --name tailscale-radarr" "$PODMAN_LOG"; then
    fail "a tailscale sidecar was started despite Tailscale=no"
fi
pass "only the service container was started"

# =========================================================================
echo "=== Pass 3: lidarr (NPM=no, Tailscale=yes, per-service key) ==="
: > "$PODMAN_LOG"
rm -f "$HOME/Pods/.tailscale_keymode"   # trigger the first-run question again
service_counts lidarr
{
    printf '3\n'                 # select lidarr
    printf 'no\n'                # NPM
    printf 'yes\n'               # Tailscale
    printf 'no\n'                # no HTTPS for this one
    printf '\n'                  # base path (default)
    printf '2\n'                 # key mode: fresh key per service
    printf '%s\n' "$TEST_KEY2"   # per-service auth key
    blanks "$ENV_COUNT"          # env var defaults
    blanks "$VOL_COUNT"          # volume defaults
    printf '\n'                  # no more volumes
    printf 'yes\n'               # confirm
    printf 'yes\nyes\n'          # slack for any re-prompt
} | run_wizard "$WORK/wizard-pass3.log"

SVC_DIR="$HOME/Pods/lidarr"
[ -f "$SVC_DIR/run.sh" ] || fail "missing generated file: $SVC_DIR/run.sh"

[ -f "$SVC_DIR/.tailscale_authkey" ] || fail "per-service key file was not created"
grep -q "$TEST_KEY2" "$SVC_DIR/.tailscale_authkey" || fail "per-service key file has wrong contents"
grep -q "lidarr/.tailscale_authkey" "$SVC_DIR/run.sh" || fail "run.sh does not reference the per-service key file"
pass "per-service key stored and referenced by run.sh"

grep -q "$TEST_KEY2" "$HOME/Pods/.tailscale_authkey" \
    && fail "per-service key leaked into the shared key file"
for f in run.sh stop.sh remove.sh diagnose.sh; do
    grep -q "$TEST_KEY2" "$SVC_DIR/$f" && fail "per-service key embedded in $f"
done
pass "per-service key not embedded in generated scripts or shared key file"

grep -q "tailscaled.state" "$SVC_DIR/run.sh" \
    || fail "run.sh does not tolerate a spent/deleted single-use key (no state fallback)"
pass "run.sh accepts existing Tailscale state in place of a key file"

run_generated "$SVC_DIR"
pass "per-service run.sh executes cleanly (stubbed podman, WAIT=0)"

# Spent-key scenario: delete the key file; state exists -> must still run
mkdir -p "$SVC_DIR/tailscale" && touch "$SVC_DIR/tailscale/tailscaled.state"
rm -f "$SVC_DIR/.tailscale_authkey"
run_generated "$SVC_DIR"
pass "run.sh still works after the single-use key file is deleted (state present)"

echo ""
echo "SMOKE TEST PASSED"
