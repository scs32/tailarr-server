#!/bin/bash
set -euo pipefail

echo "[INFO] Installing and launching Tailarr..."

WORKDIR="$(pwd)"
REPO_BASE_URL="https://raw.githubusercontent.com/scs32/tailarr-server/main"

# Package installs need sudo only when not already root (containers and
# minimal VM guests often have no sudo binary at all).
SUDO="sudo"
if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
fi

# --- Check and install podman ---
echo "[CHECK] Looking for podman..."
if ! command -v podman >/dev/null 2>&1; then
  echo "[WARN] podman not found. Attempting to install..."

  if [[ -f /etc/debian_version ]]; then
    $SUDO apt update
    $SUDO apt install -y podman
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
    $SUDO apt update
    $SUDO apt install -y jq
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

# The web controller is the only interface: an auth key is required to
# enroll the controller as its own tailnet node.
if [[ -z "${TS_AUTHKEY:-}" ]]; then
    echo "[ERROR] TS_AUTHKEY is required."
    echo ""
    echo "Create a Tailscale auth key (reusable recommended) and run:"
    echo "  TS_AUTHKEY=tskey-... bash -c \"\$(curl -fsSL $REPO_BASE_URL/install.sh)\""
    exit 1
fi

echo "[START] Bootstrapping the Tailarr controller..."
TS_AUTHKEY="$TS_AUTHKEY" ./bootstrap-tailarr.sh
