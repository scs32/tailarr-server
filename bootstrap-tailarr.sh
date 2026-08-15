#!/usr/bin/env bash
# Bootstrap the Tailarr controller: a Tailscale sidecar plus the controller
# web UI sharing its network namespace - deployed exactly like any other pod.
#
# Usage:
#   TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... ./bootstrap-tailarr.sh
#       (a Tailscale OAuth client tagged tag:tailarr-ctrl with
#       auth_keys/devices/policy_file write scopes — see the README's
#       "Tailscale credential" section. The bootstrap initializes the
#       tailnet policy, mints its own auth key, and seeds the controller's
#       API credential: tagging, ACLs, and per-pod key minting all work
#       from first boot.)
#   ./bootstrap-tailarr.sh                               (reuse existing state)
#
# The web UI is then at http://<hostname>.<your-tailnet>.ts.net:8080
# (hostname defaults to "tailarr"). Override the tailnet hostname with
# TAILARR_HOSTNAME=... — it sets ONLY the sidecar's MagicDNS name (and thus
# the HTTPS cert identifier); the controller container is always named
# "tailarr". Useful to dodge a per-hostname Let's Encrypt cert rate limit
# across repeated rebuilds (5 certs / exact hostname / 168h).
set -euo pipefail

PODS_DIR="${PODS_DIR:-$HOME/Pods}"

# >>> tailarr:version-inputs >>>
# ⚠️ THIS USED TO BE THE WHOLE STORY, AND IT SILENTLY DOWNGRADED BOXES.
# `TAILARR_VERSION` was an unconditional assignment and was the only thing that
# decided which controller image this script brought up. A copy of this file
# lives at /root/bootstrap-tailarr.sh from install onward, and the controller's
# self-upgrade moves the CONTAINER forward without touching it — so every later
# run of this script (boot recovery, converge, rebuild, re-install) brought the
# box back on whatever version the FILE was written with. Measured on a live box
# 2026-08-14: the controller was on v0.211.0 while this script said 0.209.0, so a
# run would have rolled it back TWO releases. The drift was unbounded because
# nothing reconciled it, and the failure is silent in the worst way: the box
# comes up healthy, every pod green, just older. Worse still on a release that
# revved the storage appliance — the older controller carries the older pinned
# appliance digest, so it repins and RECREATES the appliance, rolling the storage
# plane back too.
#
# The version is now DERIVED from the box (see the version-resolution fence
# further down); these two lines are only its INPUTS.
#
# An exported TAILARR_VERSION is captured HERE, before the shipped fallback
# below overwrites it: the old unconditional assignment silently ate an
# operator's override, so `TAILARR_VERSION=... ./bootstrap-tailarr.sh` did
# nothing at all. It is now honoured.
TAILARR_VERSION_REQUESTED="${TAILARR_VERSION:-}"
# The controller release this script SHIPS WITH — the first-install fallback,
# and the floor below which the resolution will not go. Pinned (not :latest) so
# a fresh install right after a release can't catch a stale :latest manifest
# from GHCR. CI enforces that this matches web/app.py's VERSION; bump both when
# cutting a release. HOMEPOD_IMAGE still overrides everything.
#
# ⚠️ KEEP THIS AN UNINDENTED, LITERAL `TAILARR_VERSION="X.Y.Z"`, AND THE ONLY
# SUCH LINE IN THIS FILE. CI and the release workflow both read it with
# `sed -n 's/^TAILARR_VERSION="\(.*\)"$/\1/p'` and compare it to web/app.py's
# VERSION; a second matching line makes that comparison read two values and
# fail. Every reassignment in the resolution fence is indented for that reason.
TAILARR_VERSION="0.215.0"
TAILARR_VERSION_SHIPPED="$TAILARR_VERSION"
# <<< tailarr:version-inputs <<<
TS_IMAGE="docker.io/tailscale/tailscale:stable"
# The controller's PUBLIC entry point: what `tailscale serve` proxies :443 to,
# what this script probes over the shared netns loopback, and what the Go front
# listener binds once TAILARR_LISTENER=go. It is NOT necessarily the port the
# Python process itself binds — in `go` mode Python steps back to 8081 (see the
# ENV block in Containerfile). Every :8080 in this file must be this variable:
# a literal here would keep pointing serve at whatever number used to be right.
PUBLIC_PORT="${PUBLIC_PORT:-8080}"
SOCKET="/run/podman/podman.sock"
KEY_FILE="$PODS_DIR/tailarr/.tailscale_authkey"
TSAPI_FILE="$PODS_DIR/.tsapi.json"

command -v podman >/dev/null || { echo "Error: podman is required" >&2; exit 1; }

