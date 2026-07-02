#!/usr/bin/env bash
set -euo pipefail

# Directory where the HomePod Creator scripts are located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load utilities
source "$SCRIPT_DIR/error-handler.sh"
source "$SCRIPT_DIR/logging-utils.sh"

# Main entry point for service creation
main() {
    setup_error_handler
    
    log_info "Starting service deployment..."
    
    # Read configuration from stdin
    local config_json
    config_json="$(cat)"
    
    if [[ -z "$config_json" ]]; then
        log_error "No JSON input provided"
        exit 1
    fi
    
    # Save config for debugging (contains no secrets - only the key file path)
    echo "$config_json" > ./.last-config.json
    
    # Parse basic service info
    source "$SCRIPT_DIR/parse-service-config.sh"
    local service_info
    service_info=$(parse_service_config "$config_json")
    
    # Create service directory structure
    source "$SCRIPT_DIR/setup-service-env.sh"
    setup_service_environment "$service_info"
    
    # Generate all management scripts
    source "$SCRIPT_DIR/generate-scripts.sh"
    generate_all_scripts "$service_info"
    
    # Display completion message
    source "$SCRIPT_DIR/display-summary.sh"
    display_service_summary "$service_info"
    
    log_info "Service deployment completed successfully"
}

# Call main if script is run directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
