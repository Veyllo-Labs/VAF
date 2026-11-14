# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A reloaded chat shows each reply as it streamed; the engine's control heads are taken off
when a reply is stored.

MEASURED BEFORE THE FIX: the transcript projection (`_history_projection`, the reload, a chat
switch, the forget and regenerate repaints) deleted every `[...]` group of a stored reply:
`arr[i]` became `arr`, `app/[id]/page.tsx` lost its folder, a task list lost its boxes, a
stopped reply ended in `**`, and `a[0] and the dimension[1]` folded the rest of the reply away
as reasoning. That deletion also hid the `[ASYNC_ACK]` and `[SYSTEM_LOG_ONLY]` heads the
stored copy of a turn's return value kept. Now the copies are stored without the head
(`returned_turn_copy`), and the projection only does three things, for chats stored before:
a leading head comes off, VAF's own file link shows as its path, and a balanced `[dim]` pair
in a chat dated before February 2026 becomes reasoning.

Every input is a Python literal on a synthetic in-memory session; nothing is read from or
written to a real store.
"""
import os
import re
from pathlib import Path

import pytest

from vaf.core import headless_runner as hr
from vaf.core.session import (Message, Session, TurnStatus, is_failed_turn, model_text,
                              returned_turn_copy)
from vaf.core.terminal_text import file_link

SID = "green123456"
NOW = "2026-11-14T10:00:00"
OLD = "2026-01-10T09:00:00"
ROOT = Path(__file__).resolve().parents[1]


def _session(*messages):
    s = Session(id=SID, name="Chat")
    for role, content, timestamp, metadata in messages:
        s.messages.append(Message(role=role, content=content, timestamp=timestamp,
                                  metadata=metadata))
    return s


def _projection(loaded):
    # Imported per call, like the suite's other projection tests: the web server module sets
    # up its app on import, which collection should not do.
    from vaf.core.web_server import _history_projection
    return _history_projection(loaded, SID)


def _stored_reply_text(text, timestamp=None):
    from vaf.core.web_server import _stored_reply_text as stored_reply_text
    return stored_reply_text(text, timestamp)


def _shown(content, timestamp=NOW):
    out = _projection(_session(("user", "Frage", timestamp, None),
                                       ("assistant", content, timestamp, None)))
    return [e["content"] for e in out if e["role"] == "assistant"]


AS_WRITTEN = [
    "Use arr[i] to index.",
    "See [the docs](https://e.org).",
    "- [x] done\n- [ ] open",
    "Edit app/[id]/page.tsx now.",
    "a[0] and the dimension[1] matter.",
    "Partial answer\n\n*[Generation stopped]*",
    "*[Generation stopped]*",
    "[Generation stopped by user]",
    "[Error] No response was produced.",
    "Matrix m[i][j] and [bold]b[/bold].",
    "<think>plan</think>Answer with x[0].",
    "Rich: [dim]text[/dim]",
]


@pytest.mark.parametrize("text", AS_WRITTEN)
def test_reload_keeps_every_bracket_of_a_reply(text):
    """MUTATION: put back the blanket `[...]` deletion - red; put back the `[...dim...]`
    rewrite - red on `a[0] and the dimension[1]`."""
    assert _shown(text) == [text]


def test_round_texts_keep_their_brackets():
    """A turn stored per round projects each round's text as stored. MUTATION: apply the
    old cleaner on the round path only - red."""
    rounds = [{"round": 1, "text": "Look at a[0].", "tool_call_ids": [], "answer": False},
              {"round": 2, "text": "Use arr[i].\n\n*[Generation stopped]*",
               "tool_call_ids": [], "answer": True}]
    s = _session(("user", "Frage", NOW, None),
                 ("assistant", "Look at a[0].Use arr[i].\n\n*[Generation stopped]*", NOW,
                  {"rounds": rounds}))
    out = _projection(s)
    assert [(e["round"], e["content"]) for e in out if e["role"] == "assistant"] == [
        (1, "Look at a[0]."), (2, "Use arr[i].\n\n*[Generation stopped]*")]


def test_a_stored_control_head_is_taken_off_only_at_the_start():
    """A chat stored before the heads were taken off at write time. MUTATION: drop the head
    step - red; take the marker off anywhere in the text - red on the quote."""
    assert _shown("[ASYNC_ACK]The coding agent is on it.") == ["The coding agent is on it."]
    assert _shown("[SYSTEM_LOG_ONLY]API returned empty responses repeatedly.") == [
        "API returned empty responses repeatedly."]
    quote = "Quote: [ASYNC_ACK] means the task runs on, [SYSTEM_LOG_ONLY] a notice."
    assert _shown(quote) == [quote]


def test_own_file_link_shows_its_path_and_a_foreign_link_stays():
    """MUTATION: drop `own_links_as_labels` from `_stored_reply_text` - red."""
    path = "/home/user/p"
    assert _shown(f"Created {file_link(path)} for you.") == [
        f"Created {os.path.abspath(path)} for you."]
    foreign = "See [link=https://e.org]x[/link] now."
    assert _shown(foreign) == [foreign]


def test_legacy_dim_pairs_convert_only_in_chats_before_february_2026():
    """MUTATION: drop the timestamp gate - red on the November `[dim]` pair; drop the pair
    conversion - red on the January ones; accept an unbalanced `[dim]` - red on
    `shape[dim]`."""
    assert _shown("[white dim]why[/]Answer x[0].", OLD) == ["<think>why</think>Answer x[0]."]
    assert _shown("[dim]why[/dim]Answer.", OLD) == ["<think>why</think>Answer."]
    assert _shown("[DIM]why[/DIM]Answer.", OLD) == ["<think>why</think>Answer."]
    for text in ("[white dim]why[/]Answer x[0].", "[dim]why[/dim]Answer."):
        assert _shown(text, NOW) == [text]
        assert _stored_reply_text(text) == text
        assert _stored_reply_text(text, "not a date") == text
    assert _shown("shape[dim] ok", OLD) == ["shape[dim] ok"]
    # Each opener takes the next closer after it; an opener with no closer stays.
    assert _shown("[dim]a[dim]b[/dim]c [dim]d", OLD) == ["<think>a[dim]b</think>c [dim]d"]


def test_the_legacy_dim_cut_off_is_the_first_of_february_2026():
    """The engine streamed that markup until 2026-01-29. MUTATION: move
    `_LEGACY_DIM_BEFORE` to 2026-03-01 - red on the February case; to 2026-01-31 - red on the
    January case."""
    assert _shown("[dim]why[/dim]Answer.", "2026-01-31T23:59:59") == [
        "<think>why</think>Answer."]
    assert _shown("[dim]why[/dim]Answer.", "2026-02-01T00:00:00") == ["[dim]why[/dim]Answer."]


def test_many_unclosed_dim_openers_cost_one_pass():
    """MEASURED: one regex with a lazy body took 28.6 s on 32000 openers without a closer,
    because every opener rescanned the rest of the text; the pairing is now one pass (0.2 ms
    there). The bound is generous for a slow runner and far below the quadratic cost.
    MUTATION: pair with the single lazy regex again - red."""
    import time
    text = "[dim]" * 16000 + "[white dim]a[b" * 16000
    started = time.perf_counter()
    assert _stored_reply_text(text, OLD) == text
    assert time.perf_counter() - started < 1.0


def test_the_chat_lane_stores_a_return_value_without_its_head():
    """MUTATION: store `_clean_for_session(response_text)` with `failed_turn_meta(turn.failed)`
    again - red."""
    assert hr._chat_turn_copy([], "[ASYNC_ACK]The coding agent is on it.", ends_at_draft=False,
                              turn=TurnStatus()) == ("The coding agent is on it.", None)
    assert hr._chat_turn_copy([], "[SYSTEM_LOG_ONLY]API returned empty responses repeatedly.",
                              ends_at_draft=False, turn=TurnStatus()) == (
        "API returned empty responses repeatedly.", {"turn_failed": True})
    # A streamed copy is the stream, whatever the return value says.
    assert hr._chat_turn_copy(["Looking."], "[ASYNC_ACK]On it.", ends_at_draft=False,
                              turn=TurnStatus()) == ("Looking.", None)


_HAND_STRIP = re.compile(
    r"""\.replace\(\s*(?:["']\[(?:ASYNC_ACK|SYSTEM_LOG_ONLY)\]["']"""
    r"""|(?:markers\.)?(?:ASYNC_ACK|SYSTEM_LOG_ONLY)\b)""")


def test_no_hand_written_control_head_strip_is_left():
    """A `.replace` takes the marker off anywhere in the text, also from a reply that quotes
    it; `split_control_head` takes only the head. MUTATION: put back the `.replace` in the
    interactive CLI or the queue runner - red."""
    hits = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _HAND_STRIP.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{no}: {line.strip()}")
    assert not hits, "hand-written control head strip:\n" + "\n".join(hits)


def test_the_guard_pattern_catches_each_shape():
    for line in ('t.replace("[ASYNC_ACK]", "")', "t.replace('[SYSTEM_LOG_ONLY]', '')",
                 "t.replace(markers.ASYNC_ACK, '')", "t.replace(SYSTEM_LOG_ONLY, '')"):
        assert _HAND_STRIP.search(line), line
    assert not _HAND_STRIP.search('t.replace("[Task:", "")')


def test_model_text_reads_a_legacy_head_like_the_live_history():
    """MUTATION: drop the head step in `model_text` - red; drop the `[SYSTEM_LOG_ONLY]`
    clause of `is_failed_turn` - red; take off any head, not only `[ASYNC_ACK]` - red."""
    assert model_text({"role": "assistant", "content": "[ASYNC_ACK]On it."}) == "On it."
    notice = {"role": "assistant", "content": "[SYSTEM_LOG_ONLY]API returned empty."}
    assert is_failed_turn(notice) and model_text(notice) is None
    quote = {"role": "assistant", "content": "Quote: [ASYNC_ACK] means it runs on."}
    assert model_text(quote) == quote["content"] and not is_failed_turn(quote)
    # Only the delegation's head comes off: a `[SYSTEM_LOG_ONLY]` text that is not a failed
    # turn (a tool-call message never is one, its calls anchor their results) stays whole.
    calls = {"role": "assistant", "content": "[SYSTEM_LOG_ONLY]x",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "t", "arguments": "{}"}}]}
    assert model_text(calls) == "[SYSTEM_LOG_ONLY]x"
    # A user's own words are never cut.
    assert model_text({"role": "user", "content": "[ASYNC_ACK]x"}) == "[ASYNC_ACK]x"
    # The stored copy and the reader agree.
    copy, meta = returned_turn_copy("[ASYNC_ACK]On it.", TurnStatus())
    assert model_text({"role": "assistant", "content": copy, "metadata": meta}) == "On it."


def test_the_prompt_interface_chat_switch_uses_the_engine_replay():
    """`session <id>` in the prompt interface put the stored content back into the agent's
    history by hand: raw, failed turns and old heads included, and through `msg.get` on the
    `Message` objects a loaded chat holds, which raised, so the switch failed every time.
    It now goes through `load_session_context`, which reads each message through
    `model_text`. MUTATION: put the hand-written loop back - red."""
    src = (ROOT / "vaf" / "cli" / "cmd" / "run.py").read_text(encoding="utf-8")
    start = src.index('tui.confirm("Switch to this session?"')
    block = src[start:src.index('tui.success("Session restored to agent context!")', start)]
    assert "agent.load_session_context(loaded_session.id, force=True)" in block
    assert "agent.history.append" not in block and "msg.get(" not in block
