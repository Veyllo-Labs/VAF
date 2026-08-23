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
    # A build against a STALE node_modules is the green that is not earned:
    # a Next minor bump entered the lock, this stage kept building with the
    # months-old copy on disk, and the first machine to resolve the lock was a
    # user's (v0.1.0a24, the frontend never came up). Comparing what is
    # INSTALLED against what is LOCKED catches that offline and in a second.
    # Only two signals are used, because a naive set difference is a false-
    # positive machine: the lock legitimately holds ~80 more entries than any
    # one platform installs (os/cpu-gated and optional packages).
    node - <<'NODE'
const fs = require('fs');
const lock = JSON.parse(fs.readFileSync('web/package-lock.json', 'utf8')).packages || {};
let installed;
try {
    installed = JSON.parse(fs.readFileSync('web/node_modules/.package-lock.json', 'utf8')).packages || {};
} catch {
    console.error('ERROR: web/node_modules is missing or was not installed by npm.');
    console.error('Run: (cd web && npm ci)');
    process.exit(1);
}
const problems = [];
for (const [name, meta] of Object.entries(installed)) {
    if (!name) continue;
    const want = lock[name];
    if (!want) { problems.push(`${name}: installed ${meta.version}, absent from the lock`); continue; }
    if (want.version && meta.version && want.version !== meta.version) {
        problems.push(`${name}: installed ${meta.version}, lock wants ${want.version}`);
    }
}
if (problems.length) {
    console.error('ERROR: web/node_modules disagrees with web/package-lock.json:');
    for (const p of problems.slice(0, 20)) console.error('  ' + p);
    if (problems.length > 20) console.error(`  ... and ${problems.length - 20} more`);
    console.error('The build would test something users never get. Run: (cd web && npm ci)');
    process.exit(1);
}
NODE
    # Whatever the BUILD writes has to be committed or known to the updater, or
    # the next `vaf update` aborts on a dirty tree the user never caused (that
    # deadlock has happened twice: package-lock.json, then next-env.d.ts).
    # Compared before against after, so work in progress is not mistaken for it.
    WEB_BEFORE="$(git status --porcelain -- web/)"
    (cd web && npm run build)
    WEB_AFTER="$(git status --porcelain -- web/)"
    WEB_NEW="$(comm -13 <(printf '%s\n' "$WEB_BEFORE" | sort) \
                        <(printf '%s\n' "$WEB_AFTER" | sort))"
    if [ -n "$WEB_NEW" ]; then
        echo "ERROR: the web build itself modified tracked files:" >&2
        echo "$WEB_NEW" >&2
        echo "Commit them, or list them in _SELF_CHURN_PATHS (vaf/cli/cmd/update.py)." >&2
        exit 1
    fi
    echo "    OK"
else
    echo "    SKIPPED (no web/ changes against origin/main)"
fi

echo ""
echo "All checks passed - safe to push."
