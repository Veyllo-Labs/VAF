# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared test isolation, plus the one helper eight dispatch tests need.

THE SUITE MUST NOT WRITE INTO THE DEVELOPER'S REAL STORES, and getting that right needs
more than one variable. `Platform` resolves ten directories; seven of them hang off
`Path.home()` and are therefore covered by running with a throwaway HOME. THREE ARE NOT:
`config_dir`, `data_dir` and `cache_dir` read `XDG_CONFIG_HOME` / `XDG_DATA_HOME` /
`XDG_CACHE_HOME`, which desktop sessions set INDEPENDENTLY of HOME. On a machine where
they are set, `HOME=$(mktemp -d) pytest` isolates nothing on those three axes - the runs
go straight into the real store, and both the house rule and the person applying it
believed otherwise.

That is not hypothetical. It produced a false SECURITY finding: 980 rows sat in a
literal-named channel-message store and were reported as user traffic orphaned by a
naming defect. They were suite output - 980 rows carrying two distinct message bodies,
one of them 653 times. The count was correct and answered a question nobody had asked.
Three further synthetic scope directories held ~3600 more rows. Same class as the earlier
incident where suite runs left synthetic security events in the production log and made
the dashboard's "threats blocked today" counter lie.

So all four axes are redirected for the WHOLE session. Tests that need their own log dir
still monkeypatch VAF_LOG_DIR per-test. The counter-proof that this actually holds -
including for directories a future `Platform` axis might add - lives in
`tests/test_suite_writes_nowhere_real.py`; it is the half that makes this docstring more
than a claim.
"""
import pytest

# The environment axes that decide where VAF writes. VAF_LOG_DIR is VAF's own; the rest are
# the ones a throwaway HOME does NOT cover, and WHICH of them applies depends on the
# platform: Linux reads the XDG names, Windows reads %LOCALAPPDATA%/%APPDATA%, and macOS
# puts all three under Library inside HOME (so it needs none of these). All of them are
# redirected everywhere - a name the platform ignores costs nothing, and leaving it out
# costs a whole operating system.
#
# The Windows half was missing until CI said so. The first version of this isolation was
# measured on Linux and frozen as if the mapping were universal, so `data_dir` on Windows
# followed neither redirected mechanism and the suite kept writing into the real
# %LOCALAPPDATA%. Same shape as the count that started this: measured on one platform, read
# as an answer about all of them.
ISOLATED_ENV_AXES = (
    "VAF_LOG_DIR",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",   # Linux
    "LOCALAPPDATA", "APPDATA",                              # Windows
)


@pytest.fixture(autouse=True, scope="session")
def _isolated_config_and_kek(tmp_path_factory):
    """`Config.APP_DIR` and the KEK backends are MACHINE-global. Redirect BOTH.

    The XDG redirection above does not reach either of them: `Config.APP_DIR` is
    `~/.vaf` unless VAF runs in Docker, the KEK file and marker hang off it, and
    `keyring_available()` probes the developer's real OS keyring.

    This is not theoretical - it happened while the keyring was being built, and
    the damage is worth recording because the shape repeats. Key resolution
    ADOPTS a legacy value out of config.json and then BLANKS the plaintext copy.
    Under test, the adoption wrote into a throwaway directory while the blanking
    wrote into the developer's REAL config.json, so one suite run stripped the
    live `secure_store_kek`, the JWT signing secret and an API key out of a
    working installation and put them nowhere. Restoring them needed the
    migration's own pre-keyring backup.

    Two lessons in the fixture, both deliberate: a test must never see the real
    `Config.CONFIG_FILE`, and any code that removes a value from one place after
    writing it to another must be isolated on BOTH sides at once - isolating the
    write alone turns a migration into a deletion.
    """
    import json

    import vaf.core.secure_store as ss
    from vaf.core.config import Config

    root = tmp_path_factory.mktemp("vaf-test-appdir")
    config_file = root / "config.json"
    config_file.write_text("{}", encoding="utf-8")

    orig_app_dir, orig_config_file = Config.APP_DIR, Config.CONFIG_FILE
    Config.APP_DIR, Config.CONFIG_FILE = root, config_file

    # `~/.vaf` itself is the last unredirected axis, and it is the one the
    # SESSION STORE hangs off (SessionManager defaults to Path.home()/".vaf"/
    # "sessions"). Suite runs had been writing synthetic chats straight into the
    # developer's real store for as long as that default existed; it only became
    # visible when those files started being encrypted with a per-test key,
    # leaving 42 unopenable records behind in a live installation. Same lesson as
    # the config axis directly above: redirect the DIRECTORY, not each consumer.
    import vaf.core.session as session_module
    sessions_root = root / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    orig_sessions_dir = session_module.default_sessions_dir
    session_module.default_sessions_dir = lambda: sessions_root

    # Modules imported during collection already hold a SessionManager that
    # captured the real directory in __init__, so patching the seam alone would
    # leave those singletons pointing at the developer's store.
    def _repoint_existing_managers(target):
        import sys
        for mod_name, attr in (("vaf.core.web_server", "session_mgr"),
                               ("vaf.core.session", "_manager")):
            mod = sys.modules.get(mod_name)
            existing = getattr(mod, attr, None) if mod else None
            if existing is not None:
                existing.storage_dir = target
                target.mkdir(parents=True, exist_ok=True)

    _repoint_existing_managers(sessions_root)

    ss._KEYRING_AVAILABLE = False  # never probe or write the real OS keyring
    orig_file, orig_marker = ss._kek_file_path, ss._kek_marker_path
    ss._kek_file_path = lambda: root / "secure_store.kek"
    ss._kek_marker_path = lambda: root / "secure_store.kek.where"
    yield
    session_module.default_sessions_dir = orig_sessions_dir
    _repoint_existing_managers(orig_sessions_dir())
    Config.APP_DIR, Config.CONFIG_FILE = orig_app_dir, orig_config_file
    ss._kek_file_path, ss._kek_marker_path = orig_file, orig_marker
    ss._KEYRING_AVAILABLE = None


@pytest.fixture(autouse=True)
def _isolated_data_keyring(tmp_path, monkeypatch):
    """A data keyring per TEST - same class as the provider-key store below.

    `vaf.core.data_keyring` caches one SecureBlobStore over data_dir; without
    this, a key minted by one test is still there for the next, and a test that
    triggers legacy adoption would blank values in whatever config the test
    happens to see. The pre-migration backup writer is pointed at the test's
    own directory for the same reason.
    """
    import vaf.core.data_keyring as dk
    import vaf.core.secure_store as ss
    from vaf.core.secure_store import SecureBlobStore

    monkeypatch.setattr(dk, "_store", SecureBlobStore("data_keys", tmp_path / "data_keys.enc"))
    # The "a ring existed here" marker belongs to the SAME installation as the
    # ring. It lives beside the config in production so that losing the data
    # directory alone still trips it; per test it has to move with the ring, or
    # the first test to mint a key makes every later one look like a machine
    # whose key store vanished.
    monkeypatch.setattr(dk, "_established_marker", lambda: tmp_path / "data_keys.established")

    def _test_backup() -> None:
        from vaf.core.config import Config
        from pathlib import Path
        src = Path(Config.CONFIG_FILE)
        dst = tmp_path / "config.json.pre-keyring.bak"
        if not dst.exists() and src.exists():
            dst.write_bytes(src.read_bytes())

    monkeypatch.setattr(ss, "ensure_pre_migration_backup", _test_backup)
    monkeypatch.setattr(dk, "ensure_pre_migration_backup", _test_backup)

    # The recovery note is the ONE artifact of the keyring that is deliberately
    # written outside the data directory: `recovery_kit.kit_path()` resolves
    # `Path.home()/"Desktop"`, and HOME is not one of the redirected axes. So
    # minting a key in a test overwrote the developer's REAL
    # Desktop/VAF-BackThisUp.md with a note for a throwaway tmp-dir keyring.
    #
    # That is not clutter, it is destruction: the note is the only copy of the
    # recovery key, the wrap it belongs to lives in the data directory, and the
    # replacement note's key opens neither. It happened on this machine - the
    # real note was overwritten by a suite run and the genuine key was lost, so
    # the machine's own recovery path had to be regenerated from the live data
    # key. A test must never be able to do that again.
    #
    # Redirected here rather than in the session fixture because the note has to
    # follow the per-test ring: a note is only meaningful next to the wrap it
    # opens, and the wrap already moves with `dk._store` above.
    import vaf.core.recovery_kit as rk
    monkeypatch.setattr(rk, "kit_path", lambda: tmp_path / rk.KIT_FILENAME)
    yield


@pytest.fixture(autouse=True)
def _isolated_provider_keys(tmp_path, monkeypatch):
    """A provider-key store per TEST, not per session.

    The store behind `vaf.core.api_keys` is a module-level singleton over one file, and the
    session store dir is shared - so without this, a key written by one test is still there
    for the next. It showed up immediately: a seed that fires only the FIRST time a Veyllo
    key appears stopped firing, because an earlier test had already put one there. Same
    class as the data-dir pollution one level up, one scope smaller.
    """
    import vaf.core.api_keys as api_keys
    from vaf.core.secure_store import SecureBlobStore

    monkeypatch.setattr(
        api_keys, "_store_singleton",
        SecureBlobStore("provider_keys", tmp_path / "provider_keys.enc"),
    )
    yield


@pytest.fixture(autouse=True, scope="session")
def _isolated_store_dirs(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("vaf-test-stores")
    previous = {}
    for var in ISOLATED_ENV_AXES:
        previous[var] = os.environ.get(var)
        target = root / var.lower()
        target.mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(target)
    # Exposed so the counter-proof can assert against the same root rather than
    # recomputing it - a proof that derives its own expectation is not a proof.
    os.environ["VAF_TEST_STORE_ROOT"] = str(root)
    yield root
    for var, old in previous.items():
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old
    os.environ.pop("VAF_TEST_STORE_ROOT", None)


@pytest.fixture(autouse=True, scope="session")
def _isolated_threat_db(tmp_path_factory):
    """The known-bad hash list is MACHINE-global by design, so it is machine-global
    under test too unless something redirects it.

    It hangs off `Platform.vaf_dir()` (`~/.vaf/security/`), which none of the axes
    above reach: HOME is not redirected here, and the fixtures that do redirect
    (`Config.APP_DIR`, the XDG vars) do not feed that helper. Left alone, any test
    that deletes a quarantined skill - the path that LISTS a digest - would append to
    the developer's real list, and a suite that silently adds entries to a live block
    list is the same class of damage the two fixtures above were written for.

    The seam is `threat_db.threat_db_dir` rather than `Platform.vaf_dir`, so the
    redirect cannot leak into unrelated consumers of the home directory.
    """
    import vaf.core.threat_db as tdb

    root = tmp_path_factory.mktemp("vaf-test-threatdb")
    original = tdb.threat_db_dir
    tdb.threat_db_dir = lambda: root
    tdb.reset_cache()
    yield root
    tdb.threat_db_dir = original
    tdb.reset_cache()


# ── duck-typed agents for the dispatch tests ─────────────────────────────────
#
# Eight tests drive `Agent.execute_tool` against a `SimpleNamespace` instead of a real
# Agent, because building one costs a model, a session store and a tool registry. The
# dispatch pipeline lives in vaf/core/tool_dispatch.py and calls back into the chat turn
# through hooks, so a fake now has to answer for those stages too.
#
# They are BOUND FROM THE REAL CLASS rather than stubbed out. Stubbing would be less work
# and would quietly gut every one of those tests: the plumbing cascade, the duplicate
# guard and the post-dispatch hooks ARE the behaviour under measurement, so a fake that
# answers them with no-ops would be a test agreeing with itself.
#
# `execute_tool` itself is in the list because the dispatcher re-enters itself: the
# `multi_tool_use.parallel` wrapper runs each call it carries through the front door again,
# so a fake that can be dispatched must also be re-enterable.
CHAT_STAGES = (
    "execute_tool", "_dispatch_session_id", "_is_channel_turn",
    "_chat_turn_gates", "_chat_session_plumbing", "_chat_post_dispatch",
    "_chat_after_dispatch_bookkeeping", "_ask_user_about_gate",
    "_push_gate_to_websocket", "_run_multi_tool_use",
)


def bind_chat_stages(fake):
    """Give a duck-typed agent the real chat-turn stages, bound to its own state."""
    from vaf.core.agent import Agent

    for name in CHAT_STAGES:
        setattr(fake, name, getattr(Agent, name).__get__(fake))
    return fake
