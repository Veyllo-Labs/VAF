# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Running the suite must not touch the developer's real stores.

THE RULE WAS HALF A RULE, AND THAT IS THE WHOLE POINT OF THIS FILE. "Run the full suite in
a throwaway HOME before saying it is green locally" was the house answer to test pollution,
and it covers seven of `Platform`'s ten directories. It does not cover `config_dir`,
`data_dir` and `cache_dir`, which read `XDG_CONFIG_HOME` / `XDG_DATA_HOME` /
`XDG_CACHE_HOME` - variables a desktop session sets independently of HOME. Where they are
set, a throwaway HOME redirects nothing on those three axes.

WHAT IT COST. A channel-message store named after a literal username held 980 rows, which
were measured, reported as user traffic stranded by a naming defect, written into a frozen
test, into two planning documents and into a commit message that is now pushed. They were
suite output: 980 rows with TWO distinct message bodies, one of them repeated 653 times,
under synthetic chat ids. Three synthetic scope directories held roughly 3600 more. The
only real data in that store was 116 rows elsewhere. Nobody had lied and nothing was
unmeasured - the number was simply an answer to a question that had not been asked.

TWO ASSERTIONS, AND THE SECOND IS THE ONE THAT PROVES ANYTHING. Checking that the paths
resolve into a temp directory is cheap and catches the obvious regression, but it measures
a computation, not an effect. So the second test RUNS pytest in a subprocess and compares
the real store before and after, file by file. That is the difference between "the address
looks right" and "nothing was delivered there" - the same distinction that made the
sidebar fix assert on the session FILE rather than on the return string.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ISOLATED_ENV_AXES

REPO = Path(__file__).resolve().parents[1]

# Cleared from the CHILD's environment, spelled out rather than reused from
# `ISOLATED_ENV_AXES`. Deriving it from the thing under test was a real bug in this file:
# shrinking `ISOLATED_ENV_AXES` to break the isolation ALSO stopped the child's environment
# being cleared, so the child inherited the developer's real XDG_DATA_HOME, wrote there,
# and left the scratch home spotless. The counter-proof passed while the defect it was
# supposed to catch was actively happening.
CHILD_ENV_TO_CLEAR = (
    "VAF_LOG_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "VAF_TEST_STORE_ROOT",
)

# Every Platform directory accessor, frozen WITH THE MECHANISM THAT GOVERNS IT - PER
# PLATFORM, because the mechanism is not the same one everywhere and the first version of
# this table said it was. Three of the ten are branched: Linux reads the XDG names, Windows
# reads %LOCALAPPDATA%/%APPDATA%, macOS puts them under Library inside HOME. Frozen for all
# three so each CI job checks its own row rather than the one the author happened to be on.
#
# An axis governed by NEITHER of the mechanisms the suite redirects is the dangerous shape -
# no discipline at the shell can close it - and that is exactly what Windows was: measured
# on Linux, declared universal, and the suite kept writing into the real %LOCALAPPDATA%.
_HOME, _XDG_C, _XDG_D, _XDG_K = "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"
_LOCAL, _APP, _PROFILE = "LOCALAPPDATA", "APPDATA", "USERPROFILE"

# THE HOME MECHANISM IS NOT CALLED HOME EVERYWHERE. `Path.home()` goes through
# `os.path.expanduser`, and on Windows that reads USERPROFILE, then HOMEDRIVE+HOMEPATH -
# HOME is not consulted at all: with only HOME set, `expanduser("~")` returns `~` unchanged.
# Verified against `ntpath.expanduser` rather than assumed. So the house rule "run the full
# suite in a throwaway HOME" does nothing whatsoever on Windows, for ANY of the ten axes -
# and that is why seven of them reported "follows NOTHING" there while Linux and macOS were
# green. The rule needs its Windows spelling, and this table is where that is written down.
HOME_MECHANISM = {"linux": _HOME, "darwin": _HOME, "win32": _PROFILE}

