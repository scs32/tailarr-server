#!/usr/bin/env bash

# Configuration.
# LOG_FILE is resolved by init_logging(), NOT at source time: the old
# relative default ("./.deployment.log") plus a source-time `touch` meant
# the whole deploy died before any output whenever the caller's CWD was
# invalid (seen in production right after a controller restart). Callers
# may pre-set an absolute LOG_FILE via the environment.
LOG_FILE="${LOG_FILE:-}"
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

# Append a line to the log file. Drops silently when file logging is
# unavailable — console output is the deploy's real interface; the file is
# best-effort and must never abort a deploy (create.sh runs under set -e).
_log_to_file() {
    [[ -n "$LOG_FILE" ]] || return 0
    echo "$1" >> "$LOG_FILE" 2>/dev/null || true
}

# Initialize logging. Optional $1 = directory to hold the log (the service
# dir, once known). Resolves LOG_FILE to an absolute, writable path with a
# tmp fallback; on total failure it WARNs and disables file logging.
# Always returns 0 — a deploy must never die over its log file.
init_logging() {
    local dir="${1:-}"
    if [[ -z "$LOG_FILE" && -n "$dir" ]]; then
        LOG_FILE="$dir/.deployment.log"
    fi
    if [[ -n "$LOG_FILE" && "$LOG_FILE" != /* ]]; then
        # Relative path (legacy callers): anchor it to a valid CWD, else fall
        # through to the tmp default below.
        if pwd -P >/dev/null 2>&1 && [[ -w "$(pwd -P)" ]]; then
            LOG_FILE="$(pwd -P)/$LOG_FILE"
        else
            LOG_FILE=""
        fi
    fi
    if [[ -z "$LOG_FILE" ]]; then
        LOG_FILE="${TMPDIR:-/tmp}/tailarr-deploy-$$.log"
    fi
    if ! mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null \
            || ! touch "$LOG_FILE" 2>/dev/null; then
        LOG_FILE="${TMPDIR:-/tmp}/tailarr-deploy-$$.log"
        if ! touch "$LOG_FILE" 2>/dev/null; then
            echo -e "${YELLOW}[WARN] cannot initialize a deployment log file;" \
                "continuing with console output only${NC}" >&2
            LOG_FILE=""
            return 0
        fi
    fi
    _log_to_file "=== Tailarr Deployment Log ==="
    _log_to_file "Started at: $(date)"
    _log_to_file "======================================"
    return 0
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
    _log_to_file "[$timestamp] [$level] $message"
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
        _log_to_file "[$(date '+%Y-%m-%d %H:%M:%S')] [PROGRESS] $description completed (100%)"
    fi
}

# Log command execution
log_command() {
    local command="$1"
    local description="${2:-$command}"

    log_debug "Executing: $command"
    _log_to_file "[$(date '+%Y-%m-%d %H:%M:%S')] [COMMAND] $command"

    # Execute command and capture output
    if output=$(eval "$command" 2>&1); then
        log_debug "Command succeeded"
        if [[ -n "$output" ]]; then
            _log_to_file "[$(date '+%Y-%m-%d %H:%M:%S')] [OUTPUT] $output"
        fi
        return 0
    else
        log_error "Command failed: $command"
        _log_to_file "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR OUTPUT] $output"
        return 1
    fi
}

# Log section headers
log_section() {
    local section="$1"
    echo ""
    echo -e "${BLUE}=== $section ===${NC}"
    _log_to_file "=== $section ==="
}

# Cleanup function to finalize logs
finalize_logging() {
    _log_to_file "======================================"
    _log_to_file "Completed at: $(date)"
    _log_to_file ""
}
