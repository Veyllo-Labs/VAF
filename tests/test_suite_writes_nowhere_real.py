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

# Every Platform directory accessor, frozen WITH THE MECHANISM THAT GOVERNS IT. Two
# mechanisms exist and neither covers the other: a throwaway HOME (the runner's job) and
# the XDG redirection in conftest (the suite's job). An axis governed by NEITHER is an
# escape route that no amount of discipline at the shell can close - that is the case this
# mapping exists to catch, and it is what "XDG is set independently of HOME" produced.
PLATFORM_DIR_AXES = {
    "home_dir": "HOME",
    "documents_dir": "HOME",
    "downloads_dir": "HOME",
    "get_vaf_output_dir": "HOME",
    "get_research_dir": "HOME",
    "vaf_dir": "HOME",
    "get_context_log_dir": "HOME",
    "config_dir": "XDG_CONFIG_HOME",
    "data_dir": "XDG_DATA_HOME",
    "cache_dir": "XDG_CACHE_HOME",
}

_PROBE = (
    "import json;from vaf.core.platform import Platform;"
    "print(json.dumps({n: str(getattr(Platform, n)()) for n in %r}))"
)


def _resolve_axes(**env_overrides):
    """Ask a FRESH interpreter where each axis lands under a given environment.

    A subprocess rather than monkeypatching, because the question is what the code does
    with an environment - reaching in to patch would measure the patch.
    """
    import json

    env = {k: v for k, v in os.environ.items() if k not in set(CHILD_ENV_TO_CLEAR)}
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE % (list(PLATFORM_DIR_AXES),)],
        cwd=REPO, env=env, capture_output=True, text=True, check=True,
    ).stdout
    return {k: Path(v) for k, v in json.loads(out).items()}


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


def test_every_platform_axis_moves_with_the_mechanism_that_claims_it():
    """MEASURED, not declared: point each mechanism somewhere else and see what follows.

    An axis that moves with neither HOME nor its XDG variable is the dangerous shape - no
    discipline at the shell can redirect it, so every run writes into the developer's real
    store no matter how carefully the suite is invoked. `data_dir` was exactly that for
    anyone who ran with a throwaway HOME and nothing else.
    """
    a = _resolve_axes(HOME="/tmp/vaf-probe-home-a", XDG_CONFIG_HOME="/tmp/vaf-probe-cfg-a",
                      XDG_DATA_HOME="/tmp/vaf-probe-data-a", XDG_CACHE_HOME="/tmp/vaf-probe-cache-a")
    b = _resolve_axes(HOME="/tmp/vaf-probe-home-b", XDG_CONFIG_HOME="/tmp/vaf-probe-cfg-a",
                      XDG_DATA_HOME="/tmp/vaf-probe-data-a", XDG_CACHE_HOME="/tmp/vaf-probe-cache-a")
    c = _resolve_axes(HOME="/tmp/vaf-probe-home-a", XDG_CONFIG_HOME="/tmp/vaf-probe-cfg-b",
                      XDG_DATA_HOME="/tmp/vaf-probe-data-b", XDG_CACHE_HOME="/tmp/vaf-probe-cache-b")

    wrong = []
    for name, mechanism in PLATFORM_DIR_AXES.items():
        follows_home = a[name] != b[name]
        follows_xdg = a[name] != c[name]
        actual = ("HOME" if follows_home else None) or (mechanism if follows_xdg else None)
        if actual != mechanism:
            wrong.append(f"{name}: declared {mechanism}, follows {actual or 'NOTHING'}")
    assert not wrong, (
        f"{wrong}. An axis following NOTHING cannot be isolated at all and will write into "
        f"the developer's store on every run; an axis following a different mechanism means "
        f"conftest redirects a variable that no longer governs it."
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
