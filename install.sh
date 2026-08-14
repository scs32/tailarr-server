#!/bin/bash
set -euo pipefail

echo "[INFO] Installing and launching Tailarr..."

# The engine scripts below are only needed to bootstrap the controller — the
# published image already carries its own copies at /app, and nothing on the
# host reads them after install. Stage them in a throwaway temp dir (never the
# invocation directory or $HOME, which is how earlier installs littered
# scripts across people's home folders) and delete it on exit, so a finished
# install leaves nothing behind but $PODS_DIR and the boot units.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/tailarr-install.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"
REPO_BASE_URL="https://raw.githubusercontent.com/scs32/tailarr-server/main"

# The whole install must run as root, not just the package steps: the
# bootstrap manages the SYSTEM podman API socket (/run/podman, root-owned
# — sudo-ing apt alone gets you a Permission denied there minutes in),
# writes /root/start-pods.sh, and installs systemd boot units. We don't
# sudo-elevate ourselves; a clean re-run as root is simpler.
if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERROR] Tailarr must be installed as root (the controller drives the system podman socket and installs boot units)." >&2
    echo "        Re-run the whole command under sudo:" >&2
    echo "          sudo bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\"" >&2
    echo "        (plain sudo, NOT 'sudo -i' — -i re-quotes the inline script and silently runs nothing)" >&2
    exit 1
fi

# --- Preflight: fail on anything checkable BEFORE touching the system -------
# The principle: every requirement that can be verified up front, is. Nothing
# below installs or modifies anything; a failure here leaves the host clean.
fail() { echo "[ERROR] $*" >&2; exit 1; }
warn() { echo "[WARN] $*"; }

# --- The Tailscale prep the installer can't do for you --------------------
# Three things must exist in the Tailscale admin console before Tailarr can
# install: the access policy (a.k.a. the ACL / policy file — the thing
# everyone loses), MagicDNS + HTTPS Certificates, and the OAuth client. The
# admin URLs and a printer for the canonical policy live here so BOTH the
# up-front walkthrough and the later preflight failures can point at (and
# hand you) exactly the right thing.
ADMIN_ACLS="https://login.tailscale.com/admin/acls"
ADMIN_DNS="https://login.tailscale.com/admin/dns"
ADMIN_OAUTH="https://login.tailscale.com/admin/settings/oauth"
# The starting policy has ONE source of truth — docs/tailnet-policy.hujson in
# the repo. Fetch + print it so nobody has to hunt for "the ACL"; fall back to
# the web copy if the fetch fails (egress to raw.githubusercontent.com is a
# preflight requirement, so this normally succeeds).
POLICY_URL="$REPO_BASE_URL/docs/tailnet-policy.hujson"
POLICY_WEB_URL="https://github.com/scs32/tailarr-server/blob/main/docs/tailnet-policy.hujson"
print_policy() {
    local body rule="------------------------------------------------------------------------"
    if body="$(curl -fsSL --max-time 15 "$POLICY_URL" 2>/dev/null)" && [[ -n "$body" ]]; then
        printf '%s\n%s\n%s\n' "$rule" "$body" "$rule"
    else
        echo "  (couldn't fetch it just now — copy it from $POLICY_WEB_URL)"
    fi
}

# A cold first-time install needs the three admin-console steps done FIRST.
# Rather than let the one-liner blow past them and fail a preflight ten
# seconds later, walk through them interactively, printing the policy inline
# and pausing after each step. Re-runners skip this (see the prompt below).
guided_tailscale_setup() {
    cat <<EOF

==================== Tailscale setup (one time) ====================
Tailarr runs on its own dedicated Tailscale tailnet. Three quick things in
the Tailscale admin console, then you'll paste one credential and we install.

-- Step 1 of 3: paste the access policy --------------------------------------
   Open:  $ADMIN_ACLS
   Replace the ENTIRE file with the policy below. THIS is "the ACL" — the one
   thing people always go looking for. Canonical copy:
   $POLICY_WEB_URL
EOF
    echo ""
    print_policy
    echo ""
    read -rp "   Press Enter once you've saved the policy... " _
    cat <<EOF

-- Step 2 of 3: turn on MagicDNS + HTTPS -------------------------------------
   Open:  $ADMIN_DNS
     - Enable MagicDNS           (gives every pod a ts.net hostname)
     - Enable HTTPS Certificates (pod HTTPS — the ONE thing the installer
                                  cannot switch on for you)
EOF
    read -rp "   Press Enter once both are on... " _
    cat <<EOF

-- Step 3 of 3: create the OAuth client --------------------------------------
   Open:  $ADMIN_OAUTH
     - Generate an OAuth client tagged  tag:tailarr-ctrl
     - Give it WRITE access to:  Auth Keys, Devices, Policy File
   Copy the client ID and secret — you'll paste them in a moment.
EOF
    read -rp "   Press Enter to continue to the credential prompt... " _
    echo ""
}

