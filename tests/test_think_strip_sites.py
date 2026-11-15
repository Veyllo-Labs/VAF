# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One rule for a model's marked reasoning, `strip_think_blocks`, and every lane on it.

MEASURED BEFORE: twenty-one files removed reasoning with their own lazy `<think>.*?</think>`
(34 lines), five more cut an unclosed opener by hand and two stripped `<redacted_reasoning>`
with their own lazy regex. The copies disagreed: the messenger lane sent `"Answer. <think>SECRET
plan"` and `"<THINK>SECRET</THINK>Answer."` with SECRET in them, the text read aloud kept a
`<thinking>` block, the compose box showed one, and a legacy failed turn after an uppercase
block passed as an answer. Every lazy copy rescans to the end from each unclosed opener: 1 to
11 s on 64 KB, also in the reply cleaner (`_sanitize_response`) and the empty-answer probe.

MUTATIONS, each turns the named test red:
- drop the stray-closer branch in `drop_redacted_blocks(cut_unclosed=True)`
  -> test_primitive_rows[stray-redacted-closer, joined-redacted-opener, joined-think-opener];
- gate the second pass on the opener only (`_REDACTED_OPEN in t`) -> [stray-redacted-closer];
- no redacted pass at all in `strip_think_blocks` -> [redacted-block];
- keep `_strip_marked_reasoning` in agent.py -> test_agent_keeps_no_copy_of_the_rule;
- `if len(probe) >= 2: break` with the overwrite again -> test_probe_keeps_the_longer_reading;
- each lazy regex restored (the echo pair, the probe's `<[^>]*>`, `\\[?\\s*\\{` in pattern 1, the
  `<|tool_call>` regex, the lines in `_clean_markdown`, `_clean_for_session`,
  `_prepare_channel_outbound`, `clean_output`, `reasoning_leaked`, `is_failed_turn`)
  -> its row of test_cleaners_are_linear;
- `split_think_blocks` closing at the LAST closer -> test_marked_reasoning_fuzz (oracle 3, 4);
- the angle-tag walk dropping a `<` that has no `>` -> (oracle 5);
- pattern 1 without the optional bracket -> (oracle 6);
- restore one site's lazy regex -> its rows of test_sites_strip_marked_reasoning;
- drop the empty-detail branch in `is_failed_turn` -> its [empty-detail*] rows;
- the old `"?\\s*$` in the sanitizer's "name" fragment pattern -> its linear row and
  test_pattern7_is_the_one_in_the_sanitizer; `"?$` without the blanks -> the fuzz (oracle 8);
- the old filler regex in `speech._drop_standalone_filler` -> its three speech rows (11 s at
  4 KB already); ignore the dot before a blank run, or drop the start-of-text branch -> the
  fuzz (oracles 9, 10); the old leading-filler loop -> [speech-leading-fillers]; try the
  phrases by the first character unlowered -> the fuzz (oracle 9);
- only `*?` as a repetition in `_is_hand_copy` -> test_the_hand_copy_shapes; no folding of
  concatenations -> test_concatenated_and_f_string_patterns_are_seen;
- re-add a lazy think regex in send_telegram.py, a `.split("<think>")` in rag.py, or remove
  the call in vision_infer -> test_no_hand_copy_of_the_rule / test_converted_functions_call_the_rule.
