#!/usr/bin/env bash

# Setup service environment (directories, permissions, etc.)
setup_service_environment() {
    local service_info="$1"
    
    log_section "Setting Up Service Environment"
    
    # Extract necessary information
    local service_dir
    local volumes_json
    local env_vars_json
    local puid
    local pgid

    service_dir=$(jq -r '.service_dir' <<<"$service_info")
    volumes_json=$(jq -c '.volumes' <<<"$service_info")
    env_vars_json=$(jq -c '.environment' <<<"$service_info")
    puid=$(jq -r '.environment.PUID // ""' <<<"$service_info")
    pgid=$(jq -r '.environment.PGID // ""' <<<"$service_info")

    # Create main service directory
    log_info "Creating service directory: $service_dir"
    ensure_directory "$service_dir" "service directory"

    # Create the Tailscale state directory (every pod has a sidecar)
    log_info "Setting up Tailscale directory"
    ensure_directory "$service_dir/tailscale" "Tailscale state directory"

    # Create volume directories
    log_info "Creating volume directories"
    create_volume_directories "$volumes_json" "$puid" "$pgid"

    # The Tailscale gate socket directory.
    #
    # ⚠️ This is NOT redundant with create_volume_directories above. That function
    # iterates `.volumes` only, and the gate socket is carried as its own scalar
    # field (`expose_ts_socket`, parse-service-config.sh) — it never appears in the
    # volumes array. generate-run-template.sh emits
    #     -v "$(pwd)/ts-sock:/var/run/tailscale"
    # for it regardless, so without this the bind source does not exist and the pod
    # dies at start with:
    #     statfs /root/Pods/<svc>/ts-sock: no such file or directory
    #
    # This bit every fresh install that enabled the flag and was repeatedly patched
    # by hand on the box rather than fixed here.
    #
    # Mode 700, and deliberately NOT chowned to PUID/PGID: the socket grants
    # whatever the tailscaled it belongs to can do, so the path stays
    # controller-only (see the mount comment in generate-run-template.sh). Passing
    # it through set_directory_ownership would widen it to the service user.
    local expose_ts_socket
    expose_ts_socket=$(jq -r '.expose_ts_socket // "no"' <<<"$service_info")
    if [[ "$expose_ts_socket" == "yes" ]]; then
        log_info "Creating Tailscale gate socket directory"
        if ! install -d -m 700 "$service_dir/ts-sock"; then
            log_error "Could not create $service_dir/ts-sock — the pod mounts it and will fail to start"
            return 1
        fi
    fi
    
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
            # the filesystem, and never chown into shared/archive data. A
            # :rslave suffix (Tailarr Storage NFS mount) is a bind-propagation
            # flag, not part of the path — strip it too, and skip ownership
            # (the mount is managed remotely; never chown across NFS).
            read_only="no"
            if [[ "$host_path" == *:ro ]]; then
                host_path="${host_path%:ro}"
                read_only="yes"
            elif [[ "$host_path" == *:rslave ]]; then
                host_path="${host_path%:rslave}"
                read_only="yes"   # skip chown: remote-managed mount
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
