#!/usr/bin/env bash

# Global error handling configuration.
# ERROR_LOG_FILE is resolved in setup_error_handler(), NOT at source time:
# the old relative default ("./.error.log") died with the caller's CWD and
# aborted deploys before any output. Callers may pre-set an absolute path.
ERROR_LOG_FILE="${ERROR_LOG_FILE:-}"

# Function to setup error handler
setup_error_handler() {
    # Trap any unhandled errors
    trap 'handle_error $? $LINENO $BASH_LINENO "$BASH_COMMAND" "${FUNCNAME[*]}"' ERR

    # Resolve an absolute error-log path: beside LOG_FILE when that is set,
    # else under tmp. Never abort the deploy over the error log — on failure
    # WARN and continue with console-only error reporting.
    if [[ -z "$ERROR_LOG_FILE" || "$ERROR_LOG_FILE" != /* ]]; then
        if [[ -n "${LOG_FILE:-}" && "${LOG_FILE:-}" == /* ]]; then
            ERROR_LOG_FILE="$(dirname "$LOG_FILE")/.error.log"
        else
            ERROR_LOG_FILE="${TMPDIR:-/tmp}/tailarr-deploy-$$.error.log"
        fi
    fi
    if ! mkdir -p "$(dirname "$ERROR_LOG_FILE")" 2>/dev/null \
            || ! touch "$ERROR_LOG_FILE" 2>/dev/null; then
        ERROR_LOG_FILE="${TMPDIR:-/tmp}/tailarr-deploy-$$.error.log"
        if ! touch "$ERROR_LOG_FILE" 2>/dev/null; then
            echo "[WARN] cannot initialize an error log file; continuing" >&2
            ERROR_LOG_FILE=""
        fi
    fi
    return 0
}

# Main error handler function
handle_error() {
    local exit_code=$1
    local line_number=$2
    local bash_line_number=$3
    local command=$4
    local functions="${5:-}"
    
    # Format the error message
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    # Write to error log (best-effort: never fail inside the error path)
    if [[ -n "$ERROR_LOG_FILE" ]]; then
        {
            echo "==== ERROR OCCURRED ===="
            echo "Timestamp: $timestamp"
            echo "Exit Code: $exit_code"
            echo "Line Number: $line_number"
            echo "Bash Line Number: $bash_line_number"
            echo "Command: $command"
            echo "Function Stack: $functions"
            echo "Script: ${BASH_SOURCE[1]}"
            echo "======================="
        } >> "$ERROR_LOG_FILE" 2>/dev/null || true
    fi

    # Display user-friendly error message
    echo "❌ Error occurred in ${BASH_SOURCE[1]} at line $line_number" >&2
    echo "Command: $command" >&2
    echo "Exit code: $exit_code" >&2
    [[ -n "$ERROR_LOG_FILE" ]] && echo "Full details logged to: $ERROR_LOG_FILE" >&2
    
    # Clean up if needed
    cleanup_on_error
    
    exit $exit_code
}

# Function to handle cleanup on errors
cleanup_on_error() {
    # Remove any partial files created
    rm -f ./.last-config.json.tmp
    
    # You can add more cleanup here as needed
}

# Function for safe execution with error context
safe_execute() {
    local command="$1"
    local description="${2:-executing command}"
    
    echo "Attempting: $description..."
    if ! eval "$command"; then
        echo "Failed: $description" >&2
        return 1
    fi
    echo "Completed: $description"
}

# Function to check required dependencies
check_required_command() {
    local cmd="$1"
    local name="${2:-$cmd}"
    
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: $name is required but not installed" >&2
        return 1
    fi
}

# Function to validate file existence
check_required_file() {
    local file="$1"
    local description="${2:-file}"
    
    if [[ ! -f "$file" ]]; then
        echo "Error: Required $description not found: $file" >&2
        return 1
    fi
}

# Function to validate directory
ensure_directory() {
    local dir="$1"
    local description="${2:-directory}"
    
    if [[ ! -d "$dir" ]]; then
        echo "Creating $description: $dir"
        mkdir -p "$dir" || {
            echo "Failed to create $description: $dir" >&2
            return 1
        }
    fi
}