# Escape a value for embedding inside a JSON string literal. A Tailscale
# OAuth secret is normally safe (tskey-client-...), but a stray backslash or
# double-quote would otherwise produce a .tsapi.json the controller can't
# parse. Handles the two structural chars plus control whitespace that a
# raw value could carry; enough for the single-line string values we write.
json_escape() {
    local s=$1 out i ch
    s=${s//\\/\\\\}   # backslash first, so we don't double-escape below
    s=${s//\"/\\\"}
    s=${s//$'\b'/\\b}
    s=${s//$'\f'/\\f}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/\\r}
    s=${s//$'\t'/\\t}
    # Any control char still raw (U+0001-U+001F, minus the named ones above)
    # is illegal inside a JSON string — emit it as \u00XX. Vanishingly rare
    # for an OAuth secret, but keeps the output strictly valid. (Bash vars
    # can't hold a NUL, so U+0000 needn't be handled.)
    if [[ $s == *[$'\x01'-$'\x1f']* ]]; then
        out=""
        for (( i=0; i<${#s}; i++ )); do
            ch=${s:i:1}
            if [[ $ch == [$'\x01'-$'\x1f'] ]]; then
                printf -v out '%s\\u%04x' "$out" "'$ch"
            else
                out+=$ch
            fi
        done
        s=$out
    fi
    printf '%s' "$s"
}

# --- container network MTU (nested VMs, e.g. apple/container guests, have
# MTU 1280; containers defaulting to larger MTUs silently blackhole TLS).
# Must precede the credential block below: the API-credential path runs a
# one-shot container that talks TLS to api.tailscale.com. ---
iface=$(awk '$2=="00000000" {print $1; exit}' /proc/net/route 2>/dev/null || true)
host_mtu=$(cat "/sys/class/net/${iface:-eth0}/mtu" 2>/dev/null || echo 1500)
if [[ "$host_mtu" -lt 1500 ]] && ! grep -qs "network_cmd_options" /etc/containers/containers.conf 2>/dev/null; then
    echo "Host MTU is $host_mtu - matching container network MTU..."
    # If the file exists and its last byte is NOT a newline, our appended
    # section would fuse onto the final line (mangling it). Command
    # substitution strips a trailing newline, so a non-empty tail -c1 means
    # the file lacks one — separate it first.
    if [[ -s /etc/containers/containers.conf ]] \
       && [[ -n "$(tail -c1 /etc/containers/containers.conf 2>/dev/null)" ]]; then
        printf '\n' >> /etc/containers/containers.conf
    fi
    printf '[engine]\nnetwork_cmd_options=["mtu=%s"]\n' "$host_mtu" >> /etc/containers/containers.conf
fi

# --- host platform fact ----------------------------------------------------
# apple/container VMs boot with init=/sbin/vminitd on the kernel command
# line — and /proc/cmdline is VM-global, NOT namespaced, so it's readable
# from inside the container this bootstrap runs in. That's the reliable
# signal: /proc/1/comm is useless here (containers get their own PID
# namespace, so PID 1 is the container's own command — live-caught
# 2026-07-21), and /sys/class/dmi may not exist on arm64 at all. The
# platform fact drives the peer-relay offer and skips the systemd mounts
# drop-in helper; the controller re-checks (and corrects) it at startup.
platform=linux
grep -q 'init=/sbin/vminitd' /proc/cmdline 2>/dev/null && platform=apple-container
mkdir -p "$PODS_DIR"
(umask 077; printf '{\n  "platform": "%s",\n  "detected_at": %s,\n  "detected_by": "bootstrap-cmdline"\n}\n' \
    "$platform" "$(date +%s)" > "$PODS_DIR/.host.json")
chmod 600 "$PODS_DIR/.host.json"
echo "Host platform: $platform"

# --- contain podman's image/container store under the data root ------------
# By default podman keeps its multi-GB graphroot at /var/lib/containers, so
# `rm -rf $PODS_DIR` at uninstall does NOT reclaim it. Point graphroot inside
# the data root instead — but ONLY on a genuinely fresh install: relocating an
# EXISTING store would orphan its images. The switch every podman entrypoint
# checks is the presence of storage.conf; a KEY_FILE / tailscaled.state means
# the controller is already enrolled (so a re-bootstrap or an upgrade keeps
# its current store untouched — bootstrap isn't re-run on upgrade anyway).
# runroot stays on podman's default (tmpfs /run) so start-pods.sh's post-reboot
# runroot wipe still works, but it's written EXPLICITLY: with graphroot
# relocated, podman 4.x's CLI refuses to infer it ("runroot must be set"). The
# driver is set from the detected platform (overlay normally; vfs inside an
# apple/container guest, where nested overlay-on-overlay isn't supported) —
# explicit so podman stops warning "driver should be set" on every invocation.
STORAGE_CONF="$PODS_DIR/storage.conf"
# The containment MUST live at the canonical system path too. The controller
# drives podman through /run/podman/podman.sock, which on a systemd host is
# served by socket-activated podman.service — and systemd units do NOT inherit
# the CONTAINERS_STORAGE_CONF env below. If only the env-pointed conf existed,
# that service would read the DEFAULT (empty) store, so the controller couldn't
# even find its own container ("could not determine the controller image") and
# first-run setup would fail. /etc/containers/storage.conf is read by EVERY root
# podman — the socket service, bootstrap's own runs, and interactive sudo podman.
SYS_STORAGE_CONF="/etc/containers/storage.conf"
if [[ "$platform" == "apple-container" ]]; then STORAGE_DRIVER="vfs"; else STORAGE_DRIVER="overlay"; fi
if [[ ! -f "$STORAGE_CONF" && ! -f "$KEY_FILE" \
      && ! -f "$PODS_DIR/tailarr/tailscale/tailscaled.state" ]]; then
    mkdir -p "$PODS_DIR/storage"
    cat > "$STORAGE_CONF" <<STORAGEEOF
# Managed by Tailarr — contains podman's image/container store inside the data
# root so uninstall reclaims it with the rest of $PODS_DIR. runroot stays on
# tmpfs /run; driver + runroot are set explicitly (see bootstrap-tailarr.sh).
[storage]
driver = "$STORAGE_DRIVER"
runroot = "/run/containers/storage"
graphroot = "$PODS_DIR/storage"
STORAGEEOF
    echo "Containing podman storage under $PODS_DIR/storage (fresh install, driver=$STORAGE_DRIVER)."
fi
# Mirror the contained store to the system path so the podman API socket uses
# it. Only when Tailarr owns the containment ($PODS_DIR/storage.conf present)
# and the host has no pre-existing /etc/containers/storage.conf to respect.
if [[ -f "$STORAGE_CONF" && ! -e "$SYS_STORAGE_CONF" ]]; then
    mkdir -p /etc/containers
    cp "$STORAGE_CONF" "$SYS_STORAGE_CONF"
    # A podman.service already activated (e.g. before this conf existed) caches
    # the old store — re-read it. Best-effort: no-op without systemd (the
    # apple/container path serves the socket differently) and never fatal.
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop podman.service 2>/dev/null || true
        systemctl restart podman.socket 2>/dev/null || true
    fi
fi
if [[ -f "$STORAGE_CONF" ]]; then
    export CONTAINERS_STORAGE_CONF="$STORAGE_CONF"
fi

# >>> tailarr:version-resolution >>>
# WHICH controller version does this run bring up? DERIVED, never transcribed.
#
# ⚠️ PLACED HERE ON PURPOSE, NOT AT THE TOP OF THE FILE. CONTAINERS_STORAGE_CONF
# must already be exported above, or `podman inspect` reads the DEFAULT store,
# finds no controller, and this falls back to the shipped pin — which is exactly
# the silent downgrade the whole block exists to remove. It still lands before
# the first use of $IMAGE (the one-shot policy/mint container below).
#
# Precedence, strongest first:
#   HOMEPOD_IMAGE        a full image override; wins outright (unchanged)
#   TAILARR_VERSION=<v>  explicit operator intent, including a deliberate
#                        downgrade — the one lever that can move backwards.
#                        ⚠️ IT DOES NOT PERSIST. It applies to this run only, so
#                        a rollback below the shipped fallback is undone by the
#                        next plain run of this script (the monotonic floor
#                        below). A rollback that must stick needs the script
#                        itself replaced with that release's copy.
#   installed container   what this box actually runs: THE derived fact
#   shipped fallback      a first install; there is no controller to read yet
#
# ⚠️ THE INSTALLED VERSION AND THE SHIPPED FALLBACK ARE COMBINED BY TAKING THE
# NEWER OF THE TWO, which makes this path MONOTONIC: a re-run of this script can
# never move the controller backwards, whichever copy of the script it is.
#
# ⚠️ AND IT IS DELIBERATELY *NOT* "ALWAYS THE NEWEST RELEASE". Nothing here asks
# GitHub, or reads the release cache, anything. A box that was never upgraded
# keeps coming up on exactly what is installed. Chasing latest here would look
# like a fix — a reboot would stop downgrading — while silently auto-upgrading
# every install on every boot, which is a worse defect than the one being fixed.
#
# ⚠️⚠️ AND IT FAILS CLOSED, WHICH IS THE WHOLE DIFFICULTY. The first version of
# this block collapsed THREE different states into one empty string — "there is
# no controller" (a first install), "podman could not answer" (a locked store, a
# timeout) and "the controller is on a reference with no X.Y.Z in it" (a digest
# pin, a dev tag) — and used the shipped fallback for all three. Two of those are
# not first installs, and the script goes on to `podman rm -f tailarr` and launch
# the fallback: it would have REPRODUCED the exact downgrade this block exists to
# remove, on the day podman hiccuped. A fix whose failure mode is the original
# defect is not a fix, so each state is now distinguished and answered:
#
#   absent   -> the shipped fallback. This IS a first install.
#   present, X.Y.Z readable -> the newer of it and the shipped fallback.
#   present, NOT readable   -> re-launch THAT EXACT REFERENCE. A container on
#                              `...@sha256:...` or `localhost/tailarr:dev` is a
#                              deliberate pin; replacing it with a release tag is
#                              a downgrade even though no number moved.
#   unknown  -> REFUSE, loudly, with the override in the message. Continuing
#               would be a silent downgrade, and every podman call below would
#               have failed anyway — this only fails earlier and says why.

# The controller container's own image reference IS the installed fact, so there
# is no second copy of it to drift. Prints "<state>|<reference>" where state is
# absent | present | unknown; the reference is empty unless state is present.
#
# `podman ps -a` is what separates "definitely not there" from "cannot tell": a
# SUCCESSFUL listing that does not name the controller is proof of absence, and a
# FAILED listing is proof of nothing at all. Asking `podman inspect` alone cannot
# tell those apart — it exits non-zero for both.
controller_image_ref() {
    local names="" ref=""
    if ! names=$(podman ps -a --format '{{.Names}}' 2>/dev/null); then
        printf 'unknown|'
        return 0
    fi
    if ! grep -qx 'tailarr' <<<"$names"; then
        printf 'absent|'
        return 0
    fi
    ref=$(podman inspect tailarr --format '{{.ImageName}}' 2>/dev/null | head -1) \
        || ref=""
    if [[ -z "$ref" ]]; then
        # .ImageName is blank on some podman versions once the image has been
        # re-tagged; .Config.Image is the reference the container was created
        # from and is the same answer for our purposes.
        ref=$(podman inspect tailarr --format '{{.Config.Image}}' 2>/dev/null | head -1) \
            || ref=""
    fi
    # It is listed but neither field could be read: that is "cannot tell", NOT
    # "no controller". Falling back here is the fail-open above.
    if [[ -z "$ref" ]]; then
        printf 'unknown|'
        return 0
    fi
    printf 'present|%s' "$ref"
}

# "$1 >= $2", comparing X.Y.Z numerically. `sort -V` is deliberately not used:
# it is a GNU extension, and a missing one would silently take the else branch.
# `10#` forces base 10 so a zero-padded component is not read as octal.
_ver_ge() {
    local -a a b
    local i x y
    IFS=. read -r -a a <<<"$1"
    IFS=. read -r -a b <<<"$2"
    for i in 0 1 2; do
        x=$((10#${a[i]:-0})); y=$((10#${b[i]:-0}))
        if [[ "$x" -gt "$y" ]]; then return 0; fi
        if [[ "$x" -lt "$y" ]]; then return 1; fi
    done
    return 0
}

_ctl=$(controller_image_ref)
_ctl_state="${_ctl%%|*}"
_ctl_ref="${_ctl#*|}"
# Components are bounded to nine digits: bash arithmetic is fixed width, so an
# absurdly long component would WRAP and misorder the comparison above rather
# than being rejected. Nine digits is far past any real release and far short of
# the 64-bit ceiling.
_installed_version=""
if [[ "$_ctl_state" == "present" \
      && "$_ctl_ref" =~ :v?([0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9})$ ]]; then
    _installed_version="${BASH_REMATCH[1]}"
fi

# ⚠️ THE REPOSITORY IS PART OF WHAT IS INSTALLED, NOT A CONSTANT. A box deployed
# from a fork, a mirror or a private registry (HOMEPOD_IMAGE at install time)
# runs e.g. registry.example.com/fork/tailarr:v0.213.0, and rebuilding the image
# from a bare version would silently swap its bits for upstream's of the same
# number — or fail to pull at all. `op_controller_upgrade` already keeps the
# deployment's registry/repo and moves only the tag, so this must too, or a
# bootstrap re-run quietly undoes what every upgrade was careful to preserve.
#
# The repository of a reference, with any tag or digest removed. A digest is
# split FIRST: `repo@sha256:abc` has a ":" inside the digest, so splitting on the
# last ":" the way app.py does would yield "repo@sha256". A tag cannot contain
# "/", so if what follows the last ":" does, there was no tag (a registry port,
# e.g. `host:5000/team/tailarr`) and the whole reference is the repository.
_repo_of() {
    local ref="$1" repo tag
    # A digest is stripped FIRST, and then the tag split runs on what is LEFT —
    # not instead of it. `repo:v1.2.3@sha256:abc` is a legal reference carrying
    # BOTH, and returning early here left the tag on, producing `repo:v1.2.3` and
    # then the invalid `repo:v1.2.3:v1.2.4`. That one fails loudly at pull rather
    # than launching a wrong image, but a repository function that returns a
    # tagged reference is wrong on its face.
    ref="${ref%%@*}"
    repo="${ref%:*}"
    tag="${ref##*:}"
    if [[ -z "$repo" || "$repo" == "$ref" || "$tag" == */* ]]; then
        printf '%s' "$ref"
    else
        printf '%s' "$repo"
    fi
    return 0
}

# ⚠️ DERIVED FROM `present`, NOT FROM "we managed to parse a version out of it".
# Gating this on $_installed_version was a real hole: an installed
# `registry.example.com:5000/team/tailarr:dev` (or a digest ref, or an untagged
# one) parses to no version, so the repository was left at upstream — and then
# an explicit `TAILARR_VERSION=` override, the one path that does not relaunch
# the reference verbatim, redirected a private-registry box to ghcr.io. That is
# the same silent-substitution defect, reintroduced through the override.
REPO_BASE="ghcr.io/scs32/tailarr"
if [[ "$_ctl_state" == "present" ]]; then
    REPO_BASE="$(_repo_of "$_ctl_ref")"
fi

# ⚠️ A BOX THAT IS ENROLLED BUT SHOWS NO CONTROLLER IS USUALLY THE WRONG STORE.
# "absent" is trusted below because it is how a first install looks — but a
# tailnet key or tailscaled state means this box was already installed, so an
# empty container list almost always means podman is reading a different store
# (a re-run with a different PODS_DIR, or a lost $PODS_DIR/storage.conf) rather
# than that the controller is gone. Not fatal: rebuilding an enrolled box with a
# genuinely empty store is a legitimate flow. But it is the one remaining route
# back to the old downgrade, so it says so instead of passing in silence.
#
# `${KEY_FILE:-}` and not `$KEY_FILE`: this fence is extracted and executed on
# its own by tests/bootstrap-shell.sh, where the script's earlier definitions do
# not exist. An empty path simply fails the -f test, and the tailscaled.state
# check below is derived from PODS_DIR, which the fence is given.
if [[ "$_ctl_state" == "absent" ]] \
   && { [[ -n "${KEY_FILE:-}" && -f "${KEY_FILE:-}" ]] \
        || [[ -f "$PODS_DIR/tailarr/tailscale/tailscaled.state" ]]; }; then
    echo "WARNING: this box is already enrolled (tailnet state exists under" >&2
    echo "         $PODS_DIR) but podman lists no 'tailarr' container. If that" >&2
    echo "         is unexpected, podman is probably reading a different store" >&2
    echo "         than the one this install uses — check PODS_DIR (currently" >&2
    echo "         $PODS_DIR) and $PODS_DIR/storage.conf BEFORE continuing," >&2
    echo "         because this run will otherwise treat it as a first install." >&2
fi

RESOLVED_IMAGE=""
if [[ -n "$TAILARR_VERSION_REQUESTED" ]]; then
    TAILARR_VERSION="$TAILARR_VERSION_REQUESTED"
    VERSION_SOURCE="TAILARR_VERSION from the environment"
elif [[ -n "${HOMEPOD_IMAGE:-}" ]]; then
    # HOMEPOD_IMAGE decides the image outright, so nothing here can matter — and
    # refusing below over a podman that cannot answer would be absurd when the
    # answer was not going to be used.
    TAILARR_VERSION="$TAILARR_VERSION_SHIPPED"
    VERSION_SOURCE="HOMEPOD_IMAGE (the version is not consulted)"
elif [[ "$_ctl_state" == "unknown" ]]; then
    echo "Error: a controller container may be installed, but podman could not" >&2
    echo "       say which image it runs, so this script cannot tell whether" >&2
    echo "       continuing would DOWNGRADE it. Refusing rather than guessing." >&2
    echo "       Check 'podman ps -a' and 'podman inspect tailarr'; once podman" >&2
    echo "       answers, re-run." >&2
    echo "       To proceed anyway, READ the version the box is actually on and" >&2
    echo "       name THAT — from the web UI, or:" >&2
    echo "         podman inspect tailarr --format '{{.ImageName}}'" >&2
    echo "         TAILARR_VERSION=<that version> $0" >&2
    echo "       ⚠️ Do not guess, and do not use this script's own version: that" >&2
    echo "       is exactly the rollback this refusal exists to prevent." >&2
    echo "       ⚠️ If that reference is NOT on ghcr.io/scs32/tailarr — a fork, a" >&2
    echo "       mirror, a private registry — pass the WHOLE reference instead," >&2
    echo "       because with podman unable to answer, the repository cannot be" >&2
    echo "       derived either and TAILARR_VERSION alone would rebuild it on" >&2
    echo "       ghcr.io:" >&2
    echo "         HOMEPOD_IMAGE=<that full reference> $0" >&2
    exit 1
elif [[ "$_ctl_state" == "absent" ]]; then
    TAILARR_VERSION="$TAILARR_VERSION_SHIPPED"
    VERSION_SOURCE="this script (first install - no controller container)"
elif [[ -z "$_installed_version" ]]; then
    # Installed, but on a reference that carries no version: keep it exactly.
    RESOLVED_IMAGE="$_ctl_ref"
    TAILARR_VERSION="$TAILARR_VERSION_SHIPPED"
    VERSION_SOURCE="the installed controller's own image reference"
elif _ver_ge "$_installed_version" "$TAILARR_VERSION_SHIPPED"; then
    # Relaunch the reference VERBATIM rather than rebuilding it from the version
    # we just parsed out of it: a tag may be `0.213.1` with no `v`, and
    # reconstructing would name an image that does not exist.
    TAILARR_VERSION="$_installed_version"
    RESOLVED_IMAGE="$_ctl_ref"
    VERSION_SOURCE="the installed controller container"
else
    TAILARR_VERSION="$TAILARR_VERSION_SHIPPED"
    VERSION_SOURCE="this script (newer than the installed v$_installed_version)"
fi
IMAGE="${HOMEPOD_IMAGE:-${RESOLVED_IMAGE:-${REPO_BASE}:v${TAILARR_VERSION}}}"
echo "Controller image: $IMAGE (from $VERSION_SOURCE)"
# <<< tailarr:version-resolution <<<

# --- credential ------------------------------------------------------------
# A Tailscale OAuth client is THE install credential (Tailarr's model is a
# dedicated tailnet with a client tagged tag:tailarr-ctrl — README). It is
# saved as the controller's .tsapi.json and used to mint the controller's
# own auth key — after initializing the tailarr-managed policy fences,
# because Tailscale refuses to mint a key for a tag that isn't in
# tagOwners yet (policy-before-mint). Re-runs on an already-enrolled
# controller need no credential.
if [[ -n "${TS_API_CLIENT_ID:-}" && -n "${TS_API_CLIENT_SECRET:-}" ]]; then
    mkdir -p "$PODS_DIR"
    (umask 077; printf '{\n  "oauth_client_id": "%s",\n  "oauth_client_secret": "%s"\n}\n' \
        "$(json_escape "$TS_API_CLIENT_ID")" "$(json_escape "$TS_API_CLIENT_SECRET")" > "$TSAPI_FILE")
    chmod 600 "$TSAPI_FILE"
    echo "Saved Tailscale API credential (OAuth client) to $TSAPI_FILE"
fi

# Adopt the policy fences (and optionally mint the controller's auth key)
# by running the controller's own code in a one-shot container — the exact
# adopt/mint path the Settings wizard uses, not a shell reimplementation.
# Prints one JSON object: {"ok": bool, "error": str|null, "key": str}.
run_tsapi_bootstrap() {
    podman run --rm -i \
      -v "$PODS_DIR:$PODS_DIR" \
      -e PODS_DIR="$PODS_DIR" \
      -e MINT_KEY="${1:-}" \
      "$IMAGE" python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, "/app/web")
import app

def fail(msg):
    print(json.dumps({"ok": False, "error": msg, "key": ""}))
    sys.exit(0)

if not app._ts_token():
    fail("could not obtain an API access token - check the OAuth client "
         "id/secret (or token) and that the client has auth_keys, devices, "
         "and policy_file write scopes")
r = app.op_policy_init_fences()
if not r["ok"]:
    fail("policy adopt: %s" % r["error"])
out = {"ok": True, "error": None, "key": ""}
if os.environ.get("MINT_KEY") == "mint":
    m = app.ts_mint_pod_key("tailarr")
    if not m["ok"]:
        fail("key mint: %s" % m["error"])
    out["key"] = m["key"]
print(json.dumps(out))
PYEOF
}

json_field() {  # json_field <field> <<<"$json"
    sed -n 's/.*"'"$1"'": *"\([^"]*\)".*/\1/p'
}

adopted=""
if [[ ! -f "$KEY_FILE" && ! -f "$PODS_DIR/tailarr/tailscale/tailscaled.state" ]]; then
    if [[ -f "$TSAPI_FILE" ]]; then
        echo "Initializing tailnet policy and minting the controller's auth key..."
        out=$(run_tsapi_bootstrap mint) || true
        if ! grep -q '"ok": true' <<<"$out"; then
            err=$(json_field error <<<"$out")
            echo "Error: bootstrap via the OAuth client failed: ${err:-no response}" >&2
            echo "Check the client id/secret, its scopes (auth_keys, devices, policy_file — write)," >&2
            echo "its tag (tag:tailarr-ctrl), and the pasted tailnet policy (README)." >&2
            exit 1
        fi
        mkey=$(json_field key <<<"$out")
        [[ -n "$mkey" ]] || { echo "Error: minted key came back empty" >&2; exit 1; }
        mkdir -p "$(dirname "$KEY_FILE")"
        (umask 077; printf '%s\n' "$mkey" > "$KEY_FILE")
        chmod 600 "$KEY_FILE"
        echo "Policy fences in place; controller auth key minted (single-use, tag:tailarr)."
        adopted=1
    else
        echo "Error: no OAuth client given and no existing state." >&2
        echo "Usage: TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... $0" >&2
        echo "See the README's 'Tailscale credential' section to create the client." >&2
        exit 1
    fi
fi

# Credential provided this run but the mint path (which adopts as a side
# effect) didn't run — a re-bootstrap of an already-enrolled controller.
# Still make sure the policy fences exist (idempotent). Non-fatal: the
# Settings wizard can redo this any time.
if [[ -z "$adopted" && -n "${TS_API_CLIENT_ID:-}" ]]; then
    echo "Checking tailnet policy fences..."
    out=$(run_tsapi_bootstrap "") || true
    if ! grep -q '"ok": true' <<<"$out"; then
        echo "Warning: policy fence init failed: $(json_field error <<<"$out")" >&2
        echo "         (continuing - re-run from Settings once the controller is up)" >&2
    fi
fi

# --- podman API socket (the controller drives the host through it) ---
# /run may not be tmpfs in these guests, so a stale socket FILE can survive
# a VM restart while the service behind it is gone — probe the API, not the
# path (same rule as start-pods.sh below).
if ! podman --url "unix://$SOCKET" info >/dev/null 2>&1; then
    echo "Starting podman API socket..."
    rm -f "$SOCKET"
    mkdir -p "$(dirname "$SOCKET")"
    nohup podman system service --time=0 "unix://$SOCKET" \
        >/var/log/podman-api.log 2>&1 &
    sleep 2
    podman --url "unix://$SOCKET" info >/dev/null 2>&1 \
        || { echo "Error: could not start podman API socket" >&2; exit 1; }
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

# Use Tailarr's contained podman store when this install has one. Fresh
# installs get $PODS_DIR/storage.conf (see bootstrap-tailarr.sh); installs that
# predate storage containment don't, and keep podman's default store. Must
# precede every podman call below so the store is consistent with the service.
PODS_DIR="${PODS_DIR:-/root/Pods}"
if [ -f "$PODS_DIR/storage.conf" ]; then
  export CONTAINERS_STORAGE_CONF="$PODS_DIR/storage.conf"
fi

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

# Storage appliance FIRST, then its host NFS mounts, THEN the rest of the
# fleet. The /srv/<vol> shares that media pods bind-mount are served by the
# tailarr-storage pod itself, so the boot unit deliberately does NOT block on
# those mounts (that circular RequiresMountsFor was the cold-boot deadlock).
# Instead we order it here: bring the appliance up, drive each self-served
# srv-*.mount until it lands (retry — the appliance needs a moment to serve
# NFS), then start consumers so they see real media, not an empty mountpoint.
# Storage mounts that never came up: a space-wrapped list of their /srv/<vol>
# host paths. A consumer that bind-mounts one of these must NOT be started —
# it would bind the EMPTY mountpoint dir as its media (M-06). Empty on the
# happy path (every mount lands), so the gate below is normally a no-op.
failed_mounts=" "
STORAGE_POD=tailarr-storage
if podman ps -a --format '{{.Names}}' | grep -q "^${STORAGE_POD}$"; then
  podman start "$STORAGE_POD" >/dev/null 2>&1 \
    || { sleep 3; podman start "$STORAGE_POD" >/dev/null 2>&1; } || true
  if command -v systemctl >/dev/null 2>&1; then
    for unit_path in /etc/systemd/system/srv-*.mount; do
      [ -e "$unit_path" ] || continue      # glob didn't match -> no storage mounts
      # Only OUR storage mounts (op_storage_mount writes this Description=), never
      # an admin's unrelated srv-*.mount.
      grep -q 'Tailarr Storage volume' "$unit_path" || continue
      unit=$(basename "$unit_path")
      # --no-block: a failing NFS mount runs mount.nfs foreground up to the
      # unit's 90s job timeout; a SYNCHRONOUS `systemctl start` would join that
      # job and stall boot minutes per dead mount. Kick it non-blocking, then
      # poll is-active (bounded 30x2s=60s). The appliance needs a moment to
      # serve, so retry the kick each pass in case the first attempt raced it.
      i=0
      while [ "$i" -lt 30 ]; do
        systemctl is-active --quiet "$unit" && break
        systemctl start --no-block "$unit" >/dev/null 2>&1 || true
        sleep 2; i=$((i + 1))
      done
      if ! systemctl is-active --quiet "$unit"; then
        # Read the exact mountpoint from Where= (never reconstruct — systemd
        # escaping makes srv-<vol>.mount -> /srv/<vol> non-trivial for some
        # names). Record it so we skip its consumers below.
        mp=$(sed -n 's/^Where=//p' "$unit_path" | head -1)
        [ -n "$mp" ] && failed_mounts="${failed_mounts}${mp} "
        echo "WARNING: $unit did not mount in 60s; its consumers will be held" \
             "back (empty-media guard). Restart them once the appliance serves." >&2
      fi
    done
  fi
fi

# Then the rest of the fleet. On the happy path every srv-*.mount landed above,
# so consumers see real media. If a mount never came up (a genuinely broken
# appliance), we SURGICALLY skip only the pods that bind that failed /srv path —
# starting them would bind an EMPTY dir as media (M-06). Pods that touch no
# storage, and pods whose mounts DID land, start normally, so one dead mount
# never leaves the whole box dark. (start-pods.sh maps pods->mounts via
# `podman inspect`, since the controller registry isn't available at this layer.)
for c in $(podman ps -a --format "{{.Names}}" | grep -v "^tailscale-"); do
  [ "$c" = "$STORAGE_POD" ] && continue    # already started above
  if [ "$failed_mounts" != " " ]; then
    skip=
    for src in $(podman inspect --format '{{range .Mounts}}{{.Source}} {{end}}' "$c" 2>/dev/null); do
      case "$failed_mounts" in *" $src "*) skip=1; break ;; esac
    done
    [ -n "$skip" ] && { echo "SKIP $c: a storage mount it depends on is not up" >&2; continue; }
  fi
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
# UNQUOTED heredoc so $PUBLIC_PORT expands — but ${TS_CERT_DOMAIN} must reach
# the file LITERALLY (tailscaled substitutes it at serve time from the node's
# cert domain), hence the backslash. Getting this backwards writes an empty
# host key and serve answers nothing on 443.
cat > "$PODS_DIR/tailarr/tailscale-serve.json" << SERVEEOF
{
  "TCP": {"443": {"HTTPS": true}},
  "Web": {
    "\${TS_CERT_DOMAIN}:443": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:$PUBLIC_PORT"}}
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
  -e TS_AUTH_ONCE=true \
  -e TS_STATE_DIR=/var/lib/tailscale \
  -e TS_USERSPACE=false \
  -e TS_DEBUG_MTU=1280 \
  -e TS_HOSTNAME="${TAILARR_HOSTNAME:-tailarr}" \
  "$TS_IMAGE"

sleep "${WAIT:-10}"
if ! podman ps --format '{{.Names}}' | grep -q '^tailscale-tailarr$'; then
    echo "Error: Tailscale sidecar failed to start. Recent logs:" >&2
    podman logs --tail 20 tailscale-tailarr >&2 || true
    exit 1
fi

echo "Starting Tailarr controller..."
# Go identity resolver wiring (flag-gated; default python = unchanged behavior).
# Mount the gate sidecar's ts-sock DIRECTORY at /run/gate so the live
# tailscaled socket appears inside the controller at /run/gate/tailscaled.sock —
# app.py's DEFAULT TAILARR_GATE_SOCK — so `tailarr-ctl -resolve` can reach it
# with no extra env.
#
# We bind-mount the DIR, not the socket FILE, for boot-order + rebind robustness:
#   (1) Cold reboot: start-pods.sh may launch the controller BEFORE the gate's
#       tailscaled has (re)created its socket. A socket-FILE mount would then find
#       nothing to bind (the old [ -S ] guard fails) and go-mode would be broken
#       until a manual relaunch. A DIR mount is a live view of the host dir, so
#       the socket shows up inside the container the instant tailscaled creates it.
#   (2) Gate restart (e.g. a converge): tailscaled recreates the socket as a NEW
#       inode, so a socket-FILE mount goes stale and the controller needs a
#       relaunch to rebind. A DIR mount tracks the current inode with no rebind.
#
# GUARDED on the ts-sock DIR existing: a gateway-less box must still start
# (go-mode then fails closed rather than being wired at all; see the _go_resolve
# S6 caveat). The ts-sock dir is created by the gate pod and PERSISTS across
# reboots once the gate has started once, so [ -d ] holds on every subsequent
# boot even before tailscaled has recreated the socket. Empty array => the run is
# byte-for-byte the old one.
GATE_SOCK_DIR="$PODS_DIR/tailarr-gate/ts-sock"
GATE_SOCK_MOUNT=()
if [ -d "$GATE_SOCK_DIR" ]; then
  GATE_SOCK_MOUNT=(-v "$GATE_SOCK_DIR:/run/gate")
fi
# /run/libpod is mounted so run.sh's IPAM-staging mkdir (see the sidecar
# notes above) lands on the HOST when the controller drives pod starts.
# The TAILARR_IDENTITY_RESOLVER[_<FLOW>] flags pass through from the host
# environment; unset => python-safe defaults, so an unset environment keeps the
# current behavior EXACTLY (global 'python'; per-flow empty falls back to it).
#
# The reconciler-driver flags (TAILARR_DRIVER and TAILARR_DRIVER_<KIND>) pass
# through the SAME way, but are collected generically rather than listed: the
# kind roster grows, and a hand-maintained list of -e lines here would silently
# drop any new kind's flag — the flag would appear to be set on the host and
# have no effect in the container. Unset => the controller's own 'python'
# default (_reconcile_driver_mode), so an empty array leaves the run
# byte-for-byte as it was before this block existed.
#
# This exists so the flip state lives in CODE + a host env file, not in a
# hand-edited relaunch script. A hand-edit does not survive a box rebuild —
# that is exactly how the 2026-08-02 clean-slate lost the entire flip.
#
# The host env files are the durable half of that, and they are sourced HERE
# rather than being something an operator has to remember to `export` before
# every run. install.sh re-runs this script from a fresh checkout, so anything
# typed on a one-off command line is gone by the next rebuild; a root-owned
# file is not. `set -a` is required — the collector below reads `env`, which
# only sees EXPORTED names, so a plain `TAILARR_LISTENER=go` line in the file
# would otherwise be sourced, be invisible to the collector, and produce
# exactly the silent lost flip these files exist to prevent. A real host env
# already set on the command line still wins, so a one-off override works.
for _envf in /root/tailarr-drivers.env /root/tailarr-listener.env; do
  if [ -r "$_envf" ]; then
    echo "Loading flip state from $_envf"
    set -a; . "$_envf"; set +a
  fi
done

# TAILARR_LISTENER / TAILARR_LISTENER_* / TAILARR_GO_ROUTES ride the SAME
# generic collector as the driver flags, for the same reason: the listener's
# rollback levels are env (kill switch) and a file, and a hand-maintained list
# of -e lines here would silently drop a new one.
#
# ⚠️ This stays an ALLOWLIST of exact prefixes, and must never become a blanket
# TAILARR_*. Container env is argv-visible to every process on the host (podman
# inspect, /proc), so a blanket match would ship every TAILARR_-prefixed secret
# the operator happens to have exported into a place secrets do not belong.
DRIVER_ENV=()
while IFS='=' read -r _dk _dv; do
  case "$_dk" in
    TAILARR_DRIVER|TAILARR_DRIVER_*|TAILARR_LISTENER|TAILARR_LISTENER_*|TAILARR_GO_ROUTES)
      DRIVER_ENV+=(-e "$_dk=$_dv") ;;
  esac
done < <(env)

podman run -d \
  --name tailarr \
  --network container:tailscale-tailarr \
  -v "$PODS_DIR:$PODS_DIR" \
  -v "$SOCKET:$SOCKET" \
  -v /run/libpod:/run/libpod \
  ${GATE_SOCK_MOUNT[@]+"${GATE_SOCK_MOUNT[@]}"} \
  -e CONTAINER_HOST="unix://$SOCKET" \
  -e PODS_DIR="$PODS_DIR" \
  -e TAILARR_IDENTITY_RESOLVER="${TAILARR_IDENTITY_RESOLVER:-python}" \
  -e TAILARR_IDENTITY_RESOLVER_PAIR="${TAILARR_IDENTITY_RESOLVER_PAIR:-}" \
  -e TAILARR_IDENTITY_RESOLVER_GATEWAY="${TAILARR_IDENTITY_RESOLVER_GATEWAY:-}" \
  -e TAILARR_IDENTITY_RESOLVER_MCP="${TAILARR_IDENTITY_RESOLVER_MCP:-}" \
  ${DRIVER_ENV[@]+"${DRIVER_ENV[@]}"} \
  --restart unless-stopped \
  "$IMAGE"

sleep 3
FQDN=$(podman exec tailscale-tailarr tailscale status --json --peers=false 2>/dev/null \
    | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
echo ""
echo "Tailarr controller is up."
echo "  Web UI: https://${FQDN%.}  (or http://${FQDN%.}:$PUBLIC_PORT)"

# Pre-warm the HTTPS cert. tailscale serve fetches its Let's Encrypt cert
# lazily on the FIRST TLS handshake, so an unprimed first visit eats the ACME
# mint (seconds) and can look broken. tailscale cert provisions into the same
# store serve reads from — do it now so the first browser hit is instant.
# Best-effort + bounded: if HTTPS Certificates aren't enabled on the tailnet
# this fails quietly (the cert would fail on first visit today too).
if [ -n "$FQDN" ]; then
    echo "  Priming the HTTPS certificate (first-visit warm-up)..."
    timeout 45 podman exec tailscale-tailarr tailscale cert \
        --cert-file /tmp/tailarr.crt --key-file /tmp/tailarr.key \
        "${FQDN%.}" >/dev/null 2>&1 || true
fi

# The controller mints an admin token on first start (required for exec /
# install / Tailscale-credential changes) and writes it to
# $PODS_DIR/.admin-token. Surface it right here in the install output so a
# fresh install doesn't have to hunt for the file — you're already at the host
# as root. Absent on a re-bootstrap of an already-provisioned install.
ADMIN_TOKEN_FILE="$PODS_DIR/.admin-token"
for _ in $(seq 1 20); do
    [ -f "$ADMIN_TOKEN_FILE" ] && break
    sleep 1
done
if [ -f "$ADMIN_TOKEN_FILE" ]; then
    admin_tok=$(grep -o 'tailarr-admin-[A-Za-z0-9_-]*' "$ADMIN_TOKEN_FILE" | head -1 || true)
    if [ -n "$admin_tok" ]; then
        echo ""
        echo "================ ADMIN TOKEN — save this now ================"
        echo "  $admin_tok"
        echo ""
        echo "  Paste it into the web UI at Settings -> Admin access to allow"
        echo "  exec / install / Tailscale-credential changes."
        echo "  (Also at $ADMIN_TOKEN_FILE — delete that file once saved.)"
        echo "============================================================"
    fi
fi

# --- First-run owner ----------------------------------------------------------
# The named-owner feature (app.py:2099, v0.141.0) exists so "the initial identity
# is a person from the start, not an anonymous token" — but its only trigger was a
# web-UI prompt. An install nobody logs into therefore sits INDEFINITELY in exactly
# the bare-bootstrap-token state the feature was built to eliminate. Observed on the
# 2026-08-02 clean-slate box: five days owner-less, `people: []`, `named: false`.
#
# install.sh is already interactive (it prompts for the Tailscale credential), so
# claim the owner here, where the controller is up and the admin token is in hand.
# Non-interactive callers pass TAILARR_OWNER_NAME. Every failure path is non-fatal:
# an unclaimed install is the status quo, and the web UI prompt still works.
#
# Scoped to a genuinely unowned install — op_owner_claim refuses once any
# server-badge person exists, and we check first so a re-bootstrap says nothing.
_owner_api() {
    # $1 = json body ("" => GET). Uses python3 in the controller image (guaranteed
    # present; the image has no curl) over the shared netns loopback.
    # _PORT is PUBLIC_PORT, not PORT: this dials the controller's entry point
    # from outside the Python process, so once the Go listener is flipped on it
    # must land on Go (which proxies), never on Python's internal 8081.
    podman exec -e _TOK="$admin_tok" -e _BODY="$1" -e _PORT="$PUBLIC_PORT" \
        tailarr python3 -c '
import json, os, urllib.request, urllib.error
body = os.environ["_BODY"] or None
req = urllib.request.Request(
    "http://127.0.0.1:%s/api/owner" % os.environ["_PORT"],
    data=body.encode() if body else None,
    headers={"Authorization": "Bearer " + os.environ["_TOK"],
             "Content-Type": "application/json"})
try:
    print(json.dumps(json.load(urllib.request.urlopen(req, timeout=15))))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
' 2>/dev/null
}

if [ -n "${admin_tok:-}" ]; then
    _owner_state=$(_owner_api "" || true)
    case "$_owner_state" in
        *'"named": false'*|*'"named":false'*)
            _owner_name="${TAILARR_OWNER_NAME:-}"
            if [ -z "$_owner_name" ] && [ -t 0 ]; then
                echo ""
                echo "Who owns this server? This names the first person (you), and"
                echo "attributes the admin token above to them. You can rename later."
                printf "  Your name: "
                read -r _owner_name || _owner_name=""
            fi
            if [ -n "$_owner_name" ]; then
                _claim=$(_owner_api "$(printf '{"do":"claim","name":"%s"}' \
                    "$(printf '%s' "$_owner_name" | sed 's/[\\"]/\\&/g')")" || true)
                case "$_claim" in
                    *'"ok": true'*|*'"ok":true'*)
                        echo "  [OK] Owner claimed: $_owner_name" ;;
                    *)  echo "  [WARN] Couldn't claim the owner automatically."
                        echo "         Name yourself in the web UI — nothing else is affected." ;;
                esac
            else
                echo "  (No name given — the web UI will prompt you on first visit.)"
            fi
            ;;
    esac
fi

# --- B15: gate netns self-heal -------------------------------------------------
# The self-config gateway is a netns owner/joiner PAIR: the serve sidecar
# `tailscale-tailarr-gate` OWNS the network namespace; the backend
# `tailarr-gate` JOINS it via `--network container:tailscale-tailarr-gate`.
#
# If the sidecar is ever restarted IN PLACE without also restarting the backend
# (e.g. applying a serve-config change by restarting the sidecar so containerboot
# re-reads tailscale-serve.json), podman DESTROYS+RECREATES the namespace and the
# backend is stranded on the now-dead netns. `tailscale serve https://tailarr-gate/
# -> http://127.0.0.1:80` then proxies to nothing -> 502 / connection-refused for
# EVERY device hitting /self/services -> device service-discovery and pairing are
# down until someone re-joins the backend. Root-caused live as B15; the live fix
# was a bare `podman restart tailarr-gate`.
#
# This is that fix as code: a cheap, deterministic, idempotent heal that runs on
# every bootstrap/converge (and is safe to run anytime). It asserts the backend
# and sidecar share a network namespace and, if they DON'T, restarts the backend
# to re-join. It is a strict no-op when the pair is already healthy, and it does
# nothing at all when the gate pod isn't deployed (fresh boxes have no gate until
# ntfy setup deploys it). This is a SAFETY NET, not the structural fix — the
# structural fix (stop restarting the sidecar to apply serve-config; use the live
# `tailscale serve --bg` CLI path `op_network_set` already uses for funnel) is
# banked internally.
heal_gate_netns() {
    local backend="tailarr-gate" sidecar="tailscale-tailarr-gate"

    # Guard: only act when BOTH halves of the pair are actually running. A box
    # with no gate deployed (fresh install) or a deliberately-stopped gate is
    # left completely alone.
    local running
    running=$(podman ps --format '{{.Names}}' 2>/dev/null || true)
    case " $running " in *" $backend "*) : ;; *) return 0 ;; esac
    case " $running " in *" $sidecar "*) : ;; *) return 0 ;; esac

    # Compare the two containers' net namespaces by inode. readlink of
    # /proc/<pid>/ns/net yields `net:[<inode>]`; an equal inode == a shared
    # namespace (the healthy joined state). We read each container's main PID
    # from podman inspect .State.Pid.
    local bpid spid bns sns
    bpid=$(podman inspect --format '{{.State.Pid}}' "$backend" 2>/dev/null || true)
    spid=$(podman inspect --format '{{.State.Pid}}' "$sidecar" 2>/dev/null || true)
    # A zero/blank PID means we can't observe a live process for one half; don't
    # thrash on an unreadable state — leave it for the next converge.
    if [ -z "$bpid" ] || [ -z "$spid" ] || [ "$bpid" = "0" ] || [ "$spid" = "0" ]; then
        return 0
    fi
    bns=$(readlink "/proc/$bpid/ns/net" 2>/dev/null || true)
    sns=$(readlink "/proc/$spid/ns/net" 2>/dev/null || true)

    # Healthy: both readable and equal -> shared namespace -> no-op.
    if [ -n "$bns" ] && [ "$bns" = "$sns" ]; then
        return 0
    fi

    # Wedged: the backend is on a different (or unreadable) namespace than the
    # sidecar it's supposed to be joining. Re-join by restarting the backend.
    echo "gate self-heal: backend '$backend' netns [${bns:-unreadable}] != sidecar" \
         "'$sidecar' netns [${sns:-unreadable}] — re-joining (B15)..."
    if podman restart "$backend" >/dev/null 2>&1; then
        echo "gate self-heal: restarted '$backend' to re-join the sidecar netns."
    else
        echo "WARNING: gate self-heal restart of '$backend' failed —" \
             "check 'podman logs $backend' (B15)." >&2
    fi
}

heal_gate_netns
