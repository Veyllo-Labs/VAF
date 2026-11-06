# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf devices`, `vaf relay`, the `app_link` row of `vaf health`, the two doctor findings and
the warning of `vaf server ca renew`."""
import pytest
from typer.testing import CliRunner

from app_link_client import paired_device
from app_link_env import SCOPE_A, SCOPE_B, app_link_home  # noqa: F401  (fixture)
from vaf.cli.cmd import devices as devices_cmd
from vaf.cli.cmd import relay as relay_cmd
from vaf.core import health
from vaf.core.app_link import bridge, devices

MEMBER = "0f8fad5b-d9cb-469f-a165-70867728950e"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh():
    bridge.reset()
    yield
    bridge.reset()


def _offline(monkeypatch, module=devices_cmd):
    monkeypatch.setattr(module, "ask_running_vaf", lambda *a, **k: None)


def test_list_shows_every_account(app_link_home, monkeypatch):
    _offline(monkeypatch)
    paired_device(SCOPE_A, model="Pixel 9")
    paired_device(SCOPE_B, username="bob", model="iPhone 16", relay_member=MEMBER)
    out = runner.invoke(devices_cmd.app, ["list"])
    assert out.exit_code == 0, out.output
    assert "alice" in out.output and "bob" in out.output and "iPhone 16" in out.output
    assert "VAF is not running" in out.output
    assert out.output.isascii()


def test_remove_offline(app_link_home, monkeypatch):
    _offline(monkeypatch)
    row = paired_device(SCOPE_A, relay_member=MEMBER)[0]
    out = runner.invoke(devices_cmd.app, ["remove", row["device_id"][:8]])
    assert out.exit_code == 0, out.output
    assert devices.get(row["device_id"]) is None
    assert devices.tombstones() == [MEMBER]
    assert app_link_home.kinds()[-1] == "channel_unpaired"


def test_remove_goes_to_the_running_vaf_first(app_link_home, monkeypatch):
    row = paired_device(SCOPE_A)[0]
    asked = []
    monkeypatch.setattr(devices_cmd, "ask_running_vaf",
                        lambda method, path, json=None: asked.append((method, path)) or (200, {"removed": True}))
    out = runner.invoke(devices_cmd.app, ["remove", row["device_id"]])
    assert out.exit_code == 0, out.output
    assert asked == [("DELETE", f"/api/app-link/devices/{row['device_id']}")]
    assert devices.get(row["device_id"]) is not None, "the running VAF removes it, not the CLI"
    out = runner.invoke(devices_cmd.app, ["remove", "nothing"])
    assert out.exit_code == 1


def test_relay_off_and_on_without_a_running_vaf(app_link_home, monkeypatch):
    _offline(monkeypatch, relay_cmd)
    from vaf.core.app_link import settings
    assert runner.invoke(relay_cmd.app, ["off"]).exit_code == 0
    assert settings()["relay_enabled"] is False
    out = runner.invoke(relay_cmd.app, ["status"])
    assert "Relay:            off" in out.output and "never reachable through the relay" in out.output
    assert runner.invoke(relay_cmd.app, ["on"]).exit_code == 0
    assert settings()["relay_enabled"] is True
    from vaf.core.channel_secrets import set_channel_secret
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    app_link_home.set_block(relay_instance_id="inst-1")
    out = runner.invoke(relay_cmd.app, ["dissolve"], input="n\n")
    assert out.exit_code == 0 and "Nothing was changed" in out.output


def test_health_row(app_link_home):
    assert health.app_link_check({"state": "idle"}) is None
    paired_device(SCOPE_A, relay_member=MEMBER)
    ok = health.app_link_check({"state": "connected"})
    assert ok["status"] == health.OK and "Relay connected, 1 device" in ok["reason"]
    now = 2_000_000_000.0
    fresh = health.app_link_check({"state": "backoff", "down_since": now - 299}, now=now)
    assert fresh["status"] == health.OK
    late = health.app_link_check({"state": "backoff", "down_since": now - 301,
                                  "last_error": "connect:OSError"}, now=now)
    assert late["status"] == health.DEGRADED and "5 minutes" in late["reason"]
    for state in ("revoked", "secret_rejected", "replaced"):
        assert health.app_link_check({"state": state})["status"] == health.DEGRADED
    app_link_home.set_block(relay_enabled=False)
    off = health.app_link_check({"state": "revoked"})
    assert off["status"] == health.OK and "direct path only" in off["reason"]
    result = health.collect_health(status_probe=lambda: {}, worker_probe=lambda: None,
                                   backup_probe=lambda: health._check("backup", health.OK, "x"),
                                   engine_probe=lambda: None, queue_probe=lambda: [],
                                   model_probe=lambda: None,
                                   app_link_probe=lambda: health._check("app_link", health.OK, "y"))
    assert [c["name"] for c in result["checks"]][-1] == "app_link"


def test_doctor_findings(app_link_home):
    from vaf.core.channel_secrets import set_channel_secret
    from vaf.core.config import Config
    from vaf.core.security_misconfig import collect_security_findings

    def _codes():
        return {f["code"]: f["severity"] for f in collect_security_findings(Config.load())}
    assert "app_relay_active" not in _codes() and "app_relay_secret_missing" not in _codes()
    paired_device(SCOPE_A, relay_member=MEMBER)
    app_link_home.set_block(relay_instance_id="inst-1")
    codes = _codes()
    assert codes["app_relay_active"] == "low"
    assert codes["app_relay_secret_missing"] == "medium"
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    assert "app_relay_secret_missing" not in _codes()
    app_link_home.set_block(relay_enabled=False)
    assert "app_relay_active" not in _codes()


def test_ca_renew_warning(app_link_home, monkeypatch):
    from vaf.cli.cmd import server
    paired_device(SCOPE_A)
    paired_device(SCOPE_B, username="bob")
    out = runner.invoke(server.ca_app, ["renew"], input="n\n")
    assert "2 Veyllo App devices pin this authority and will have to pair again" in out.output
    assert "Nothing was changed" in out.output


def test_the_perimeter_row_has_no_time_before_a_device_was_seen(app_link_home):
    from vaf.api.security_routes import collect_channels_status
    paired_device(SCOPE_A)
    with devices._locked():
        state = devices._load()
        for d in state["devices"]:
            d["last_seen_at"] = None
        devices._save(state)
    row = [c for c in collect_channels_status()["channels"] if c["name"] == "veyllo_app"][0]
    assert row["paired"] == 1 and row["last_ts"] is None


def test_the_web_server_stops_the_link_at_shutdown():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = src.split('@app.on_event("shutdown")\nasync def stop_app_link():', 1)[1].split("\n\n\n", 1)[0]
    assert "app_link_relay.stop()" in block and "app_link_bridge.stop()" in block


def test_rotate_and_dissolve_without_a_relay_say_so(app_link_home, monkeypatch):
    _offline(monkeypatch, relay_cmd)
    for command in (["rotate"], ["dissolve", "--yes"]):
        out = runner.invoke(relay_cmd.app, command)
        assert out.exit_code == 1, command
        assert "No relay is set up on this machine" in out.output
        assert "Traceback" not in out.output


def test_an_unreachable_relay_changes_nothing(app_link_home, monkeypatch):
    import httpx

    from vaf.core.app_link import relay
    from vaf.core.channel_secrets import channel_secret, set_channel_secret
    _offline(monkeypatch, relay_cmd)
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    app_link_home.set_block(relay_instance_id="inst-1")

    class _Down:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("no route")

        async def delete(self, *a, **k):
            raise httpx.ConnectError("no route")
    monkeypatch.setattr(relay, "_connector", relay.RelayConnector(http_factory=_Down))
    for command in (["rotate"], ["dissolve", "--yes"]):
        out = runner.invoke(relay_cmd.app, command)
        assert out.exit_code == 1 and "could not be reached; nothing was changed" in out.output
    assert channel_secret("veyllo_app", "relay_secret") == "vrs_known"


def test_a_refusal_shows_the_reason_vaf_gave(app_link_home, monkeypatch):
    refused = (403, {"detail": "Only an admin can change the Veyllo relay."})
    monkeypatch.setattr(relay_cmd, "ask_running_vaf", lambda *a, **k: refused)
    out = runner.invoke(relay_cmd.app, ["off"])
    assert out.exit_code == 1 and "Only an admin can change the Veyllo relay." in out.output
    row = paired_device(SCOPE_A)[0]
    monkeypatch.setattr(devices_cmd, "ask_running_vaf",
                        lambda *a, **k: (404, {"detail": "No such device of this account."}))
    out = runner.invoke(devices_cmd.app, ["remove", row["device_id"]])
    assert out.exit_code == 1 and "No such device of this account." in out.output


def test_status_uses_the_health_sentences(app_link_home, monkeypatch):
    paired_device(SCOPE_A, relay_member=MEMBER)
    answer = (200, {"enabled": True, "state": "secret_rejected", "instance": True, "secret": True,
                    "members": 1})
    monkeypatch.setattr(relay_cmd, "ask_running_vaf", lambda *a, **k: answer)
    out = runner.invoke(relay_cmd.app, ["status"])
    expected = health.app_link_check(answer[1])["reason"]
    assert expected in out.output.replace("\n", " ") or expected[:40] in out.output
    assert "secret_rejected" not in out.output
    _offline(monkeypatch, relay_cmd)
    assert "(not set up yet)" in runner.invoke(relay_cmd.app, ["status"]).output
