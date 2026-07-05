#!/usr/bin/env bash

# Get the script directory (where all our scripts are)
SCRIPT_DIR="${SCRIPT_DIR:-$(dirname "$(realpath "${BASH_SOURCE[0]}")")}"

# Generate all management scripts for the service
generate_all_scripts() {
    local service_info="$1"
    
    log_section "Generating Management Scripts"
    
    # Extract service directory
    local service_dir
    service_dir=$(jq -r '.service_dir' <<<"$service_info")
    
    # Generate each script type
    generate_run_script "$service_info"
    generate_stop_script "$service_info"
    generate_remove_script "$service_info"
    generate_diagnose_script "$service_info"
    
    # Make all scripts executable
    chmod +x "$service_dir"/*.sh
    
    log_success "All scripts generated successfully"
}

# Generate the run.sh script
generate_run_script() {
    local service_info="$1"
    
    log_info "Generating run.sh"
    
    # Extract variables
    local service=$(jq -r '.service' <<<"$service_info")
    local service_dir=$(jq -r '.service_dir' <<<"$service_info")
    local service_image=$(jq -r '.image' <<<"$service_info")
    local ts_image=$(jq -r '.ts_image' <<<"$service_info")
    local restart_policy=$(jq -r '.restart_policy' <<<"$service_info")
    local auth_key_file=$(jq -r '.auth_key_file' <<<"$service_info")
    local primary_port=$(jq -r '.primary_port' <<<"$service_info")

    # Load the run script template
    source "$SCRIPT_DIR/generate-run-template.sh"

    # Generate content
    local run_content
    run_content=$(generate_run_template \
        "$service" \
        "$auth_key_file" \
        "$ts_image" \
        "$service_image" \
        "$restart_policy" \
        "$primary_port" \
        "$service_info")
    
    # Write to file
    echo "$run_content" > "$service_dir/run.sh"
    
    log_success "run.sh generated"
}

# Generate the stop.sh script
generate_stop_script() {
    local service_info="$1"
    
    log_info "Generating stop.sh"
    
    local service=$(jq -r '.service' <<<"$service_info")
    local service_dir=$(jq -r '.service_dir' <<<"$service_info")

    # Create stop script content
    cat > "$service_dir/stop.sh" << EOF
#!/bin/sh
set -e

echo "Stopping services..."

# Stop main service
echo "Stopping $service..."
podman stop $service 2>/dev/null || true

# Stop the Tailscale sidecar
echo "Stopping tailscale-$service..."
podman stop tailscale-$service 2>/dev/null || true

echo "All services stopped"
EOF

    log_success "stop.sh generated"
}

# Generate the remove.sh script
generate_remove_script() {
    local service_info="$1"
    
    log_info "Generating remove.sh"
    
    local service=$(jq -r '.service' <<<"$service_info")
    local service_dir=$(jq -r '.service_dir' <<<"$service_info")

    # Create remove script content
    cat > "$service_dir/remove.sh" << EOF
#!/bin/sh
set -e

echo "Removing services..."

# Remove main service
echo "Removing $service..."
podman rm -f $service 2>/dev/null || true

# Remove the Tailscale sidecar
echo "Removing tailscale-$service..."
podman rm -f tailscale-$service 2>/dev/null || true

echo "All services removed"
echo "To reclaim ownership of volumes: sudo chown -R \\\$USER:\\\$USER ."
EOF

    log_success "remove.sh generated"
}

# Generate the diagnose.sh script
generate_diagnose_script() {
    local service_info="$1"
    
    log_info "Generating diagnose.sh"
    
    local service=$(jq -r '.service' <<<"$service_info")
    local service_dir=$(jq -r '.service_dir' <<<"$service_info")
    local primary_port=$(jq -r '.primary_port' <<<"$service_info")
    
    # Load the diagnose script template
    source "$SCRIPT_DIR/generate-diagnose-template.sh"
    
    # Generate content
    local diagnose_content
    diagnose_content=$(generate_diagnose_template "$service" "$primary_port")
    
    # Write to file
    echo "$diagnose_content" > "$service_dir/diagnose.sh"
    
    log_success "diagnose.sh generated"
}
