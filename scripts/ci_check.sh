#!/usr/bin/env bash
# Run the same checks as CI locally before pushing.
# Usage: ./scripts/ci_check.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# The runtime venv is only the source of an interpreter here. The checks run in a venv
# of their own that holds requirements.lock and nothing else (below), and nothing is
# ever installed into this one: it is the venv the running app imports from.
if [ -x "venv/bin/python" ]; then
    RUNTIME_VENV="venv"
elif [ -x ".venv/bin/python" ]; then
    RUNTIME_VENV=".venv"
else
    echo "ERROR: No venv found at venv/ or .venv/" >&2
    exit 1
fi
BASE_PY="$ROOT/$RUNTIME_VENV/bin/python"

echo ""
echo "=== 0/9  The tree under test is the tree that gets pushed ==="
# Everything below tests the WORKING TREE. What reaches CI is what is committed.
# Those are the same thing only when the tree is clean, and when they diverge the
# stages below report a green for a state nobody will ever run.
#
# Live incident 2026-08-30: a suite run green here, then a commit of a SUBSET of
# that tree (a test file without the web/ change it asserts on, edited by another
# session in the same checkout). The push was red on six runners inside seven
# minutes, and no amount of test coverage could have caught it - the checks were
# right, the tree was not the one being shipped. This stage is the missing half.
#
# It also covers the shared-checkout case directly: another session's in-flight
# edits are exactly what makes the tree differ from the commit.
DIRTY="$(git status --porcelain)"
if [ -n "$DIRTY" ]; then
    if [ "${VAF_CI_ALLOW_DIRTY:-}" = "1" ]; then
        echo "    WARNING: tree is dirty and VAF_CI_ALLOW_DIRTY=1 - this run does NOT"
        echo "    speak for what a commit would push:"
        echo "$DIRTY" | sed 's/^/      /'
    else
        echo "ERROR: the working tree is not clean, so this run cannot vouch for what" >&2
        echo "a push would contain. Commit or stash first, then re-run." >&2
        echo "" >&2
        echo "$DIRTY" | sed 's/^/  /' >&2
        echo "" >&2
        echo "Set VAF_CI_ALLOW_DIRTY=1 to run anyway (the verdict is then advisory)." >&2
        exit 1
    fi
else
    echo "    OK"
fi

echo ""
echo "=== The venv the checks run in: requirements.lock ==="
# Push CI installs requirements.lock, hash-checked, on every runner, so the checks here
# run on the same versions: in a venv built from the lock alone, cached per lock and
# Python under ${XDG_CACHE_HOME:-~/.cache}/vaf-ci/. The runtime venv was the wrong place
# twice over. It differed from the lock on 36 of 189 pins and carried 53 packages the
# lock does not name (measured), so a green here said little about CI. And this script
# used to install ruff and pytest into it, under the app that runs from it.
CI_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/vaf-ci"
# The gate's own pip cache, apart from the user's, for the small hash-checked installs (pip,
# setuptools, uv); install_lock.sh installs the lock itself with --no-cache-dir, because pip
# trusts a wheel it built and cached past --require-hashes. Nothing prunes this cache: it
# holds only those few wheels.
GATE_PIP_CACHE="$CI_CACHE/pip"
if [ "${VAF_CI_USE_RUNTIME_VENV:-}" = "1" ]; then
    echo "    WARNING: VAF_CI_USE_RUNTIME_VENV=1 - the checks run in $RUNTIME_VENV/, whose"
    echo "    versions are not the lock's. The verdict is advisory: push CI may disagree."
    # shellcheck disable=SC1091
    source "$RUNTIME_VENV/bin/activate"
