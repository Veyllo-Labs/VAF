#!/usr/bin/env bash
# Install requirements.lock into one interpreter, with every file hash-checked.
# Usage: bash scripts/install_lock.sh [python]   (default: the python on PATH)
#
# Every gating CI lane (.github/workflows/_test.yml, ci.yml narrow-console, the release
# gate) and the check venv of scripts/ci_check.sh run this, so the steps live in one place.
# The project itself is not installed here: each caller installs it editable with --no-deps
# and then proves the result with `check_requirements_lock.py --drift`.
#
# Why each step, in order:
#   1. pip itself comes by hash (.github/requirements/pip.lock): the tool that checks every
#      other hash must not be the one unchecked download, and PyPI lets a release gain new
#      files after the fact, so a version pin alone is not an artefact pin.
#   2. setuptools comes from the lock, by hash, before anything is built. The lock holds a
#      release with no wheel (proxy-tools, built from its sdist on every target), and pip
#      builds an sdist in an isolated environment that it fills from PyPI with no pin and no
#      hash (measured: setuptools 84.2.1 there while the lock pins 81.0.0). With the locked
#      setuptools installed, step 3 builds without isolation and downloads nothing unlocked.
#      A future sdist that needs another build backend then fails here, which is the point.
#   3. The lock, every file by hash, built with the locked setuptools.
#
# --force-reinstall on every step: pip calls a requirement whose version is already
# installed "satisfied" and checks no hash at all (measured: an installed pip 26.1.2 passed
# a lock line carrying a wrong hash, exit 0). Forcing it makes pip fetch and verify the file.
#
# --no-cache-dir on step 3: pip trusts a wheel it built and cached earlier past
# --require-hashes (measured with a rewritten cached proxy-tools wheel), and a cache can be
# shared with a lane or a checkout that built it differently. Steps 1 and 2 install wheels
# only, whose cached downloads pip checks against the hash, so they keep the cache.
#
# No step may swallow a failure: a setuptools step that failed quietly would let step 3
# build with whatever setuptools the interpreter has (tests/test_python_ci_lock.py).
set -euo pipefail
PY="${1:-python}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/requirements.lock"

"$PY" -m pip install --disable-pip-version-check --require-hashes --force-reinstall \
    -r "$ROOT/.github/requirements/pip.lock"

SETUPTOOLS="$(mktemp "${TMPDIR:-/tmp}/setuptools-lock.XXXXXX")"
trap 'rm -f "$SETUPTOOLS"' EXIT
awk '/^setuptools==/{p=1; print; next} p && /^[^ ]/{exit} p{print}' "$LOCK" > "$SETUPTOOLS"
if ! grep -q '^setuptools==' "$SETUPTOOLS"; then
    echo "ERROR: requirements.lock pins no setuptools; the sdist build would fetch one unpinned" >&2
    exit 1
fi
"$PY" -m pip install --disable-pip-version-check --require-hashes --force-reinstall --no-deps \
    -r "$SETUPTOOLS"

"$PY" -m pip install --disable-pip-version-check --require-hashes --force-reinstall --no-cache-dir \
    --no-build-isolation -r "$LOCK"