"""
import ast
import random
import re
import time
from pathlib import Path

import pytest

from vaf.core import completion
from vaf.core.completion import drop_redacted_blocks, split_think_blocks, strip_think_blocks

ROOT = Path(__file__).resolve().parents[1]
OPEN, CLOSE = "<redacted_reasoning>", "</redacted_reasoning>"


# ─────────────────────────────────────────────────────────────────────────────
# The primitive's rule
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("greedy", [False, True], ids=["plain", "greedy"])
@pytest.mark.parametrize("text, expected", [
    ("<redacted_reasoning>SECRET</redacted_reasoning>Answer", "Answer"),
    ("plan</redacted_reasoning>Answer", "planAnswer"),
    ("Answer <redacted_reasoning>SECRET", "Answer"),
    ("<redacted_</redacted_reasoning>reasoning>SECRET", ""),
    ("<thi</redacted_reasoning>nk>SECRET", ""),
    ("<think>plan</think>Answer", "Answer"),
], ids=["redacted-block", "stray-redacted-closer", "redacted-unclosed", "joined-redacted-opener",
        "joined-think-opener", "think-block"])
def test_primitive_rows(text, expected, greedy):
    assert strip_think_blocks(text, greedy=greedy) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_falsy_input_is_returned_as_is(value):
    assert strip_think_blocks(value) is value
    assert strip_think_blocks(value, greedy=True) is value


def test_agent_keeps_no_copy_of_the_rule():
    """The reply cleaner's helper moved into the primitive; agent.py defines none of it."""
    tree = ast.parse((ROOT / "vaf" / "core" / "agent.py").read_bytes())
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    names |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
              if isinstance(t, ast.Name)}
    assert not names & {"_strip_marked_reasoning", "_drop_redacted_blocks", "_REDACTED_OPEN",
                        "_REDACTED_CLOSE"}
    from vaf.core.agent import Agent, _final_answer_probe
    for func in (Agent._clean_reasoning, _final_answer_probe):
        assert "strip_think_blocks" in func.__code__.co_names, func.__qualname__


def test_probe_keeps_the_longer_reading():
    """Non-greedy the reply is the answer "4", greedy it is nothing: the longer reading wins."""
    from vaf.core.agent import _final_answer_probe
    assert _final_answer_probe("<think>a</think>4<think>b</think>") == "4"


# ─────────────────────────────────────────────────────────────────────────────
# Linear: 128 KB under 2 s for every shape that was quadratic
# ─────────────────────────────────────────────────────────────────────────────

N = 128 * 1024


