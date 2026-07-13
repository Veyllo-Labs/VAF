# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Channel-aware delivery of a thinking-mode question.

`deliver_tracked_message` delivers a proactive question to the user's configured
main_messenger FIRST (Telegram/WhatsApp/Discord) and only falls back to the Web
UI when none is configured. If a messenger question stays unanswered past the
skip window, `_process_waiting_reply` escalates it ONCE to the Web UI with a note
that it was already asked there, then gives up on the next miss. Storage is
isolated per test to a tmp vaf_dir; the messenger send and the Web UI emit are
stubbed.
"""
import time

import pytest

import vaf.core.messaging_connections as mc
import vaf.core.thinking_mode as tm
import vaf.core.thinking_requests as tr
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("VAF_THINKING_MODE", "1")


def _backdate_waiting(scope, minutes_ago):
    """Move the waiting state's question timestamp into the past so the skip window has elapsed."""
    data = tm._load_waiting()
    key = tm._key(scope)
    data[key]["question_sent_at_ts"] = time.time() - minutes_ago * 60
    data[key]["nudge_sent_at_ts"] = None
    tm._save_waiting(data)


@pytest.mark.parametrize("channel", ["telegram", "whatsapp", "discord"])
def test_delivers_via_main_messenger_and_skips_web(monkeypatch, tmp_path, channel):
    """main_messenger configured -> the question goes to that messenger, NOT the Web UI, and the
    waiting state records the channel so a later reply / escalation knows where it went."""
    _isolate(monkeypatch, tmp_path)
    web_emits = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: (web_emits.append(content), "sid-web")[1])
    sent = []
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, uname, text, record=True: (sent.append((scope, text, record)), (True, channel))[1])

    scope = f"user-{channel}"
    req = tm.deliver_tracked_message(scope, "Soll ich das Update einspielen?", source_note_id="n1")

    # Delivered via the messenger, exactly once, with the user-facing text.
    assert req and req.get("delivered") is True
    assert len(sent) == 1 and sent[0][1] == "Soll ich das Update einspielen?"
    # Tracked questions must opt out of router session recording: they are
    # reconstructed scope-keyed at reply time and would appear twice otherwise.
    assert sent[0][2] is False
    # NO double-delivery to the Web UI.
    assert web_emits == []
    # Request tracked.
    reqs = tr.list_requests(scope, status="asked")
    assert len(reqs) == 1 and reqs[0]["source_note_id"] == "n1"
    # Waiting state carries the resolved channel.
    waiting = tm.get_waiting_for_reply(scope)
    assert waiting and waiting.get("channel") == channel
    assert waiting.get("request_id") == reqs[0]["id"]
    assert waiting.get("escalated_to_web") is False


def test_no_messenger_falls_back_to_web(monkeypatch, tmp_path):
    """No main_messenger -> the existing Web UI delivery path runs and the channel is recorded as 'web'."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: False)
    web_emits = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: (web_emits.append(content), "sid-web")[1])
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, uname, text, record=True: (False, None))

    scope = "user-noch"
    req = tm.deliver_tracked_message(scope, "Web-Frage?", source_note_id="n1")

    assert req and req.get("delivered") is True
    assert web_emits == ["Web-Frage?"]
    waiting = tm.get_waiting_for_reply(scope)
    assert waiting and waiting.get("channel") == "web"


@pytest.mark.parametrize("channel,label", [
    ("telegram", "Telegram"), ("whatsapp", "WhatsApp"), ("discord", "Discord"),
])
def test_unanswered_messenger_question_escalates_once_to_web(monkeypatch, tmp_path, channel, label):
    """A messenger question the user never answers is re-asked ONCE in the Web UI (with an
    'already asked on <Channel>' note); a second miss gives up and clears the waiting state."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    web_emits = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: (web_emits.append((content, session_id)), "sid-web")[1])

    scope = f"user-esc-{channel}"
    tm.set_waiting_for_reply(
        scope, username="admin", display_name="admin",
        question_text="Soll ich das Meeting verschieben?", request_id="r1",
        session_id="sid-anchor", channel=channel,
    )

    # 1) Past the skip window, still on the messenger and not yet escalated -> escalate ONCE, keep waiting.
    _backdate_waiting(scope, minutes_ago=11)
    assert tm._process_waiting_reply(scope) == "skip"
    assert len(web_emits) == 1
    note_text = web_emits[0][0]
    assert "Soll ich das Meeting verschieben?" in note_text  # the original question is included
    assert label in note_text                                # ...with the 'already asked on <Channel>' note
    w = tm.get_waiting_for_reply(scope)
    assert w and w.get("escalated_to_web") is True
    assert w.get("channel") == "web"

    # 2) Still unanswered after the one-time web escalation -> give up and clear, no second escalation.
    _backdate_waiting(scope, minutes_ago=11)
    assert tm._process_waiting_reply(scope) == "allow_run"
    assert tm.get_waiting_for_reply(scope) is None
    assert len(web_emits) == 1