else
    PYVER="$("$BASE_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    # The key covers both files the venv is installed from: the lock and the pip that
    # installed it (a raised pip.lock alone builds a new venv).
    LOCK_HASH="$("$BASE_PY" -c 'import hashlib, sys; print(hashlib.sha256(b"".join(open(p, "rb").read() for p in sys.argv[1:])).hexdigest()[:16])' requirements.lock .github/requirements/pip.lock)"
    CHECKOUT_KEY="$("$BASE_PY" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$ROOT")"
    # One venv per lock, Python AND checkout. The editable install below points the venv at
    # one tree, so two checkouts sharing a venv would leave it pointing at whichever ran
    # last, and a child process of the other one that imports vaf without PYTHONPATH would
    # load the wrong tree.
    LOCK_VENV="$CI_CACHE/lock-$PYVER-$LOCK_HASH-$CHECKOUT_KEY"
    # Built in place and marked complete last: a venv cannot be renamed after the build,
    # its scripts (pytest, pip) carry the absolute path of its interpreter. A build that
    # died half way has no marker and is rebuilt from scratch. The owner is written first,
    # so a build in progress is never mistaken for an abandoned venv by the pruning below.
    if [ ! -f "$LOCK_VENV/.complete" ]; then
        echo "    building $LOCK_VENV"
        echo "    (once per lock and checkout: a few minutes, needs the network)"
        rm -rf "$LOCK_VENV"
        mkdir -p "$CI_CACHE"
        # A log of this run's own: two checkouts may build at the same time.
        BUILD_LOG="$(mktemp "$CI_CACHE/build-XXXXXX.log")"
        if ! { "$BASE_PY" -m venv "$LOCK_VENV" \
               && printf '%s\n' "$ROOT" > "$LOCK_VENV/.checkout" \
               && PIP_CACHE_DIR="$GATE_PIP_CACHE" bash scripts/install_lock.sh "$LOCK_VENV/bin/python"; } \
               >"$BUILD_LOG" 2>&1; then
            echo "ERROR: installing requirements.lock into the check venv failed; last lines:" >&2
            tail -5 "$BUILD_LOG" >&2
            echo "Without the network, VAF_CI_USE_RUNTIME_VENV=1 runs the checks in" >&2
            echo "$RUNTIME_VENV/ instead (the verdict is then advisory)." >&2
            rm -rf "$LOCK_VENV" "$BUILD_LOG"
            exit 1
        fi
        rm -f "$BUILD_LOG"
        touch "$LOCK_VENV/.complete"
    else
        echo "    reusing $LOCK_VENV"
    fi
    # Pruning never takes a venv another gate may be running in: another checkout's venv
    # goes only once that checkout is gone, this checkout's venvs of other locks go (one
    # gate per checkout at a time), and a venv with no owner (a build that has not written
    # one yet) only when it is a day old.
    for OLD in "$CI_CACHE"/lock-*; do
        if [ ! -d "$OLD" ] || [ "$OLD" = "$LOCK_VENV" ]; then
            continue
        fi
        OWNER="$(cat "$OLD/.checkout" 2>/dev/null || true)"
        if [ -n "$OWNER" ]; then
            if [ "$OWNER" != "$ROOT" ] && [ -d "$OWNER" ]; then
                continue
            fi
        elif [ -z "$(find "$OLD" -maxdepth 0 -mmin +1440 2>/dev/null)" ]; then
            continue
        fi
        echo "    removing $OLD (its checkout is gone, or it holds an older lock of this one)"
        rm -rf "$OLD"
    done
    # The tree under test, editable and without dependencies, built with the lock's own
    # setuptools (offline, seconds); it keeps entry points and metadata current.
    PIP_CACHE_DIR="$GATE_PIP_CACHE" "$LOCK_VENV/bin/python" -m pip install -q \
        --disable-pip-version-check --no-deps --no-build-isolation -e "$ROOT"
    # Nothing but the lock: a package installed into the check venv by hand fails here.
    if ! "$LOCK_VENV/bin/python" scripts/check_requirements_lock.py --drift; then
        echo "ERROR: the check venv no longer holds exactly requirements.lock." >&2
        echo "Delete $LOCK_VENV and run again to rebuild it." >&2
        exit 1
    fi
    # shellcheck disable=SC1091
    source "$LOCK_VENV/bin/activate"
    echo "    OK"
fi

echo ""
echo "=== 1/9  Ruff lint (errors only) ==="
ruff check . --select=E9,F63,F7,F82 --exclude vaf/tools/coder_templates
echo "    OK"

echo ""
echo "=== 2/9  Ruff lint (warnings) ==="
ruff check . --exit-zero --exclude vaf/tools/coder_templates
echo "    OK (warnings don't block CI)"