PLATFORM_DIR_AXES = {
    "home_dir":           {"linux": _HOME,  "darwin": _HOME, "win32": _PROFILE},
    "documents_dir":      {"linux": _HOME,  "darwin": _HOME, "win32": _PROFILE},
    "downloads_dir":      {"linux": _HOME,  "darwin": _HOME, "win32": _PROFILE},
    "get_vaf_output_dir": {"linux": _HOME,  "darwin": _HOME, "win32": _PROFILE},
    "get_research_dir":   {"linux": _HOME,  "darwin": _HOME, "win32": _PROFILE},
    "vaf_dir":            {"linux": _HOME,  "darwin": _HOME, "win32": _PROFILE},
    "get_context_log_dir": {"linux": _HOME, "darwin": _HOME, "win32": _PROFILE},
    "config_dir":         {"linux": _XDG_C, "darwin": _HOME, "win32": _APP},
    "data_dir":           {"linux": _XDG_D, "darwin": _HOME, "win32": _LOCAL},
    "cache_dir":          {"linux": _XDG_K, "darwin": _HOME, "win32": _LOCAL},
}

# Every mechanism that could govern an axis on any platform. Varied one at a time.
MECHANISMS = (_HOME, _PROFILE, _XDG_C, _XDG_D, _XDG_K, _LOCAL, _APP)


def _platform_key():
    """From `sys.platform`, NOT from `Platform`. Asking the module under test which platform
    it thinks it is on would let a broken branch pick its own expectation."""
    if sys.platform.startswith("win"):
        return "win32"
    return "darwin" if sys.platform == "darwin" else "linux"


_PROBE = (
    "import json;from vaf.core.platform import Platform;"
    "print(json.dumps({n: str(getattr(Platform, n)()) for n in %r}))"
)


def _resolve_axes(env_overrides):
    """Ask a FRESH interpreter where each axis lands under a given environment.

    A subprocess rather than monkeypatching, because the question is what the code does with
    an environment - reaching in to patch would measure the patch.
    """
    import json

    env = {k: v for k, v in os.environ.items() if k not in set(CHILD_ENV_TO_CLEAR)}
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE % (list(PLATFORM_DIR_AXES),)],
        cwd=REPO, env=env, capture_output=True, text=True, check=True,
    ).stdout
    return {k: Path(v) for k, v in json.loads(out).items()}


def _governing_mechanisms(tmp_path):
    """Which mechanism does each axis actually follow? Vary one at a time and watch.

    Mechanical on purpose: the DETECTION knows nothing about platforms, only the frozen
    expectation does. A detector that branched on the platform could agree with a wrong
    branch in the code it is checking.
    """
    base_env = {m: str(tmp_path / f"base-{m.lower()}") for m in MECHANISMS}
    base = _resolve_axes(base_env)
    out = {axis: set() for axis in PLATFORM_DIR_AXES}
    for m in MECHANISMS:
        moved_env = dict(base_env)
        moved_env[m] = str(tmp_path / f"moved-{m.lower()}")
        moved = _resolve_axes(moved_env)
        for axis in PLATFORM_DIR_AXES:
            if moved[axis] != base[axis]:
                out[axis].add(m)
    return out


def test_the_env_axes_actually_point_into_the_session_temp_root():
    """The cheap half: the four variables that decide where VAF writes are redirected.

    Asserted against the root the fixture published rather than against a freshly derived
    one - an expectation the test computes itself would agree with any answer.
    """
    root = os.environ.get("VAF_TEST_STORE_ROOT")
    assert root, "the session isolation fixture did not run"
    for var in ISOLATED_ENV_AXES:
        value = os.environ.get(var)
        assert value, f"{var} is not set; that axis writes into the developer's real store"
        assert Path(value).is_relative_to(Path(root)), (
            f"{var} points at {value!r}, outside the session temp root. A throwaway HOME "
            f"does NOT cover the XDG axes - that is exactly how the suite came to write "
            f"into the real data directory."
        )


