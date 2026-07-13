# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Guards for the channel-agnostic send_to_user delivery tool.

The tool wraps the one canonical send_to_main_messenger router: the platform is
resolved at RUN time from main_messenger, never chosen by the model (live
incident f9efc6d6: a generated automation hardwired send_telegram and dumped
raw tool output). These tests pin the tool contract, every registry copy the
tool must join (thinking-mode strip set, agent/engine scope injection, router
pin, engine path map), the front-office default-deny, and the automation-lane
double-delivery dedup.
"""
from pathlib import Path

import vaf.core.agent as agent_mod
import vaf.workflows.engine as engine_mod
from vaf.core.automation import _SEND_STEP_TOOLS, _delivered_via_send_step
from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
from vaf.core.thinking_mode import _SENT_TOOLS
from vaf.tools.send_to_user import SendToUserTool
from vaf.workflows.engine import _WORKFLOW_REL_PATH_ARGS


# ── tool contract ────────────────────────────────────────────────────────────

def test_tool_contract():
    t = SendToUserTool()
    assert t.name == "send_to_user"
    assert t.permission_level == "write"
    assert t.side_effect_class == "irreversible"  # Whare Wananga: never probe-send
    assert t.admin_only is False  # non-admin users deliver to their own messenger
    assert t.parameters["required"] == ["message"]
    assert "file_path" in t.parameters["properties"]


# ── registry copies the tool must join (Rule 2) ──────────────────────────────

def test_thinking_mode_strips_send_to_user():
    # The strip filter is a NAME SET, not a prefix match: a new send tool that
    # is not listed becomes an untracked outbound channel in background runs.
    assert "send_to_user" in _SENT_TOOLS


def test_agent_dispatch_injects_user_scope():
    # Without the injection branch, send_to_main_messenger falls back to
    # username="admin" - a non-admin automation would deliver to the admin's
    # messenger (cross-user leak).
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert '"send_whatsapp", "send_to_user")' in src, (
        "agent.py send-tool arg-injection tuple lost send_to_user"
    )


def test_agent_router_pins_send_to_user_when_messenger_available():
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert 'tools_set.add("send_to_user")' in src, (
        "router exposure block no longer pins send_to_user for connected users"
    )


def test_engine_injects_user_scope():
    src = Path(engine_mod.__file__).read_text(encoding="utf-8")
    assert '"send_whatsapp", "send_to_user")' in src, (
        "workflow engine scope-injection tuple lost send_to_user"
    )


def test_engine_resolves_relative_file_path_into_project_dir():
    # A bare filename written by an earlier write_file step resolves into the
    # shared project dir; send_to_user's file_path must resolve the same way or
    # the attachment silently never arrives (router ignores missing files).
    assert _WORKFLOW_REL_PATH_ARGS.get("send_to_user") == ("file_path",)


def test_front_office_default_deny():
    # Contacts must not gain a second owner back-channel: front office is a
    # strict allow-list and send_to_user deliberately stays out of it.
    assert "send_to_user" not in FRONT_OFFICE_ALLOWED_TOOLS


# ── run() behavior ───────────────────────────────────────────────────────────

def _run(monkeypatch, router_result, notifications, **kwargs):
    calls = {}

    def fake_send(user_scope_id, username, text, file_path=None):
        calls["args"] = (user_scope_id, username, text, file_path)
        return router_result

    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", fake_send)
    import vaf.core.user_notifications as un
    monkeypatch.setattr(un, "append_notification",
                        lambda *a, **k: notifications.append((a, k)) or {})
    result = SendToUserTool().run(**kwargs)
    return result, calls


def test_success_reports_actual_channel(monkeypatch):
    notifications = []
    result, calls = _run(
        monkeypatch, (True, "discord"), notifications,
        message="Report ready.", username="mert", user_scope_id="scope1",
    )
    assert "sent to the user via Discord" in result
    assert calls["args"][0] == "scope1" and calls["args"][1] == "mert"
    assert notifications == []


def test_no_messenger_falls_back_honestly(monkeypatch):
    # (False, None) = no main_messenger / channel down / send failed. The tool
    # must NOT claim messenger success and must leave a Web UI notification
    # preview instead of dropping the content silently.
    notifications = []
    result, _ = _run(
        monkeypatch, (False, None), notifications,
        message="Hello", username="mert", user_scope_id="scope1",
    )
    assert "sent to the user via" not in result.lower()
    assert "Could not deliver via messenger" in result
    assert len(notifications) == 1
    assert notifications[0][0][0] == "scope1"  # scoped at the emit site


def test_think_blocks_are_stripped(monkeypatch):
    notifications = []
    _, calls = _run(
        monkeypatch, (True, "telegram"), notifications,
        message="<think>secret reasoning</think>Hi Mert", username="mert", user_scope_id=None,
    )
    assert "secret reasoning" not in calls["args"][2]
    assert "Hi Mert" in calls["args"][2]


def test_missing_attachment_still_sends_text(monkeypatch):
    # Attachment is best-effort by router contract: a bad file path must not
    # kill the text delivery, but the result must say the file was skipped.
    notifications = []
    result, calls = _run(
        monkeypatch, (True, "telegram"), notifications,
        message="Summary", file_path="/nonexistent/nowhere_42.html",
        username="mert", user_scope_id=None,
    )
    assert calls["args"][3] is None
    assert "sent to the user via Telegram" in result
    assert "attachment skipped" in result


# ── automation-lane double-delivery dedup ────────────────────────────────────

def test_send_step_tools_cover_all_send_tools():
    assert _SENT_TOOLS <= _SEND_STEP_TOOLS


def test_dedup_detects_confirmed_delivery():
    assert _delivered_via_send_step([
        {"tool": "web_search", "status": "success", "result": "..."},
        {"tool": "send_to_user", "status": "success",
         "result": "Message sent to the user via Telegram."},
    ]) is True


def test_dedup_keeps_push_on_failed_or_unclear_send():
    # Fail-safe direction: a duplicate message beats a lost one.
    assert _delivered_via_send_step([
        {"tool": "send_telegram", "status": "success",
         "result": "No Telegram contact found for this user."},
    ]) is False
    assert _delivered_via_send_step([
        {"tool": "send_to_user", "status": "failed",
         "result": "Message sent to the user via Telegram."},
    ]) is False
    assert _delivered_via_send_step([]) is False
    assert _delivered_via_send_step(None) is False


# ── prompt-lane double-delivery dedup (live 2026-07-14: calendar check) ──────

def test_agent_history_dedup_detects_confirmed_send():
    from vaf.core.automation import _delivered_via_agent_history
    assert _delivered_via_agent_history([
        {"role": "user", "content": "run"},
        {"role": "tool", "tool_call_id": "1", "name": "send_telegram",
         "content": "Message sent to the user via Telegram."},
    ]) is True


def test_agent_history_dedup_requires_send_tool_and_success():
    from vaf.core.automation import _delivered_via_agent_history
    # phrase quoted inside a NON-send tool result must not suppress the push
    assert _delivered_via_agent_history([
        {"role": "tool", "tool_call_id": "1", "name": "web_search",
         "content": "docs say: Message sent to the user via Telegram."},
    ]) is False
    # send tool that FAILED must not suppress the push
    assert _delivered_via_agent_history([
        {"role": "tool", "tool_call_id": "1", "name": "send_telegram",
         "content": "No Telegram contact found for this user."},
    ]) is False
    assert _delivered_via_agent_history([]) is False
    assert _delivered_via_agent_history(None) is False


# ── router outbound recording (live 2026-07-14: 'aktive timer?' context gap) ─

def test_router_records_outbound_into_channel_session(monkeypatch):
    import vaf.core.messaging_connections as mc
    saved = {}

    class _FakeSession:
        def __init__(self):
            self.messages = []
        def add_message(self, role, content, **kw):
            self.messages.append((role, content))

    class _FakeSM:
        def load(self, sid, restore_state=False):
            saved["sid"] = sid
            saved.setdefault("session", _FakeSession())
            return saved["session"]
        def save(self, session, sync_state=False):
            saved["saved"] = True

    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "SessionManager", _FakeSM)
    store_calls = []
    import vaf.core.channel_message_store as store_mod
    monkeypatch.setattr(store_mod, "append_message",
                        lambda **kw: store_calls.append(kw))

    mc._record_outbound("telegram", "12345", "Hello", "mert", "scope1")
    assert saved["sid"] == "telegram_12345"
    assert ("assistant", "Hello") in saved["session"].messages
    assert saved.get("saved") is True
    assert len(store_calls) == 1 and store_calls[0]["channel"] == "telegram"


def test_router_recording_respects_whatsapp_bridge_ownership(monkeypatch):
    # WhatsApp: session id follows the bridge convention (username inside) and
    # the STORE append is skipped - the bridge sender loop records it itself.
    import vaf.core.messaging_connections as mc
    saved = {}

    class _FakeSession:
        def add_message(self, role, content, **kw):
            saved["msg"] = content

    class _FakeSM:
        def load(self, sid, restore_state=False):
            saved["sid"] = sid
            return _FakeSession()
        def save(self, session, sync_state=False):
            pass

    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "SessionManager", _FakeSM)
    store_calls = []
    import vaf.core.channel_message_store as store_mod
    monkeypatch.setattr(store_mod, "append_message",
                        lambda **kw: store_calls.append(kw))

    mc._record_outbound("whatsapp", "4917012345@s.whatsapp.net", "Hi", "mert", None)
    assert saved["sid"] == "whatsapp_mert_4917012345"
    assert store_calls == []


def test_thinking_callers_opt_out_of_recording():
    # Tracked requests are reconstructed scope-keyed at reply time; a session
    # append would duplicate them in context. Both thinking-mode call sites
    # must pass record=False.
    from pathlib import Path
    import re
    import vaf.core.thinking_mode as tm
    src = Path(tm.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"send_to_main_messenger\([^)]*\)", src)
    assert calls, "thinking-mode no longer calls send_to_main_messenger?"
    without_opt_out = [c for c in calls if "record=False" not in c]
    assert not without_opt_out, (
        f"thinking-mode send_to_main_messenger call(s) missing record=False: {without_opt_out}"
    )
