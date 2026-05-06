#!/usr/bin/env bash
# Run the same checks as CI locally before pushing.
# Usage: ./scripts/ci_check.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# Activate venv
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "ERROR: No venv found at venv/ or .venv/" >&2
    exit 1
fi

# Ensure ruff and pytest are available
pip install --quiet ruff pytest

echo ""
echo "=== 1/3  Ruff lint (errors only) ==="
ruff check . --select=E9,F63,F7,F82 --exclude vaf/tools/coder_templates
echo "    OK"

echo ""
echo "=== 2/3  Ruff lint (warnings) ==="
ruff check . --exit-zero --exclude vaf/tools/coder_templates
echo "    OK (warnings don't block CI)"

echo ""
echo "=== 3/3  Pytest ==="
pytest tests/ --ignore=tests/test_gpu_inference.py -q
echo "    OK"

echo ""
echo "All checks passed — safe to push."
