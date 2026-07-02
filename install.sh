#!/bin/bash
set -euo pipefail

echo "[INFO] Installing and launching Podscale..."

WORKDIR="$(pwd)"
REPO_BASE_URL="https://raw.githubusercontent.com/scs32/podscale/main"

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

# --- Download all files ---
FILES=(
    # Main orchestrator (replaces old homelab.sh)
    "homelab-orchestrator.sh"
    
    # User interface components
    "user-interface.sh"
    "config-builder.sh"
    
    # Service database
    "homelab.js"
    
    # Deployment components
    "create.sh"
    "error-handler.sh"
    "logging-utils.sh"
    "parse-service-config.sh"
    "setup-service-env.sh"
    "generate-scripts.sh"
    "generate-run-template.sh"
    "generate-diagnose-template.sh"
    "display-summary.sh"

    # Controller bootstrap (web UI mode)
    "bootstrap-homepod.sh"
)

echo "[FETCH] Downloading core files into: $WORKDIR"
for file in "${FILES[@]}"; do
    echo "  - Downloading $file..."
    curl -fsSL "$REPO_BASE_URL/$file" -o "$file"
done

# Make all scripts executable
chmod +x *.sh

# Create cleanup script inline
cat > cleanup.sh << 'EOF_CLEANUP'
#!/bin/bash
# Auto-cleanup script for temporary files

# Define all files that should be cleaned up
FILES=(
    "homelab-orchestrator.sh"
    "user-interface.sh"
    "config-builder.sh"
    "homelab.js"
    "create.sh"
    "error-handler.sh"
    "logging-utils.sh"
    "parse-service-config.sh"
    "setup-service-env.sh"
    "generate-scripts.sh"
    "generate-run-template.sh"
    "generate-diagnose-template.sh"
    "display-summary.sh"
    "homelab.sh"
    "cleanup.sh"
)

echo ""
echo "[CLEAN] Removing temporary files..."
for file in "${FILES[@]}"; do
    if [[ -f "$file" ]]; then
        rm -f "$file"
        echo "  - Removed $file"
    fi
done
echo "[DONE] Cleanup complete."
EOF_CLEANUP

chmod +x cleanup.sh

# Create an alias for backward compatibility
if [[ ! -f "homelab.sh" ]]; then
    ln -s homelab-orchestrator.sh homelab.sh
fi

# Controller mode: with a Tailscale auth key provided, stand up the web UI
# controller pod (pulls ghcr.io/scs32/podscale) and manage everything from
# the browser afterwards.
if [[ -n "${TS_AUTHKEY:-}" ]]; then
    echo "[START] TS_AUTHKEY provided - bootstrapping the Podscale controller..."
    TS_AUTHKEY="$TS_AUTHKEY" ./bootstrap-homepod.sh
    exit 0
fi

# Check if we're running interactively
if [[ -t 0 ]]; then
    # Running interactively, launch homelab.sh directly
    echo "[START] Running Podscale..."
    ./homelab.sh
else
    # Not running interactively (piped), save scripts and provide instructions
    echo "[NOTICE] Interactive mode required for configuration."
    echo ""
    echo "Files downloaded to: $WORKDIR"
    echo ""
    echo "To continue, run:"
    echo "  ./homelab.sh"
    echo ""
    echo "When finished, clean up with:"
    echo "  ./cleanup.sh"
    echo ""
    echo "[DONE] Download complete. Ready for interactive mode."
fi
