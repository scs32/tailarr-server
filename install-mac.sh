#!/bin/bash
# Tailarr installer for macOS + apple/container (Apple silicon, macOS 15+).
#
# One command on the Mac does the whole thing (it prompts for the
# Tailscale OAuth client; or pass TS_API_CLIENT_ID/TS_API_CLIENT_SECRET
# via env for non-interactive runs):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install-mac.sh)"
#
# It (1) creates/starts the apple/container guest, (2) runs the normal
# Linux installer inside it, and (3) turns this Mac into a Tailscale
# **peer relay** so pod traffic bypasses DERP (apple/container guests sit
# behind a NAT'd vmnet subnet; without a relay every tailnet connection
# falls back to DERP speeds). The relay only engages once the controller
# emits the matching policy grant — which it does automatically only when
# the tailnet passes its dedicated-tailnet pre-flight (see the README).
#
# Written for the stock macOS bash 3.2 — keep it free of bash-4isms.
set -euo pipefail

GUEST_NAME="${GUEST_NAME:-podhost}"
MEDIA_DIR="${MEDIA_DIR:-$HOME/poddata}"
RELAY_PORT="${RELAY_PORT:-40000}"
REPO_BASE_URL="https://raw.githubusercontent.com/scs32/tailarr-server/main"
TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
GUEST_IMAGE="docker.io/library/debian:bookworm"

info()  { printf '[INFO] %s\n' "$*"; }
fail()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

# --- credential -------------------------------------------------------------
# Prompted interactively when possible (the "$(curl ...)" pattern leaves
# stdin on the terminal); env vars still win when set. Forwarded into the
# guest install below, so the Linux installer never re-prompts. Validated
# LIVE against the Tailscale API before the guest is even created — a bad
# credential should cost seconds, not a whole guest build.
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
        echo "[ERROR] A Tailscale OAuth client is required. Run interactively, or pass it via env:" >&2
        echo "" >&2
        echo "  TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c \"\$(curl -fsSL $REPO_BASE_URL/install-mac.sh)\"" >&2
        echo "" >&2
        echo "Create it per the README's 'Tailscale credential' section (paste the" >&2
        echo "tailnet policy, then generate the client at" >&2
        echo "https://login.tailscale.com/admin/settings/oauth)." >&2
        exit 1
    fi
    echo ""
    echo "Tailarr manages your tailnet through a Tailscale OAuth client."
    echo "Create it per the README's 'Tailscale credential' section (paste the"
    echo "tailnet policy, then generate the client at"
    echo "https://login.tailscale.com/admin/settings/oauth)."
    echo ""
    prompt_credential
fi

info "Validating the OAuth client against api.tailscale.com..."
while true; do
    if mint_oauth_token; then
        info "OAuth client accepted."
        break
    elif [[ $? -eq 2 ]]; then
        fail "Could not reach https://api.tailscale.com — check this Mac's network."
    fi
    if [[ -t 0 ]]; then
        echo "[WARN] Tailscale rejected this client ID + secret. Re-check both (a revoked client fails the same way) and try again."
        TS_API_CLIENT_ID="" TS_API_CLIENT_SECRET=""
        prompt_credential
    else
        fail "Tailscale rejected the OAuth client ID + secret (revoked, mistyped, or from a different tailnet's account)."
    fi
done

# Seed-policy + MagicDNS checks (the guest installer re-checks these too,
# but failing here costs seconds instead of a guest build). Scope probe
# rides along: a 401/403 on the policy read means a mis-scoped client.
ACL_TMP="$(mktemp)"
trap 'rm -f "$ACL_TMP"' EXIT
ACL_CODE="$(curl -s --max-time 20 -o "$ACL_TMP" -w '%{http_code}' \
    -H "Authorization: Bearer $TS_OAUTH_TOKEN" \
    "https://api.tailscale.com/api/v2/tailnet/-/acl" 2>/dev/null || echo 000)"