echo "[CHECK] Host preflight..."

# OS: the auto-install path is Debian-family only. A host that already has
# podman gets a pass regardless of distro.
if [[ ! -f /etc/debian_version ]] && ! command -v podman >/dev/null 2>&1; then
    fail "Unsupported OS: not Debian-family and podman is not installed. Install podman manually, then re-run."
fi

# Architecture: the controller image is published for amd64 + arm64 only.
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|aarch64|arm64) : ;;
    *) fail "Unsupported architecture '$ARCH' — the Tailarr controller image ships for amd64 and arm64 only." ;;
esac

# Disk: podman's image/container storage lives under /var/lib/containers.
# The controller + tailscale images alone want a few GB; pods want more.
DISK_AVAIL_KB="$(df -Pk /var/lib 2>/dev/null | awk 'NR==2 {print $4}')"
if [[ -n "$DISK_AVAIL_KB" ]]; then
    if (( DISK_AVAIL_KB < 3 * 1024 * 1024 )); then
        fail "Only $(( DISK_AVAIL_KB / 1024 / 1024 ))GB free on the filesystem holding /var/lib (podman storage). Tailarr needs at least 3GB free to install, and pods will want more."
    elif (( DISK_AVAIL_KB < 10 * 1024 * 1024 )); then
        warn "Only $(( DISK_AVAIL_KB / 1024 / 1024 ))GB free on the filesystem holding /var/lib. The install fits, but service images and downloads fill disks fast — consider growing it."
    fi
fi

# Memory: the controller + sidecars run fine small, but sub-1GB hosts swap
# themselves to death once a couple of services join.
MEM_TOTAL_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)"
if [[ -n "$MEM_TOTAL_KB" ]] && (( MEM_TOTAL_KB < 1000000 )); then
    warn "This host has less than 1GB of RAM. The controller will run, but real services will struggle."
fi

# Boot persistence: without systemd the bootstrap skips the boot unit — pods
# must be started after a host reboot by whatever supervises this environment.
if [[ ! -d /run/systemd/system ]]; then
    warn "No systemd detected: pods will not auto-start at boot (run /root/start-pods.sh after a restart). Inside an apple/container guest this is expected."
fi

# Egress: every network dependency of the install, checked by name so a
# failure says exactly which one is blocked. Any HTTP response counts —
# we only care that TLS + routing work.
for host in raw.githubusercontent.com ghcr.io api.tailscale.com login.tailscale.com; do
    curl -sI --max-time 10 "https://$host/" >/dev/null 2>&1 \
        || fail "Cannot reach https://$host — the install needs it (check DNS, egress firewall, and the system clock: a badly wrong clock breaks TLS)."
done

