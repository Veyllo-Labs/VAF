# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A turn's reply is cleaned of MARKED reasoning only, never of a phrase guess.

`Agent._clean_reasoning` builds the value `chat_step` returns (`vaf run -p`, `vaf prompt`, the
facade's `Agent.run()`, agent rooms, channels on `[ASYNC_ACK]`, automations), and
`SpeechManager._clean_markdown` builds the text read aloud. Both used to delete, by a list of
English phrases, every line that began "I will", "I need to", "Let me check", "Okay, the user",
"First, I should" or "The user wants" (a one-paragraph answer to the end of the text), and
everything before "answer in ..." / "respond in ..." / "Antworte auf ...".

MEASURED BEFORE THE FIX: 11 of 11 plain answers below lost text, 5 of them everything (a one-line
"I will send the report ..." came back as ""); the recorded real leak (`LEAKED`) was not caught,
only cut in the middle. And `chat_step` built the returned reply from `full_response`, which also
holds a local engine's reasoning channel WITHOUT its `<think>` marks: a German reasoning text came
back glued in front of the answer, an English one ("Okay, the user ...") took the answer with it.

MUTATIONS, each turns the named test red:
- restore the phrase list and the "Answer in" cut in `_clean_reasoning`
  -> test_plain_text_is_the_answer (every row), test_api_turn_returns_its_answer_whole,
     test_cleaners_keep_unmarked_text (every seed), test_no_phrase_list_in_the_cleaners;
- call `strip_think_blocks` without `greedy=True` in `_clean_reasoning`
  -> test_marked_reasoning_is_still_removed[greedy-to-last-close];
- keep the text after an unclosed opener in `strip_think_blocks`
  -> the unclosed rows, test_strip_think_blocks_keeps_its_rule, test_cleaners_keep_unmarked_text;
- drop re.IGNORECASE from `_THINK_TAG` -> the upper and mixed-case rows, the fuzz;
- drop the `<thinking>` alias from `_THINK_TAG` -> the thinking rows, the fuzz,
  test_strip_think_blocks_rows[joined-alias];
- keep the text after a tag that dropping a block joined -> test_strip_think_blocks_rows[joined-*];
- one more pass per joined tag instead of the cut -> test_strip_think_blocks_is_linear;
- restore the lazy-regex `strip_think_blocks` -> test_strip_think_blocks_is_linear and
  test_strip_think_blocks_rows[length-changing-lower];
- strip the redacted blocks AFTER strip_think_blocks in `_clean_reasoning`
  -> test_marked_reasoning_is_still_removed[think-quoted-in-redacted*];
- give `_final_answer_probe` its own <think> regexes again -> test_probe_reads_the_cleaner_rule,
  test_a_turn_with_only_marked_reasoning_is_asked_again;
- skip the second redacted pass in `_strip_marked_reasoning`
  -> test_marked_reasoning_is_still_removed[redacted-*-joined];
- skip the cut at a redacted opener left unclosed -> [redacted-unclosed];
- let the probe call strip_think_blocks without the redacted pass
  -> test_probe_counts_the_answer_the_chat_shows[thinking-quoted-in-redacted],
     test_probe_reads_the_cleaner_rule[redacted-only];
- make the probe greedy -> test_probe_counts_the_answer_the_chat_shows[answer-between-blocks];
- drop the probe's greedy second reading
  -> test_probe_counts_the_answer_the_chat_shows[stray-closer-after-redacted];
- skip strip_think_blocks after the second redacted pass
  -> test_marked_reasoning_is_still_removed[think-joined-by-redacted-pass];
- skip the cut at a redacted opener that cleanup joined
  -> test_marked_reasoning_is_still_removed[redacted-joined-by-cleanup];
- strip redacted blocks with the lazy regex again -> test_redacted_strip_is_linear;
- build the kept answer from `full_response` again
  -> test_local_turn_returns_content_not_the_reasoning_channel (all four rows: the reasoning
     comes back glued in front of the answer);
- restore the phrase list or the "Answer in" cut in `_clean_markdown`
  -> test_tts_speaks_the_answer_as_written, test_cleaners_keep_unmarked_text,
     test_no_phrase_list_in_the_cleaners.
"""
import ast
import copy
import inspect
import json
import random
import re
import textwrap

import pytest

from test_chat_step_return import TASK, _Install
from test_outbound_sanitizer import LEAKED
from vaf.core.agent import Agent
from vaf.core.completion import strip_think_blocks
from vaf.core.speech import SpeechManager

# Plain answers: every one of them lost text to the phrase guess.
ANSWERS = {
    "ans-okay-user-guide": (
        "Okay, the user guide on page 4 explains it: open Settings, choose Accounts and press "
        "Add. The new account shows up in the list right after that."),
    "ans-i-will-three-steps": (
        "I will explain this in three steps so you can follow along.\n\n"
        "Open the file, edit the line and save it. That is all."),
    "ans-i-will-one-line": "I will send the report to Alice at 9:00 tomorrow, as you asked.",
    "ans-i-need-to": "I need to know which server you mean before I can restart it: web1 or web2?",
    "ans-let-me-check-off": (
        "Let me check off what is done:\n- backup finished\n- update installed\n- reboot pending"),
    "ans-first-i-should": (
        "First, I should say that the numbers are estimates.\n"
        "The budget for next year is about twelve thousand euros."),
    "ans-user-wants-ticket": (
        "The user wants to be able to log in with a passkey - that is how the ticket starts, "
        "and the rest asks for a fallback code."),
    "ans-answer-in-detail": (
        "Your disk check is done. I will answer in detail below.\n\n"
        "The disk is 40% full and the rest looks healthy."),
    "ans-respond-in-writing": (
        "Dear Bob,\n\nthank you for your letter. Please respond in writing within 14 days. "
        "Kind regards, Alice"),
    "ans-de-antworte-auf": (
        "Hier ist dein Entwurf. Antworte auf seine Mail am besten noch heute, dann ist der "
        "Termin sicher."),
    "ans-system-prompt-answer-in": (
        "Add this line to your system prompt: Answer in German. Then every reply comes back "
        "in German."),
}

# Reasoning a model wrote WITHOUT marks: the phrase guess caught some of it, but it cannot be told
# from an answer, so it is kept. The lanes that leave VAF keep their own structural guards
# (headless_runner._strip_untagged_cot_prefix, voice_agent._META_REASONING_RE).
UNMARKED = {
    "unmarked-okay-user": (
        "Okay, the user greets me in German. I should answer briefly.\n"
        "Hallo! Wie kann ich dir helfen?"),
    "unmarked-answer-in": (
        "The user asked about the weather tomorrow. Answer in German. "
        "Morgen wird es sonnig und warm."),
    "leak-fixture": LEAKED,
}

PLAIN = {**ANSWERS, **UNMARKED}


def _clean(text: str) -> str:
    return Agent.__new__(Agent)._clean_reasoning(text)


def _speak(text: str) -> str:
    return SpeechManager.__new__(SpeechManager)._clean_markdown(text)


@pytest.mark.parametrize("row", list(PLAIN.values()), ids=list(PLAIN))
def test_plain_text_is_the_answer(row):
    assert _clean(row) == row.strip()


@pytest.mark.parametrize("text, expected", [
    ("<think>A</think>\n\nB", "B"),
    ("<think><think>A</think></think>B", "B"),
    ("<think>A</think>B</think>", ""),
    ("A <think>", "A"),
    ("<redacted_reasoning>A</redacted_reasoning>B", "B"),
    ("<think>Okay, the user greets me.</think>\n\nHallo! Wie geht es dir?", "Hallo! Wie geht es dir?"),
    ('Done, the file is saved. {"name": "x", "arguments": {}}', "Done, the file is saved."),
    ("Here is the summary for Bob.\n<think>\nOkay, the user also mentioned the private note.",
     "Here is the summary for Bob."),
    ("<THINK>Plan.</THINK>Answer.", "Answer."),
    ("<Think>Plan.</think>Answer.", "Answer."),
    ("Answer.\n<THINK>never closed", "Answer."),
    ("<thinking>Plan.</thinking>Answer.", "Answer."),
    ("<THINKING>Plan.</Thinking>\n\nAnswer.", "Answer."),
    ("Answer.\n<thinking>never closed", "Answer."),
    ("<redacted_reasoning>R <think> R</redacted_reasoning>Answer", "Answer"),
    ("<redacted_reasoning>The private note says the door code is 4711. I could explain the "
     "<think> tag, but keep it short.</redacted_reasoning>Hello Bob, models mark their "
     "reasoning with a special tag.",
     "Hello Bob, models mark their reasoning with a special tag."),
    ("<redacted_<think>x</think>reasoning>SECRET door code 4711</redacted_reasoning>Answer",
     "Answer"),
    ("<redacted_reasoning>SECRET</redacted_<think>x</think>reasoning>Answer", "Answer"),
    ("Answer <redacted_reasoning>SECRET, never closed", "Answer"),
    ("<thi<redacted_<think>x</think>reasoning>hidden</redacted_reasoning>nk>SECRET", ""),
    ("<redacted_reasoning<redact<think>x</think>ed_reasoning></redacted_reasoning>>SECRET door code 4711",
     ""),
], ids=["block", "doubled-tags", "greedy-to-last-close", "stray-tag", "redacted", "marked-okay-user",
        "leaked-tool-json", "unclosed-after-answer", "upper", "mixed-case", "upper-unclosed",
        "thinking", "thinking-upper", "thinking-unclosed", "think-quoted-in-redacted",
        "think-quoted-in-redacted-long", "redacted-opener-joined", "redacted-closer-joined",
        "redacted-unclosed", "think-joined-by-redacted-pass", "redacted-joined-by-cleanup"])
def test_marked_reasoning_is_still_removed(text, expected):
    assert _clean(text) == expected


# ─────────────────────────────────────────────────────────────────────────────
# strip_think_blocks, the primitive behind step 1: the same rule as before, linear, and greedy
# for a turn's reply
# ─────────────────────────────────────────────────────────────────────────────

def _reference_strip(text: str) -> str:
    """The regex form strip_think_blocks had before it became linear: its semantics, with
    <thinking> read as <think>."""
    if not text:
        return text
    text = re.sub(r"<(/?)thinking>", r"<\1think>", text, flags=re.IGNORECASE)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    m = re.search(r"<think>", text, flags=re.IGNORECASE)
    if m:
        text = text[:m.start()]
    return re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()


def _reference_greedy(text: str) -> str:
    text = re.sub(r"<(/?)thinking>", r"<\1think>", text, flags=re.IGNORECASE)
    return _reference_strip(re.sub(r"<think>[\s\S]*</think>", "", text, flags=re.IGNORECASE))


_TAG_TOKENS = ["<think>", "</think>", "<THINK>", "</Think>", "<thinking>", "</THINKING>", "a",
               "b ", " ", "\n", "x y", "think", "ok.", "\u00c4"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_strip_think_blocks_keeps_its_rule(seed):
    rng = random.Random(seed)
    bad = []
    for _ in range(500):
        t = "".join(rng.choice(_TAG_TOKENS) for _ in range(rng.randint(0, 30)))
        if strip_think_blocks(t) != _reference_strip(t):
            bad.append(("default", t))
        if strip_think_blocks(t, greedy=True) != _reference_greedy(t):
            bad.append(("greedy", t))
    assert bad == [], f"{len(bad)} mismatches, first: {bad[:3]}"


@pytest.mark.parametrize("text, greedy, expected", [
    ("<think>A</think>B</think>C", False, "BC"),
    ("<think>A</think>B</think>C", True, "C"),
    ("x <thi<think>a</think>nk>secret", False, "x"),
    ("x <thi<think>a</think>nk>secret", True, "x"),
    ("\u0130 <think>a", False, "\u0130"),
    ("keep <THINK>drop", True, "keep"),
    ("<thi<think>x</think>nking>secret", False, ""),
    ("<thi<think>x</think>nking>secret", True, ""),
    ("a " + "</" * 3 + "<think></think>" + "think>" * 3 + " secret", False, "a </</"),
    ("a <</think>/think> secret", False, "a"),
], ids=["closes-at-first", "greedy-to-last", "joined-tag", "joined-tag-greedy",
        "length-changing-lower", "upper-unclosed-greedy", "joined-alias", "joined-alias-greedy",
        "joined-nested-closers", "joined-closer"])
def test_strip_think_blocks_rows(text, greedy, expected):
    assert strip_think_blocks(text, greedy=greedy) == expected


def _nested_closers(k: int) -> str:
    s = "</think>"
    for _ in range(k):
        s = "<" + s + "/think>"
    return s


@pytest.mark.parametrize("text, expected", [
    ("Answer. " + "<think>a " * 14500, "Answer."),
    ("Answer. " + "</" * 16000 + "<think></think>" + "think>" * 16000, "Answer. " + "</" * 15999),
    ("Answer. " + _nested_closers(16000), "Answer. " + "<" * 15999),
], ids=["unclosed-openers", "nested-joined-closers", "nested-closers-recursive"])
def test_strip_think_blocks_is_linear(text, expected):
    """About 128 KB each. Measured: the lazy regex took 9.7 s on the openers, a pass per
    joined tag 9.8 s on the nested closers; the walk takes a few milliseconds on all three."""
    import time
    started = time.perf_counter()
    assert strip_think_blocks(text) == expected
    assert strip_think_blocks(text, greedy=True).startswith("Answer.")
    _clean(text)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("text", [
    "<thinking>Private plan.</thinking>",
    "<THINK>Private plan.</THINK>",
    "<Thinking>Private plan, never closed",
    "<redacted_reasoning>Private plan.</redacted_reasoning>",
], ids=["thinking", "upper", "thinking-unclosed", "redacted-only"])
def test_probe_reads_the_cleaner_rule(text):
    """The empty-response check sees no answer exactly where the cleaner returns none."""
    from vaf.core.agent import _final_answer_probe
    assert _clean(text) == ""
    assert _final_answer_probe(text) == ""


@pytest.mark.parametrize("text", [
    "<redacted_reasoning><thinking>R</redacted_reasoning>Die Zahl ist 42.",
    "<think>a</think>Hello<think>b</think>",
    "<think>plan</think><redacted_reasoning>more</think>Here is the result: 42 apples.",
], ids=["thinking-quoted-in-redacted", "answer-between-blocks", "stray-closer-after-redacted"])
def test_probe_counts_the_answer_the_chat_shows(text):
    """No retry for a reply with an answer on screen: a probe that called it empty would let a
    guard retract a bubble the person sees. The chat closes a leading block at its first
    </think> (row two shows "Hello", only the non-greedy reading finds it) unless a stray
    closer follows, then at the last one (row three shows the result, only the greedy reading
    finds it), so the probe counts an answer in either."""
    from vaf.core.agent import _final_answer_probe
    assert _final_answer_probe(text) != ""


def _redacted_shapes():
    n = 128 * 1024
    unit = "<redacted_reasoning>"
    return {
        "unclosed-openers": "Answer. " + unit * (n // len(unit)),
        "opener-and-text": "Answer. " + (unit + "x") * (n // (len(unit) + 1)),
        "think-block-then-openers": "<think>a</think>Answer. " + unit * (n // len(unit)),
        "opener-and-stray-closer": "Answer. " + (unit + "</think>") * (n // (len(unit) + 8)),
        "spliced-openers": "Answer. " + "<redacted_<think>x</think>reasoning>" * (n // 36),
    }


@pytest.mark.parametrize("name", list(_redacted_shapes()))
def test_redacted_strip_is_linear(name):
    """About 128 KB each. Measured: the lazy redacted regex, run twice, took 4.6 to 8.2 s per
    call on these, in the cleaner and the probe alike; the walk takes milliseconds."""
    import time
    from vaf.core.agent import _final_answer_probe
    text = _redacted_shapes()[name]
    started = time.perf_counter()
    assert _clean(text) == "Answer."
    _final_answer_probe(text)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("name", list(ANSWERS))
def test_tts_speaks_the_answer_as_written(name):
    row = ANSWERS[name]
    out = _speak(row)
    if name == "ans-let-me-check-off":
        # The bullets become sentences, as for any list; every word stays.
        words = [w for w in row.split() if w != "-"]
        assert [w.strip(".,:") for w in out.split()] == [w.strip(".,:") for w in words], out
    else:
        assert out.split() == row.split(), out


@pytest.mark.parametrize("text", [
    "<think>Okay, the user greets me.</think>Hallo! Wie geht es dir?",
    "<redacted_reasoning>The user wants a greeting.</redacted_reasoning>Hallo! Wie geht es dir?",
], ids=["think", "redacted"])
def test_tts_still_drops_marked_reasoning(text):
    assert _speak(text).split() == "Hallo! Wie geht es dir?".split()


# ─────────────────────────────────────────────────────────────────────────────
# The source: no phrase list left in either cleaner, the kept answer built from content
# ─────────────────────────────────────────────────────────────────────────────

_PHRASES = re.compile(r"Okay, the user|First, I should|Let me check|I need to|I will|The user wants"
                      r"|Answer\||Antworte")


def _string_constants(func) -> list:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None),
                                                               ast.Constant):
        body = body[1:]  # the docstring explains the rule and may name the phrases
    return [n.value for stmt in body for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@pytest.mark.parametrize("func", [Agent._clean_reasoning, SpeechManager._clean_markdown],
                         ids=["clean_reasoning", "clean_markdown"])
def test_no_phrase_list_in_the_cleaners(func):
    hits = [s for s in _string_constants(func) if _PHRASES.search(s)]
    assert hits == [], f"a phrase guess is back in {func.__qualname__}: {hits}"


# ─────────────────────────────────────────────────────────────────────────────
# The real chat_step: what a turn returns
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def api_agent(monkeypatch, tmp_path):
    """The real Agent on the API lane, the provider faked: one streamed round per turn."""
    from vaf.core.platform import Platform
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_THINKING_MODE", "VAF_IN_AUTOMATION"):
        monkeypatch.delenv(var, raising=False)
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    monkeypatch.setattr(a, "_try_workflow", lambda *x, **k: None)
    monkeypatch.setattr(a, "_route_tools", lambda text: [TASK])
    monkeypatch.setattr(a, "get_token_usage", lambda **k: (1000, 128000))
    monkeypatch.setattr(a, "analyze_intent", lambda *x, **k: 0.7)
    monkeypatch.setattr(a, "_detect_ungrounded_result_claim", lambda text, res: (False, None))
    a.tools[TASK] = _Install()
    a.current_session_id = "green123456"
    a._bind_session_persistence("green123456")

    def run(chunks):
        requests = []

        def provider(messages=None, **kw):
            if not kw.get("tools"):
                yield "ok"  # a side request (title, intent), not the turn's main request
                return
            requests.append(copy.deepcopy(messages))
            if len(requests) > 6:
                raise AssertionError("runaway turn: more than six requests")
            if chunks and isinstance(chunks[0], list):
                yield from chunks[min(len(requests), len(chunks)) - 1]
            else:
                yield from chunks
        a.api_backend.chat_completion = provider
        streamed = []
        returned = a.chat_step(user_input="Kannst du das erledigen?",
                               raw_user_input="Kannst du das erledigen?",
                               stream_callback=streamed.append)
        a.main_requests = len(requests)
        return "".join(c for c in streamed if isinstance(c, str)), returned

    a.run = run
    return a


@pytest.mark.parametrize("chunks, expected", [
    (["I will send the report to Alice at 9:00 tomorrow, as you asked."],
     "I will send the report to Alice at 9:00 tomorrow, as you asked."),
    (["<think>Plan.</think>", "I will answer in detail below.\n\nThe disk is 40% full."],
     "I will answer in detail below.\n\nThe disk is 40% full."),
], ids=["i-will-one-line", "marked-then-answer-in-detail"])
def test_api_turn_returns_its_answer_whole(api_agent, chunks, expected):
    streamed, returned = api_agent.run(chunks)
    assert expected in streamed
    assert returned == expected


def test_a_turn_with_only_marked_reasoning_is_asked_again(api_agent):
    """A generation that is all <thinking> leaves no answer, so the empty-response retry asks
    again; before, the probe read the alias as text and the turn returned "" as a success."""
    _streamed, returned = api_agent.run([["<thinking>Private plan.</thinking>"], ["Die Antwort."]])
    assert api_agent.main_requests == 2
    assert returned == "Die Antwort."


def _local_agent(monkeypatch, tmp_path, engine):
    from vaf.core.config import Config
    from vaf.core.platform import Platform
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: engine if key == "local_engine" else real(key, default)))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_THINKING_MODE", "VAF_IN_AUTOMATION"):
        monkeypatch.delenv(var, raising=False)
    a = Agent(register_signals=False, run_kind="chat", config_overrides={"provider": "local"})
    a.current_session_id = "green123456"
    a.api_backend = None
    a.use_server = True
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    monkeypatch.setattr(a, "_route_tools", lambda text: ["web_search"])
    monkeypatch.setattr(a, "get_token_usage", lambda **kw: (1000, 32768))
    monkeypatch.setattr(a, "manage_context", lambda *args, **kw: None)
    monkeypatch.setattr(a, "analyze_intent", lambda *args, **kw: 0.7)
    return a


class _Stream:
    status_code = 200
    text = ""

    def __init__(self, deltas):
        self._lines = [b"data: " + json.dumps({"choices": [{"delta": d}]}).encode() for d in deltas]
        self._lines.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        self._lines.append(b"data: [DONE]")

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        pass


class _Refused:
    status_code = 500
    text = "{}"

    def json(self):
        return {}


@pytest.mark.parametrize("engine, field", [("llama", "reasoning_content"), ("vllm", "reasoning")])
@pytest.mark.parametrize("reasoning", [
    "Okay, the user greets me. I should answer in German.",
    "Der Nutzer gruesst mich auf Deutsch, ich antworte kurz.",
], ids=["en-okay-user", "de-plain"])
def test_local_turn_returns_content_not_the_reasoning_channel(monkeypatch, tmp_path, engine, field,
                                                               reasoning):
    import requests
    agent = _local_agent(monkeypatch, tmp_path, engine)
    deltas = [{field: reasoning}, {"content": "Hallo zurueck."}]
    payloads, chunks = [], []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            payloads.append(json)
            return _Stream(deltas)
        return _Refused()
    monkeypatch.setattr(requests, "post", post)
    returned = agent.chat_step(user_input="Hallo", raw_user_input="Hallo",
                               stream_callback=chunks.append)
    shown = "".join(c for c in chunks if isinstance(c, str))
    assert payloads, "the turn never reached the local server"
    # The person still sees the reasoning, marked as such.
    assert "<think>" in shown and reasoning in shown and "</think>" in shown, shown
    assert returned == "Hallo zurueck."


# ─────────────────────────────────────────────────────────────────────────────
# Fuzz: unmarked text in, the same text out
# ─────────────────────────────────────────────────────────────────────────────

# Built to trigger the removed rules and their neighbours. No markdown, digits, quotes, braces,
# brackets or "<", so the tool-JSON sanitizer and the TTS markdown steps are the identity on it.
FUZZ_TOKENS = [
    "Okay, the user ", "First, I should ", "Let me check ", "I need to ", "I will ",
    "The user wants ", "Answer in German. ", "Antworte auf Deutsch ", "Respond in writing ",
    "answer in detail ", "okay ", "the ", "user ", "wants ", "Hallo! ", "Die ", "Antwort ",
    "report ", "server ", "is ", "fine. ", "äöüß ", "Gerne, ", "\n", "\n\n",
    " ", "and ", "so ", "Bob ", "Alice ", "send ", "it ", "today. ", "Natürlich ", "also ",
]


# Every way a model marks its reasoning that the chat also treats as reasoning.
FUZZ_MARKS = [("<think>", "</think>"), ("<THINK>", "</THINK>"), ("<Think>", "</think>"),
              ("<thinking>", "</thinking>"), ("<THINKING>", "</Thinking>")]


def fuzz_text(rng: random.Random) -> str:
    return "".join(rng.choice(FUZZ_TOKENS) for _ in range(rng.randint(0, 60)))


def fuzz_mismatches(rng: random.Random, draws: int) -> list:
    """The oracle, shared with the larger run outside the suite. Returns (oracle, inputs)."""
    from vaf.core.speech_fillers import THINKING_FILLERS
    fillers = [f.lower() for v in THINKING_FILLERS.values() for f in v if isinstance(f, str) and f]
    bad = []
    for _ in range(draws):
        t, r, b = fuzz_text(rng), fuzz_text(rng), fuzz_text(rng)
        if any(f in t.lower() for f in fillers):
            raise AssertionError(f"the fuzz alphabet produced a TTS filler phrase: {t!r}")
        if _clean(t) != t.strip():
            bad.append((1, t))
        opener, closer = rng.choice(FUZZ_MARKS)
        if _clean(t + opener + r + closer + b) != (t + b).strip():
            bad.append((2, t, r, b))
        if _clean(t + opener + r) != t.strip():
            bad.append((5, t, r))
        if _speak(t).split() != t.split():
            bad.append((3, t))
        if _speak("<think>" + r + "</think>" + t).split() != t.split():
            bad.append((4, r, t))
    return bad


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cleaners_keep_unmarked_text(seed):
    bad = fuzz_mismatches(random.Random(seed), 500)
    assert bad == [], f"{len(bad)} mismatches, first: {bad[:3]}"