def _fill(unit: str, head: str = "") -> str:
    return head + unit * (N // len(unit))


def _agent():
    from vaf.core.agent import Agent
    return Agent.__new__(Agent)


def _speak(text):
    from vaf.core.speech import SpeechManager
    return SpeechManager.__new__(SpeechManager)._clean_markdown(text)


def _shapes():
    from vaf.core import composer, composer_lane, headless_runner, session
    from vaf.core.agent import _final_answer_probe, _reply_is_reasoning_echo
    return {
        "echo-think-runs": (_reply_is_reasoning_echo, _fill("<think>")),
        "echo-unclosed-openers": (_reply_is_reasoning_echo, _fill("<think>a ")),
        "probe-think-runs": (_final_answer_probe, _fill("<think>")),
        "probe-angle-runs": (_final_answer_probe, _fill("<", "x")),
        "probe-angle-a-runs": (_final_answer_probe, _fill("<a ", "x")),
        "sanitize-blank-run": (_agent()._sanitize_response, _fill(" ", "x") + "x"),
        "sanitize-tool-call-openers": (_agent()._sanitize_response, _fill("<|tool_call>a ")),
        "clean-reasoning-tool-call-openers": (_agent()._clean_reasoning, _fill("<|tool_call>a ")),
        "speech-think-openers": (_speak, _fill("<think>a ")),
        "speech-redacted-openers": (_speak, _fill("<redacted_reasoning>a ")),
        "web-copy-redacted-openers": (headless_runner._clean_for_session,
                                      _fill("<redacted_reasoning>a ")),
        "channel-outbound-think-openers": (headless_runner._prepare_channel_outbound,
                                           _fill("<think>a ", "Answer. ")),
        "drain-copy-think-openers": (headless_runner._clean_drain_text,
                                     _fill("<think>a ", "Answer. ")),
        "compose-box-think-openers": (composer.clean_output, _fill("<think>a ", "Answer. ")),
        "compose-leak-check-think-openers": (
            lambda t: composer_lane.reasoning_leaked(t, "Answer."), _fill("<think>a ")),
        "failed-turn-think-openers": (
            lambda t: session.is_failed_turn({"role": "assistant", "content": t}),
            _fill("<think>a ")),
        "sanitize-name-fragment-blanks": (_agent()._sanitize_response,
                                          ', "name": "' + " " * N + 'x"y'),
        "speech-blank-run": (_speak, "x" + " " * N + "x"),
        "speech-newline-run": (_speak, "x" + "\n" * N + "x"),
        "speech-filler-then-blanks": (_speak, "Moment" + " " * N + "x"),
        "speech-leading-fillers": (_speak, _fill("\u7b49\u4e00\u4e0b ")),
        "speech-leading-fillers-then-dotted-i": (
            _speak, "\u7b49\u4e00\u4e0b " * 12 + "\u0130" + " " * N + "x"),
        "delivery-judgement-think-openers": (_reply_failure, _fill("<think>a ", "Answer. ")),
    }


def _reply_failure(text):
    from vaf.core.headless_runner import _reply_failure as judge
    return judge(text)


@pytest.mark.parametrize("name", list(_shapes()))
def test_cleaners_are_linear(name):
    """Measured on the lazy regexes: 1 to 11 s at 64 KB, about four times that at 128 KB."""
    func, text = _shapes()[name]
    started = time.perf_counter()
    func(text)
    assert time.perf_counter() - started < 2.0


# ─────────────────────────────────────────────────────────────────────────────
# The sites: rows that leaked before, and the lane that keeps its blocks
# ─────────────────────────────────────────────────────────────────────────────

LEAKS = ["Answer. <think>SECRET plan", "<THINK>SECRET</THINK>Answer.",
         "<thinking>SECRET</thinking>Answer."]


@pytest.mark.parametrize("text", LEAKS, ids=["unclosed", "upper", "thinking"])
def test_sites_strip_marked_reasoning(text):
    from vaf.core import composer, headless_runner
    from vaf.memory import attachment_rag
    from vaf.whare_wananga import runtime
    assert headless_runner._prepare_channel_outbound(text) == "Answer."
    assert headless_runner._clean_drain_text(text) == "Answer."
    spoken = _speak(text)
    assert "SECRET" not in spoken and "Answer" in spoken, spoken
    assert composer.clean_output(text) == "Answer."
    assert attachment_rag._normalize_query(text) == "Answer."
    assert runtime._extract_json(text.replace("Answer.", '{"a": 1}')) == {"a": 1}


@pytest.mark.parametrize("content", ["<THINK>p</THINK>[Error] x", "<thinking>p</thinking>[Error] x",
                                     "[Error] ", "[Error] <tHiNk>"],
                         ids=["upper-block", "thinking-block", "empty-detail",
                              "empty-detail-then-opener"])
def test_a_legacy_failure_after_marked_reasoning_is_recognised(content):
    """Text before a stray closer is kept by the rule, so "plan</think>[Error] x" begins
    with "plan" and is not recognised, as before."""
    from vaf.core.session import is_failed_turn
    assert is_failed_turn({"role": "assistant", "content": content}) is True


def test_memory_lines_drafted_inside_reasoning_are_never_parsed():
    from vaf.memory.rag import _parse_memory_reply
    assert _parse_memory_reply('NO_REPLY <THINK>MEMORY: "drafted"') == []
    assert _parse_memory_reply('<thinking>MEMORY: "drafted"</thinking>MEMORY: "kept fact"') \
        == [("kept fact", [])]


def test_web_copy_keeps_thinking():
    """The web chat's stored copy keeps its think blocks: the browser shows them after reload."""
    from vaf.core.headless_runner import _clean_for_session
    assert _clean_for_session("<think>plan</think>Answer") == "<think>plan</think>Answer"
    assert _clean_for_session(
        "<redacted_reasoning>x</redacted_reasoning>A</redacted_reasoning>") == "A</redacted_reasoning>"


# ─────────────────────────────────────────────────────────────────────────────
# The guard: no hand copy of the rule anywhere in vaf/
# ─────────────────────────────────────────────────────────────────────────────

# Lazy block regexes that stay, each for a reason the primitive cannot serve.
ALLOWED_HAND_COPIES = {
    # The terminal's display whitespace fix re-emits the block (group 1): it keeps it on purpose.
    "vaf/cli/cmd/run.py": 1,
    # The DeepSeek/Veyllo replay moves a block into `reasoning_content`, a protocol transform
    # for the provider, not a strip (the coder's two copies of it).
    "vaf/tools/coder.py": 2,
    # The same replay in `_prepare_messages`, and the tool router, whose rule differs: text
    # before a leading stray closer is reasoning there (the opener came with the prompt).
    "vaf/core/agent.py": 2,
}

# Hand-written cuts at "<think>" that stay. The voice lane's streaming split walks one chunk
# at a time with its own in-reasoning state across chunks, which a whole-text rule cannot
# serve; it is the voice lane's own family of marks (docs/agents/VOICE_AGENT.md).
ALLOWED_SPLITS = {"vaf/core/voice_agent.py": 1}

# Every function converted to the rule, with the helper it must call by name.
CONVERTED = [
    ("vaf/core/agent.py", "Agent._clean_reasoning", "strip_think_blocks"),
    ("vaf/core/agent.py", "_final_answer_probe", "strip_think_blocks"),
    ("vaf/core/agent.py", "_reply_is_reasoning_echo", "split_think_blocks"),
    ("vaf/core/agent.py", "Agent._process_subagent_result", "strip_think_blocks"),
    ("vaf/core/agent.py", "Agent._enter_session", "strip_think_blocks"),
    ("vaf/core/agent.py", "Agent.analyze_intent", "strip_think_blocks"),
    ("vaf/core/speech.py", "SpeechManager._clean_markdown", "strip_think_blocks"),
    ("vaf/core/headless_runner.py", "_clean_for_session", "drop_redacted_blocks"),
    ("vaf/core/headless_runner.py", "_clean_drain_text", "strip_think_blocks"),
    ("vaf/core/headless_runner.py", "_prepare_channel_outbound", "strip_think_blocks"),
    ("vaf/core/headless_runner.py", "_maybe_open_draft_in_editor", "strip_think_blocks"),
    ("vaf/core/web_server.py", "_detect_language_simple", "strip_think_blocks"),
    ("vaf/core/composer.py", "clean_output", "strip_think_blocks"),
    ("vaf/core/composer_lane.py", "reasoning_leaked", "split_think_blocks"),
    ("vaf/core/session.py", "is_failed_turn", "strip_think_blocks"),
    ("vaf/core/vision_infer.py", "vision_infer", "strip_think_blocks"),
    ("vaf/memory/rag.py", "RagPipeline.search", "strip_think_blocks"),
    ("vaf/memory/rag.py", "_parse_memory_reply", "strip_think_blocks"),
    ("vaf/memory/rag.py", "run_memory_search_sync", "strip_think_blocks"),
    ("vaf/memory/attachment_rag.py", "_normalize_query", "strip_think_blocks"),
    ("vaf/tools/base.py", "BaseTool.sanitize_model_text", "strip_think_blocks"),
    ("vaf/tools/learn_document.py", "_contextualize_section_llm", "strip_think_blocks"),
    ("vaf/tools/learn_document.py", "_summarize_doc_from_contexts", "strip_think_blocks"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool._generate_title", "strip_think_blocks"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool._augment_queries", "strip_think_blocks"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool._sanitize_section_output",
     "strip_think_blocks"),
    ("vaf/tools/send_telegram.py", "SendTelegramTool.run", "strip_think_blocks"),
    ("vaf/tools/send_whatsapp.py", "SendWhatsAppTool.run", "strip_think_blocks"),
    ("vaf/tools/send_discord.py", "SendDiscordTool.run", "strip_think_blocks"),
    ("vaf/tools/send_to_user.py", "SendToUserTool.run", "strip_think_blocks"),
    ("vaf/whare_wananga/runner.py", "_extract_json", "strip_think_blocks"),
    ("vaf/whare_wananga/runtime.py", "_extract_json", "strip_think_blocks"),
]


def _docstring_ids(tree) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _repeat_after(low: str, start: int) -> int:
    """Index of the first regex repetition at or after `start`, or -1: a `*` or `+` (lazy or
    not) right behind something a regex repeats, `.`, a class `]`, a group `)` or an escape
    like `\\s`. A markdown bullet's `*` in prose stands behind a blank and is not one."""
    for i in range(start, len(low)):
        if low[i] in "*+" and i > 0 and (low[i - 1] in ".])" or (i > 1 and low[i - 2] == "\\")):
            return i
    return -1


def _is_hand_copy(s: str) -> bool:
    """A regex that removes a whole reasoning block: an opener with the `<think` prefix (so
    also `<think\\b`, `<think(?:ing)?>`, `<think[^>]*>`), any repetition after it, then a
    `</think` closer; or a `<redacted_reasoning` opener with any repetition after it."""
    low = s.lower()
    start = low.find("<think")
    if start >= 0:
        rep = _repeat_after(low, start)
        if rep >= 0 and low.find("</think", rep) >= 0:
            return True
    start = low.find("<redacted_reasoning")
    return start >= 0 and _repeat_after(low, start) >= 0


def _folded(node) -> str:
    """The text of a string expression, with `{}` for each part that is not a literal: a
    constant, a concatenation of strings, or an f-string."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else "{}"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _folded(node.left) + _folded(node.right)
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return "{}"


def _string_expressions(tree, docs):
    """Every outermost string expression that is not a docstring: a concatenation or an
    f-string once as a whole, never its parts again."""
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inner.update(id(v) for v in node.values)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            inner.update((id(node.left), id(node.right)))
    for node in ast.walk(tree):
        if id(node) in inner or id(node) in docs:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr) or (
                isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            text = _folded(node)
            if text.strip("{}"):
                yield text


@pytest.mark.parametrize("pattern, expected", [
    (r"<think>.*?</think>", True),
    (r"<think>.*</think>", True),
    (r"<think>[\s\S]*?</think>", True),
    (r"<think>.+?</think>", True),
    (r"<think>[^<]*</think>", True),
    (r"<think(?:ing)?>.*?</think(?:ing)?>", True),
    (r"<think[^>]*>[\s\S]*?</think>", True),
    (r"(?s)<think\b.*?</think\b", True),
    (r"<THINK>(.*?)</THINK>", True),
    (r"<redacted_reasoning>.*?</redacted_reasoning>", True),
    (r"<redacted_reasoning>.*</redacted_reasoning>", True),
    (r"<redacted_reasoning>[\s\S]+", True),
    (r"</?think>", False),
    (r"<think>", False),
    (r"<(/?)think(?:ing)?>", False),
    (r"</?redacted_reasoning>", False),
    ("Wrap your reasoning in <think>...</think> tags.", False),
    ("Use `<think>` tags:\n- *The* block ends at `</think>`\n- +1 more", False),
], ids=lambda v: v if isinstance(v, str) else None)
def test_the_hand_copy_shapes(pattern, expected):
    assert _is_hand_copy(pattern) is expected


def test_concatenated_and_f_string_patterns_are_seen():
    tree = ast.parse('A = "<think>" + ".*?" + "</think>"\nB = f"<think>{x}.*?</think>"\n'
                     'C = "</?think>" + y\n')
    found = [s for s in _string_expressions(tree, set()) if _is_hand_copy(s)]
    assert len(found) == 2, found


def _scan():
    copies, splits = {}, {}
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_bytes())
        docs = _docstring_ids(tree)
        for text in _string_expressions(tree, docs):
            if _is_hand_copy(text):
                copies[rel] = copies.get(rel, 0) + 1
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "split" and node.args
                    and "<think" in _folded(node.args[0]).lower()):
                splits[rel] = splits.get(rel, 0) + 1
    return copies, splits


def test_no_hand_copy_of_the_rule():
    copies, splits = _scan()
    assert copies == ALLOWED_HAND_COPIES, (
        "a lazy <think>/<redacted_reasoning> regex outside the named exceptions: call "
        "vaf.core.completion.strip_think_blocks instead")
    assert splits == ALLOWED_SPLITS, "a hand-written unclosed-opener cut: call strip_think_blocks"


def _function(tree, qualname):
    nodes = tree.body
    found = None
    for part in qualname.split("."):
        found = next((n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                          ast.ClassDef)) and n.name == part), None)
        assert found is not None, qualname
        nodes = found.body
    return found


@pytest.mark.parametrize("rel, qualname, helper", CONVERTED,
                         ids=[f"{r}::{q}" for r, q, _ in CONVERTED])
def test_converted_functions_call_the_rule(rel, qualname, helper):
    func = _function(ast.parse((ROOT / rel).read_bytes()), qualname)
    called = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
              for n in ast.walk(func) if isinstance(n, ast.Call)}
    assert helper in called, f"{rel}::{qualname} no longer calls {helper}"


# ─────────────────────────────────────────────────────────────────────────────
# Fuzz: the new walks against literal copies of the code they replaced
# ─────────────────────────────────────────────────────────────────────────────

def _old_drop_redacted_blocks(text, *, cut_unclosed):
    kept, pos = [], 0
    while True:
        start = text.find(OPEN, pos)
        if start < 0:
            break
        end = text.find(CLOSE, start + len(OPEN))
        if end < 0:
            if cut_unclosed:
                kept.append(text[pos:start])
                return "".join(kept)
            break
        kept.append(text[pos:start])
        pos = end + len(CLOSE)
    kept.append(text[pos:])
    return "".join(kept)


def _old_strip_think_blocks(text, greedy):
    # The think walk itself is unchanged (`_strip_think_tags`, pinned against its regex form
    # by test_marked_reasoning_only.test_strip_think_blocks_keeps_its_rule).
    return text if not text else completion._strip_think_tags(text, greedy)


def _old_strip_marked_reasoning(text, *, greedy):
    t = _old_drop_redacted_blocks(text or "", cut_unclosed=False)
    t = _old_strip_think_blocks(t, greedy)
    if OPEN in t:
        t = _old_strip_think_blocks(_old_drop_redacted_blocks(t, cut_unclosed=True), greedy)
        t = t.split(OPEN, 1)[0]
    return t.strip()


def _alias(text):
    return re.sub(r"<(/?)thinking>", r"<\1think>", text, flags=re.IGNORECASE)


def _old_split(raw):
    blocks = re.findall(r'<think>([\s\S]*?)</think>', raw, flags=re.IGNORECASE)
    visible = re.sub(r'<think>[\s\S]*?</think>', ' ', raw, flags=re.IGNORECASE)
    return visible, blocks


def _old_echo(full_content):
    raw = full_content or ""
    blocks = re.findall(r'<think>([\s\S]*?)</think>', raw, flags=re.IGNORECASE)
    if not blocks:
        return False
    visible = " ".join(re.sub(r'<think>[\s\S]*?</think>', ' ', raw, flags=re.IGNORECASE).split())
    if len(visible) < 400:
        return False
    return visible in (" ".join(blocks[-1].split()), " ".join(" ".join(blocks).split()))


def _old_sanitize(text):
    if not text:
        return ""
    patterns = [
        r'\[?\s*\{\s*"tool_calls":.*$',
        r'\{\s*"name":\s*"[^"]*",\s*"arguments":.*$',
        r'\{\s*"index":\s*\d+,\s*"id":.*$',
        r'",\s*"name":\s*"[^"]*",\s*"type":\s*"function".*$',
        r'"\}\s*,\s*"type":\s*"function".*$',
        r'",\s*"name":\s*"[^"]*"\}\s*,\s*"type":\s*"function".*$',
        r',\s*"name":\s*"[^"]*"?\s*$',
        r'\}\s*,\s*\{\s*"index":\s*\d+.*$',
        r'\}\s*,\s*\{\s*"name":\s*"[^"]*".*$',
        r'\]\s*\}\s*$',
        r'\}\s*\]\s*\}\s*$',
    ]
    cleaned = text
    for pattern in patterns:
        if re.search(pattern, cleaned, flags=re.DOTALL | re.MULTILINE):
            cleaned = re.sub(pattern, '', cleaned, flags=re.DOTALL | re.MULTILINE)
    cleaned = re.sub(r'<\|tool_call>[\s\S]*?<tool_call\|>', '', cleaned)
    cleaned = cleaned.replace('<|tool_call>', '').replace('<tool_call|>', '')
    return cleaned.strip()


TOKENS = ["<think>", "</think>", "<THINK>", "</Think>", "<thinking>", "</THINKING>", OPEN, CLOSE,
          "<thi", "nk>", "ing>", "<redacted_", "reasoning>", "</", "<", ">", "<a ",
          "<|tool_call>", "<tool_call|>", '{"tool_calls": ', "{", "[", "]", "}", '"name": "x"',
          "```", " ", "   ", "\n", "a", "Answer.", "x y", "İ", "ı", "K", "ok", ",", '"',
          ', "name": "', '"name": ']
# Block contents for the sentinel draws: no tag and no tag fragment, so the block is a natural one.
SAFE = [" ", "\n", "a", "Answer.", "x y", "ok", "{", "]", "İ", "K"]
MARKS = [("<think>", "</think>"), ("<THINK>", "</Think>"), ("<thinking>", "</THINKING>"),
         (OPEN, CLOSE)]


def _draw(rng, k=40):
    return "".join(rng.choice(TOKENS) for _ in range(rng.randint(0, k)))


def _sentinel_draw(rng):
    parts = [_draw(rng, 10) for _ in range(rng.randint(1, 4))]
    out = []
    for part in parts:
        opener, closer = rng.choice(MARKS)
        inner = "".join(rng.choice(SAFE) for _ in range(rng.randint(0, 5)))
        out.append(part + opener + inner + "SECRET" + inner + closer)
    return "".join(out) + _draw(rng, 10)


def _echo_draw(rng):
    block = "".join(rng.choice(TOKENS + SAFE * 4) for _ in range(rng.randint(5, 20)))
    block = (block + " ") * (420 // max(1, len(block)) + 1)
    opener, closer = rng.choice(MARKS[:3])
    shape = rng.randrange(4)
    if shape == 0:
        return opener + block + closer + block
    if shape == 1:
        return _draw(rng, 5) + opener + block + closer + block + _draw(rng, 5)
    if shape == 2:
        return block + opener + block + closer + block
    return opener + block + closer + _draw(rng, 40) * 12


def _has_stray_redacted_closer(text):
    pending = False
    for m in re.finditer(re.escape(OPEN) + "|" + re.escape(CLOSE), text):
        if m.group() == OPEN:
            pending = True
        elif pending:
            pending = False
        else:
            return True
    return False


def fuzz_mismatches(rng: random.Random, draws: int) -> dict:
    """Mismatches per oracle (shared with the larger run outside the suite)."""
    from vaf.core.agent import Agent, _drop_angle_tags, _reply_is_reasoning_echo
    agent = Agent.__new__(Agent)
    from vaf.core.speech import _drop_standalone_filler
    bad = {k: [] for k in range(1, 11)}
    for _ in range(draws):
        t = _draw(rng)
        for greedy in (False, True):
            out = strip_think_blocks(t, greedy=greedy)
            old = _old_strip_marked_reasoning(t, greedy=greedy)
            if not _has_stray_redacted_closer(t) and CLOSE not in old and out != old:
                bad[1].append((t, greedy))
            if completion._THINK_TAG.search(out) or OPEN in out:
                bad[2].append((t, greedy))
            s = _sentinel_draw(rng)
            if "SECRET" in strip_think_blocks(s, greedy=greedy):
                bad[2].append((s, greedy))
        visible, blocks = split_think_blocks(t)
        if (_alias(visible), [_alias(b) for b in blocks]) != _old_split(_alias(t)):
            bad[3].append(t)
        # Both on the alias-rewritten text: the old body read only <think>, and a rewrite
        # shortens a <thinking> left in the visible text, which moves the length threshold.
        e = _alias(_echo_draw(rng))
        if _reply_is_reasoning_echo(e) != _old_echo(e):
            bad[4].append(e)
        if _drop_angle_tags(t) != re.sub(r'<[^>]*>', '', t):
            bad[5].append(t)
        if agent._sanitize_response(t) != _old_sanitize(t):
            bad[6].append(t)
        if drop_redacted_blocks(t, cut_unclosed=False) != re.sub(
                re.escape(OPEN) + ".*?" + re.escape(CLOSE), "", t, flags=re.DOTALL):
            bad[7].append(t)
        if (re.sub(_PATTERN7, "", t, flags=re.DOTALL | re.MULTILINE)
                != re.sub(_OLD_PATTERN7, "", t, flags=re.DOTALL | re.MULTILINE)):
            bad[8].append(t)
        s = _speech_draw(rng)
        if _speak(s) != _old_speak(s):
            bad[9].append(s)
        # The standalone step alone, also where the leading strip leaves nothing for it.
        phrase = rng.choice(STANDALONE_PHRASES)
        if _drop_standalone_filler(s, phrase) != _old_standalone(s, phrase):
            bad[10].append((s, phrase))
    return {k: v for k, v in bad.items() if v}


_OLD_PATTERN7 = r',\s*"name":\s*"[^"]*"?\s*$'
_PATTERN7 = r',\s*"name":\s*"[^"]*(?:"\s*)?$'


def test_pattern7_is_the_one_in_the_sanitizer():
    """The oracle's pattern is the sanitizer's own (the fuzz compares this text)."""
    import inspect
    from vaf.core.agent import Agent
    assert "r'" + _PATTERN7 + "'" in inspect.getsource(Agent._sanitize_response)


# Speech: filler phrases among blanks, dots and the punctuation the leading strip eats,
# in mixed case, with the characters whose lowercase is not one for one.
SPEECH_TOKENS = ["Moment", "moment", "MOMENT", "Einen Moment bitte.", "Einen Moment",
                 "\u7b49\u4e00\u4e0b", "Let me check", "let me check", "Okay, Moment.",
                 "Anlad\u0131m, hemen bak\u0131yorum", " ", "  ", "\n", "\n\n", "\t", ".", ",",
                 ";", ":", "x", "Answer", "\u0130", "\u03a3", "\u03a3\u0391", "\u212a",
                 "\u0131", "- ", "**"]


STANDALONE_PHRASES = ["Moment", "Einen Moment bitte.", "等一下", "Let me check",
                      "Okay, Moment."]


def _speech_draw(rng):
    return "".join(rng.choice(SPEECH_TOKENS) for _ in range(rng.randint(0, 24)))


def _old_leading(t, all_fillers):
    changed = True
    while changed and t.strip():
        changed = False
        t_stripped = t.strip()
        for phrase in all_fillers:
            if not phrase:
                continue
            if t_stripped.lower().startswith(phrase.lower()):
                rest = t_stripped[len(phrase):].lstrip(".,;: ")
                t = rest
                changed = True
                break
    return t


def _old_standalone(t, phrase):
    esc = re.escape(phrase)
    return re.sub(r'(?:^|[\s.])\s*' + esc + r'\s*(?=[\s.]|$)', ' ', t, flags=re.IGNORECASE)


def _old_speak(text):
    """`_clean_markdown` with its two filler steps as they were (literal copies above)."""
    from unittest import mock
    from vaf.core import speech
    with mock.patch.object(speech, "_strip_leading_fillers", _old_leading), \
            mock.patch.object(speech, "_drop_standalone_filler", _old_standalone):
        return _speak(text)


def test_no_filler_phrase_lowers_out_of_step():
    """`speech._strip_leading_fillers` compares a slice of the phrase's length: exact while no
    phrase holds a character whose lowercase is not one for one (U+0130, a capital sigma)
    or lowers to a sigma or a combining dot."""
    from vaf.core.speech_fillers import THINKING_FILLERS
    phrases = {f for v in THINKING_FILLERS.values() for f in v if isinstance(f, str) and f}
    odd = [p for p in phrases if len(p.lower()) != len(p)
           or any(c in p.lower() for c in "\u03c3\u03c2\u0307") or "\u03a3" in p]
    assert odd == []


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_marked_reasoning_fuzz(seed):
    bad = fuzz_mismatches(random.Random(seed), 500)
    assert bad == {}, {k: (len(v), v[:2]) for k, v in bad.items()}
