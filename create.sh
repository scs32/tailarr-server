#!/usr/bin/env bash
set -euo pipefail

# Directory where the Tailarr scripts are located
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
    
    # Save config for debugging. It carries the pod's `environment`, which can
    # hold secrets (e.g. the gateway secret, a user's API keys), so it must be
    # owner-only (S3). Born 0600 via a scoped umask, and rm'd first so a rewrite
    # can't inherit an old world-readable mode — no window at 0644. Best-effort:
    # the CWD may be invalid or read-only.
    ( umask 077; rm -f ./.last-config.json
      printf '%s\n' "$config_json" > ./.last-config.json ) 2>/dev/null || true

    # Parse basic service info
    source "$SCRIPT_DIR/parse-service-config.sh"
    local service_info
    service_info=$(parse_service_config "$config_json")

    # The target log dir is now known: point the deployment log at the
    # service dir (unless the caller pinned an absolute LOG_FILE via env).
    # init_logging never fails — a deploy must not die over a log file.
    local service_dir
    service_dir=$(jq -r '.service_dir' <<<"$service_info")
    init_logging "$service_dir"

    # Create service directory structure
    source "$SCRIPT_DIR/setup-service-env.sh"
    setup_service_environment "$service_info"
    
    # Generate all management scripts
    source "$SCRIPT_DIR/generate-scripts.sh"
    generate_all_scripts "$service_info"

    # Persist the parsed config beside the scripts. It carries the pod's
    # `environment` (which can hold secrets — gateway secret, API keys), so it
    # must be owner-only (S3). Born 0600 via a scoped umask, rm'd first so a
    # re-render can't inherit an old world-readable mode — no window at 0644.
    # Used by update tooling. The auth key itself still travels only as a file
    # path, never inlined here.
    # ATOMIC: write a temp file, then rename. `rm -f` + `>` left a window in
    # which .config.json did not exist (or existed truncated) on EVERY render,
    # and a controller kill or ENOSPC inside that window leaves a pod with
    # RUNNING CONTAINERS and no readable config. The share gate refuses such a
    # pod rather than recreating it blind, so the window is now a refusal
    # instead of an ungated `podman rm -f` — but the window should not exist.
    # rename(2) is atomic within the directory; the temp file is born 0600 under
    # the same umask, and mv preserves that mode.
    ( umask 077; rm -f "$service_dir/.config.json.tmp"
      printf '%s\n' "$service_info" > "$service_dir/.config.json.tmp"
      mv -f "$service_dir/.config.json.tmp" "$service_dir/.config.json" )
    
    # Display completion message
    source "$SCRIPT_DIR/display-summary.sh"
    display_service_summary "$service_info"
    
    log_info "Service deployment completed successfully"
}

# Call main if script is run directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
