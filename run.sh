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

FLASK_FLAGS="--debug"

# Check if --production flag was passed
if [ "$1" = "--production" ]; then
    FLASK_FLAGS=""
    echo "Starting Mizalla Flask App in PRODUCTION mode..."
else
    echo "Starting Mizalla Flask App in DEBUG mode..."
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

exec "$PYTHON_CMD" -m flask --app mizalla run $FLASK_FLAGS
