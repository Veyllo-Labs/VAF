# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf update` release resolution + prerelease eligibility.

The updater queries the releases LIST endpoint (not /releases/latest, which excludes prereleases)
so an alpha build can see alpha releases. These tests pin: the newest *eligible* release is picked,
prereleases are included only when eligible, drafts are skipped, and eligibility defaults to AUTO
(track prereleases iff the installed build is itself a prerelease) with an explicit override.
"""
import vaf.cli.cmd.update as upd
from vaf.core.config import Config


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _rel(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "html_url": f"https://x/{tag}", "body": "", "prerelease": prerelease, "draft": draft}


def _mock_list(monkeypatch, releases):
    monkeypatch.setattr(upd.requests, "get",
                        lambda url, timeout=5, params=None, headers=None: _Resp(200, releases))


# --- release resolution from the LIST endpoint ----------------------------------------------------

def test_resolve_picks_newest_stable_when_excluding_pre(monkeypatch):
    _mock_list(monkeypatch, [_rel("v9.9.8"), _rel("v9.9.9"), _rel("v10.0.0a1", prerelease=True)])
    rel = upd._resolve_latest_release(include_prereleases=False)
    assert rel["version"] == "9.9.9" and rel["prerelease"] is False    # newest stable, ignores the a1


def test_resolve_includes_prerelease_when_eligible(monkeypatch):
    _mock_list(monkeypatch, [_rel("v9.9.9"), _rel("v10.0.0a1", prerelease=True)])
    rel = upd._resolve_latest_release(include_prereleases=True)
    assert rel["version"] == "10.0.0a1" and rel["prerelease"] is True   # the prerelease is newer + eligible


def test_resolve_orders_prereleases_correctly(monkeypatch):
    _mock_list(monkeypatch, [_rel("v2.6.0a0", prerelease=True), _rel("v2.6.0a2", prerelease=True),
                             _rel("v2.6.0a1", prerelease=True)])
    rel = upd._resolve_latest_release(include_prereleases=True)
    assert rel["version"] == "2.6.0a2"                                  # a0 < a1 < a2


def test_resolve_skips_drafts(monkeypatch):
    _mock_list(monkeypatch, [_rel("v9.9.9", draft=True), _rel("v9.9.8")])
    rel = upd._resolve_latest_release(include_prereleases=False)
    assert rel["version"] == "9.9.8"


def test_resolve_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(upd.requests, "get",
                        lambda url, timeout=5, params=None, headers=None: _Resp(404, []))
    assert upd._resolve_latest_release(False) is None


def test_resolve_offline_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(upd.requests, "get", boom)
    assert upd._resolve_latest_release(False) is None


# --- prerelease eligibility -----------------------------------------------------------------------

def test_eligible_explicit_arg_wins():
    assert upd._eligible_prereleases(True) is True
    assert upd._eligible_prereleases(False) is False


def test_eligible_auto_alpha_install_tracks_prereleases(monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: None))   # config unset -> auto
    monkeypatch.setattr(upd, "__version__", "2.6.0a0")
    assert upd._eligible_prereleases() is True


def test_eligible_auto_stable_install_is_stable_only(monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: None))
    monkeypatch.setattr(upd, "__version__", "2.6.0")
    assert upd._eligible_prereleases() is False


def test_eligible_config_override_beats_auto(monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: False))  # force stable-only
    monkeypatch.setattr(upd, "__version__", "2.6.0a0")                          # even on an alpha build
    assert upd._eligible_prereleases() is False


# --- version comparison + check command (unchanged behavior) --------------------------------------

def test_compare_versions():
    assert upd._compare_versions("2.6.0a0", "2.6.0") < 0   # prerelease < final
    assert upd._compare_versions("2.6.0", "2.6.0") == 0
    assert upd._compare_versions("2.7.0", "2.6.0") > 0


def test_check_command_update_available(monkeypatch):
    from typer.testing import CliRunner
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda pre=None: {
        "version": "999.0.0", "tag": "v999.0.0", "html_url": "https://x/rel", "body": "", "prerelease": False
    })
    result = CliRunner().invoke(upd.app, ["check"])
    assert result.exit_code == 0


def test_check_command_offline_is_graceful(monkeypatch):
    from typer.testing import CliRunner
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda pre=None: None)
    result = CliRunner().invoke(upd.app, ["check"])
    assert result.exit_code == 0