echo ""
echo "=== 3/9  Doc links ==="
python scripts/check_doc_links.py
echo "    OK"

echo ""
echo "=== 4/9  License headers ==="
python scripts/check_license_headers.py
echo "    OK"

echo ""
echo "=== 5/9  Lock file freshness ==="
# The lock-sync job in remote CI regenerates requirements.lock and fails on any
# difference, and a dependency swap once shipped with a stale lock as the first
# red of a 28-commit push because nothing here did the same. When an input of a
# lock or a lock itself changed, this stage runs that job's own check: pip and uv
# installed by hash from the same files the job installs (.github/requirements/,
# never copied) in a throwaway venv, the committed locks present so no pin moves
# by itself, and the same commands as the job and the locks' headers. A universal
# resolution does not depend on the interpreter it runs on, so any Python of the
# runtime venv does. Then requirements.lock is checked for every OS and Python of
# the CI matrices.
# requirements.txt includes requirements-headless.txt (-r), so the included
# file is an input of the lock like requirements.txt itself, and so are uv's
# settings in requirements-lock.toml. The small locks of the CI tools (pip, uv,
# the publish tools) live in .github/requirements/ with their inputs.
REQ_FILES="requirements.txt requirements-headless.txt"
LOCK_CONFIG="requirements-lock.toml"
TOOL_LOCKS=".github/requirements"
TOOL_NAMES="pip uv publish"
if git rev-parse --verify -q origin/main >/dev/null; then
    # shellcheck disable=SC2086  # separate path arguments
    REQ_CHANGED=$(git diff --name-only origin/main...HEAD -- $REQ_FILES $LOCK_CONFIG $TOOL_LOCKS; git diff --name-only HEAD -- $REQ_FILES $LOCK_CONFIG $TOOL_LOCKS)
    LOCK_CHANGED=$(git diff --name-only origin/main...HEAD -- requirements.lock; git diff --name-only HEAD -- requirements.lock)
    if [ -z "$REQ_CHANGED" ] && [ -z "$LOCK_CHANGED" ]; then
        echo "    OK (no lock and no input of one changed)"
    else
        LOCK_TMP="$(mktemp -d)"
        mkdir -p "$LOCK_TMP/$TOOL_LOCKS"
        # shellcheck disable=SC2086  # separate path arguments
        cp $REQ_FILES $LOCK_CONFIG requirements.lock "$LOCK_TMP/"
        for TOOL in $TOOL_NAMES; do
            cp "$TOOL_LOCKS/$TOOL.in" "$TOOL_LOCKS/$TOOL.lock" "$LOCK_TMP/$TOOL_LOCKS/"
        done
        echo "    regenerating the locks like the lock-sync job (pip and uv from $TOOL_LOCKS)"
        if ! { "$BASE_PY" -m venv "$LOCK_TMP/venv" \
               && PIP_CACHE_DIR="$GATE_PIP_CACHE" "$LOCK_TMP/venv/bin/python" -m pip install -q --require-hashes --force-reinstall -r "$TOOL_LOCKS/pip.lock" \
               && PIP_CACHE_DIR="$GATE_PIP_CACHE" "$LOCK_TMP/venv/bin/python" -m pip install -q --require-hashes --force-reinstall -r "$TOOL_LOCKS/uv.lock" \
               && (cd "$LOCK_TMP" \
                   && "$LOCK_TMP/venv/bin/uv" pip compile requirements.txt --config-file requirements-lock.toml --universal --generate-hashes --python-version 3.10 -o requirements.lock \
                   && "$LOCK_TMP/venv/bin/uv" pip compile .github/requirements/pip.in --universal --generate-hashes --python-version 3.10 -o .github/requirements/pip.lock \
                   && "$LOCK_TMP/venv/bin/uv" pip compile .github/requirements/uv.in --universal --generate-hashes --python-version 3.10 -o .github/requirements/uv.lock \
                   && "$LOCK_TMP/venv/bin/uv" pip compile .github/requirements/publish.in --universal --generate-hashes --python-version 3.10 -o .github/requirements/publish.lock); } \
               >"$LOCK_TMP/compile.log" 2>&1; then
            echo "ERROR: uv pip compile failed (a resolution conflict, or no network); last lines:" >&2
            tail -5 "$LOCK_TMP/compile.log" >&2
            rm -rf "$LOCK_TMP"
            exit 1
        fi
        if ! { diff -u requirements.lock "$LOCK_TMP/requirements.lock" \
               && (for TOOL in $TOOL_NAMES; do
                       diff -u "$TOOL_LOCKS/$TOOL.lock" "$LOCK_TMP/$TOOL_LOCKS/$TOOL.lock" || exit 1
                   done); } >"$LOCK_TMP/lock.diff"; then
            echo "ERROR: a lock is out of sync with its input:" >&2
            head -40 "$LOCK_TMP/lock.diff" >&2
            echo "Regenerate it with the command in its header and commit it (.github/CONTRIBUTING.md)." >&2
            rm -rf "$LOCK_TMP"
            exit 1
        fi
        rm -rf "$LOCK_TMP"
        echo "    OK (regenerated locks are identical)"
        # The diff cannot see a hand-edited hash (uv keeps the hashes a committed lock has
        # for a pin it keeps, measured); the checker compares every hash with PyPI.
        echo "    checking the locks for every OS and Python of the CI matrices (network)"
        if ! python scripts/check_requirements_lock.py; then
            echo "ERROR: requirements.lock does not install on every CI target (above)." >&2
            exit 1
        fi
        for TOOL in $TOOL_NAMES; do
            if ! python scripts/check_requirements_lock.py --requirements "$TOOL_LOCKS/$TOOL.in" "$TOOL_LOCKS/$TOOL.lock"; then
                echo "ERROR: $TOOL_LOCKS/$TOOL.lock does not check out (above)." >&2
                exit 1
            fi
        done
        echo "    OK"
    fi
