#!/usr/bin/env bash

# Configuration
LOG_FILE="${LOG_FILE:-./.deployment.log}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"  # DEBUG, INFO, WARN, ERROR
CONSOLE_COLORS=true

# Color codes for console output
if [[ "$CONSOLE_COLORS" == "true" ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'  # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

# Initialize logging
init_logging() {
    # Create log file if it doesn't exist
    touch "$LOG_FILE"
    
    # Write header
    echo "=== Tailarr Deployment Log ===" >> "$LOG_FILE"
    echo "Started at: $(date)" >> "$LOG_FILE"
    echo "======================================" >> "$LOG_FILE"
}

# Core logging function
_log() {
    local level="$1"
    local message="$2"
    local color="${3:-$NC}"
    
    # Format the timestamp
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    # Console output with color
    echo -e "${color}[${level}] ${message}${NC}"
    
    # File output without color
    echo "[$timestamp] [$level] $message" >> "$LOG_FILE"
}

# Logging functions with different levels
log_debug() {
    if [[ "$LOG_LEVEL" == "DEBUG" ]]; then
        _log "DEBUG" "$1" "$BLUE"
    fi
}

log_info() {
    _log "INFO" "$1" "$GREEN"
}

log_warn() {
    _log "WARN" "$1" "$YELLOW"
}

log_error() {
    _log "ERROR" "$1" "$RED" >&2
}

# Special logging functions
log_step() {
    local step="$1"
    local message="$2"
    _log "STEP" "[$step] $message" "$BLUE"
}

log_success() {
    _log "SUCCESS" "$1" "$GREEN"
}

# Progress logging for long operations
log_progress() {
    local current="$1"
    local total="$2"
    local description="${3:-Processing}"
    
    local percentage=$((current * 100 / total))
    echo -ne "\r${GREEN}[PROGRESS]${NC} $description... ${percentage}%"
    
    if [[ $current -eq $total ]]; then
        echo ""  # New line when done
        echo "[$timestamp] [PROGRESS] $description completed (100%)" >> "$LOG_FILE"
    fi
}

# Log command execution
log_command() {
    local command="$1"
    local description="${2:-$command}"
    
    log_debug "Executing: $command"
    echo "[$timestamp] [COMMAND] $command" >> "$LOG_FILE"
    
    # Execute command and capture output
    if output=$(eval "$command" 2>&1); then
        log_debug "Command succeeded"
        if [[ -n "$output" ]]; then
            echo "[$timestamp] [OUTPUT] $output" >> "$LOG_FILE"
        fi
        return 0
    else
        log_error "Command failed: $command"
        echo "[$timestamp] [ERROR OUTPUT] $output" >> "$LOG_FILE"
        return 1
    fi
}

# Log section headers
log_section() {
    local section="$1"
    echo ""
    echo -e "${BLUE}=== $section ===${NC}"
    echo "=== $section ===" >> "$LOG_FILE"
}

# Cleanup function to finalize logs
finalize_logging() {
    echo "======================================" >> "$LOG_FILE"
    echo "Completed at: $(date)" >> "$LOG_FILE"
    echo "" >> "$LOG_FILE"
}

# Initialize logging when sourced
init_logging
