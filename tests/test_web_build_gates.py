# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The frontend has to be built by SOMETHING before a user gets it.

For a long time nothing in the pipeline installed or built web/: remote CI was
Python-only, and the local gate ran `npm run build` against whatever
node_modules happened to be on the developer's disk. A Next minor bump entered
the lock, every gate stayed green against a months-old copy, and the first
computer to resolve that lock was a user's Mac - where the build failed on a
type error and the app never came up (v0.1.0a24).

Three separate things had to be true for that to happen, so three things are
pinned here. None of them is provable by running the app; they are properties of
the configuration, which is why they are static assertions.
"""
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_RELEASE = _REPO / ".github" / "workflows" / "release.yml"
_TSCONFIG = _REPO / "web" / "tsconfig.json"


def test_ci_builds_the_frontend_on_all_three_operating_systems():
    """npm resolves optional and platform-gated packages differently per OS, so a
    green Linux install proves nothing about a Mac - which is exactly how this
    shipped. The job must also use `npm ci`: `npm install` would paper over a
    lock that disagrees with package.json instead of failing on it."""
    ci = _CI.read_text(encoding="utf-8")
    assert "web-build:" in ci, "the frontend build job is gone from CI"
    job = ci.split("web-build:", 1)[1].split("\n  lock-sync:", 1)[0]
    for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert os_name in job, f"the web build no longer runs on {os_name}"
    assert "npm ci" in job, "the web build stopped installing from the committed lock"
    assert "npm run build" in job, "the web job installs but no longer builds"
    assert "fail-fast: false" in job, \
        "one red platform would hide the others - the point is to see all three"


def test_the_production_type_check_excludes_files_no_runner_runs():
    """web/ has no test runner, and its one __tests__ file cannot resolve
    @testing-library/react or the jest globals. Next 16.2 filtered such
    diagnostics away inside its own checker; 16.3 runs the tsc CLI, which does
    not - so the file went from invisible to fatal without anyone editing it."""
    cfg = json.loads(_TSCONFIG.read_text(encoding="utf-8"))
    exclude = cfg.get("exclude", [])
    assert "**/__tests__/**" in exclude, "test directories are back inside the build's type check"
    assert "**/*.test.tsx" in exclude and "**/*.test.ts" in exclude, \
        "a *.test.* file outside __tests__ would break the production build again"


def test_the_updater_knows_what_a_build_rewrites():
    """Files the frontend tooling regenerates must be listed as self-churn, or
    the update pre-check reads them as user edits and aborts every future
    update. That deadlock has now happened twice: package-lock.json on a7, and
    next-env.d.ts on a24 (Next 16.3 adds a root-params reference to it)."""
    src = (_REPO / "vaf" / "cli" / "cmd" / "update.py").read_text(encoding="utf-8")
    block = src.split("_SELF_CHURN_PATHS = (", 1)[1].split("\n)", 1)[0]
    for path in ("web/package-lock.json", "web/next-env.d.ts"):
        assert f'"{path}"' in block, f"{path} is no longer treated as self-churn"


def test_the_local_gate_refuses_a_stale_node_modules():
    """The gate that said OK while the tree was stale. It now compares what is
    installed against what is locked, and it must not go back to building
    whatever happens to be on disk."""
    gate = (_REPO / "scripts" / "ci_check.sh").read_text(encoding="utf-8")
    # Anchored on the stage NAME, not its number: inserting a stage renumbers every
    # one after it, and an anchor that carries the count turns that into a red test
    # about nothing (it did, when stage 0 was added).
    assert "Web build ===" in gate, "the web build stage is gone from the local gate"
    web_stage = gate.split("Web build ===", 1)[1]
    assert "node_modules/.package-lock.json" in web_stage, \
        "the local web build no longer checks the installed tree against the lock"
    assert re.search(r"npm run build", web_stage), "the local web build stage is gone"
    assert "git status --porcelain -- web/" in web_stage, \
        "the local gate no longer notices a build that dirties tracked files"


def test_the_release_gate_builds_the_frontend_before_publishing():
    """The push-time job above is not a required status check, so a red frontend can
    reach main and from there a tag. The release gate was Python-only, which left a
    hole the size of the original incident: a tag could be released, published to
    PyPI and offered to `vaf update` without the frontend having been built
    anywhere. A published PyPI version cannot be withdrawn and reused, so this is
    the last point at which the mistake is still free.
    """
    release = _RELEASE.read_text(encoding="utf-8")
    assert "release-web-build:" in release, "the release gate no longer builds the frontend"
    job = release.split("release-web-build:", 1)[1].split("\n  gate-and-release:", 1)[0]
    for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert os_name in job, f"the release web build no longer runs on {os_name}"
    assert "npm ci" in job, "the release web build stopped installing from the committed lock"
    assert "npm run build" in job, "the release job installs but no longer builds"

    # The build has to GATE the release, not merely run beside it.
    gate = release.split("\n  gate-and-release:", 1)[1].split("\n  publish-pypi:", 1)[0]
    assert re.search(r"needs:\s*release-web-build", gate), (
        "gate-and-release does not depend on the frontend build, so a red build would "
        "not stop the release"
    )