# Clock skew: OAuth and cert issuance tolerate small drift; big drift causes
# confusing failures much later. Compare against tailscale's Date header.
REMOTE_DATE="$(curl -sI --max-time 10 https://api.tailscale.com/ 2>/dev/null | tr -d '\r' | sed -n 's/^[Dd]ate: //p')"
if [[ -n "$REMOTE_DATE" ]]; then
    REMOTE_EPOCH="$(date -d "$REMOTE_DATE" +%s 2>/dev/null || true)"
    if [[ -n "$REMOTE_EPOCH" ]]; then
        SKEW=$(( $(date -u +%s) - REMOTE_EPOCH )); SKEW=${SKEW#-}
        if (( SKEW > 120 )); then
            warn "System clock is ${SKEW}s off from api.tailscale.com — fix it (e.g. 'timedatectl set-ntp true') or expect TLS/auth weirdness."
        fi
    fi
fi

echo "[OK] Host preflight passed."

# The web controller is the only interface: it enrolls as its own tailnet
# node and manages the tailnet through ONE Tailscale OAuth client (see the
# README's "Tailscale credential" section — dedicated tailnet, client
# tagged tag:tailarr-ctrl with auth_keys/devices/policy_file write scopes).
# Collected interactively when possible — the "$(curl ...)" install pattern
# leaves stdin on the terminal — so nobody has to paste secrets into the
# middle of a one-liner (or leave them in shell history). Env vars still
# win when set (automation, install-mac.sh forwarding into the guest).
prompt_credential() {
    while [[ -z "${TS_API_CLIENT_ID:-}" ]]; do
        read -rp "Tailscale OAuth client ID: " TS_API_CLIENT_ID
        TS_API_CLIENT_ID="${TS_API_CLIENT_ID//[[:space:]]/}"
        if [[ "$TS_API_CLIENT_ID" == tskey-client-* ]]; then
            echo "  That looks like the client SECRET — the ID is the short string shown above it."
            TS_API_CLIENT_ID=""
        fi
    done
    while [[ -z "${TS_API_CLIENT_SECRET:-}" ]]; do
        read -rsp "Tailscale OAuth client secret (hidden): " TS_API_CLIENT_SECRET
        echo ""
        TS_API_CLIENT_SECRET="${TS_API_CLIENT_SECRET//[[:space:]]/}"
        if [[ -n "$TS_API_CLIENT_SECRET" && "$TS_API_CLIENT_SECRET" != tskey-client-* ]]; then
            echo "  [WARN] Secrets normally start with 'tskey-client-' — continuing anyway."
        fi
    done
}

# The server's name = its tailnet hostname (and the URL you'll visit:
# https://<name>.<tailnet>.ts.net). Defaults to "tailarr". Ask interactively so
# nobody has to know the env var; TAILARR_HOSTNAME (env) still wins for
# automation. Only the tailnet hostname changes — the controller container is
# always "tailarr". Changing it also sidesteps the per-hostname Let's Encrypt
# cert limit (5 / exact hostname / 168h) across repeated rebuilds.
prompt_hostname() {
    [[ -n "${TAILARR_HOSTNAME:-}" ]] && return            # env override wins
    if [[ ! -t 0 ]]; then TAILARR_HOSTNAME="tailarr"; return; fi
    local name
    while true; do
        read -rp "Server name (its tailnet hostname + URL) [tailarr]: " name
        name="${name//[[:space:]]/}"
        [[ -z "$name" ]] && name="tailarr"
        if [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
            TAILARR_HOSTNAME="$name"
            echo "  Server will be at https://${name}.<your-tailnet>.ts.net"
            return
        fi
        echo "  Use 1-63 chars: lowercase letters, digits, and hyphens (not at the ends)."
    done
}

# Exchange the client for an access token — the definitive credential check,
# run BEFORE anything installs. Sets TS_OAUTH_TOKEN on success. Returns 1 on
# a rejected credential, 2 when the API itself couldn't be reached.
TS_OAUTH_TOKEN=""
mint_oauth_token() {
    local resp
    resp="$(curl -s --max-time 20 \
        -d "client_id=${TS_API_CLIENT_ID}" \
        -d "client_secret=${TS_API_CLIENT_SECRET}" \
        "https://api.tailscale.com/api/v2/oauth/token")" || return 2
    TS_OAUTH_TOKEN="$(printf '%s' "$resp" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    [[ -n "$TS_OAUTH_TOKEN" ]]
}

if [[ -z "${TS_API_CLIENT_ID:-}" || -z "${TS_API_CLIENT_SECRET:-}" ]]; then
    if [[ ! -t 0 ]]; then
        echo "[ERROR] A Tailscale OAuth client is required. Run interactively, or pass it via env:"
        echo ""
        echo "  TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\""
        echo ""
        echo "Create it per the README's 'Tailscale credential' section (paste the"
        echo "tailnet policy, then generate the client at"
        echo "https://login.tailscale.com/admin/settings/oauth)."
        exit 1
    fi
    # Offer the step-by-step Tailscale walkthrough on a cold install; a
    # re-runner (policy + client already in place) just answers 'y' and
    # skips straight to the credential prompt. Opt out entirely with
    # TAILARR_SKIP_SETUP_GUIDE=1.
    if [[ "${TAILARR_SKIP_SETUP_GUIDE:-}" != "1" ]]; then
        echo ""
        echo "Tailarr manages a dedicated Tailscale tailnet through one OAuth client."
        read -rp "Have you already pasted the policy and created the OAuth client? [y/N] " _ready
        case "$_ready" in
            [Yy]*) echo "  Great — paste the client ID and secret below." ;;
            *)     guided_tailscale_setup ;;
        esac
    fi
    prompt_credential
