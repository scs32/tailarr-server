#!/usr/bin/env bash
set -euo pipefail

# HomePod Creator Orchestrator
# Coordinates the workflow between UI, config building, and deployment

# Locate this script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JSON_FILE="$SCRIPT_DIR/homelab.js"

# Default paths
DEFAULT_BASE_PATH="${BASE_PATH:-$HOME/Pods}"
TS_AUTHKEY_FILE="${TS_AUTHKEY_FILE:-$HOME/Pods/.tailscale_authkey}"

# Load components
source "$SCRIPT_DIR/user-interface.sh"
source "$SCRIPT_DIR/config-builder.sh"

# Dependency checks
check_dependencies() {
    command -v jq >/dev/null || { printf "Error: jq is required but not installed\n"; exit 1; }
    [[ -f "$JSON_FILE" ]] || { printf "Error: homelab.js not found in %s\n" "$SCRIPT_DIR"; exit 1; }
}

# Main workflow
main() {
    check_dependencies

    # Step 1: Container Selection
    echo "=== Container Selection ==="
    local selected_service
    selected_service=$(select_container "$JSON_FILE") || { echo "No container selected. Exiting."; exit 0; }

    # Step 2: NPM Configuration
    echo "=== NPM Configuration ==="
    local npm_choice
    npm_choice=$(ask_yes_no "Would you like to package this with NPM (Nginx Proxy Manager)?" "yes") || { echo "Exiting..."; exit 0; }

    # Step 3: Tailscale Configuration
    echo "=== Tailscale Configuration ==="
    local tailscale_choice
    tailscale_choice=$(ask_yes_no "Would you like to enable Tailscale?" "yes") || { echo "Exiting..."; exit 0; }

    # Step 4: Resolve Tailscale auth key file (if needed).
    # Only the file path travels through the config; generated scripts read
    # the key from this file at runtime.
    local auth_key_file=""
    if [[ "$tailscale_choice" == "yes" ]]; then
        auth_key_file=$(get_auth_key_file "$TS_AUTHKEY_FILE") || { echo "Exiting..."; exit 0; }
    fi

    # Step 5: Get Base Path
    echo "=== Path Configuration ==="
    local base_path
    base_path=$(get_input "Base path" "$DEFAULT_BASE_PATH") || { echo "Exiting..."; exit 0; }

    # Step 6: Collect Environment Variables
    echo "=== Environment Variables ==="
    collect_env_vars "$JSON_FILE" "$selected_service" || { echo "Exiting..."; exit 0; }

    # Step 7: Collect Volume Mappings
    echo "=== Volume Mappings ==="
    collect_volumes "$JSON_FILE" "$selected_service" "$base_path" || { echo "Exiting..."; exit 0; }

    # Step 8: Build Configuration
    echo "=== Building Configuration ==="
    local config_file
    config_file=$(build_configuration "$JSON_FILE" "$selected_service" "$npm_choice" "$tailscale_choice" "$auth_key_file" "$base_path")

    # Display summary
    display_config_summary "$config_file"

    # Step 9: Confirm Configuration
    if ! confirm_configuration "$config_file"; then
        rm -f "$config_file"
        echo "Configuration cancelled."
        exit 0
    fi

    # Step 10: Save Configuration
    save_configuration "$config_file" "$selected_service"

    # Step 11: Deploy Service ($BASH keeps the same interpreter; the
    # deployment scripts need bash >= 4)
    echo "=== Deploying Service ==="
    "$BASH" "$SCRIPT_DIR/create.sh" < "$config_file"
    rm -f "$config_file"

    echo ""
    echo "Deployment complete!"
}

# Error handling
trap 'echo "Error occurred at line $LINENO. Exiting."; exit 1' ERR

# Run main function
main "$@"
