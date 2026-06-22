import vaf.cli.cmd.update as upd


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_resolve_latest_success(monkeypatch):
    monkeypatch.setattr(
        upd.requests, "get",
        lambda url, timeout=5: _Resp(200, {
            "tag_name": "v9.9.9", "html_url": "https://x/rel", "body": "notes", "prerelease": False
        }),
    )
    rel = upd._resolve_latest_release()
    assert rel["tag"] == "v9.9.9"
    assert rel["version"] == "9.9.9"
    assert rel["prerelease"] is False


def test_resolve_latest_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(upd.requests, "get", lambda url, timeout=5: _Resp(404, {}))
    assert upd._resolve_latest_release() is None


def test_resolve_latest_offline_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(upd.requests, "get", boom)
    assert upd._resolve_latest_release() is None


def test_compare_versions():
    assert upd._compare_versions("2.6.0a0", "2.6.0") < 0   # prerelease < final
    assert upd._compare_versions("2.6.0", "2.6.0") == 0
    assert upd._compare_versions("2.7.0", "2.6.0") > 0


def test_check_command_update_available(monkeypatch):
    from typer.testing import CliRunner
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda: {
        "version": "999.0.0", "tag": "v999.0.0", "html_url": "https://x/rel", "body": "", "prerelease": False
    })
    result = CliRunner().invoke(upd.app, ["check"])
    assert result.exit_code == 0


def test_check_command_offline_is_graceful(monkeypatch):
    from typer.testing import CliRunner
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda: None)
    result = CliRunner().invoke(upd.app, ["check"])
    assert result.exit_code == 0
