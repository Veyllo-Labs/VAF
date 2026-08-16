# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The framework half of updating: reading the cache, checking on demand, and
starting an updater that survives the service it is about to stop.

The check core moved out of `vaf/cli/cmd/update.py` when the web UI needed the
same answers; these tests cover what the CLI never had - a cache read that never
touches the network, an explicit check that always records WHEN it happened, and
a spawn that is detached correctly on each platform.
"""
import json
import platform
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import vaf.core.update_check as uc


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "update_cache.json"
    monkeypatch.setattr(uc, "update_cache_path", lambda: path)
    return path


@pytest.fixture
def breadcrumb(tmp_path, monkeypatch):
    path = tmp_path / "last_update.json"
    monkeypatch.setattr(uc, "last_update_breadcrumb_path", lambda: path)
    return path


# ── reading the cache ────────────────────────────────────────────────────────

def test_reading_the_cache_never_asks_the_network(cache, monkeypatch):
    """The "last checked" line must be free. VAF promises one outbound version
    check at startup and otherwise only when a person presses the button."""
    cache.write_text(json.dumps({"checked_at": "2026-08-01T10:00:00+00:00",
                                 "latest_version": "9.9.9", "relevant": True}))

    def boom(*a, **k):
        raise AssertionError("read_update_cache went to the network")

    monkeypatch.setattr(uc, "resolve_latest_release", boom)
    data = uc.read_update_cache()
    assert data["latest_version"] == "9.9.9"
    assert data["checked_at"].startswith("2026-08-01")


def test_an_absent_or_broken_cache_reads_as_no_answer(cache):
    assert uc.read_update_cache() is None
    cache.write_text("{not json at all")
    assert uc.read_update_cache() is None
    cache.write_text(json.dumps({"latest_version": "9.9.9"}))    # no timestamp
    assert uc.read_update_cache() is None


def test_a_stale_cache_is_still_readable(cache):
    """Stale is information, not an error: the dialog shows the age and lets
    the user decide whether to check again."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cache.write_text(json.dumps({"checked_at": old, "latest_version": "1.0.0",
                                 "relevant": False}))
    assert uc.read_update_cache()["latest_version"] == "1.0.0"


# ── checking on demand ───────────────────────────────────────────────────────

def test_check_now_ignores_a_fresh_cache_and_records_the_new_answer(cache, monkeypatch):
    # An hour old: still FRESH for the 24h TTL, which is the point of the test,
    # but far enough from now that the comparison below does not depend on the
    # clock's resolution. Asserting against a timestamp taken in the same breath
    # failed on Windows, whose clock granularity handed both calls the same
    # microsecond - a true statement about the code, expressed so that a coarse
    # clock could refute it.
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cache.write_text(json.dumps({"checked_at": fresh, "latest_version": "1.0.0",
                                 "relevant": False}))
    monkeypatch.setattr(uc, "resolve_latest_release", lambda pre=None: (
        {"version": "999.0.0", "tag": "v999.0.0", "html_url": "https://x/r",
         "body": "", "prerelease": False}, None))
    result = uc.check_now()
    assert result["latest"] == "999.0.0"
    assert result["relevant"] is True
    assert result["release_url"] == "https://x/r"
    written = json.loads(cache.read_text())
    assert written["latest_version"] == "999.0.0"
    assert datetime.fromisoformat(written["checked_at"]) > datetime.fromisoformat(fresh), \
        "the button must refresh the timestamp"


def test_check_now_reports_an_older_release_as_not_relevant(cache, monkeypatch):
    monkeypatch.setattr(uc, "resolve_latest_release", lambda pre=None: (
        {"version": "0.0.1", "tag": "v0.0.1", "html_url": "", "body": "",
         "prerelease": False}, None))
    assert uc.check_now()["relevant"] is False


def test_check_now_carries_the_reason_when_github_says_nothing(cache, monkeypatch):
    monkeypatch.setattr(uc, "resolve_latest_release", lambda pre=None: (None, "offline"))
    result = uc.check_now()
    assert result["latest"] is None
    assert result["why"] == "offline"
    assert "reach GitHub" in result["message"]
    assert result["current"] == uc.__version__


# ── the unfinished-update breadcrumb ─────────────────────────────────────────

def test_the_breadcrumb_says_an_update_did_not_finish(breadcrumb):
    assert uc.read_last_update() is None
    uc.write_breadcrumb({"target_tag": "v9.9.9", "from_version": "1.0.0"})
    assert uc.read_last_update()["target_tag"] == "v9.9.9"
    uc.clear_breadcrumb()
    assert uc.read_last_update() is None


# ── starting the updater ─────────────────────────────────────────────────────

