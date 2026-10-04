# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What became of a send is said, not guessed - and never repeated by a second click.

The measured gaps: a WhatsApp send the bridge did not confirm in time ("No delivery
confirmation ...") was parked `failed`, and `failed` is one click from being sent again - a
message that may have arrived could be delivered twice. The approval ran the tool unbounded.
A second Send of a draft that already left (another tab, a retried request) answered "no such
draft" for a mail and "not waiting" for a call. And a tool call that was stopped or ran out of
time was recorded as finished, with a green check. Each test names the mutation it catches.
"""
import time
from types import SimpleNamespace

import pytest

from vaf.core import channel_message_store as store
from vaf.core import outbound_hold
from vaf.core.platform import Platform
from vaf.tools.send_whatsapp import SendWhatsAppTool

SCOPE = "11111111-2222-3333-4444-555555555555"
USER = "alice"


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    monkeypatch.setattr(store, "_local_admin", lambda: "admin")
    monkeypatch.setattr(store, "_local_admin_scope_id", lambda: "admin-scope")
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()


class _Send:
    """A send tool answering like the real one, read the way the real one declares."""
    delivery_markers = SendWhatsAppTool.delivery_markers

    def __init__(self, answer, delay=0.0, budget=None):
        self.answer, self.delay, self.calls = answer, delay, []
        if budget is not None:
            self.timeout_seconds = budget

    def run(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self.delay)
        return self.answer


def _park():
    return outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "x"},
                                             username=USER, user_scope_id=SCOPE)


def _approve(entry_id, tool):
    return outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                      user_role="user", tools={"send_whatsapp": tool})


def test_the_tools_markers_are_the_bridges_own_words():
    """The bridge says it, the tool declares how to read it: one drifting from the other
    would read every delivery as a failure. MUTATION: reword a bridge text."""
    from vaf.api.whatsapp_bridge import SEND_UNCONFIRMED, SENT_TEXTS
    sent = SendWhatsAppTool.delivery_markers["sent"]
    for text in SENT_TEXTS:
        assert text.lower().startswith(sent), text
    assert SEND_UNCONFIRMED.lower().startswith(SendWhatsAppTool.delivery_markers["unconfirmed"])


def test_a_send_the_bridge_did_not_confirm_is_ambiguous_and_never_repeated(scratch):
    """MUTATION: read "unconfirmed" as a failure, and the second click sends it again."""
    from vaf.api.whatsapp_bridge import SEND_UNCONFIRMED
    entry_id = _park()
    tool = _Send(SEND_UNCONFIRMED)
    first = _approve(entry_id, tool)
    assert first["ok"] is False and first["state"] == "ambiguous"
    assert store.held_send(entry_id, USER, SCOPE)["state"] == "ambiguous"
    second = _approve(entry_id, tool)
    assert second["ok"] is False and "may already have been sent" in second["result"]
    assert len(tool.calls) == 1


def test_an_approval_that_runs_out_of_time_is_ambiguous(scratch):
    """The approval runs bounded, and a send abandoned mid-way may have left. MUTATION: call
    tool.run directly again (the click then waits for the whole send)."""
    entry_id = _park()
    tool = _Send("Message sent via WhatsApp.", delay=2.5, budget=1)
    started = time.monotonic()
    out = _approve(entry_id, tool)
    assert time.monotonic() - started < 2.2
    assert out["state"] == "ambiguous"
    assert store.held_send(entry_id, USER, SCOPE)["state"] == "ambiguous"


def test_a_second_send_of_a_sent_call_answers_what_the_first_got(scratch, monkeypatch):
    """Another tab, a retried request: the answer is the stored one, nothing is sent again
    and the chat is not woken twice. MUTATION: refuse a sent row as "not waiting"."""
    woken = []
    monkeypatch.setattr(outbound_hold, "wake_after_send", lambda *a, **k: woken.append(a))
    entry_id = _park()
    tool = _Send("Message sent via WhatsApp.")
    first = outbound_hold.send_draft("call", entry_id, username=USER, user_scope_id=SCOPE,
                                     tools={"send_whatsapp": tool})
    again = outbound_hold.send_draft("call", entry_id, username=USER, user_scope_id=SCOPE,
                                     tools={"send_whatsapp": tool})
    assert first["ok"] and again["ok"] and again["state"] == "sent"
    assert len(tool.calls) == 1 and len(woken) == 1
    assert store.held_send(entry_id, USER, SCOPE)["result"] == "Message sent via WhatsApp."


def test_a_second_send_of_a_mail_that_left_is_answered_from_its_state():
    """MUTATION: answer "not waiting" for an op that is not held (the route made it a 404)."""
    from vaf.mail.service import release_held_draft
    op = {"id": 7, "kind": "send", "state": "done", "payload": {}}
    released = []
    svc = SimpleNamespace(
        store=SimpleNamespace(get_op=lambda op_id: op),
        chat_draft=lambda o: {"state": "sent"},
        approve_draft=lambda op_id: released.append(op_id) or True)
    out = release_held_draft("scope", "alice", 7, service=svc)
    assert out["ok"] is True and out["state"] == "done" and released == []
    op["state"] = "discarded"
    svc.chat_draft = lambda o: {"state": "discarded"}
    assert release_held_draft("scope", "alice", 7, service=svc)["error"] == "not waiting"


# ── a call that was abandoned is not a finished one ──────────────────────────

def test_a_stopped_call_says_its_outcome_is_unknown():
    """The funnel's tool_end carries `aborted`. MUTATION: leave it out of the event."""
    from vaf.core.bounded_run import cancel_requested
    from vaf.core.tool_dispatch import ToolCaller

    class _Slow:
        name = "slow"
        description = "waits"
        parameters = {"type": "object", "properties": {}}
        identity_kwargs = ()
        timeout_seconds = 30

        def run(self, **kwargs):
            while not cancel_requested():
                time.sleep(0.05)
            return "done"

    events = []
    stop = {"now": False}
    caller = ToolCaller({"slow": _Slow()}, on_event=events.append, poll=0.1,
                        stop_check=lambda: stop["now"])
    import threading
    threading.Timer(0.3, lambda: stop.update(now=True)).start()
    caller.execute("slow", {})
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["aborted"] == "stopped"


def test_a_reloaded_chat_shows_an_abandoned_call_as_unknown():
    """MUTATION: project every answered tool message as completed again."""
    from vaf.core.bounded_run import STOPPED_PREFIX, TIMEOUT_PREFIX
    from vaf.core.tool_dispatch import abort_kind
    assert abort_kind(f"{STOPPED_PREFIX} 'x' was cancelled") == "stopped"
    assert abort_kind(f"{TIMEOUT_PREFIX} 'x' did not finish") == "timeout"
    assert abort_kind("Message sent via WhatsApp.") is None
    from vaf.core.web_server import _history_projection
    session = SimpleNamespace(messages=[
        {"role": "tool", "content": f"{STOPPED_PREFIX} 'host_bash' was cancelled", "name": "host_bash",
         "tool_call_id": "c1"},
        {"role": "tool", "content": "OK", "name": "read_file", "tool_call_id": "c2"},
    ])
    rows = [m for m in _history_projection(session, "web_chat-x") if m.get("role") == "tool"]
    assert [m["toolStatus"] for m in rows] == ["unknown", "completed"]
