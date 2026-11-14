# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""VAF's own file link shows as the path it opens wherever text leaves the terminal.

The coder and the workflow engine name their folder and files with `file_link(path)`, Rich
markup (`[link=file:///abs]/abs[/link]`) that the terminal shows as a clickable path. A model
that repeats the coder's result carries that markup into its reply. MEASURED BEFORE THE FIX:
the live web bubble, the stored web chat and drain copies, the messenger and Veyllo App text
and the main-messenger lane all held the raw markup, and the chat's Markdown renderer shows it
as literal text. Every copy now shows the path, as the terminal's own web copy already did
(`own_links_as_labels`); any other link, and every other bracket, stays as written.

Every input is a Python literal or `file_link` of a literal path; nothing is opened, sent or
executed. The `WebInterface` is a singleton, so its push is patched on the instance.
"""
import inspect
import os
import re

import pytest

import vaf
from vaf.core import headless_runner as hr
from vaf.core import web_interface as wi_mod
from vaf.core.session import TurnRound, TurnStatus
from vaf.core.terminal_text import file_link


def _own(path):
    """A sentence holding VAF's own link to `path`, and the same sentence with its path. The
    label is the absolute path `file_link` writes on this host (a drive path on Windows)."""
    shown = os.path.abspath(path)
    return f"Created {file_link(path)} for you.", f"Created {shown} for you."


OWN_CASES = {
    "posix": _own("/home/user/p"),
    "route": _own("/home/user/p/app/[id]/page.tsx"),
    "space": _own("/home/user/My Project"),
}

#: Text that is not VAF's own link and must reach every reader byte for byte.
KEPT_CASES = {
    "foreign": "See [link=https://evil.example]click[/link] now.",
    "mismatched": "Open [link=file:///etc/passwd]https://bank.example[/link] now.",
    "host": "Open [link=file://evil.example/share/x]/share/x[/link] now.",
    "brackets": "arr[i], [the docs](https://e.org)\n- [x] done",
}

CASES = ([(name, *pair) for name, pair in OWN_CASES.items()]
         + [(name, text, text) for name, text in KEPT_CASES.items()])
CASE_IDS = [c[0] for c in CASES]


@pytest.fixture
def pushes(monkeypatch):
    """Every payload the browser bridge pushes, captured on the singleton instance."""
    wi = wi_mod.get_web_interface()
    sent = []
    monkeypatch.setattr(wi, "_push_session_update", lambda sid, payload: sent.append(payload))
    return wi, sent


@pytest.mark.parametrize("name,text,shown", CASES, ids=CASE_IDS)
def test_the_browser_bridge_shows_vafs_own_link_as_its_path(pushes, name, text, shown):
    """AC 1, 2. The three payloads that carry an assistant's text show the own link as its
    path, and nothing else changes. MUTATION: drop the conversion in each of the three
    methods - red for the own cases. MUTATION: replace it with a strip of every `[...]` group
    - red for the foreign, mismatched, host and bracket cases."""
    wi, sent = pushes
    wi.emit_agent_message("assistant", text, session_id="s")
    wi.emit_agent_message("assistant", text, session_id="s", round=2, answer=True)
    wi.emit_agent_message_append(text, session_id="s", role="assistant", kind="automation")
    wi.emit_message_complete(text, session_id="s")
    assert [(p["type"], p["content"]) for p in sent] == [
        ("agent_message_update", shown), ("agent_message_update", shown),
        ("agent_message_append", shown), ("message_complete", shown)]
    assert sent[1]["round"] == 2 and sent[1]["answer"] is True


def test_a_wake_keeps_its_text_and_no_content_stays_none(pushes):
    """AC 2. A timer or process wake is the person's side of the chat (`role="user"`) and keeps
    its text; content that is not a string passes unchanged, never coerced to text.
    MUTATION: apply the conversion for every role - red."""
    wi, sent = pushes
    text, _ = OWN_CASES["posix"]
    wi.emit_agent_message_append(text, session_id="s", role="user", kind="timer")
    wi.emit_agent_message("assistant", None, session_id="s")
    wi.emit_agent_message_append(None, session_id="s")
    wi.emit_message_complete(None, session_id="s")
    assert [p["content"] for p in sent] == [text, None, None, None]


def test_the_web_chat_stream_mirrors_the_path(pushes):
    """AC 3. The chat lane's stream with its default emit, fed the reply in 7-character chunks
    and closed the way the runner closes it (`emit_rounds`), leaves the bubble with the path.
    MUTATION: drop the conversion in `emit_agent_message` - red."""
    _, sent = pushes
    text, shown = OWN_CASES["route"]
    stream = hr._ChatTurnStream("s", clock=lambda: 0.0)
    for i in range(0, len(text), 7):
        stream(text[i:i + 7])
    assert stream.emit_rounds([TurnRound(1, (), True)], clean=hr._strip_tool_calls_json) == 1
    updates = [p for p in sent if p["type"] == "agent_message_update"]
    assert updates[-1]["content"] == shown and updates[-1]["round"] == 1
    assert "".join(stream.parts) == text      # the stream itself keeps what the model wrote


@pytest.mark.parametrize("name,text,shown", CASES, ids=CASE_IDS)
def test_the_stored_web_chat_copies_hold_the_path(name, text, shown):
    """AC 4. The chat turn's stored copy (streamed and return value), the per-round texts and
    the drain's stored copy hold the path. MUTATION: drop the conversion from
    `_clean_for_session` - red; from `_clean_drain_text` - red."""
    copy, meta = hr._chat_turn_copy([text[:9], text[9:]], "unused", ends_at_draft=False,
                                    turn=TurnStatus())
    assert (copy, meta) == (shown, None)
    assert hr._chat_turn_copy([], text, ends_at_draft=False, turn=TurnStatus()) == (shown, None)
    assert hr._drain_turn_copy([text], turn=TurnStatus()) == (shown, None)

    stream = hr._ChatTurnStream("s", emit=lambda *a: None)
    stream("Looking.")
    stream.on_event({"type": "round_start", "round": 2})
    stream(text)
    rounds = hr._turn_rounds_meta([TurnRound(1, ("c1",), False), TurnRound(2, (), True)], stream)
    assert [r["text"] for r in rounds] == ["Looking.", shown]


def test_an_answered_then_failed_turn_keeps_its_answer_with_the_path():
    """AC 4. The `stored_turn_copy` contract holds with the new step: the answer comes back
    with the path and without the failure that followed it."""
    text, shown = OWN_CASES["posix"]
    failure = "[Error] API backend failure: locked"
    copy, meta = hr._chat_turn_copy([text, "\n", failure], "unused", ends_at_draft=False,
                                    turn=TurnStatus(failure=failure))
    assert meta and copy.endswith(meta["turn_failure"])
    assert vaf.model_text({"role": "assistant", "content": copy, "metadata": meta}) == shown
    copy, meta = hr._drain_turn_copy([text, "\n", failure], turn=TurnStatus(failure=failure))
    assert vaf.model_text({"role": "assistant", "content": copy, "metadata": meta}) == shown


@pytest.mark.parametrize("name,text,shown", CASES, ids=CASE_IDS)
def test_messengers_and_the_app_get_the_path(name, text, shown):
    """AC 5. The one outbound chain of the messenger replies, the drain's messenger summary
    and the Veyllo App (`bridge._clean`). MUTATION: drop the conversion from
    `_prepare_channel_outbound` - red for the messenger and the App alike."""
    from vaf.core.app_link import bridge

    assert hr._prepare_channel_outbound(text) == shown
    assert bridge._clean(text) == shown


def test_the_main_messenger_lane_sends_the_path(monkeypatch):
    """AC 5. `send_to_main_messenger` (automation results, background questions,
    `send_to_user`) sends the path and records the same text it sent. MUTATION: drop the
    conversion from `send_to_main_messenger` - red."""
    import vaf.core.messaging_connections as mc
    import vaf.core.telegram_reply as tr

    text, shown = OWN_CASES["space"]
    monkeypatch.setattr(mc, "get_messaging_connections", lambda **k: {"main_messenger": "telegram"})
    monkeypatch.setattr(mc, "get_telegram_chat_id", lambda *a, **k: "12345")
    sent, recorded = [], []
    monkeypatch.setattr(tr, "send_telegram_reply",
                        lambda chat_id, body, **kw: (sent.append(body), True)[1])
    monkeypatch.setattr(mc, "_record_outbound", lambda *a, **k: recorded.append(a[2]))
    assert mc.send_to_main_messenger("scope", "user", f"  {text}\n") == (True, "telegram")
    assert sent == [shown] and recorded == [shown]


def test_the_drain_fallback_names_the_path():
    """AC 6. The drain's deterministic message when the model produced no usable summary:
    the head, then the result with the path, cut at 2000 characters AFTER the conversion, so
    a link that starts at offset 1990 leaves no half of its markup. MUTATION: convert after
    the cut - red (straddle case)."""
    text, shown = OWN_CASES["posix"]
    assert hr._drain_fallback_message(text, "en") == \
        f"The sub-agent finished. Result:\n\n{shown}"
    assert hr._drain_fallback_message(text, "de") == \
        f"Der Sub-Agent ist fertig. Ergebnis:\n\n{shown}"
    assert hr._drain_fallback_message("  ", "en") == \
        "The sub-agent finished. Result:\n\n(no textual result payload)"
    assert hr._drain_fallback_message(None, "de").endswith("(kein Text-Ergebnis)")

    path = os.path.abspath("/home/user/project")
    straddle = "x" * 1990 + file_link(path) + " and more"
    message = hr._drain_fallback_message(straddle, "en")
    assert "[link=" not in message
    assert message == "The sub-agent finished. Result:\n\n" + ("x" * 1990 + path)[:2000]


def test_both_drain_fallback_sites_share_the_one_message():
    """AC 6. The chat and the messenger fallback build their text from one helper; no hand
    copy of the `[:2000]` cut of the raw result is left in the drain. MUTATION: restore one
    inline copy of the messenger body - red."""
    source = inspect.getsource(hr.run_headless_agent)
    assert source.count("_drain_fallback_message(combined_results, user_lang)") == 2
    assert not re.search(r"combined_results[^\n]*\[:2000\]", source)
    assert "_drain_fallback_head(user_lang)" in source    # the head alone stays the last resort
