# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser's half of a turn shown per generation round (web/components/chat/turnRounds.ts,
web/app/page.tsx).

The helpers are the web's REAL code, transpiled with the app's own TypeScript and run in node
(the pattern of tests/test_ask_user_chat.py): a copy here would test the copy. Skipped without
node. The wiring in page.tsx is pinned at the source, because the handlers live inside one
React effect.

MUTATIONS, each turns the named test red:
- update only `messages[last]` -> test_place_round_update_late_tail;
- cut the previous bubble's text off as a prefix delta -> test_place_round_update_no_prefix_strip;
- ignore `turnId` -> test_place_round_update_keeps_other_turns;
- always append -> test_place_round_update_insert_order;
- fall back to the newest assistant in `removeRoundBubble` -> test_remove_round_bubble_only_that_round;
- treat `roundAnswer` rounds as 'say', or put a flagged round into the body -> test_body_and_say;
- drop `round` or `roundAnswer` from the history_update rebuild
  -> test_history_update_forwards_round_fields.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "web" / "components" / "chat" / "turnRounds.ts"
PAGE = ROOT / "web" / "app" / "page.tsx"
_TS = ROOT / "web" / "node_modules" / "typescript"


def _call(fn, *args):
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const [fn, args] = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(m.exports[fn](...args)));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(MODULE), json.dumps([fn, list(args)])],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _user(text="Bitte installieren", ts=1):
    return {"role": "user", "content": text, "timestamp": ts}


def _bubble(round_, text, ts, turn="t2", **extra):
    return {"role": "assistant", "content": text, "timestamp": ts, "turnId": turn, "round": round_, **extra}


def _tool(tool_id="c1", ts=5):
    return {"role": "tool", "content": "", "timestamp": ts, "toolId": tool_id}


def _place(messages, round_, content, turn="t2", answer=None, now=100):
    upd = {"round": round_, "content": content, "turnId": turn, "now": now}
    if answer is not None:
        upd["answer"] = answer
    return _call("placeRoundUpdate", messages, upd)


def test_place_round_update_late_tail():
    """A throttled tail of round 1 arrives after round 2's tool card: it updates round 1's
    bubble where it stands, above the card."""
    msgs = [_user(), _bubble(1, "<think>A</think>", 2), _tool(), _bubble(2, "<think>B</think>", 6)]
    placed = _place(msgs, 1, "<think>A</think>\n\nIch installiere jetzt.")
    assert placed["created"] is False and placed["index"] == 1
    assert placed["messages"][1]["content"] == "<think>A</think>\n\nIch installiere jetzt."
    assert [m.get("round") for m in placed["messages"]] == [None, 1, None, 2]


def test_place_round_update_new_round_below_the_card():
    msgs = [_user(), _bubble(1, "Ich schaue nach.", 2), _tool()]
    placed = _place(msgs, 2, "<think>C</think>\n\nFertig.", answer=True)
    assert placed["created"] is True and placed["index"] == 3
    new = placed["messages"][3]
    assert (new["round"], new["roundAnswer"], new["turnId"], new["content"]) == (
        2, True, "t2", "<think>C</think>\n\nFertig.")


def test_place_round_update_insert_order():
    """A round whose first update comes late is put in front of a later round's bubble."""
    msgs = [_user(), _bubble(1, "A", 2), _tool(), _bubble(3, "C", 100)]
    placed = _place(msgs, 2, "B", now=100)
    assert [m.get("round") for m in placed["messages"]] == [None, 1, None, 2, 3]
    assert placed["index"] == 3
    times = [m["timestamp"] for m in placed["messages"] if m.get("round")]
    assert len(set(times)) == 3, "two bubbles of one turn share a timestamp"


def test_place_round_update_keeps_other_turns():
    """An earlier turn's bubble of the same round, a bubble of another exchange and a
    proactive message (`kind`) are never touched."""
    earlier = [_user("frueher", 0), _bubble(1, "Alte Antwort.", 1, turn="t1"), _user()]
    placed = _place(earlier, 1, "Neue Antwort.")
    assert placed["messages"][1]["content"] == "Alte Antwort." and placed["created"] is True
    other = [_user(), _bubble(1, "Andere Runde.", 2, turn="t1")]
    placed = _place(other, 1, "Neue Antwort.")
    assert placed["messages"][1]["content"] == "Andere Runde." and placed["created"] is True
    nudge = [_user(), _bubble(1, "Bist du noch da?", 2, kind="nudge")]
    placed = _place(nudge, 1, "Neue Antwort.")
    assert placed["messages"][1]["content"] == "Bist du noch da?" and len(placed["messages"]) == 3


def test_place_round_update_no_prefix_strip():
    """The update is the round's own text: nothing is cut off as a delta of the bubble before."""
    msgs = [_user(), _bubble(1, "Hallo", 2)]
    placed = _place(msgs, 2, "Hallo Welt")
    assert placed["messages"][2]["content"] == "Hallo Welt"
    assert _place(msgs, 2, "   \n")["messages"] == msgs, "whitespace made a bubble"


def test_remove_round_bubble_only_that_round():
    msgs = [_user(), _bubble(1, "A", 2), _tool(), _bubble(2, "B", 6)]
    assert [m.get("round") for m in _call("removeRoundBubble", msgs, 2)] == [None, 1, None]
    assert _call("removeRoundBubble", msgs, 3) == msgs, "a round without a bubble removed another"
    assert _call("removeRoundBubble", [_user(), _bubble(1, "A", 2), _tool()], 2) == \
        [_user(), _bubble(1, "A", 2), _tool()]


def test_body_and_say():
    flagged = [{"text": "Ich installiere jetzt.", "roundAnswer": False}, {"text": ""},
               {"text": "Fertig."}]
    assert _call("bodyAndSay", flagged, 2) == {"bodyLead": [], "say": [0]}
    answered = [{"text": "Hier ist die Liste.", "roundAnswer": True},
                {"text": "Einen Moment.", "roundAnswer": False}, {"text": "Alles erledigt."}]
    assert _call("bodyAndSay", answered, 2) == {"bodyLead": [0], "say": [1]}
    legacy = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    assert _call("bodyAndSay", legacy, 2) == {"bodyLead": [], "say": [0, 1]}


def _page():
    return PAGE.read_text(encoding="utf-8")


def test_history_update_forwards_round_fields():
    """Rule 2: the rebuild copies field by field, so an unforwarded field is dropped."""
    src = _page()
    rebuild = src.split("const serverMsgs: Array<Message & { _order: number }> = data.messages", 1)[1]
    rebuild = rebuild.split("}));", 1)[0]
    assert "round: typeof m.round === 'number' ? m.round : undefined" in rebuild
    assert "roundAnswer: typeof m.roundAnswer === 'boolean' ? m.roundAnswer : undefined" in rebuild


def test_page_routes_round_frames_through_the_helpers():
    src = _page()
    update = src.split("else if (data.type === 'agent_message_update') {", 1)[1]
    update = update.split("else if (data.type === 'agent_message_append')", 1)[0]
    branch = update.split("if (typeof data.round === 'number') {", 1)[1].split("return;\n", 1)[0]
    assert "placeRoundUpdate(prev, { ...roundUpd, now: Date.now() })" in branch
    assert "answer: typeof data.answer === 'boolean' ? data.answer : undefined" in update
    clear = src.split("else if (data.type === 'clear_last_assistant') {", 1)[1].split("else if", 1)[0]
    assert "removeRoundBubble(prev, data.round)" in clear
    assert "bodyAndSay(" in src and "bodyLead:" in src
    assert "roundAnswer?: boolean;" in src and "round?: number;" in src