@pytest.fixture
def spawned(monkeypatch, tmp_path):
    calls = []

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return FakeProc()

    monkeypatch.setattr(uc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(uc, "_server_mode", lambda: False)
    return calls


def test_the_updater_runs_the_cli_update_command(spawned, tmp_path):
    result = uc.spawn_update_process(log_path=tmp_path / "u.log")
    argv = spawned[0]["argv"]
    assert argv[1:] == ["-m", "vaf.main", "update", "--yes"]
    assert result["pid"] == 4242
    assert result["via"] == "popen"


def test_the_updater_is_detached_from_this_process(spawned, tmp_path):
    """It stops the very service that started it. A child in this process group
    would be killed in the middle of its own update."""
    uc.spawn_update_process(log_path=tmp_path / "u.log")
    kwargs = spawned[0]["kwargs"]
    if platform.system() == "Windows":
        assert kwargs.get("creationflags")
    else:
        assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") is subprocess.DEVNULL
    assert kwargs.get("stdout") not in (None,)


def test_the_updater_does_not_stand_in_the_directory_it_replaces(spawned, tmp_path):
    uc.spawn_update_process(log_path=tmp_path / "u.log")
    assert spawned[0]["kwargs"]["cwd"] == str(Path.home())


def test_the_updater_writes_to_a_log_because_it_has_no_console(spawned, tmp_path):
    log = tmp_path / "deep" / "u.log"
    result = uc.spawn_update_process(log_path=log)
    assert result["log"] == str(log)
    assert log.parent.is_dir()


@pytest.mark.skipif(platform.system() != "Linux", reason="systemd is a Linux lane")
def test_server_mode_puts_the_updater_outside_the_service_cgroup(spawned, monkeypatch, tmp_path):
    """The systemd user unit kills its whole control group on stop, and a plain
    child sits in it. systemd-run gives the updater a unit of its own."""
    monkeypatch.setattr(uc, "_server_mode", lambda: True)
    monkeypatch.setattr(uc, "_which", lambda name: "/usr/bin/systemd-run")
    result = uc.spawn_update_process(log_path=tmp_path / "u.log")
    argv = spawned[0]["argv"]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--user" in argv and any(a.startswith("--unit=vaf-update-") for a in argv)
    assert argv[-4:] == ["-m", "vaf.main", "update", "--yes"]
    assert result["via"] == "systemd-run"


@pytest.mark.skipif(platform.system() != "Linux", reason="systemd is a Linux lane")
def test_server_mode_without_systemd_run_refuses_instead_of_being_shot(spawned, monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "_server_mode", lambda: True)
    monkeypatch.setattr(uc, "_which", lambda name: None)
    with pytest.raises(RuntimeError) as excinfo:
        uc.spawn_update_process(log_path=tmp_path / "u.log")
    assert "terminal" in str(excinfo.value)
    assert spawned == [], "nothing may be started when it would be killed"


# ── can this installation update itself at all? ──────────────────────────────

def test_a_package_install_cannot_update_itself_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "_server_mode", lambda: False)
    ok, reason = uc.describe_update_ability(tmp_path)
    assert ok is False
    assert "pip install -U --pre vaf" in reason


def _source_tree(path):
    (path / "vaf").mkdir(exist_ok=True)
    (path / "vaf" / "version.py").write_text("__version__ = '1.0'")
    (path / "requirements.txt").write_text("")
    return path


def test_a_source_checkout_can(tmp_path, monkeypatch):
    _source_tree(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(uc, "_server_mode", lambda: False)
    assert uc.describe_update_ability(tmp_path) == (True, "")


def test_a_downloaded_archive_is_sent_to_the_terminal(tmp_path, monkeypatch):
    """`vaf update` can adopt a ZIP install by turning it into a git checkout,
    but it ASKS first, in a prompt explaining what `git reset --hard` does to
    the source tree. An unattended run answers that prompt with yes, so the
    button would perform a conversion the user was never shown."""
    _source_tree(tmp_path)          # a ZIP ships both files, but no .git
    monkeypatch.setattr(uc, "_server_mode", lambda: False)
    ok, reason = uc.describe_update_ability(tmp_path)
    assert ok is False
    assert "terminal" in reason
    assert "asks before" in reason


@pytest.mark.skipif(platform.system() != "Linux", reason="systemd is a Linux lane")
def test_server_mode_without_systemd_run_says_so_before_the_button_is_pressed(tmp_path, monkeypatch):
    """A button that promises a restart and then fails is worse than a button
    that explains itself up front."""
    (tmp_path / "vaf").mkdir()
    (tmp_path / "vaf" / "version.py").write_text("__version__ = '1.0'")
    (tmp_path / "requirements.txt").write_text("")
    monkeypatch.setattr(uc, "_server_mode", lambda: True)
    monkeypatch.setattr(uc, "_which", lambda name: None)
    ok, reason = uc.describe_update_ability(tmp_path)
    assert ok is False
    assert "terminal" in reason