def test_escalation_gives_up_when_no_reachable_web_session(monkeypatch, tmp_path):
    """If there is no web chat to escalate to (emit returns None), the unanswered messenger question is
    given up on (cleared) — it must NOT loop forever returning 'skip'."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    # No reachable web session -> emit_message_to_web_ui returns None.
    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)

    scope = "user-noweb"
    tm.set_waiting_for_reply(
        scope, username="admin", question_text="Q?", request_id="r1",
        session_id="sid-anchor", channel="telegram",
    )
    _backdate_waiting(scope, minutes_ago=11)
    assert tm._process_waiting_reply(scope) == "allow_run"
    assert tm.get_waiting_for_reply(scope) is None


def test_escalated_flag_alone_blocks_re_escalation(monkeypatch, tmp_path):
    """Independently exercises the `and not escalated_to_web` half of the once-only guard: a state with a
    messenger channel BUT escalated_to_web already True must NOT escalate again (kills a guard-removal mutation)."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    web_emits = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: (web_emits.append(content), "sid")[1])

    scope = "user-escflag"
    tm.set_waiting_for_reply(
        scope, username="admin", question_text="Q?", request_id="r1",
        session_id="sid", channel="telegram", escalated_to_web=True,
    )
    _backdate_waiting(scope, minutes_ago=11)
    assert tm._process_waiting_reply(scope) == "allow_run"
    assert web_emits == []
    assert tm.get_waiting_for_reply(scope) is None


def test_legacy_waiting_entry_without_channel_treated_as_web(monkeypatch, tmp_path):
    """A waiting.json entry written by the pre-change code has no channel/escalated_to_web keys; the new
    escalation logic must treat it as 'web' (clear at skip, no messenger escalation) and never crash."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    web_emits = []
    monkeypatch.setattr(tm, "emit_message_to_web_ui",
                        lambda scope, content, session_id=None: (web_emits.append(content), "sid")[1])

    scope = "user-legacy"
    key = tm._key(scope)
    tm._save_waiting({key: {
        "question_sent_at_ts": time.time() - 11 * 60,
        "nudge_sent_at_ts": None,
        "username": "admin", "display_name": "admin",
        "question_text": "Legacy Q?", "request_id": "r-old", "session_id": "sid-old",
    }})
    assert tm._process_waiting_reply(scope) == "allow_run"
    assert tm.get_waiting_for_reply(scope) is None
    assert web_emits == []  # treated as web -> cleared, no messenger escalation


def test_nudge_routes_to_messenger_for_messenger_channel(monkeypatch, tmp_path):
    """In the nudge window (between nudge_min and skip_min) a messenger-channel question nudges via the
    messenger, and the waiting state records the nudge timestamp."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    nudges = []
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, uname, text, record=True: (nudges.append((scope, text)), (True, "telegram"))[1])

    scope = "user-nudge"
    tm.set_waiting_for_reply(
        scope, username="admin", question_text="Q?", request_id="r1",
        session_id="sid", channel="telegram",
    )
    _backdate_waiting(scope, minutes_ago=4)  # >= nudge_min (3), < skip_min (10)
    assert tm._process_waiting_reply(scope) == "skip"
    assert len(nudges) == 1
    w = tm.get_waiting_for_reply(scope)
    assert w and w.get("nudge_sent_at_ts") is not None


def test_nudge_after_escalation_goes_to_web_not_messenger(monkeypatch, tmp_path):
    """Once a question has been escalated to web (channel='web'), its nudge must NOT buzz the messenger
    again — the messenger send is skipped and the web fallback is used."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    messenger_calls = []
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, uname, text, record=True: (messenger_calls.append(text), (True, "telegram"))[1])
    # Web nudge fallback path: stub the web interface lookup to a no-op-ish failure so _send_nudge returns
    # without a messenger send; we only assert the messenger was NOT used.
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: None)

    scope = "user-postesc"
    tm.set_waiting_for_reply(
        scope, username="admin", question_text="Q?", request_id="r1",
        session_id="sid", channel="web", escalated_to_web=True,
    )
    _backdate_waiting(scope, minutes_ago=4)
    tm._process_waiting_reply(scope)
    assert messenger_calls == []  # channel='web' -> messenger send skipped
