import json
from datetime import datetime, timedelta, timezone

import vaf.cli.cmd.update as upd
from vaf.core.config import Config


def test_notify_disabled_does_not_fetch(monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: False))
    called = {"n": 0}
    monkeypatch.setattr(upd, "_resolve_latest_release",
                        lambda: (called.__setitem__("n", called["n"] + 1), {"version": "9.9.9"})[1])
    upd.maybe_notify_update()
    assert called["n"] == 0  # gate short-circuits before any network call


def test_fresh_cache_skips_fetch(tmp_path, monkeypatch):
    cache = tmp_path / "update_cache.json"
    cache.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latest_version": "9.9.9", "relevant": True,
    }))
    monkeypatch.setattr(upd, "_update_cache_path", lambda: cache)
    called = {"n": 0}
    monkeypatch.setattr(upd, "_resolve_latest_release",
                        lambda: (called.__setitem__("n", called["n"] + 1), None)[1])
    info = upd._cached_or_fetch_latest()
    assert info["version"] == "9.9.9" and info["relevant"] is True
    assert called["n"] == 0  # used the cache, no fetch


def test_stale_cache_fetches_and_recaches(tmp_path, monkeypatch):
    cache = tmp_path / "update_cache.json"
    cache.write_text(json.dumps({
        "checked_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        "latest_version": "0.0.1", "relevant": False,
    }))
    monkeypatch.setattr(upd, "_update_cache_path", lambda: cache)
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda: {"version": "999.0.0", "tag": "v999.0.0"})
    info = upd._cached_or_fetch_latest()
    assert info["version"] == "999.0.0" and info["relevant"] is True
    assert json.loads(cache.read_text())["latest_version"] == "999.0.0"


def test_offline_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "_update_cache_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda: None)
    assert upd._cached_or_fetch_latest() is None
