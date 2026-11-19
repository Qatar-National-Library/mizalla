#!/usr/bin/env bash
set -e

# Navigate to script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check and activate virtual environment (supports Linux/macOS and Windows Git Bash/WSL)
if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "venv/bin/activate"
elif [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
elif [ -f "venv/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source "venv/Scripts/activate"
elif [ -f ".venv/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source ".venv/Scripts/activate"
else
    echo "[ERROR] Virtual environment (venv) not found!"
    echo "Please create it first using: python3 -m venv venv (or python -m venv venv)"
    exit 1
fi

# Detect python executable in active environment
if [ -x "venv/bin/python" ]; then
    PYTHON_CMD="venv/bin/python"
elif [ -x ".venv/bin/python" ]; then
    PYTHON_CMD=".venv/bin/python"
elif [ -f "venv/Scripts/python.exe" ]; then
    PYTHON_CMD="venv/Scripts/python.exe"
elif [ -f ".venv/Scripts/python.exe" ]; then
    PYTHON_CMD=".venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "[ERROR] Python interpreter not found."
    exit 1
fi

# Default configuration
PORT="8080"
THREADS="1"
POSITIONAL_ARGS=()

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        --port=*)
            PORT="${1#*=}"
            shift
            ;;
        -t|--threads)
            THREADS="$2"
            shift 2
            ;;
        --threads=*)
            THREADS="${1#*=}"
            shift
            ;;
        -h|--help)
            echo "Usage: ./run.sh [-p|--port PORT] [-t|--threads THREADS]"
            echo "       ./run.sh [PORT] [THREADS]"
            echo "Options:"
            echo "  -p, --port PORT       Port to bind (default: 8080)"
            echo "  -t, --threads THREADS Number of worker threads (default: 1)"
            exit 0
            ;;
        *)
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
    esac
done

# Support positional parameters: run.sh [PORT] [THREADS]
if [ ${#POSITIONAL_ARGS[@]} -ge 1 ]; then
    PORT="${POSITIONAL_ARGS[0]}"
fi
if [ ${#POSITIONAL_ARGS[@]} -ge 2 ]; then
    THREADS="${POSITIONAL_ARGS[1]}"
fi

echo "Starting Mizalla with Gunicorn on 0.0.0.0:${PORT} (threads: ${THREADS})..."

exec "$PYTHON_CMD" -m gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --threads "${THREADS}" \
    "mizalla:create_app()"
