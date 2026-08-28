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
    # The Windows mechanisms too: leaving the caller's real %LOCALAPPDATA%/%APPDATA% in the
    # child meant a broken isolation delivered into the REAL store on win32 while the scratch
    # profile stayed spotless - the counter-proof shape this comment block warns about.
    "LOCALAPPDATA", "APPDATA",
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
    # darwin follows a SET XDG variable since the store dirs gained that seam - it is the
    # only mechanism test isolation has there (everything else on macOS derives from HOME
    # alone, so before the seam a suite run without a throwaway HOME wrote synthetic rows
    # into the developer's REAL ~/Library store). Unset XDG still means the native
    # Library paths under HOME; this table records what governs when the detector varies
    # a mechanism that is present.
    "config_dir":         {"linux": _XDG_C, "darwin": _XDG_C, "win32": _APP},
    "data_dir":           {"linux": _XDG_D, "darwin": _XDG_D, "win32": _LOCAL},
    "cache_dir":          {"linux": _XDG_K, "darwin": _XDG_K, "win32": _LOCAL},
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

    SCOPE, stated so a green run is not over-read: the assertion covers the WHOLE scratch
    home except `~/.vaf` (one rule on every platform - the earlier version globbed three
    Linux directory names that do not exist on macOS or Windows, so the guard was silently
    green there while proving nothing), but the witness module only exercises the DATA
    axis. Config and cache are guarded for the day something starts writing there, not
    demonstrated today. The redirect for all four axes is covered by the two tests above;
    only delivery on the data axis is proven here.
    """
    scratch_home = tmp_path / "home"
    scratch_home.mkdir()

    env = dict(os.environ)
    for var in CHILD_ENV_TO_CLEAR:
        env.pop(var, None)                 # the child must isolate itself, not inherit it
    env["HOME"] = str(scratch_home)
    # The home mechanism Windows actually reads (see HOME_MECHANISM): without this the
    # child's Path.home() on win32 is the runner's REAL profile and this probe both
    # pollutes it and proves nothing. Harmless on POSIX.
    env["USERPROFILE"] = str(scratch_home)

    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_messaging_connections.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )

    # ONE rule on every platform, instead of globbing three Linux directory names that do
    # not exist on the other two (which made this assertion unfailable there - a guard
    # that was silently green on macOS and Windows while the darwin store had NO
    # isolation seam at all): everything the child leaves in the scratch home is an
    # escape, except `~/.vaf` - that one follows the home mechanism and is SUPPOSED to
    # land here. The conftest isolation must have redirected every store axis (XDG on
    # POSIX - macOS honours a SET variable now - LOCALAPPDATA/APPDATA on Windows) into
    # pytest's temp area, so a clean scratch home IS the proof.
    escaped = sorted(
        p.relative_to(scratch_home).as_posix()
        for p in scratch_home.rglob("*")
        if p.is_file() and ".vaf" not in p.relative_to(scratch_home).parts[:1]
    )
    assert not escaped, (
        f"a test run wrote into the caller's home outside ~/.vaf: {escaped}. A throwaway "
        f"HOME does not redirect the store axes - only the conftest isolation does, and "
        f"without it every suite run mixes synthetic rows into the developer's real "
        f"stores. That has already turned one measurement into a false security finding."
    )


def test_the_darwin_store_dirs_honor_a_set_xdg_variable(monkeypatch):
    """The seam itself, exercised on every platform via a forced branch: the
    macOS store dirs follow an explicitly SET XDG variable - the only
    isolation mechanism they have, since everything else there derives from
    HOME alone - and keep the native Library defaults when it is unset."""
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "is_windows", staticmethod(lambda: False))
    monkeypatch.setattr(Platform, "is_macos", staticmethod(lambda: True))
    monkeypatch.setenv("XDG_DATA_HOME", "/synthetic/data")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/synthetic/cfg")
    monkeypatch.setenv("XDG_CACHE_HOME", "/synthetic/cache")
    assert Platform.data_dir() == Path("/synthetic/data/vaf")
    assert Platform.config_dir() == Path("/synthetic/cfg/vaf")
    assert Platform.cache_dir() == Path("/synthetic/cache/vaf")

    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var)
    home = Path.home()
    assert Platform.data_dir() == home / "Library" / "Application Support" / "vaf"
    assert Platform.config_dir() == home / "Library" / "Application Support" / "vaf"
    assert Platform.cache_dir() == home / "Library" / "Caches" / "vaf"


def test_the_suite_never_sees_the_real_config_file():
    """`Config.APP_DIR` is the axis the XDG redirection does NOT cover.

    A suite run once blanked the live `secure_store_kek`, JWT secret and an API
    key out of the developer's own installation, because key adoption wrote into
    a temp directory while the follow-up blanking wrote into the real
    config.json. Both sides have to be isolated, so this asserts on the axis
    itself rather than on any one consumer.
    """
    from pathlib import Path

    from vaf.core.config import Config

    real_home_vaf = Path.home() / ".vaf"
    assert Path(Config.APP_DIR).resolve() != real_home_vaf.resolve()
    assert real_home_vaf not in Path(Config.CONFIG_FILE).resolve().parents
    Config.set("__isolation_probe__", "x")
    real_config = real_home_vaf / "config.json"
    if real_config.exists():  # a CI runner has no VAF installation at all
        assert "__isolation_probe__" not in real_config.read_text(encoding="utf-8")


def test_the_kek_never_lands_in_the_real_keyring_or_home():
    """A minted KEK in the developer's OS keyring would strand every wrapped DEK."""
    from pathlib import Path

    import vaf.core.secure_store as ss

    assert ss.keyring_available() is False, "tests must not touch the real OS keyring"
    for probe in (ss._kek_file_path(), ss._kek_marker_path()):
        assert (Path.home() / ".vaf") not in Path(probe).resolve().parents


def test_the_session_store_is_not_the_developers_real_one():
    """The axis that leaked 42 synthetic chats into a live installation."""
    from pathlib import Path

    from vaf.core.session import SessionManager, default_sessions_dir

    real = Path.home() / ".vaf" / "sessions"
    assert Path(default_sessions_dir()).resolve() != real.resolve()
    assert Path(SessionManager().storage_dir).resolve() != real.resolve()


def test_minting_a_key_never_touches_the_real_desktop(tmp_path):
    """The recovery note is written OUTSIDE the data directory, by design.

    `recovery_kit.kit_path()` resolves `Path.home()/"Desktop"`, and HOME is not
    one of the redirected axes, so every test that mints a data key used to
    overwrite the developer's real `VAF-BackThisUp.md` with a note for a
    throwaway keyring. The note is the only copy of a recovery key and the
    replacement opens nothing, so that is data loss, not pollution - it cost
    this machine its genuine recovery key before the redirect existed.

    MUTATION: drop the `kit_path` redirect in conftest and this goes red,
    because the note lands on the real Desktop instead of the test's tmp dir.
    """
    from pathlib import Path

    from vaf.core import recovery_kit
    from vaf.core.data_keyring import get_data_key

    real_desktop = Path.home() / "Desktop" / recovery_kit.KIT_FILENAME
    before = real_desktop.stat().st_mtime_ns if real_desktop.exists() else None

    get_data_key("file_store_encryption_key")      # mints, and writes the kit

    after = real_desktop.stat().st_mtime_ns if real_desktop.exists() else None
    assert after == before, f"a test rewrote the real recovery note at {real_desktop}"

    written = recovery_kit.kit_path()
    assert written.exists(), "the kit was not written at all - the test proves nothing"
    assert str(tmp_path) in str(written), f"the kit escaped the test directory: {written}"
