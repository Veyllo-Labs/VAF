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
echo "=== 1/4  Ruff lint (errors only) ==="
ruff check . --select=E9,F63,F7,F82 --exclude vaf/tools/coder_templates
echo "    OK"

echo ""
echo "=== 2/4  Ruff lint (warnings) ==="
ruff check . --exit-zero --exclude vaf/tools/coder_templates
echo "    OK (warnings don't block CI)"

echo ""
echo "=== 3/4  Pytest ==="
# HOME is redirected even here. The suite has twice written into the real user
# store - once destroying the machine's recovery key - and the isolation
# fixtures do not cover every axis. A scratch home costs nothing and removes
# the whole class.
VAF_CI_HOME="$(mktemp -d)"
mkdir -p "$VAF_CI_HOME/.vaf"
HOME="$VAF_CI_HOME" USERPROFILE="$VAF_CI_HOME" \
    pytest tests/ --ignore=tests/test_gpu_inference.py -q
rm -rf "$VAF_CI_HOME"
echo "    OK"

echo ""
echo "=== 4/4  Hostile environment ==="
# The same suite in an environment less generous than this machine: narrow
# output encoding, optional packages hidden, scratch home. Three CI failures in
# a row were environment differences rather than logic errors, and none could
# be reproduced locally because the suite only ever ran in one environment.
# The Windows entry takes 27 minutes to answer; this takes the same 2.5 the
# suite already takes.
python scripts/hostile_env.py
echo "    OK"

echo ""
echo "All checks passed - safe to push."