case "$ACL_CODE" in
    200)
        for tag in "tag:tailarr-ctrl" "tag:tailarr"; do
            grep -q "\"$tag\"" "$ACL_TMP" \
                || fail "The tailnet policy does not declare $tag. Paste the README's starting policy (or at minimum its fenced tagOwners block) at https://login.tailscale.com/admin/acls before installing."
        done
        info "Tailnet policy declares the Tailarr seed tags."
        ;;
    401|403)
        fail "The OAuth client lacks the 'Policy File' scope (API returned $ACL_CODE). Recreate it with WRITE access to Auth Keys, Devices, and Policy File, tagged tag:tailarr-ctrl."
        ;;
    *)
        echo "[WARN] Could not read the tailnet policy (HTTP $ACL_CODE) — skipping the seed-tag check."
        ;;
esac
DNS_PREFS="$(curl -s --max-time 20 -H "Authorization: Bearer $TS_OAUTH_TOKEN" \
    "https://api.tailscale.com/api/v2/tailnet/-/dns/preferences" 2>/dev/null || true)"
if printf '%s' "$DNS_PREFS" | grep -q '"magicDNS"[[:space:]]*:[[:space:]]*false'; then
    fail "MagicDNS is disabled on this tailnet. Enable it (admin console → DNS → MagicDNS) — Tailarr's hostnames and HTTPS depend on it."
fi
echo "[NOTE] One thing this script CANNOT check: 'HTTPS Certificates' must be"
echo "       enabled in the admin console (DNS tab)."

# --- host preflight ----------------------------------------------------------
# The guest image + controller + service images land on this Mac's disk.
DISK_AVAIL_KB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
if [[ -n "$DISK_AVAIL_KB" ]] && [[ "$DISK_AVAIL_KB" -lt $(( 10 * 1024 * 1024 )) ]]; then
    echo "[WARN] Less than 10GB free on this Mac — the guest, controller image, and service images will want more."
fi

# --- prerequisites ----------------------------------------------------------
command -v container >/dev/null 2>&1 \
    || fail "apple/container is not installed. Run: brew install container"

[[ -x "$TS_BIN" ]] \
    || fail "Tailscale.app not found. Install it from https://tailscale.com/download and log in."

