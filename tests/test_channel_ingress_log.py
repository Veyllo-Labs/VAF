# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A messenger sender the agent refused to answer is channel traffic, not a security event.

Every drop at ingress used to be mirrored into the security log as `channel_rejected`.
The ingress policy refuses for one reason only, "not paired", so the kind meant "someone
who is not paired wrote", which on a number that receives ordinary WhatsApp traffic is the
everyday case (and the message is kept for the owner's inbox on purpose). As a security
event it lit the alert dot on every stranger's message and buried the real signals. The
drop is recorded in the channel's own inbound lane now, written with debug logging off
too, because the sender gets no reply and that line is the only trace of the attempt.

MUTATION: put the `log_security_event("channel_rejected", ...)` call back into a bridge and
the source guard goes red; drop `always=True` from a bridge's REJECT line and the Telegram
test below finds an empty lane with debug logging off.
"""
import re
from datetime import datetime
from pathlib import Path

import pytest

from vaf.core import log_helper
from vaf.core.security_events import SECURITY_EVENT_KINDS

REPO = Path(__file__).resolve().parents[1]
BRIDGES = {
    "whatsapp": REPO / "vaf" / "api" / "whatsapp_bridge.py",
    "telegram": REPO / "vaf" / "api" / "telegram_bridge.py",
    "discord": REPO / "vaf" / "api" / "discord_bridge.py",
}


@pytest.fixture
def quiet_logs(tmp_path, monkeypatch):
    """A scratch log directory with debug logging OFF, the setting most installs run with."""
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(log_helper, "is_debug_logging_enabled", lambda: False)
    return tmp_path


def test_the_lane_writer_writes_only_with_always_when_debug_is_off(quiet_logs):
    day = datetime.now().strftime("%Y-%m-%d")
    log_helper.log_channel_inbound("whatsapp", "ACCEPT from=x")
    assert not (quiet_logs / f"whatsapp_inbound_{day}.log").exists(), "an ordinary diagnostic line stays debug-gated"
    log_helper.log_channel_inbound("whatsapp", "REJECT not_paired from=x", always=True)
    line = (quiet_logs / f"whatsapp_inbound_{day}.log").read_text(encoding="utf-8").strip()
    # ISO timestamp first: the Logs window's line parser renders it as the timestamp column.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T[\d:.]+\s+REJECT not_paired from=x$", line)
    assert not list(quiet_logs.glob("security_*")), "nothing of this reaches the security log"


def test_the_five_channel_lanes_share_one_writer():
    """The always flag lives in one body; five copies of the same ten lines used to carry
    the debug gate each, and a sixth would have been the natural place to forget it."""
    src = (REPO / "vaf" / "core" / "log_helper.py").read_text(encoding="utf-8")
    for wrapper, lane in (("log_telegram_reply", "telegram_reply"), ("log_discord_reply", "discord_reply"),
                          ("log_whatsapp_qr", "whatsapp_qr"), ("log_whatsapp_reply", "whatsapp_reply")):
        body = src.split(f"def {wrapper}(", 1)[1].split("\ndef ", 1)[0]
        assert f'append_lane_log("{lane}", message)' in body, wrapper
    wa = src.split("def log_whatsapp_inbound(", 1)[1].split("\ndef ", 1)[0]
    assert 'log_channel_inbound("whatsapp", message)' in wa
    assert 'append_lane_log(f"{channel}_inbound", message, always=always)' in src


def test_a_telegram_drop_is_one_lane_line_per_sender_and_window_and_no_security_event(quiet_logs, monkeypatch):
    import vaf.core.channel_ingress_policy as policy_mod
    from vaf.api import telegram_bridge as tg
    from vaf.core.config import Config
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: quiet_logs / "data"))
    cfg = {"channel_ingress_policy": {"mode": "paired_only", "throttle_seconds": 60}, "telegram_config": {}}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    monkeypatch.setattr("vaf.core.security_events.log_security_event",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a refused sender is not a security event")))
    policy_mod._log_last.clear()

    tg._drop_unauthorized_telegram("9001", "9001", "text")
    tg._drop_unauthorized_telegram("9001", "9001", "voice")      # same sender, inside the window: throttled
    tg._drop_unauthorized_telegram("9002", "9002", "text")       # another sender: recorded

    day = datetime.now().strftime("%Y-%m-%d")
    lines = (quiet_logs / f"telegram_inbound_{day}.log").read_text(encoding="utf-8").strip().splitlines()
    assert [re.sub(r"^\S+\s+", "", l) for l in lines] == [
        "REJECT not_paired user_id=9001 chat_id=9001 kind=text",
        "REJECT not_paired user_id=9002 chat_id=9002 kind=text",
    ]
    assert not list(quiet_logs.glob("security_*"))


def test_no_bridge_mirrors_a_refused_sender_into_the_security_log():
    assert "channel_rejected" not in SECURITY_EVENT_KINDS
    for rel in sorted((REPO / "vaf").rglob("*.py")) + sorted((REPO / "web" / "components").rglob("*.tsx")):
        assert "channel_rejected" not in rel.read_text(encoding="utf-8", errors="ignore"), rel
    for channel, path in BRIDGES.items():
        src = path.read_text(encoding="utf-8")
        site = src[src.index(f'log_channel_inbound(\n                        "{channel}"' if channel == "whatsapp"
                              else f'log_channel_inbound("{channel}"'):]
        assert "always=True" in site[:400], f"{channel}: the REJECT line must survive debug logging being off"
        assert "should_log_unauthorized" in src[:src.index(f'"{channel}"', src.index("REJECT"))], \
            f"{channel}: the lane line sits behind the per-sender throttle"


def test_the_overview_channel_module_reads_the_posture_and_never_the_event_log(monkeypatch):
    from vaf.api import security_routes as sr
    from vaf.core.config import Config
    cfg = {"telegram_config": {"enabled": True, "whitelist": [{"telegram_user_id": "1"}]},
           "whatsapp_config": {"enabled": True, "whitelist": [{"phone_number": "+491700000042"}]},
           "discord_config": {}, "channel_ingress_policy": {"mode": "paired_only"}}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    monkeypatch.setattr(sr, "read_security_events",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the channel module reads config, not events")))
    out = sr.collect_channels_status()
    assert out["state"] == "ok" and "rejected_today" not in out
    by_name = {c["name"]: c for c in out["channels"]}
    assert by_name["whatsapp"]["paired"] == 1 and by_name["telegram"]["paired"] == 1
    assert all("rejected_today" not in c for c in out["channels"])
