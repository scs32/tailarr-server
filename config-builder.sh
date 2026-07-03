#!/usr/bin/env bash

# Configuration builder for Podscale
# Assembles the final JSON configuration from user inputs

# Function to build the final configuration JSON.
# Reads the global ENV_VARS/env_keys and VOLUMES/vol_keys populated by
# user-interface.sh. Echoes the path of the temporary config file.
build_configuration() {
    local json_file="$1"
    local service_name="$2"
    local tailscale_choice="$3"
    local auth_key_file="$4"
    local base_path="$5"
    local https_choice="${6:-no}"

    # Get service specification from homelab.js
    local spec
    spec=$(jq -c --arg name "$service_name" \
        '.[] | select(.name == $name)' "$json_file")

    local image ports restart_policy default_network network_mode
    image=$(jq -r '.image' <<<"$spec")
    ports=$(jq -c '.ports // {}' <<<"$spec")
    restart_policy=$(jq -r '.restart_policy' <<<"$spec")
    default_network=$(jq -r '.network_mode // "bridge"' <<<"$spec")

    # Determine network mode based on Tailscale choice
    if [[ "$tailscale_choice" == "yes" ]]; then
        network_mode="service:tailscale-$service_name"
    else
        network_mode="$default_network"
    fi

    # Assemble environment and volume objects with jq (safe quoting)
    local env_json='{}' vol_json='{}' key
    for key in "${env_keys[@]}"; do
        env_json=$(jq -c --arg k "$key" --arg v "${ENV_VARS[$key]}" \
            '. + {($k): $v}' <<<"$env_json")
    done
    for key in "${vol_keys[@]}"; do
        vol_json=$(jq -c --arg k "$key" --arg v "${VOLUMES[$key]}" \
            '. + {($k): $v}' <<<"$vol_json")
    done

    # Write the configuration to a temporary file
    local tmp_config
    tmp_config="$(mktemp)"

    jq -n \
        --arg container "$service_name" \
        --arg image "$image" \
        --arg network_mode "$network_mode" \
        --arg restart_policy "$restart_policy" \
        --arg include_tailscale "$tailscale_choice" \
        --arg include_https "$https_choice" \
        --arg auth_key_file "$auth_key_file" \
        --arg base_path "$base_path" \
        --argjson ports "$ports" \
        --argjson environment "$env_json" \
        --argjson volumes "$vol_json" \
        '{
            container: $container,
            image: $image,
            network_mode: $network_mode,
            ports: $ports,
            restart_policy: $restart_policy,
            include_tailscale: $include_tailscale,
            include_https: $include_https,
            auth_key_file: $auth_key_file,
            base_path: $base_path,
            environment: $environment,
            volumes: $volumes
        }' > "$tmp_config"

    echo "$tmp_config"
}

# Function to save configuration for later use.
# Configs contain the auth key FILE PATH only, never the key itself.
save_configuration() {
    local config_file="$1"
    local service_name="$2"
    local save_dir="${3:-$HOME/Pods/.configs}"

    # Create save directory if it doesn't exist
    mkdir -p "$save_dir"

    # Save with timestamp
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local save_path="$save_dir/${service_name}_${timestamp}.json"

    cp "$config_file" "$save_path"
    echo "Configuration saved to: $save_path"
}

# Function to display configuration summary
display_config_summary() {
    local config_file="$1"

    echo ""
    echo "=== Configuration Summary ==="
    echo "Service: $(jq -r '.container' "$config_file")"
    echo "Image: $(jq -r '.image' "$config_file")"
    echo "Tailscale: $(jq -r '.include_tailscale' "$config_file")"
    echo "Auth key file: $(jq -r '.auth_key_file // "n/a"' "$config_file")"
    echo "Network: $(jq -r '.network_mode' "$config_file")"
    echo "Base Path: $(jq -r '.base_path' "$config_file")"
    echo ""
    echo "Environment Variables:"
    jq -r '.environment | to_entries[] | "  \(.key)=\(.value)"' "$config_file"
    echo ""
    echo "Volume Mappings:"
    jq -r '.volumes | to_entries[] | "  \(.value) → \(.key)"' "$config_file"
    echo "=========================="
}
