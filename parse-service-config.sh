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
    local include_npm
    local network_mode

    service=$(jq -r '.container' <<<"$config_json")
    image_raw=$(jq -r '.image' <<<"$config_json")
    restart_policy=$(jq -r '.restart_policy' <<<"$config_json")
    auth_key_file=$(jq -r '.auth_key_file // ""' <<<"$config_json")
    base_path=$(jq -r '.base_path' <<<"$config_json")
    include_ts=$(jq -r '.include_tailscale' <<<"$config_json")
    include_npm=$(jq -r '.include_npm' <<<"$config_json")
    local include_https
    include_https=$(jq -r '.include_https // "no"' <<<"$config_json")
    network_mode=$(jq -r '.network_mode // "bridge"' <<<"$config_json")
    
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
    local npm_image
    
    service_image=$(qualify_image "$image_raw")
    ts_image=$(qualify_image "tailscale/tailscale:stable")
    npm_image=$(qualify_image "jc21/nginx-proxy-manager:latest")
    
    # Parse environment variables
    local env_vars_json
    env_vars_json=$(jq -c '.environment // {}' <<<"$config_json")
    
    # Parse volumes
    local volumes_json
    volumes_json=$(jq -c '.volumes // {}' <<<"$config_json")
    
    # Parse ports
    local ports_json
    ports_json=$(jq -c '.ports // {}' <<<"$config_json")
    
    # Determine primary port
    local primary_port
    primary_port=$(jq -r 'keys[0] // ""' <<<"$ports_json")
    
    # Build service directory path
    local service_dir
    service_dir="${base_path}/${service}"
    
    # Create output object
    local service_info
    service_info=$(jq -n \
        --arg service "$service" \
        --arg image "$service_image" \
        --arg ts_image "$ts_image" \
        --arg npm_image "$npm_image" \
        --arg restart_policy "$restart_policy" \
        --arg auth_key_file "$auth_key_file" \
        --arg base_path "$base_path" \
        --arg service_dir "$service_dir" \
        --arg include_ts "$include_ts" \
        --arg include_npm "$include_npm" \
        --arg include_https "$include_https" \
        --arg network_mode "$network_mode" \
        --arg primary_port "$primary_port" \
        --argjson env_vars "$env_vars_json" \
        --argjson volumes "$volumes_json" \
        --argjson ports "$ports_json" \
        '{
            service: $service,
            image: $image,
            ts_image: $ts_image,
            npm_image: $npm_image,
            restart_policy: $restart_policy,
            auth_key_file: $auth_key_file,
            base_path: $base_path,
            service_dir: $service_dir,
            include_tailscale: $include_ts,
            include_npm: $include_npm,
            include_https: $include_https,
            network_mode: $network_mode,
            primary_port: $primary_port,
            environment: $env_vars,
            volumes: $volumes,
            ports: $ports
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