# Peer relays need Tailscale 1.86+. Version output looks like "1.88.1\n...".
TS_VER=$("$TS_BIN" version 2>/dev/null | head -1 | tr -dc '0-9.')
TS_MAJOR=${TS_VER%%.*}
TS_REST=${TS_VER#*.}
TS_MINOR=${TS_REST%%.*}
[[ -n "$TS_MAJOR" && -n "$TS_MINOR" ]] \
    || fail "Could not read the Tailscale version from $TS_BIN."
if [[ "$TS_MAJOR" -lt 1 || ( "$TS_MAJOR" -eq 1 && "$TS_MINOR" -lt 86 ) ]]; then
    fail "Tailscale $TS_VER is too old for peer relays - update to 1.86 or later."
fi

"$TS_BIN" status >/dev/null 2>&1 \
    || fail "Tailscale is installed but not connected. Open Tailscale.app and log in to the SAME tailnet the OAuth client belongs to."

# --- guest ------------------------------------------------------------------
info "Starting apple/container..."
container system start >/dev/null 2>&1 || true

if container list --all 2>/dev/null | grep -q "[[:space:]]${GUEST_NAME}\$\|^${GUEST_NAME}[[:space:]]"; then
    info "Guest '$GUEST_NAME' already exists - starting it."
    container start "$GUEST_NAME" >/dev/null 2>&1 || true
else
    info "Creating guest '$GUEST_NAME' (media dir: $MEDIA_DIR -> /data)..."
    mkdir -p "$MEDIA_DIR"
    container run -d --name "$GUEST_NAME" \
        --cpus 4 --memory 4g \
        --volume "$MEDIA_DIR:/data" \
        "$GUEST_IMAGE" sleep infinity >/dev/null
fi

# --- install inside the guest ----------------------------------------------
info "Preparing the guest (curl)..."
container exec "$GUEST_NAME" bash -c \
    "apt-get update -qq && apt-get install -y -qq curl" >/dev/null

info "Running the Tailarr installer inside the guest (this takes a few minutes)..."
container exec "$GUEST_NAME" bash -c \
    "cd /root && TS_API_CLIENT_ID='$TS_API_CLIENT_ID' TS_API_CLIENT_SECRET='$TS_API_CLIENT_SECRET' bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\""

# --- peer relay on this Mac ---------------------------------------------
# Idempotent: `tailscale set` persists across restarts. Clients >=1.86
# discover and prefer the relay automatically once the controller's
# policy grant is in place - there is no per-client configuration.
info "Configuring this Mac as a Tailscale peer relay (UDP port $RELAY_PORT)..."
if "$TS_BIN" set --relay-server-port="$RELAY_PORT"; then
    info "Peer relay enabled."
else
    echo "[WARN] Could not enable the peer relay (older client, or the App" >&2
    echo "       Store build refusing the port). Tailarr still works - pods" >&2
    echo "       just stay on DERP until you run this on the Mac:" >&2
    echo "         $TS_BIN set --relay-server-port=$RELAY_PORT" >&2
fi

# The macOS application firewall is off on most Macs; when it's on, make
# sure Tailscale.app may accept the relay's inbound UDP. Best-effort only.
FW="/usr/libexec/ApplicationFirewall/socketfilterfw"
if [[ -x "$FW" ]] && "$FW" --getglobalstate 2>/dev/null | grep -qi "enabled"; then
    info "macOS firewall is on - allowing Tailscale.app (may prompt for sudo)..."
    sudo "$FW" --add /Applications/Tailscale.app >/dev/null 2>&1 || true
    sudo "$FW" --unblockapp /Applications/Tailscale.app >/dev/null 2>&1 || true
fi

# --- verdict ------------------------------------------------------------
# The controller decides whether the relay GRANT is safe to auto-emit
# (dedicated-tailnet pre-flight). Read its verdict over the tailnet -
# this Mac is a member, and /api/info needs no token.
info "Checking the controller's peer-relay verdict..."
FQDN=$(container exec "$GUEST_NAME" podman exec tailscale-tailarr \
        tailscale status --json --peers=false 2>/dev/null \
        | grep -o '"DNSName": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
FQDN=${FQDN%.}
if [[ -n "$FQDN" ]]; then
    sleep 3  # give the controller a moment to finish its startup pre-flight
    INFO_JSON=$(curl -fsS --max-time 15 "http://$FQDN:8080/api/info" 2>/dev/null || true)
    if [[ -n "$INFO_JSON" ]]; then
        printf '%s' "$INFO_JSON" | /usr/bin/python3 -c '
import json, sys
r = json.load(sys.stdin).get("relay") or {}
if r.get("grant_active"):
    print("[OK] Relay grant is active - pods will use this Mac instead of DERP.")
elif r.get("enabled") is False:
    print("[NOTE] The relay grant is switched off in Settings.")
else:
    print("[NOTE] The relay grant was NOT auto-enabled. The controller found:")
    for reason in r.get("reasons") or ["(pre-flight has not run yet)"]:
        print("   - " + reason)
    print("   Review and enable it under Network -> Peer relay.")
' || true
    else
        echo "[NOTE] Could not reach the controller at http://$FQDN:8080 yet;"
        echo "       check Network -> Peer relay once the UI is up."
    fi
fi

echo ""
echo "Tailarr is up."
if [[ -n "$FQDN" ]]; then
    echo "  Web UI: https://$FQDN  (or http://$FQDN:8080)"
fi
echo ""
echo "After a Mac reboot, bring the fleet back with:"
echo "  container start $GUEST_NAME && container exec $GUEST_NAME /root/start-pods.sh"
