#!/bin/bash
# Wrapper to run VAF in the virtual environment without manual activation

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$SCRIPT_DIR"

if [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
    source "$PROJECT_ROOT/venv/bin/activate"
    export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"
    echo "🚀 Starting VAF..."
    python3 -m vaf.main "$@"
else
    echo "❌ Virtual environment not found."
    echo "Please run: ./scripts/setup_mac.sh"
fi
