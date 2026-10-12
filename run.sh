#!/usr/bin/env bash
set -e

# Navigate to script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if virtual environment exists
if [ ! -f ".venv/bin/activate" ]; then
    echo "[ERROR] Virtual environment (.venv) not found!"
    echo "Please create it first using: python3 -m venv .venv"
    exit 1
fi

# Activate virtual environment
source .venv/bin/activate

FLASK_FLAGS="--debug"

# Check if --production flag was passed
if [ "$1" = "--production" ]; then
    FLASK_FLAGS=""
    echo "Starting Mizalla Flask App in PRODUCTION mode..."
else
    echo "Starting Mizalla Flask App in DEBUG mode..."
fi

exec python3 -m flask --app mizalla run $FLASK_FLAGS
