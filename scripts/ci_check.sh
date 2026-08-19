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
echo "=== 1/8  Ruff lint (errors only) ==="
ruff check . --select=E9,F63,F7,F82 --exclude vaf/tools/coder_templates
echo "    OK"

echo ""
echo "=== 2/8  Ruff lint (warnings) ==="
ruff check . --exit-zero --exclude vaf/tools/coder_templates
echo "    OK (warnings don't block CI)"

echo ""
echo "=== 3/8  Doc links ==="
python scripts/check_doc_links.py
echo "    OK"

echo ""
echo "=== 4/8  License headers ==="
python scripts/check_license_headers.py
echo "    OK"

echo ""
echo "=== 5/8  Lock file freshness ==="
# The lock-sync job exists ONLY in remote CI, and that asymmetry is how a
# dependency swap once shipped with a stale requirements.lock as the first red
# of a 28-commit push. Regenerating the lock here would need CI's exact pins in
# a throwaway venv; what CAN be checked cheaply is the incident's exact shape:
# requirements.txt changed and requirements.lock did not.
if git rev-parse --verify -q origin/main >/dev/null; then
    REQ_CHANGED=$(git diff --name-only origin/main...HEAD -- requirements.txt; git diff --name-only -- requirements.txt)
    LOCK_CHANGED=$(git diff --name-only origin/main...HEAD -- requirements.lock; git diff --name-only -- requirements.lock)
    if [ -n "$REQ_CHANGED" ] && [ -z "$LOCK_CHANGED" ]; then
        echo "ERROR: requirements.txt changed but requirements.lock did not." >&2
        echo "Regenerate the lock in the SAME commit (recipe in CLAUDE.md Rule 5)." >&2
        exit 1
    fi
    echo "    OK"
else
    echo "    SKIPPED (no origin/main to compare against)"
fi

echo ""
echo "=== 6/8  Pytest ==="
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
echo "=== 7/8  Hostile environment ==="
# The same suite in an environment less generous than this machine: narrow
# output encoding, optional packages hidden, scratch home. Three CI failures in
# a row were environment differences rather than logic errors, and none could
# be reproduced locally because the suite only ever ran in one environment.
# The Windows entry takes 27 minutes to answer; this takes the same 2.5 the
# suite already takes.
python scripts/hostile_env.py
echo "    OK"

echo ""
echo "=== 8/8  Web build ==="
# Only when web/ differs from what origin/main has (or carries uncommitted
# changes): the build takes a minute and proves nothing when nothing changed.
# Without origin/main to compare against, it runs - a skipped stage that might
# have been needed makes the final verdict a lie.
WEB_CHANGED=1
if git rev-parse --verify -q origin/main >/dev/null; then
    WEB_CHANGED="$(git diff --name-only origin/main...HEAD -- web/; git diff --name-only -- web/)"
fi
if [ -n "$WEB_CHANGED" ]; then
    # Building .next under a RUNNING app corrupts the pages it is serving
    # (live incident: every login bounced back to the login screen). Failing
    # loudly beats skipping: a pre-push gate that quietly leaves a stage out
    # reports a green it did not earn.
    if pgrep -f "next-server|vaf\.main tray" >/dev/null 2>&1; then
        echo "ERROR: VAF is running - stop it before the web build (rebuilding" >&2
        echo ".next under the live app breaks the session it is serving)." >&2
        exit 1
    fi
    (cd web && npm run build)
    echo "    OK"
else
    echo "    SKIPPED (no web/ changes against origin/main)"
fi

echo ""
echo "All checks passed - safe to push."
