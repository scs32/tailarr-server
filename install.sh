#!/bin/bash
set -euo pipefail

echo "[INFO] Installing and launching Tailarr..."

# Engine scripts land in the current directory — but never scatter them
# across the filesystem root. `container exec` (no login shell) starts at
# "/", which is how people run this inside an apple/container guest.
WORKDIR="$(pwd)"
if [[ "$WORKDIR" == "/" ]]; then
    WORKDIR="${HOME:-/root}/tailarr"
    mkdir -p "$WORKDIR"
    cd "$WORKDIR"
    echo "[INFO] Running from /: installing into $WORKDIR instead."
fi
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

# The web controller is the only interface: it enrolls as its own tailnet
# node and manages the tailnet through ONE Tailscale OAuth client (see the
# README's "Tailscale credential" section — dedicated tailnet, client
# tagged tag:tailarr-ctrl with auth_keys/devices/policy_file write scopes).
# Collected interactively when possible — the "$(curl ...)" install pattern
# leaves stdin on the terminal — so nobody has to paste secrets into the
# middle of a one-liner (or leave them in shell history). Env vars still
# win when set (automation, install-mac.sh forwarding into the guest).
if [[ -z "${TS_API_CLIENT_ID:-}" || -z "${TS_API_CLIENT_SECRET:-}" ]]; then
    if [[ -t 0 ]]; then
        echo ""
        echo "Tailarr manages your tailnet through a Tailscale OAuth client."
        echo "Create it per the README's 'Tailscale credential' section (paste the"
        echo "tailnet policy, then generate the client at"
        echo "https://login.tailscale.com/admin/settings/oauth)."
        echo ""
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
        echo ""
    else
        echo "[ERROR] A Tailscale OAuth client is required. Run interactively, or pass it via env:"
        echo ""
        echo "  TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\""
        echo ""
        echo "Create it per the README's 'Tailscale credential' section (paste the"
        echo "tailnet policy, then generate the client at"
        echo "https://login.tailscale.com/admin/settings/oauth)."
        exit 1
    fi
fi

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

echo "[FETCH] Downloading core files into: $WORKDIR"
for file in "${FILES[@]}"; do
    echo "  - Downloading $file..."
    curl -fsSL "$REPO_BASE_URL/$file" -o "$file"
done

# Make all scripts executable
chmod +x *.sh


echo "[START] Bootstrapping the Tailarr controller..."
TS_API_CLIENT_ID="$TS_API_CLIENT_ID" TS_API_CLIENT_SECRET="$TS_API_CLIENT_SECRET" \
./bootstrap-tailarr.sh