else
    echo "    SKIPPED (no origin/main to compare against)"
fi

echo ""
echo "=== 6/9  Pytest ==="
# HOME is redirected even here. The suite has twice written into the real user
# store - once destroying the machine's recovery key. tests/conftest.py now
# moves a run in the account's own home into a home of its own, but only on a
# POSIX developer machine; this scratch home is the outer layer that holds
# wherever that check does not.
VAF_CI_HOME="$(mktemp -d)"
mkdir -p "$VAF_CI_HOME/.vaf"
HOME="$VAF_CI_HOME" USERPROFILE="$VAF_CI_HOME" \
    pytest tests/ --ignore=tests/test_gpu_inference.py -q
rm -rf "$VAF_CI_HOME"
echo "    OK"

echo ""
echo "=== 7/9  Hostile environment ==="
# The same suite in an environment less generous than this machine: narrow
# output encoding, optional packages hidden, a scratch home reached through a
# link and read the Windows way, no docker, the old mail parser, a host that
# reports macOS, tracked files read back with CRLF, and refused local connects
# counted the way a Windows runner pays them (the axes are documented in the
# script). Three CI failures in
# a row were environment differences rather than logic errors, and none could
# be reproduced locally because the suite only ever ran in one environment.
# The Windows entry takes about 90 minutes to answer; this takes the time the
# suite already takes.
python scripts/hostile_env.py
echo "    OK"

echo ""
echo "=== 8/9  Web build ==="
# Only when web/ differs from what origin/main has (or carries uncommitted
# changes): the build takes a minute and proves nothing when nothing changed.
# Without origin/main to compare against, it runs - a skipped stage that might
# have been needed makes the final verdict a lie.
WEB_CHANGED=1
if git rev-parse --verify -q origin/main >/dev/null; then
    WEB_CHANGED="$(git diff --name-only origin/main...HEAD -- web/; git diff --name-only -- web/)"
