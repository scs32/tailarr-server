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
# sudo-elevate ourselves because the credential env vars would need
# forwarding through sudo's env filter; a clean re-run as root is simpler.
if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERROR] Tailarr must be installed as root (the controller drives the system podman socket and installs boot units)." >&2
    echo "        Re-run the whole command under sudo:" >&2
    echo "          sudo env TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\"" >&2
    echo "        (plain sudo, NOT 'sudo -i' — -i re-quotes the inline script and silently runs nothing)" >&2
    exit 1
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

# The web controller is the only interface: it enrolls as its own tailnet
# node and manages the tailnet through ONE Tailscale OAuth client (see the
# README's "Tailscale credential" section — dedicated tailnet, client
# tagged tag:tailarr-ctrl with auth_keys/devices/policy_file write scopes).
if [[ -z "${TS_API_CLIENT_ID:-}" || -z "${TS_API_CLIENT_SECRET:-}" ]]; then
    echo "[ERROR] A Tailscale OAuth client is required:"
    echo ""
    echo "  TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\""
    echo ""
    echo "Create it per the README's 'Tailscale credential' section (paste the"
    echo "tailnet policy, then generate the client at"
    echo "https://login.tailscale.com/admin/settings/oauth)."
    exit 1
fi

echo "[START] Bootstrapping the Tailarr controller..."
TS_API_CLIENT_ID="$TS_API_CLIENT_ID" TS_API_CLIENT_SECRET="$TS_API_CLIENT_SECRET" \
./bootstrap-tailarr.sh
