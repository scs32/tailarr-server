#!/usr/bin/env bash

# Parse service configuration from JSON input
parse_service_config() {
    local config_json="$1"
    
    # Extract all necessary fields
    local service
    local image_raw
    local restart_policy
    local auth_key_file
    local base_path
    local include_ts
    local network_mode

    service=$(jq -r '.container' <<<"$config_json")
    image_raw=$(jq -r '.image' <<<"$config_json")
    restart_policy=$(jq -r '.restart_policy' <<<"$config_json")
    auth_key_file=$(jq -r '.auth_key_file // ""' <<<"$config_json")
    base_path=$(jq -r '.base_path' <<<"$config_json")
    local include_https
    local command_str memory_limit
    command_str=$(jq -r '.command // ""' <<<"$config_json")
    memory_limit=$(jq -r '.memory_limit // ""' <<<"$config_json")
    
    # Validate required fields
    if [[ -z "$service" || "$service" == "null" ]]; then
        log_error "Service name not found in configuration"
        return 1
    fi
    
    if [[ -z "$image_raw" || "$image_raw" == "null" ]]; then
        log_error "Service image not found in configuration"
        return 1
    fi
    
    # Process image names
    local service_image
    local ts_image

    service_image=$(qualify_image "$image_raw")
    ts_image=$(qualify_image "tailscale/tailscale:stable")

    # Parse environment variables
    local env_vars_json
    env_vars_json=$(jq -c '.environment // {}' <<<"$config_json")
    
    # Parse volumes. Entries whose HOST path is blank are dropped: the
    # install forms prefill a per-app media path (e.g. /movies) that users
    # blank out when the shared /data mount covers it — rendering those
    # would emit `-v :/movies` and podman dies at start with "host
    # directory cannot be empty".
    local volumes_json
    volumes_json=$(jq -c '.volumes // {} | with_entries(
        select(((.value // "") | rtrimstr(":ro") | gsub("\\s+"; "")) != ""))' \
        <<<"$config_json")
    
    # Parse ports
    local ports_json
    ports_json=$(jq -c '.ports // {}' <<<"$config_json")

    # Shared-folder names (web UI concept) ride along into .config.json so
    # pods can be re-rendered against the shares registry later.
    local shares_json
    shares_json=$(jq -c '.shares // []' <<<"$config_json")

    # Optional catalog-defined app-config seeding: key=value lines to set
    # in config_file inside the container, once, after the first start
    # (e.g. pointing nzbget's DestDir under the shared /data mount when
    # the base image defaults to a path that is mounted nowhere).
    local config_file config_set_json
    config_file=$(jq -r '.config_file // ""' <<<"$config_json")
    config_set_json=$(jq -c '.config_set // {}' <<<"$config_json")
    
    # --- Privileged deploy class (storage appliance ONLY) ---
    # The storage appliance is the one service that needs elevated container
    # flags to run JuiceFS/FUSE + userspace NFS: --device /dev/fuse, --cap-add
    # SYS_ADMIN, and (if Phase 1 measurement proves it needed) --security-opt
    # apparmor=unconfined. These are NEVER caller-supplied through any
    # user-facing path — op_install strips them and only the controller's
    # internal _install_privileged() sets them. This engine boundary is
    # defense in depth: it HARD-REFUSES the privileged fields unless
    # deploy_class == "storage-appliance", and refuses any value outside the
    # fixed allowlist even then.
    local deploy_class devices_json cap_add_json security_opt_json
    deploy_class=$(jq -r '.deploy_class // ""' <<<"$config_json")
    devices_json=$(jq -c '.devices // []' <<<"$config_json")
    cap_add_json=$(jq -c '.cap_add // []' <<<"$config_json")
    security_opt_json=$(jq -c '.security_opt // []' <<<"$config_json")

    # deploy_class must be empty or exactly the storage-appliance sentinel.
    if [[ -n "$deploy_class" && "$deploy_class" != "storage-appliance" ]]; then
        log_error "Unknown deploy_class: $deploy_class"
        return 1
    fi
    # Any privileged field present requires the storage-appliance class.
    local priv_count
    priv_count=$(jq -r '((.devices // []) + (.cap_add // []) + (.security_opt // [])) | length' <<<"$config_json")
    if [[ "$priv_count" != "0" && "$deploy_class" != "storage-appliance" ]]; then
        log_error "devices/cap_add/security_opt require deploy_class=storage-appliance"
        return 1
    fi
    # Even with the class, every value must be on the fixed allowlist.
    if [[ "$deploy_class" == "storage-appliance" ]]; then
        local bad
        bad=$(jq -r '[(.devices // [])[] | select(. != "/dev/fuse")] | length' <<<"$config_json")
        [[ "$bad" == "0" ]] || { log_error "device not allowed for storage-appliance (only /dev/fuse)"; return 1; }
        bad=$(jq -r '[(.cap_add // [])[] | select(. != "SYS_ADMIN" and . != "DAC_READ_SEARCH")] | length' <<<"$config_json")
        [[ "$bad" == "0" ]] || { log_error "cap_add not allowed for storage-appliance (only SYS_ADMIN, DAC_READ_SEARCH)"; return 1; }
        bad=$(jq -r '[(.security_opt // [])[] | select(. != "apparmor=unconfined")] | length' <<<"$config_json")
        [[ "$bad" == "0" ]] || { log_error "security_opt not allowed for storage-appliance (only apparmor=unconfined)"; return 1; }
    fi

    # Determine primary port
    local primary_port
    primary_port=$(jq -r 'keys[0] // ""' <<<"$ports_json")

    # Tailscale is mandatory: every pod gets its own tailnet identity via a
    # sidecar, so the network mode is always the sidecar's namespace. HTTPS
    # via `tailscale serve` is always on whenever there is a port to proxy —
    # there is no plain-HTTP or no-Tailscale mode. The input's
    # include_tailscale / include_https / network_mode fields are ignored.
    include_ts="yes"
    network_mode="service:tailscale-${service}"
    include_https="no"
    [[ -n "$primary_port" ]] && include_https="yes"

    # Public exposure via Tailscale Funnel is opt-in per pod and only
    # meaningful when HTTPS serve exists to be funneled.
    local funnel
    funnel=$(jq -r '.funnel // "no"' <<<"$config_json")
    [[ "$include_https" == "yes" && "$funnel" == "yes" ]] || funnel="no"

    # Tailscale LocalAPI socket exposure (Go identity plane, transport A):
    # opt-in per pod, default OFF. When "yes", the pod's SIDECAR bind-mounts
    # its tailscaled socket dir onto a host path ($(pwd)/ts-sock) so a sibling
    # container (the controller — which already mounts all of $PODS_DIR) can
    # reach that sidecar's LocalAPI socket in-process via the typed
    # tailscale.com/client/local adapter, instead of shelling out
    # `podman exec … tailscale whois/status`. Only the gateway pod carries this
    # flag; every other pod leaves it off, so their run.sh is byte-identical to
    # today. Any value other than the exact
    # string "yes" is treated as off (fail closed — an opt-in privilege mount is
    # never enabled by a typo/garbage value).
    local expose_ts_socket
    expose_ts_socket=$(jq -r '.expose_ts_socket // "no"' <<<"$config_json")
    [[ "$expose_ts_socket" == "yes" ]] || expose_ts_socket="no"

    # Build service directory path
    local service_dir
    service_dir="${base_path}/${service}"
    
    # Create output object
    local service_info
    service_info=$(jq -n \
        --arg service "$service" \
        --arg image "$service_image" \
        --arg ts_image "$ts_image" \
        --arg restart_policy "$restart_policy" \
        --arg auth_key_file "$auth_key_file" \
        --arg base_path "$base_path" \
        --arg service_dir "$service_dir" \
        --arg include_ts "$include_ts" \
        --arg include_https "$include_https" \
        --arg funnel "$funnel" \
        --arg expose_ts_socket "$expose_ts_socket" \
        --arg command "$command_str" \
        --arg memory_limit "$memory_limit" \
        --arg network_mode "$network_mode" \
        --arg primary_port "$primary_port" \
        --arg config_file "$config_file" \
        --arg deploy_class "$deploy_class" \
        --argjson env_vars "$env_vars_json" \
        --argjson volumes "$volumes_json" \
        --argjson ports "$ports_json" \
        --argjson shares "$shares_json" \
        --argjson config_set "$config_set_json" \
        --argjson devices "$devices_json" \
        --argjson cap_add "$cap_add_json" \
        --argjson security_opt "$security_opt_json" \
        '{
            service: $service,
            image: $image,
            ts_image: $ts_image,
            restart_policy: $restart_policy,
            auth_key_file: $auth_key_file,
            base_path: $base_path,
            service_dir: $service_dir,
            include_tailscale: $include_ts,
            include_https: $include_https,
            funnel: $funnel,
            expose_ts_socket: $expose_ts_socket,
            command: $command,
            memory_limit: $memory_limit,
            network_mode: $network_mode,
            primary_port: $primary_port,
            environment: $env_vars,
            volumes: $volumes,
            ports: $ports,
            shares: $shares,
            config_file: $config_file,
            config_set: $config_set,
            deploy_class: $deploy_class,
            devices: $devices,
            cap_add: $cap_add,
            security_opt: $security_opt
        }')
    
    echo "$service_info"
}

# Helper function to qualify image names
qualify_image() {
    local img="${1:-}"
    local prefix="${img%%/*}"
    
    if [[ -n "$img" && "$prefix" != *.* && "$prefix" != *:* ]]; then
        echo "docker.io/$img"
    else
        echo "$img"
    fi
}
