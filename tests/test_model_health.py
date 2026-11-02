# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The model-health score: deterministic signals in any language, one cumulative score per chat.

The scorer (vaf/core/model_health.py) is pure, so most of it is pinned here without an agent:
the signals and their gates, the CUSUM, the measured stuck series replayed synthetically, the
ledger, the notes, the deletion of the guard it replaced, the provider retreat that must keep a
forced call forced, and the health row. The turn behaviour lives in test_model_health_turn.py.

MUTATIONS, each turns the named test red:
- add an `import requests` to the module -> test_scorer_is_pure;
- drop the question-mark gate, or hard-code de/en instead of `parse_reply` ->
  test_question_is_not_a_confirmation / test_confirmation_in_seven_languages;
- count step_repetition on a progress turn -> test_repeat_counts_only_without_progress;
- remove the 40-character drift gate, or move the chat language on "ja" ->
  test_drift_needs_40_chars / test_chat_language_is_sticky;
- weight 2 for reasoning_action_mismatch, reset only on a successful tool, K = 2 ->
  test_tool_naming_alone_never_accumulates / test_failed_tool_resets /
  test_replay_reaches_mid_then_high;
- delete the `tr` note -> test_stuck_note_languages;
- let a question fire the action signals -> test_follow_up_questions_about_a_tool_stay_below_mid;
- force a dangerous, system, irreversible, outward or deleting tool -> test_never_forced;
- no K drain without a scored generation -> test_commit_without_a_scored_generation_drains_by_k;
- compare only the head of long replies -> test_long_replies_that_only_open_alike_are_no_repeat;
- key the ledger file by scope, or accept any format -> the ledger tests;
- leave `false_promise_detection_enabled` in DEFAULTS -> test_deleted_guard_leaves_no_trace;
- retreat to "auto" unconditionally -> test_allowed_tools_retreat_keeps_required;
- put a session id into the metrics, or set the row degraded -> test_health_row_counts_only.
"""
import ast
import json
from pathlib import Path

import pytest

from vaf.core import model_health as mh

ROOT = Path(__file__).resolve().parents[1]


class _Lang:
    """The agent's own detector (`Agent._detect_user_language`), on a bare agent whose turn
    language is unclear, as it is on a turn whose whole text is "ja"."""

    def __init__(self):
        from vaf.core.agent import Agent
        agent = Agent.__new__(Agent)

        class _PM:
            user_language = "auto"
        agent.prompt_manager = _PM()
        self._agent = agent

    def __call__(self, text):
        return self._agent._detect_user_language(text)


@pytest.fixture(scope="module")
def detect():
    return _Lang()


# ── purity ───────────────────────────────────────────────────────────────────────────────

def test_scorer_is_pure():
    """No model client, no file access: the score costs a similarity ratio, never a call."""
    tree = ast.parse((ROOT / "vaf/core/model_health.py").read_bytes())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    forbidden = ("requests", "httpx", "urllib", "openai", "anthropic", "api_backend", "completion",
                 "vaf.core.agent", "data_files", "socket")
    hits = sorted(m for m in imported if any(f in m for f in forbidden))
    assert not hits, f"the scorer reaches for a client or a store: {hits}"
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in calls, "the scorer opens a file"


# ── confirmation ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, lang", [
    ("ja bitte fang an", "de"), ("JA BITTE FANG AN", "de"), ("ja", "de"),
    ("yes please", "en"), ("はい、お願いします", "ja"), ("네 시작해", "ko"),
    ("ใช่ เริ่มเลย", "th"), ("evet başla", "tr"), ("好的", "zh"),
])
def test_confirmation_in_seven_languages(text, lang):
    assert mh.is_confirmation(text, lang), (text, lang)


@pytest.mark.parametrize("text, lang", [
    ("sicher ?", "de"), ("はい？", "ja"), ("ja, aber erst morgen, heute habe ich keine Zeit dafuer", "de"),
    ("Nein danke", "de"), ("", "de"),
])
def test_question_is_not_a_confirmation(text, lang):
    assert not mh.is_confirmation(text, lang), (text, lang)


# ── signals and gates ────────────────────────────────────────────────────────────────────

ANNOUNCE = ("I will run the pip installs for all ten packages in the sandbox now. "
            "Let me run the commands directly.")


def test_repeat_counts_only_without_progress(detect):
    """A real, repeated "10/10 OK" report after real tool calls is healthy."""
    report = "10/10 Pakete erfolgreich installiert. Alles ist bereit."
    healthy = mh.evaluate(mh.State(4.0, "de"), reply=report, user_text="und nochmal bitte",
                          prev_reply=report, detect_language=detect, progressed=True)
    assert healthy.score == 0.0 and healthy.signals == ()
    stuck = mh.evaluate(mh.State(0.0, "de"), reply=report, user_text="und nochmal bitte",
                        prev_reply=report, detect_language=detect)
    assert "step_repetition" in stuck.signals


def test_drift_needs_40_chars(detect):
    state = mh.State(0.0, "de")
    short = mh.evaluate(state, reply="Gerne.", user_text="danke", detect_language=detect)
    assert "language_drift" not in short.signals
    long = mh.evaluate(state, reply=ANNOUNCE, user_text="danke", detect_language=detect)
    assert "language_drift" in long.signals


def test_chat_language_is_sticky(detect):
    """The detector calls "ja" unclear and "danke" English; neither moves a German chat."""
    first = mh.evaluate(None, reply="Mache ich.", detect_language=detect,
                        user_text="Bitte installiere mir diese 10 Pakete mit pip")
    assert first.chat_language == "de"
    state = mh.commit(None, first, progressed=False)
    for text in ("ja", "danke", "JA BITTE"):
        v = mh.evaluate(state, reply=ANNOUNCE, user_text=text, detect_language=detect)
        assert v.chat_language == "de", text
        assert "language_drift" in v.signals
    stored = mh.commit(state, mh.evaluate(state, reply="ok", user_text="ja", detect_language=detect),
                       progressed=False)
    assert stored.language == "de"


def test_tool_naming_alone_never_accumulates(detect):
    """The failure mode of the guard this replaced: a chat ABOUT tools never climbs."""
    state = mh.State()
    for turn in range(10):
        v = mh.evaluate(state, reply=f"`host_bash` führt einen Befehl in der Shell aus ({turn}).",
                        user_text=f"Erzähl mir noch etwas über host_bash, Teil {turn}.",
                        task_tools=["host_bash", "python_sandbox"], detect_language=detect)
        assert v.signals == ("reasoning_action_mismatch",), v.signals
        state = mh.commit(state, v, progressed=False)
        assert state.score == 0.0, (turn, state)


def test_failed_tool_resets():
    """A tool that RAN and failed is the normal error, not a stuck model: the score resets."""
    state = mh.State(7.0, "de")
    v = mh.evaluate(state, reply="Die Installation ist nach 600 s abgebrochen.",
                    user_text="ja", progressed=True)
    assert mh.commit(state, v, progressed=True) == mh.State(0.0, "de")


def test_replay_reaches_mid_then_high(detect):
    """The measured stuck series, as a synthetic fixture: a German instruction, an English
    announcement and no call, then the same reply to every "yes"."""
    turns = [
        "Bitte installiere mir diese 10 Pakete mit pip in der Sandbox",
        "ja bitte fang an",
        "JA BITTE FANG AN",
    ]
    state, scores, levels, prev_user, prev_reply = mh.State(), [], [], "", ""
    for text in turns:
        v = mh.evaluate(state, reply=ANNOUNCE, user_text=text, prev_reply=prev_reply,
                        prev_user=prev_user, task_tools=["python_sandbox", "host_bash"],
                        detect_language=detect)
        scores.append(v.score)
        levels.append(v.level)
        state = mh.commit(state, v, progressed=False)
        prev_user, prev_reply = text, ANNOUNCE
    assert levels == [mh.NONE, mh.MID_LEVEL, mh.HIGH_LEVEL], (levels, scores)
    assert scores == [1.0, 6.0, 12.0], scores


def test_signals_count_once_per_turn(detect):
    first = mh.evaluate(mh.State(2.0, "de"), reply=ANNOUNCE, user_text="ja",
                        detect_language=detect)
    again = mh.evaluate(mh.State(2.0, "de"), reply=ANNOUNCE, user_text="ja",
                        detect_language=detect, prior=first.signals)
    assert again.signals == first.signals and again.score == first.score
    lifted = mh.evaluate(mh.State(2.0, "de"), reply=ANNOUNCE, user_text="ja",
                         detect_language=detect, prior=first.signals, grounding_hit=True)
    assert lifted.score == first.score + mh.WEIGHTS["ungrounded"]


def test_action_tag_counts_only_a_task_tool():
    v = mh.evaluate(None, reply="x", user_text="los", task_tools=["web_search"],
                    action_matches=[("web_search", 1.0)])
    assert "action_tag_mismatch" in v.signals
    v = mh.evaluate(None, reply="x", user_text="los", task_tools=["web_search"],
                    action_matches=[("memory_search", 1.0), ("web_search", 0.5)])
    assert "action_tag_mismatch" not in v.signals


# ── notes ────────────────────────────────────────────────────────────────────────────────

def test_stuck_note_languages():
    langs = {"de", "en", "ja", "ko", "th", "tr", "zh"}
    assert set(mh.STUCK_NOTE) == langs
    assert len({mh.STUCK_NOTE[code] for code in langs}) == 7
    assert mh.stuck_note("de-DE") == mh.STUCK_NOTE["de"]
    assert mh.stuck_note("tr") == mh.STUCK_NOTE["tr"]
    assert mh.stuck_note("fr") == mh.STUCK_NOTE["en"]
    assert mh.stuck_note("auto") == mh.STUCK_NOTE["en"]
    assert mh.stuck_note(None) == mh.STUCK_NOTE["en"]


def test_grounding_note_shares_the_picker():
    from vaf.core.agent import _GROUNDING_NOTE, _grounding_note
    assert _grounding_note("ko_KR") == _GROUNDING_NOTE["ko"]
    assert _grounding_note("auto") == _GROUNDING_NOTE["en"]
    source = (ROOT / "vaf/core/agent.py").read_text(encoding="utf-8")
    body = source.split("def _grounding_note(", 1)[1].split("\ndef ", 1)[0]
    assert "pick_note(" in body and ".split(" not in body, "a second copy of the picker"


def test_retry_note_is_specific():
    note = mh.retry_note("Bitte installiere die 10 Pakete", ["python_sandbox", "host_bash"])
    assert 'the user asked: "Bitte installiere die 10 Pakete"' in note
    assert "one of python_sandbox, host_bash," in note
    assert "do not repeat any reason given above" in note
    one = mh.retry_note("Bitte installiere die 10 Pakete", ["python_sandbox"])
    assert "the tool python_sandbox," in one and "one of" not in one
    none = mh.retry_note("los", [])
    assert "one of" not in none and "now: the tool" not in none and "now, with real" in none


def test_retry_note_quotes_only_this_turns_words():
    """A plain yes is quoted as a confirmation of the person's own earlier message in the
    conversation; no stored request is ever quoted back with system authority."""
    note = mh.retry_note("ja bitte fang an", ["python_sandbox"], confirmed=True)
    assert 'the user confirmed: "ja bitte fang an"' in note
    assert "earlier message in the conversation above" in note
    assert "asked:" not in note


# ── a conversation ABOUT a tool never climbs ─────────────────────────────────────────────

QUESTIONS = [
    "Was macht das Tool host_bash eigentlich?",
    "Und kann host_bash auch Dateien löschen?",
    "Was macht host_bash, wenn ein Befehl hängt?",
    "Wie sicher ist host_bash eigentlich, ehrlich gesagt?",
]


@pytest.mark.parametrize("same_answer", [False, True])
def test_follow_up_questions_about_a_tool_stay_below_mid(detect, same_answer):
    """The measured probe: four German follow-up questions about host_bash climbed 0, 1, 2, 3
    (mid on the fourth) with different answers, and to mid on the second with a near-identical
    answer. A question fires none of the "should have acted" signals."""
    state, prev_user, prev_reply = mh.State(), "", ""
    for i, text in enumerate(QUESTIONS):
        reply = ("`host_bash` führt einen Befehl in der Shell dieses Rechners aus."
                 if same_answer else f"`host_bash` führt Befehl Nummer {i} in der Shell aus, "
                                     f"und dazu gibt es Antwort {i} mit eigenem Inhalt.")
        v = mh.evaluate(state, reply=reply, user_text=text, prev_reply=prev_reply,
                        prev_user=prev_user, task_tools=["host_bash"], detect_language=detect)
        assert v.asks and v.signals == () and v.level == mh.NONE, (i, v)
        state = mh.commit(state, v, progressed=False)
        prev_user, prev_reply = text, reply
    assert state.score == 0.0


def test_is_question_reads_every_script():
    for text in ("Was macht host_bash?", "Was macht host_bash? Erklär mal.", "これは何？",
                 "هل تريد ذلك؟", "Ծանոթ ե՞ս", "kurz ❓"):
        assert mh.is_question(text), text
    for text in ("ja bitte fang an", "Mach das jetzt.", ""):
        assert not mh.is_question(text), text


def test_a_question_still_counts_drift_and_ungrounded(detect):
    v = mh.evaluate(mh.State(0.0, "de"), reply=ANNOUNCE, user_text="Was macht host_bash?",
                    task_tools=["host_bash"], detect_language=detect, grounding_hit=True)
    assert v.asks and set(v.signals) == {"language_drift", "ungrounded"}


# ── what may be forced ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, permission, side_effect, outward, expected", [
    ("python_sandbox", "write", "reversible", False, True),
    ("web_search", "read", "none", False, True),
    ("host_bash", "dangerous", "irreversible", False, False),
    ("settings_tool", "system", "reversible", False, False),
    ("send_mail", "write", "irreversible", True, False),
    ("send_telegram", "write", "reversible", True, False),
    ("delete_mail", "write", "reversible", False, False),
    ("remove_member", "write", "reversible", False, False),
    ("write_file", "write", "irreversible", False, False),
])
def test_never_forced(name, permission, side_effect, outward, expected):
    assert mh.forceable(name, permission, side_effect, outward=outward) is expected


def test_commit_without_a_scored_generation_drains_by_k():
    assert mh.commit(mh.State(5.0, "de"), None, progressed=False) == mh.State(4.0, "de")
    assert mh.commit(mh.State(0.5, "de"), None, progressed=False) == mh.State(0.0, "de")
    assert mh.commit(mh.State(5.0, "de"), None, progressed=True) == mh.State(0.0, "de")


def test_long_replies_that_only_open_alike_are_no_repeat():
    head = "Hier ist der Bericht. " * 120          # 2640 characters, the same opening
    a = head + "Ergebnis A: alle Pakete installiert, keine Fehler, fertig. " * 30
    b = head + "Ergebnis B: zwei Pakete fehlen noch, bitte Netzwerk pruefen. " * 30
    assert len(head) > mh.REPEAT_CHARS
    assert mh.similarity(a, b) < mh.REPEAT_RATIO
    assert mh.similarity(a, a) == 1.0


# ── the ledger ───────────────────────────────────────────────────────────────────────────

@pytest.fixture
def stores(tmp_path, monkeypatch):
    from vaf.core.main_persistence import MainPersistenceManager
    monkeypatch.chdir(tmp_path)
    return lambda sid: MainPersistenceManager(str(tmp_path), session_id=sid)


def test_ledger_is_per_session(stores):
    a, b = stores("green123456"), stores("red654321")
    a.save_model_health(mh.to_payload(mh.State(6.0, "de")))
    assert mh.from_payload(stores("green123456").get_model_health()) == mh.State(6.0, "de")
    assert b.get_model_health() == {}
    assert mh.from_payload(b.get_model_health()) == mh.State()


def test_ledger_foreign_tag_discarded(stores):
    store = stores("green123456")
    store.save_model_health({"format": "modelhealth-9-000000", "score": 9.0, "language": "de"})
    assert mh.from_payload(store.get_model_health()) == mh.State()
    store.save_model_health({"score": 4.0, "language": "de"})       # untagged: read
    assert mh.from_payload(store.get_model_health()) == mh.State(4.0, "de")
    for bad in ({"format": mh.LEDGER_FORMAT, "score": "nan"}, {"format": mh.LEDGER_FORMAT, "score": -3},
                None, [1, 2], {"format": mh.LEDGER_FORMAT, "score": "x"}):
        assert mh.from_payload(bad).score == 0.0, bad


def test_ledger_holds_no_text(stores, detect):
    words = "Bitte installiere mir Paket QZXWV mit pip"
    v = mh.evaluate(None, reply="Ich kann QZXWV nicht installieren.", user_text=words,
                    detect_language=detect)
    store = stores("green123456")
    store.save_model_health(mh.to_payload(mh.commit(None, v, progressed=False)))
    payload = store.get_model_health()
    assert set(payload) == {"format", "score", "language", "updated_at"}
    assert payload["format"] == "modelhealth-1-d52e8a"
    assert "QZXWV" not in json.dumps(payload)
    raw = (store.context_dir / "model_health.json").read_bytes()
    assert b"QZXWV" not in raw


# ── the conversion deleted what it replaced ──────────────────────────────────────────────

GONE = ("false_promise_detection_enabled", "_detect_false_tool_promise", "_false_promise_retries",
        "_max_false_promise_retries", "_check_language_mismatch", "_user_requested_translation",
        "_detect_mixed_languages", "_last_language_mismatch")


def test_deleted_guard_leaves_no_trace():
    this = Path(__file__).resolve()
    found = []
    for folder in ("vaf", "tests", "docs"):
        for path in (ROOT / folder).rglob("*"):
            if not path.is_file() or path.resolve() == this or path.suffix not in (
                    ".py", ".md", ".json", ".ts", ".tsx", ".txt", ".yml", ".yaml"):
                continue
            text = path.read_bytes().decode("utf-8", "replace")
            found += [f"{path.relative_to(ROOT).as_posix()}: {name}" for name in GONE if name in text]
    assert not found, found
    from vaf.core.config import Config
    assert "false_promise_detection_enabled" not in Config.DEFAULTS
    assert Config.DEFAULTS["model_health_guard_enabled"] is True
    assert "model_health_guard_enabled" in Config.GLOBAL_CONFIG_KEYS
    schema = (ROOT / "docs/setup/CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "| `model_health_guard_enabled` | `True` |" in schema


# ── the provider retreat keeps a forced call forced ──────────────────────────────────────

def _tool(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


def test_allowed_tools_retreat_keeps_required(monkeypatch):
    """A provider that refuses `allowed_tools` is retried once. A restriction in mode "auto"
    retries plainly; one in mode "required" was a FORCED call and stays one, narrowed."""
    from vaf.core.api_backend import OpenAIProvider
    provider = OpenAIProvider("openai", "sk-test", base_url="http://127.0.0.1:9/v1")
    sent = []

    def create(kwargs):
        sent.append({"tools": [t["function"]["name"] for t in kwargs.get("tools") or []],
                     "tool_choice": kwargs.get("tool_choice")})
        if len(sent) == 1:
            raise RuntimeError("Error code: 400 - Invalid value for 'tool_choice': allowed_tools")
        return iter(())
    monkeypatch.setattr(provider, "_create_with_retry", create)
    tools = [_tool("web_search"), _tool("python_sandbox"), _tool("ask_user")]
    forced = {"type": "allowed_tools", "allowed_tools": {
        "mode": "required", "tools": [{"type": "function", "function": {"name": "python_sandbox"}}]}}
    list(provider.chat_completion([{"role": "user", "content": "x"}], 0.7, 100, True, "gpt-4o-mini",
                                  tools, forced))
    assert provider.allowed_tools_refused is True
    assert sent[1] == {"tools": ["python_sandbox"], "tool_choice": "required"}, sent

    sent.clear()
    provider.allowed_tools_refused = False
    restricted = {"type": "allowed_tools", "allowed_tools": {
        "mode": "auto", "tools": [{"type": "function", "function": {"name": "python_sandbox"}}]}}
    list(provider.chat_completion([{"role": "user", "content": "x"}], 0.7, 100, True, "gpt-4o-mini",
                                  tools, restricted))
    assert sent[1] == {"tools": ["web_search", "python_sandbox", "ask_user"], "tool_choice": "auto"}


# ── the health row ───────────────────────────────────────────────────────────────────────

def test_health_row_counts_only(monkeypatch):
    from vaf.core import health
    mh.reset_metrics()
    quiet = dict(status_probe=lambda: {}, worker_probe=lambda: None,
                 usage=lambda p: type("U", (), {"free": 40 * 1024 ** 3})(),
                 backup_probe=lambda: {"name": "backup", "status": "ok", "reason": "fine"},
                 engine_probe=lambda: None, queue_probe=lambda: [])
    names = [c["name"] for c in health.collect_health(**quiet, model_probe=mh.metrics)["checks"]]
    assert "model" not in names, "a row with nothing scored"

    mh.record(mh.NONE, mh.REACTION_NONE)
    mh.record(mh.MID_LEVEL, mh.REACTION_FORCED)
    mh.record(mh.MID_LEVEL, mh.REACTION_RETRY)
    mh.record(mh.HIGH_LEVEL, mh.REACTION_NOTE)
    mh.record(mh.HIGH_LEVEL, mh.REACTION_NOTE, now=1.0)      # outside the hour
    result = health.collect_health(**quiet, model_probe=mh.metrics)
    row = next(c for c in result["checks"] if c["name"] == "model")
    assert row["status"] == "ok"
    assert row["reason"] == ("In the last 60 minutes: 4 turns scored, 2 health retries "
                             "(1 forced), 1 stuck note.")
    assert row["metrics"] == {"window_s": 3600, "scored": 4, "mid": 2, "high": 1,
                              "retries": 2, "forced": 1, "notes": 1}
    # Counts and nothing else: no session, no account, no text reaches the row.
    assert set(mh.metrics()) == {"window_s", "scored", "mid", "high", "retries", "forced", "notes"}

    # Only in the process that runs the agent loops (the agent row's rule).
    monkeypatch.setattr(health, "_worker_status", lambda: None)
    assert health._model_metrics() is None
    monkeypatch.setattr(health, "_worker_status", lambda: {"started": 1, "alive": 1})
    assert health._model_metrics()["scored"] == 4
    mh.reset_metrics()