def test_the_log_resolver_follows_the_redirected_variable():
    """The variable being redirected is not the same claim as the resolver obeying it.

    `VAF_LOG_DIR` has been redirected for the whole session since long before this file
    existed, and the test above proves the variable is set - which is the stage, not the
    wiring. What decides where a log line actually lands is `get_app_log_dir()`, and its
    SECOND candidate, used whenever the variable is absent, is `<repo>/logs`. That
    directory is not a scratch area: it is where a VAF running from this checkout keeps
    its own logs, including `security_events_<date>.jsonl`, which backs the dashboard.

    So a miss on this axis does not produce stray files, it produces FALSE SECURITY
    READINGS. On 2026-07-31 the owner's Overview showed "1 threat blocked (today)" and
    "1 admin override (today)"; all nine events in the live log that day were suite
    output, traceable line by line to fixtures (`bad_skill`, the `danger` skill,
    `internal.example` with `user=u`, the LAN addresses from the auth tests). The suite
    as it stands writes nothing there - measured on three full runs - and this assertion
    is what says so on every run instead of once.
    """
    from vaf.core.log_helper import get_app_log_dir

    root = os.environ.get("VAF_TEST_STORE_ROOT")
    assert root, "the session isolation fixture did not run"
    resolved = get_app_log_dir()
    assert resolved.is_relative_to(Path(root)), (
        f"get_app_log_dir() resolves to {resolved!r}, outside the session temp root. "
        f"Its fallback is the repository's own logs/ directory, which a VAF started from "
        f"this checkout reads as live security data."
    )
    assert not resolved.is_relative_to(REPO / "logs"), "the suite is writing into the live log directory"


def test_every_platform_axis_moves_with_the_mechanism_that_claims_it(tmp_path):
    """MEASURED, not declared: move each mechanism in turn and see what follows.

    An axis that follows NONE of the mechanisms the suite redirects is the dangerous shape -
    no discipline at the shell can close it, so every run writes into the developer's real
    store however carefully the suite is invoked. That was `data_dir` on Windows, which
    follows %LOCALAPPDATA%: the first version of this table was measured on Linux, declared
    universal, and CI on two other platforms is what said otherwise.
    """
    platform_key = _platform_key()
    governing = _governing_mechanisms(tmp_path)

    wrong = []
    for axis, per_platform in PLATFORM_DIR_AXES.items():
        expected = per_platform[platform_key]
        actual = governing[axis]
        if actual != {expected}:
            wrong.append(f"{axis}: declared {expected}, follows "
                         f"{sorted(actual) if actual else 'NOTHING'}")
    assert not wrong, (
        f"on {platform_key}: {wrong}. An axis following NOTHING cannot be isolated at all; "
        f"one following something else means conftest redirects a variable that does not "
        f"govern it here. Both leave the suite writing into the developer's real store."
    )


def test_every_governing_mechanism_is_one_the_suite_redirects(tmp_path):
    """The bridge between the table and the isolation, which nothing checked before.

    The table can be right and the isolation still incomplete - that is precisely what
    Windows was. Whatever actually governs an axis here has to appear in
    `conftest.ISOLATED_ENV_AXES`, or a correct table sits next to a suite that pollutes.
    """
    redirected = set(ISOLATED_ENV_AXES) | set(HOME_MECHANISM.values())   # the home axis is the runner's job
    missing = sorted({m for ms in _governing_mechanisms(tmp_path).values() for m in ms} - redirected)
    assert not missing, (
        f"mechanism(s) govern a VAF directory on {_platform_key()} and are not redirected: "
        f"{missing}. Add them to conftest.ISOLATED_ENV_AXES."
    )


