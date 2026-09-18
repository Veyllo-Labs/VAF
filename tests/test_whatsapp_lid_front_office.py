# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An unresolved @lid under an open Inbound (vaf/api/whatsapp_bridge.py, FRONT_OFFICE.md): the
sender has no number, so nothing can match them in the book and nothing is enrolled. The open
channel does not apply and the message is refused as not_paired until the LID is assigned to a
number; a resolved sender goes through as front_office_open. The refusal is its own branch
rather than a faked denial, because "nobody decided about this person" and "the owner refused
them" are different answers and the log prints the reason. Isolated: tmp data dir, in-memory
config, the debounce flush stubbed so no task queue is touched.

MUTATION: drop the `not raw` branch from the bridge's admission block and the unresolved
sender is answered (front_office_open)."""
import time

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core import channel_message_store as store
from vaf.core.channel_ingress_policy import set_front_office
from vaf.core.config import Config
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def decisions(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    cfg = {"whatsapp_config": {"enabled": True}, "local_admin_scope_id": SCOPE, "local_admin_username": "alice",
           "channel_ingress_policy": set_front_office(None, True, "whatsapp", now=1)}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, c: cfg.update(c)))
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "_wa_flush", lambda key: None)
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: None)
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()
    calls = []
    real = wa.evaluate_ingress

    def recording(channel, policy, **kw):
        out = real(channel, policy, **kw)
        calls.append((kw.get("access"), out))
        return out

    monkeypatch.setattr(wa, "evaluate_ingress", recording)
    return calls


def test_an_unresolved_lid_is_not_answered_by_the_open_door_but_a_resolved_sender_is(decisions):
    now = int(time.time())
    wa._dispatch_bridge_event("alice", SCOPE, "message", {"from": "123456789012345@lid", "body": "hallo", "ts": now,
                                                          "message_id": "L1", "content_type": "text"})
    assert decisions == [], "an unresolved @lid is refused before the policy is asked at all"
    wa._dispatch_bridge_event("alice", SCOPE, "message", {"from": "491700000042@s.whatsapp.net", "body": "hallo", "ts": now,
                                                          "message_id": "P1", "content_type": "text"})
    assert decisions[-1] == (None, (True, "front_office_open")), "nobody decided about them; the open channel did"
    from vaf.core.contacts_store import find_contact_by_channel
    assert find_contact_by_channel("whatsapp", "+491700000042", "alice", SCOPE) is not None, "the resolved sender is enrolled"
    assert find_contact_by_channel("whatsapp", "123456789012345@lid", "alice", SCOPE) is None, "the LID never became a phantom number"