fi

# Validate the credential live. Interactively, a rejected credential
# re-prompts instead of failing ten minutes into the install.
echo "[CHECK] Validating the OAuth client against api.tailscale.com..."
while true; do
    if mint_oauth_token; then
        echo "[OK] OAuth client accepted."
        break
    elif [[ $? -eq 2 ]]; then
        fail "Could not reach https://api.tailscale.com to validate the credential (it was reachable moments ago — transient network trouble?)."
    fi
    if [[ -t 0 ]]; then
        echo "[WARN] Tailscale rejected this client ID + secret. Re-check both (a revoked client fails the same way) and try again."
        TS_API_CLIENT_ID="" TS_API_CLIENT_SECRET=""
        prompt_credential
    else
        fail "Tailscale rejected the OAuth client ID + secret (revoked, mistyped, or from a different tailnet's account)."
    fi
done

# Probe each API scope the controller depends on, so a mis-scoped client is
# named precisely now instead of failing obscurely mid-bootstrap. The client
# needs WRITE on all three; these reads prove the scope is present at all.
api_status() {  # api_status <path> <bodyfile>
    curl -s --max-time 20 -o "$2" -w '%{http_code}' \
        -H "Authorization: Bearer $TS_OAUTH_TOKEN" \
        "https://api.tailscale.com/api/v2/$1" 2>/dev/null || echo 000
}
PREFLIGHT_TMP="$(mktemp -d)"
trap 'rm -rf "$PREFLIGHT_TMP"' EXIT

echo "[CHECK] Verifying the client's API scopes (auth_keys, devices, policy_file)..."
for probe in "tailnet/-/keys:Auth Keys:auth_keys" \
             "tailnet/-/devices?fields=default:Devices:devices" \
             "tailnet/-/acl:Policy File:policy_file"; do
    path="${probe%%:*}"; rest="${probe#*:}"
    label="${rest%%:*}"; scope="${rest##*:}"
    body="$PREFLIGHT_TMP/${scope}.json"
    code="$(api_status "$path" "$body")"
    case "$code" in
        200) : ;;
        401|403) fail "The OAuth client lacks the '$label' scope (API returned $code). Recreate it at https://login.tailscale.com/admin/settings/oauth with WRITE access to Auth Keys, Devices, and Policy File, tagged tag:tailarr-ctrl." ;;
        *) fail "Unexpected API response ($code) probing the '$label' scope — try again; if it persists, check https://status.tailscale.com." ;;
    esac
done
echo "[OK] All three scopes present."

# The tailnet policy must already declare the two seed tags (README's
# starting policy) — Tailscale rejects any policy write that references an
# undeclared tag, so a missing declaration wedges the bootstrap later.
echo "[CHECK] Inspecting the tailnet policy for Tailarr's seed tags..."
ACL_BODY="$PREFLIGHT_TMP/policy_file.json"
seed_tags_present() {  # <aclfile> — both seed tags declared?
    grep -q '"tag:tailarr-ctrl"' "$1" && grep -q '"tag:tailarr"' "$1"
}
while ! seed_tags_present "$ACL_BODY"; do
    echo "[ERROR] Your tailnet policy doesn't declare Tailarr's seed tags" >&2
    echo "        (tag:tailarr-ctrl / tag:tailarr). Open $ADMIN_ACLS and paste" >&2
    echo "        this policy — the whole file on a fresh tailnet, or just the" >&2
    echo "        fenced tagOwners block into an existing one:" >&2
    print_policy >&2
    if [[ -t 0 ]]; then
        read -rp "   Press Enter after you've saved it and I'll re-check... " _
        api_status "tailnet/-/acl" "$ACL_BODY" >/dev/null
    else
        fail "Paste the policy above at $ADMIN_ACLS, then re-run."
    fi
done
if ! grep -q "tailarr-managed:tagowners" "$ACL_BODY"; then
    warn "The policy declares the seed tags but not the '// >>> tailarr-managed:tagowners' fence markers — the bootstrap will try to adopt anyway, but the README's fenced block is the supported shape."
fi
echo "[OK] Seed tags declared."