def test_the_isolation_covers_EVERY_platform_in_the_table_not_just_this_one():
    """The same bridge, asked of all three columns - and the half that had to exist.

    The test above only ever sees the platform it runs on, so a Windows-only gap is
    invisible to a developer on Linux and only CI can find it. That is exactly how the gap
    got in: measured on one platform, frozen as universal, and no local run could have said
    otherwise. Checking the whole table costs nothing and moves the discovery from a red CI
    job to the machine where the change is written.
    """
    redirected = set(ISOLATED_ENV_AXES) | set(HOME_MECHANISM.values())
    missing = sorted({
        f"{axis} on {plat} -> {mech}"
        for axis, per_platform in PLATFORM_DIR_AXES.items()
        for plat, mech in per_platform.items()
        if mech not in redirected
    })
    assert not missing, (
        f"the table names mechanism(s) the suite does not redirect: {missing}. On that "
        f"platform every run writes into the developer's real store, and no test on any "
        f"other platform would notice."
    )


def test_the_frozen_axis_list_still_matches_platform():
    """A new accessor is a new escape route, so it has to be classified deliberately."""
    from vaf.core.platform import Platform

    live = {n for n in dir(Platform)
            if n.endswith("_dir") and callable(getattr(Platform, n, None))}
    assert not (set(PLATFORM_DIR_AXES) - live), (
        f"frozen axis gone from Platform: {sorted(set(PLATFORM_DIR_AXES) - live)}"
    )
    assert not (live - set(PLATFORM_DIR_AXES)), (
        f"Platform grew a directory accessor nobody classified: {sorted(live - set(PLATFORM_DIR_AXES))}. "
        f"Say which mechanism isolates it - HOME or an XDG variable - or that it needs none."
    )


def test_a_real_pytest_run_delivers_nothing_into_the_callers_home(tmp_path):
    """THE proof, and the only test here that measures an EFFECT rather than a computation.

    Runs `tests/test_messaging_connections.py` in a subprocess whose HOME is a scratch
    directory and whose XDG variables are UNSET - the shape of a developer machine that has
    no XDG overrides. If the isolation holds, the child's own conftest redirects the data
    directory into pytest's temp area and the scratch home stays untouched. If it does not,
    `scopes/u1/channel_messages.db` lands under it.

    THAT MODULE IS NOT A GUESS. The first version of this test drove
    `tests/test_send_to_user.py` because a grep showed it referencing the channel store -
    and that module MONKEYPATCHES `append_message`, so it writes nothing and the test was
    green with the isolation removed. It measured nothing while reading as proof, which is
    the failure mode this whole file exists to document. The module below was found by
    running candidates with the isolation disabled and watching which one actually produced
    a file.

    Asserted against a scratch HOME rather than the developer's real store on purpose: the
    property is "writes land where the isolation says", and proving it must not require
    dirtying the thing being protected.

    SCOPE, stated so a green run is not over-read: the assertion covers all three XDG
    locations, but the witness module only exercises the DATA one. Config and cache are
    guarded for the day something starts writing there, not demonstrated today. The
    redirect for all four axes is covered by the two tests above; only delivery on the data
    axis is proven here.
    """
    scratch_home = tmp_path / "home"
    scratch_home.mkdir()

    env = dict(os.environ)
    for var in CHILD_ENV_TO_CLEAR:
        env.pop(var, None)                 # the child must isolate itself, not inherit it
    env["HOME"] = str(scratch_home)

    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_messaging_connections.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )

    # Only the XDG-governed default locations. `~/.vaf` follows HOME and is SUPPOSED to be
    # written here - that is the other mechanism doing its job, not a leak.
    escaped = sorted(
        str(p.relative_to(scratch_home))
        for base in (".local/share", ".config", ".cache")
        for p in (scratch_home / base).rglob("*") if p.is_file()
    )
    assert not escaped, (
        f"a test run wrote into the caller's home on an XDG axis: {escaped}. A throwaway "
        f"HOME does not redirect those - only the conftest isolation does, and without it "
        f"every suite run mixes synthetic rows into the developer's real stores. That has "
        f"already turned one measurement into a false security finding."
    )
