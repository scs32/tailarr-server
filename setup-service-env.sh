#!/usr/bin/env bash

# Setup service environment (directories, permissions, etc.)
setup_service_environment() {
    local service_info="$1"
    
    log_section "Setting Up Service Environment"
    
    # Extract necessary information
    local service_dir
    local volumes_json
    local env_vars_json
    local include_ts
    local puid
    local pgid

    service_dir=$(jq -r '.service_dir' <<<"$service_info")
    volumes_json=$(jq -c '.volumes' <<<"$service_info")
    env_vars_json=$(jq -c '.environment' <<<"$service_info")
    include_ts=$(jq -r '.include_tailscale' <<<"$service_info")
    puid=$(jq -r '.environment.PUID // ""' <<<"$service_info")
    pgid=$(jq -r '.environment.PGID // ""' <<<"$service_info")
    
    # Create main service directory
    log_info "Creating service directory: $service_dir"
    ensure_directory "$service_dir" "service directory"

    # Create Tailscale state directory if needed
    if [[ "$include_ts" == "yes" ]]; then
        log_info "Setting up Tailscale directory"
        ensure_directory "$service_dir/tailscale" "Tailscale state directory"
    fi
    
    # Create volume directories
    log_info "Creating volume directories"
    create_volume_directories "$volumes_json" "$puid" "$pgid"
    
    # Store the working directory for reference (but don't change to it)
    # This allows scripts to be generated in the service directory without changing context
    log_info "Service directory prepared: $service_dir"
}

# Create all volume directories with proper ownership
create_volume_directories() {
    local volumes_json="$1"
    local puid="${2:-}"
    local pgid="${3:-}"
    
    # Get all host paths from volumes
    local host_paths
    readarray -t host_paths < <(jq -r '.[]' <<<"$volumes_json")
    
    local read_only
    for host_path in "${host_paths[@]}"; do
        if [[ -n "$host_path" ]]; then
            # A :ro suffix marks a read-only mount: strip it before touching
            # the filesystem, and never chown into shared/archive data.
            read_only="no"
            if [[ "$host_path" == *:ro ]]; then
                host_path="${host_path%:ro}"
                read_only="yes"
            fi

            log_info "Creating volume directory: $host_path"
            if ! ensure_directory "$host_path" "volume directory"; then
                # Not fatal: a shared media root (NAS-backed /data, read-only
                # export...) may not be mounted or writable at install time,
                # and podman creates missing bind sources when the pod runs.
                log_warn "Could not create volume path $host_path - it must exist when the pod starts"
                continue
            fi

            # Set ownership if PUID/PGID are provided
            if [[ "$read_only" == "no" && -n "$puid" && -n "$pgid" ]]; then
                set_directory_ownership "$host_path" "$puid" "$pgid"
            fi
        fi
    done
}

# Set ownership on a directory
set_directory_ownership() {
    local path="$1"
    local puid="$2"
    local pgid="$3"
    
    log_debug "Setting ownership on $path to $puid:$pgid"

    # Try without sudo first; only offer sudo at an interactive terminal so
    # non-interactive runs never hang on a password prompt.
    if chown -R "${puid}:${pgid}" "$path" 2>/dev/null; then
        log_success "Ownership set successfully"
    elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1; then
        log_warn "Trying with sudo..."
        if sudo chown -R "${puid}:${pgid}" "$path"; then
            log_success "Ownership set with sudo"
        else
            log_warn "Could not set ownership on $path (fix later with: sudo chown -R ${puid}:${pgid} $path)"
        fi
    else
        log_warn "Could not set ownership on $path (fix later with: sudo chown -R ${puid}:${pgid} $path)"
    fi
}
