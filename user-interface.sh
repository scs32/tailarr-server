#!/usr/bin/env bash

# User interface component for HomePod Creator
# Handles all interactive prompts and user input.
#
# Every function that returns a value is called via command substitution,
# so prompts and menus MUST go to stderr; only the resulting value is
# written to stdout. Entering "q" at any prompt returns status 2 (quit).

# Formatting
BOLD=$'\e[1m'
NC=$'\e[0m'

# Print a prompt or message to the terminal (stderr)
ui_msg() {
    printf '%b' "$*" >&2
}

# Function to select a container
select_container() {
    local json_file="$1"
    local raw_names=() normalized_names=() name lc sel idx

    # Load available containers
    mapfile -t raw_names < <(jq -r '.[].name' "$json_file")
    for name in "${raw_names[@]}"; do
        lc="${name,,}"
        normalized_names+=("${lc^}")
    done

    # Display menu
    ui_msg "\n${#normalized_names[@]} Available Containers:\n\n"
    for i in "${!normalized_names[@]}"; do
        printf '%2d) %s\n' $((i+1)) "${normalized_names[i]}" >&2
    done

    # Get selection
    while true; do
        ui_msg "\nSelect a container (1-${#normalized_names[@]}, q to quit): "
        read -r sel
        [[ "$sel" == "q" ]] && return 2
        if [[ "$sel" =~ ^[0-9]+$ ]] && (( sel >= 1 && sel <= ${#normalized_names[@]} )); then
            break
        fi
        ui_msg "Invalid selection.\n"
    done

    idx=$((sel-1))
    ui_msg "\nYou selected: ${normalized_names[idx]} (${raw_names[idx]})\n\n"

    echo "${raw_names[idx]}"
}

# Function to ask yes/no questions
ask_yes_no() {
    local question="$1"
    local default="${2:-yes}"
    local answer=""

    while true; do
        if [[ "$default" == "yes" ]]; then
            ui_msg "$question (${BOLD}yes${NC}/no): "
        else
            ui_msg "$question (${BOLD}no${NC}/yes): "
        fi

        read -r answer
        [[ "$answer" == "q" ]] && return 2
        answer="${answer:-$default}"

        if [[ "$answer" =~ ^(yes|no)$ ]]; then
            break
        fi
        ui_msg "Please answer yes or no.\n"
    done

    ui_msg "\n"
    echo "$answer"
}

# Function to get text input with default
get_input() {
    local prompt="$1"
    local default="$2"
    local value=""

    ui_msg "$prompt (${BOLD}$default${NC}): "
    read -r value
    [[ "$value" == "q" ]] && return 2
    value="${value:-$default}"
    ui_msg "Using: $value\n\n"

    echo "$value"
}

# Function to collect environment variables.
# Populates the global ENV_VARS map and env_keys array.
collect_env_vars() {
    local json_file="$1"
    local service_name="$2"
    local env_json key val default k v

    env_json=$(jq -c --arg name "$service_name" \
        '.[] | select(.name == $name).environment // {}' "$json_file")

    declare -gA ENV_VARS=()
    while IFS=" " read -r k v; do
        [[ -n "$k" ]] && ENV_VARS["$k"]="$v"
    done < <(jq -r 'to_entries[] | "\(.key) \(.value)"' <<<"$env_json")

    # Collect user input for each variable
    for key in "${!ENV_VARS[@]}"; do
        default="${ENV_VARS[$key]}"
        ui_msg "Enter $key (${BOLD}$default${NC}): "
        read -r val
        [[ "$val" == "q" ]] && return 2
        ENV_VARS["$key"]="${val:-$default}"
    done

    env_keys=("${!ENV_VARS[@]}")
}

# Function to collect volume mappings.
# Populates the global VOLUMES map and vol_keys array.
collect_volumes() {
    local json_file="$1"
    local service_name="$2"
    local base_path="$3"
    local container_paths=() cp sub default_host h hp more

    mapfile -t container_paths < <(jq -r --arg name "$service_name" \
        '.[] | select(.name == $name).volumes | to_entries[].value' "$json_file")

    declare -gA VOLUMES=()
    for cp in "${container_paths[@]}"; do
        sub="${cp#/}"
        default_host="$base_path/$service_name/$sub"
        ui_msg "Host path for $cp (${BOLD}$default_host${NC}): "
        read -r h
        [[ "$h" == "q" ]] && return 2
        VOLUMES["$cp"]="${h:-$default_host}"
    done

    # Ask for additional volumes
    ui_msg "Would you like to add more volumes? [${BOLD}no${NC}/yes]: "
    read -r more
    [[ "$more" == "q" ]] && return 2
    more="${more:-no}"

    while [[ "$more" == "yes" ]]; do
        ui_msg "Enter additional container path: "
        read -r cp
        [[ "$cp" == "q" ]] && return 2
        ui_msg "Enter host path for $cp: "
        read -r hp
        [[ "$hp" == "q" ]] && return 2
        VOLUMES["$cp"]="$hp"
        ui_msg "More volumes? [${BOLD}no${NC}/yes]: "
        read -r more
        [[ "$more" == "q" ]] && return 2
        more="${more:-no}"
    done

    vol_keys=("${!VOLUMES[@]}")
}

# Function to display and confirm configuration
confirm_configuration() {
    local config_json="$1"
    local cont

    ui_msg "\nThe following will be used to create a pod:\n\n"
    cat "$config_json" >&2
    ui_msg "\n"

    while true; do
        ui_msg "Would you like to continue? (yes/no): "
        read -r cont
        if [[ "$cont" == "q" || "$cont" == "no" ]]; then
            ui_msg "Aborted.\n"
            return 1
        elif [[ "$cont" == "yes" ]]; then
            return 0
        fi
        ui_msg "Please answer yes or no.\n\n"
    done
}

# Function to resolve the Tailscale auth key file.
# The key itself is never returned or embedded anywhere; generated scripts
# read it from this file at runtime. Echoes the key file path.
get_auth_key_file() {
    local key_file="${1:-$HOME/Pods/.tailscale_authkey}"
    local input_key=""

    if [[ -f "$key_file" ]]; then
        ui_msg "Tailscale auth key file: $key_file\n"
        ui_msg "Press Enter to use it, or paste a new auth key to replace it: "
    else
        ui_msg "No Tailscale auth key file found at $key_file\n"
        ui_msg "Paste your Tailscale auth key (stored there with mode 600): "
    fi

    read -r input_key
    [[ "$input_key" == "q" ]] && return 2

    if [[ -n "$input_key" ]]; then
        mkdir -p "$(dirname "$key_file")"
        printf '%s\n' "$input_key" > "$key_file"
        chmod 600 "$key_file"
        ui_msg "Auth key saved to $key_file\n\n"
    elif [[ ! -f "$key_file" ]]; then
        ui_msg "Error: an auth key is required when Tailscale is enabled.\n"
        return 1
    else
        ui_msg "\n"
    fi

    echo "$key_file"
}
