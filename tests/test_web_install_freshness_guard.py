# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The gate that answers "is web/node_modules what the lockfile says", and the lie it took.

The stage exists because a build against a stale node_modules is a green nobody earned:
a Next bump entered the lock, the stage kept building with the months-old copy, and the
first machine to resolve the lock for real was a user's.

It compared web/package-lock.json against web/node_modules/.package-lock.json - a manifest
npm writes - and that manifest can be written without a single package being unpacked.
`npm ci --dry-run` does exactly that. Measured 2026-09-03, self-inflicted during a security
bump whose entire point was getting new bytes onto the disk: the dry run refreshed the
manifest, the two files agreed, the stage went green, and all six bumped packages were
still the old version in node_modules.

So the stage reads the disk now, every package of it, through
vaf.core.node_runtime.lock_drift: the same comparison the WhatsApp bridge's install uses,
so the rule exists once. This file drives the stage's Python out of the shell script
against fixture trees, because the failure it guards is one a reader cannot see by
looking at it.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (ROOT / "scripts" / "ci_check.sh").read_text(encoding="utf-8")


def _guard_source() -> str:
    """The Python program out of the ci_check stage, so the test cannot drift from it."""
    body = re.search(r"python - <<'NPMTREE'\n(.*?)\nNPMTREE\n", _script(), re.S)
    assert body, "the freshness stage is no longer a heredoc named NPMTREE"
    return body.group(1)


def test_stage_8_has_one_implementation():
    """MUTATION: put the Node heredoc back next to it - two copies of one rule, red."""
    script = _script()
    assert "node - <<" not in script
    assert "lock_drift(web, include_dev=True)" in _guard_source()


def _run(tmp_path, lock: dict, manifest, on_disk: dict) -> subprocess.CompletedProcess:
    web = tmp_path / "web"
    (web / "node_modules").mkdir(parents=True)
    (web / "package-lock.json").write_text(json.dumps({"packages": lock}), encoding="utf-8")
    if manifest is not None:
        (web / "node_modules" / ".package-lock.json").write_text(
            json.dumps({"packages": manifest}), encoding="utf-8")
    for name, version in on_disk.items():
        pkg = web / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (tmp_path / "guard.py").write_text(_guard_source(), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))}
    return subprocess.run([sys.executable, "guard.py"], cwd=tmp_path, capture_output=True, text=True, env=env)


def test_a_tree_that_matches_the_lock_passes(tmp_path):
    lock = {"node_modules/left-pad": {"version": "1.3.0"}}
    out = _run(tmp_path, lock, lock, {"node_modules/left-pad": "1.3.0"})
    assert out.returncode == 0, out.stderr


def test_the_manifest_alone_is_not_believed(tmp_path):
    """MUTATION: trust node_modules/.package-lock.json and stop there.

    This is the exact shape a dry run leaves behind: the manifest says the new version,
    the package on disk is the old one, and the two lockfiles agree with each other.
    """
    lock = {"node_modules/left-pad": {"version": "1.3.0"}}
    out = _run(tmp_path, lock, lock, {"node_modules/left-pad": "1.2.0"})
    assert out.returncode == 1
    assert "on disk 1.2.0" in out.stderr and "lock wants 1.3.0" in out.stderr


def test_a_package_the_manifest_claims_and_nobody_unpacked_is_caught(tmp_path):
    """The other half of the same lie: a bump that ADDS a package. The manifest names
    it, the directory was never created."""
    lock = {"node_modules/left-pad": {"version": "1.3.0"}}
    out = _run(tmp_path, lock, lock, {})
    assert out.returncode == 1
    assert "NOT UNPACKED" in out.stderr


def test_a_platform_gated_package_is_not_a_false_alarm(tmp_path):
    """MUTATION: check every entry, gated or not.

    The lock names packages this platform never unpacks - os/cpu/libc-gated and optional.
    Reporting those turns the stage into a false-positive machine, which is exactly why
    it never used a naive set difference in the first place.
    """
    lock = {
        "node_modules/left-pad": {"version": "1.3.0"},
        "node_modules/@img/sharp-win32-arm64": {"version": "0.35.3", "os": ["win32"], "cpu": ["arm64"]},
        "node_modules/fsevents": {"version": "2.3.3", "optional": True},
        "node_modules/@img/sharp-linuxmusl-x64": {"version": "0.35.3", "libc": ["musl"]},
    }
    out = _run(tmp_path, lock, lock, {"node_modules/left-pad": "1.3.0"})
    assert out.returncode == 0, out.stderr


def test_a_missing_manifest_still_says_run_npm_ci(tmp_path):
    out = _run(tmp_path, {"node_modules/left-pad": {"version": "1.3.0"}}, None, {})
    assert out.returncode == 1
    assert "npm ci" in out.stderr


def test_a_package_on_disk_the_lock_does_not_name_is_caught(tmp_path):
    lock = {"node_modules/left-pad": {"version": "1.3.0"}}
    out = _run(tmp_path, lock, None, {"node_modules/left-pad": "1.3.0", "node_modules/stray": "0.1.0"})
    assert out.returncode == 1
    assert "node_modules/stray: installed 0.1.0, absent from the lock" in out.stderr


def test_an_unreadable_lock_fails_the_stage(tmp_path):
    """MUTATION: treat None (cannot tell) as in sync - a broken lock builds green, red."""
    web = tmp_path / "web"
    (web / "node_modules").mkdir(parents=True)
    (web / "package-lock.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "guard.py").write_text(_guard_source(), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "guard.py"], cwd=tmp_path, capture_output=True, text=True, env=env)
    assert out.returncode == 1 and "unreadable" in out.stderr