fi
if [ -n "$WEB_CHANGED" ]; then
    # The lock must describe a tree npm can install from it, the way the CI's
    # `npm ci` demands: a nested peer entry hand-deleted from the lock (next-intl's
    # own @swc/helpers, taken for a leftover of a removed top-level package) passed
    # `npm ci --dry-run` here and failed all three web-build jobs remotely with
    # "Missing: @swc/helpers@0.5.23 from lock file". `npm ls` over the lock alone
    # reports that hole as "invalid"; it reads nothing but the two files, so it runs
    # before the running-app check below and with the app up.
    echo "  lock consistency (npm ls --package-lock-only)"
    if ! (cd web && npm ls --package-lock-only --all >/dev/null 2>&1); then
        echo "ERROR: web/package-lock.json does not describe an installable tree" >&2
        echo "(npm ci on the CI runner refuses it). Regenerate it from a checkout" >&2
        echo "of the previous lock or restore the entry npm ls names:" >&2
        (cd web && npm ls --package-lock-only --all 2>&1 | grep -iE "missing|invalid|error" | head -5 >&2)
        exit 1
    fi
    # Building .next under a RUNNING app corrupts the pages it is serving
    # (live incident: every login bounced back to the login screen). Failing
    # loudly beats skipping: a pre-push gate that quietly leaves a stage out
    # reports a green it did not earn.
    # The match is on the process title (Next retitles itself "next-server
    # (v<version>)") and on exact argv elements for the tray, never on the joined
    # command line: a shell that only waited for or grepped for a server carried
    # the words in its -c payload and failed a whole run here with no server up.
    # The helper names every PID it matched (exit 1). Any other non-zero code
    # (2 unreadable process table, 3 the helper's own error, 127 no python, a
    # signal) means the answer is unknown, and that refuses too.
    RUNNING_APP=0
    python scripts/running_app.py || RUNNING_APP=$?
    if [ "$RUNNING_APP" -eq 1 ]; then
        echo "ERROR: VAF is running - stop it before the web build (rebuilding" >&2
        echo ".next under the live app breaks the session it is serving)." >&2
        echo "The process(es) listed above matched a Next server or the VAF tray." >&2
        exit 1
    elif [ "$RUNNING_APP" -ne 0 ]; then
        echo "ERROR: cannot tell whether VAF is running (the check exited" >&2
        echo "$RUNNING_APP, reason above), so the web build is refused rather than" >&2
        echo "risked against a live app." >&2
        exit 1
    fi
    # A build against a STALE node_modules is the green that is not earned:
    # a Next minor bump entered the lock, this stage kept building with the
    # months-old copy on disk, and the first machine to resolve the lock was a
    # user's (v0.1.0a24, the frontend never came up). Comparing what is
    # INSTALLED against what is LOCKED catches that offline.
    # The comparison is vaf.core.node_runtime.lock_drift, the one the WhatsApp
    # bridge's install uses too. It reads the real package.json of every package
    # on disk and never npm's node_modules/.package-lock.json: `npm ci --dry-run`
    # rewrites that manifest without unpacking anything, so the two lockfiles
    # agreed while six bumped packages were still old on disk (measured during a
    # security bump). Optional and os/cpu/libc-gated entries the platform does
    # not unpack are not reported, or the stage would be a false-positive
    # machine nobody reads twice. Nothing is sampled: the whole web tree takes
    # under 150 ms.
    python - <<'NPMTREE'
import sys
from pathlib import Path

from vaf.core.node_runtime import lock_drift

web = Path("web")
if not (web / "node_modules").is_dir():
    print("ERROR: web/node_modules is missing.", file=sys.stderr)
    print("Run: (cd web && npm ci)", file=sys.stderr)
    sys.exit(1)
drift = lock_drift(web, include_dev=True)
if drift is None:
    print("ERROR: web/package-lock.json is unreadable.", file=sys.stderr)
    sys.exit(1)
problems = []
for d in drift:
    if not d.locked:
        problems.append(f"{d.name}: installed {d.installed}, absent from the lock")
    elif not d.installed:
        problems.append(f"{d.name}: locked {d.locked}, NOT UNPACKED on disk")
    else:
        problems.append(f"{d.name}: on disk {d.installed}, lock wants {d.locked}")
if problems:
    print("ERROR: web/node_modules disagrees with web/package-lock.json:", file=sys.stderr)
    for line in problems[:20]:
        print("  " + line, file=sys.stderr)
    if len(problems) > 20:
        print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
    print("The build would test something users never get. Run: (cd web && npm ci)", file=sys.stderr)
    sys.exit(1)
NPMTREE
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