# MagicDNS is required for ts.net hostnames and HTTPS certs. New tailnets
# have it on by default, so a 'false' here usually means an old tailnet.
DNS_BODY="$PREFLIGHT_TMP/dns.json"
DNS_CODE="$(api_status "tailnet/-/dns/preferences" "$DNS_BODY")"
if [[ "$DNS_CODE" == "200" ]]; then
    while grep -q '"magicDNS"[[:space:]]*:[[:space:]]*false' "$DNS_BODY"; do
        echo "[ERROR] MagicDNS is disabled on this tailnet — Tailarr's hostnames and" >&2
        echo "        HTTPS depend on it. Enable it (and HTTPS Certificates while" >&2
        echo "        you're on that page) at $ADMIN_DNS." >&2
        if [[ -t 0 ]]; then
            read -rp "   Press Enter after enabling MagicDNS and I'll re-check... " _
            DNS_CODE="$(api_status "tailnet/-/dns/preferences" "$DNS_BODY")"
            [[ "$DNS_CODE" == "200" ]] || { warn "Couldn't re-read DNS preferences (HTTP $DNS_CODE) — continuing."; break; }
        else
            fail "Enable MagicDNS at $ADMIN_DNS, then re-run."
        fi
    done
    echo "[OK] MagicDNS is enabled."
else
    warn "Could not read the tailnet's DNS preferences (HTTP $DNS_CODE) — skipping the MagicDNS check."
fi

# HTTPS Certificates has no public API to read — the one prerequisite we
# cannot verify. Say so instead of pretending.
echo "[NOTE] One thing this script CANNOT check: 'HTTPS Certificates' must be"
echo "       enabled in the admin console (DNS tab, $ADMIN_DNS). If pod HTTPS"
echo "       fails later, check there first."

# Existing controller identity: the bootstrap silently reuses it. Fine for a
# re-run on the SAME tailnet; wrong-and-confusing if this credential belongs
# to a different one.
EXISTING_STATE="${HOME:-/root}/Pods/tailarr/tailscale/tailscaled.state"
if [[ -f "$EXISTING_STATE" ]]; then
    warn "Existing controller identity found ($EXISTING_STATE) — the bootstrap will REUSE it. If this OAuth client belongs to a DIFFERENT tailnet than that identity, abort and remove ${HOME:-/root}/Pods first."
    if [[ -t 0 ]]; then
        read -rp "Press Enter to continue reusing it, or Ctrl-C to abort. "
    fi
fi

# Name the server (its tailnet hostname + URL). Last interactive question
# before the install runs unattended.
prompt_hostname

# --- Check and install podman ---
echo "[CHECK] Looking for podman..."
if ! command -v podman >/dev/null 2>&1; then
  echo "[WARN] podman not found. Attempting to install..."

  if [[ -f /etc/debian_version ]]; then
    apt update
    apt install -y podman
    echo "[OK] podman successfully installed."
  else
    echo "[ERROR] Unsupported OS for auto-install of podman. Please install it manually."
    exit 1
  fi
else
  echo "[OK] podman is already installed."
fi

# --- Check and install jq ---
echo "[CHECK] Looking for jq..."
if ! command -v jq >/dev/null 2>&1; then
  echo "[WARN] jq not found. Attempting to install..."

  if [[ -f /etc/debian_version ]]; then
    apt update
    apt install -y jq
    echo "[OK] jq successfully installed."
  else
    echo "[ERROR] Unsupported OS for auto-install of jq. Please install it manually."
    exit 1
  fi
else
  echo "[OK] jq is already installed."
fi

# --- Download the engine + bootstrap ---
FILES=(
    # Service database
    "homelab.js"

    # Deployment engine (the controller renders pods through these)
    "create.sh"
    "error-handler.sh"
    "logging-utils.sh"
    "parse-service-config.sh"
    "setup-service-env.sh"
    "generate-scripts.sh"
    "generate-run-template.sh"
    "generate-diagnose-template.sh"
    "display-summary.sh"

    # Controller bootstrap (the web UI is the product)
    "bootstrap-tailarr.sh"
)

echo "[FETCH] Staging install scripts in a temp dir (removed when done)..."
for file in "${FILES[@]}"; do
    echo "  - Downloading $file..."
    curl -fsSL "$REPO_BASE_URL/$file" -o "$file"
done

# Make all scripts executable
chmod +x *.sh


echo "[START] Bootstrapping the Tailarr controller..."
TS_API_CLIENT_ID="$TS_API_CLIENT_ID" TS_API_CLIENT_SECRET="$TS_API_CLIENT_SECRET" \
TAILARR_HOSTNAME="${TAILARR_HOSTNAME:-tailarr}" \
./bootstrap-tailarr.sh
