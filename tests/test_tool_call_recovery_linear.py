# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The XML tool-call recovery lanes read in linear time and return what the regexes returned.

`extract_xml_tool_calls` and `strip_tool_call_markup` used lazy regexes that rescanned to the
end of the text from every opener with no closer after it: 3.2 s for 128 KB of unclosed
``<invoke name="x">``, 27 s for 128 KB of unclosed child tags inside one ``<tool_use>``, and
about 15 minutes for 128 KB of ``<tool_use name="a" `` heads (the head backtracked over every
``name="`` before the first ``>``). The main agent runs the lane twice per generation round.
The walks that replace them find every opener, ``>`` and closer once.

The binding rule is equivalence: for every input and name list the three public functions
return what the regex implementation returned. That implementation is frozen verbatim below
as the oracle (`_old_extract`, `_old_strip`) and compared on a pinned corpus and a seeded fuzz.

Mutations that turn these tests red (each run and restored):
- only the last ``name="`` of a ``<tool_use`` head is tried: the ``two_names`` corpus row;
- the child walk stops at an opener whose name never closes: the ``child_unclosed`` row;
- child openers inside a match are not skipped, or a child opener closes at any closer: the
  ``child_nested`` row;
- ``<tool_use`` without ``\\b``: the ``tool_user`` row;
- dialect 3 walks the names sorted: the ``name_order`` row;
- dialect 3 runs although a named dialect found a call: the ``named_dialect_wins`` row;
- the tool_use strip runs before the invoke strip: the ``invoke_joins_tool_use`` row;
- a parameter, tool_use or tool-tag opener inside a match is tried again: the
  ``param_inside_value``, ``tool_use_inside_body`` and ``tool_tag_nested`` rows;
- the position index steps by the needle's length: the ``overlapping_closer`` row;
- the invoke search keeps its no-closer guard but loses the cut at the last closer: the
  ``invoke_after_valid`` shape in the cost test;
- a tool tag closes at any closer: the prose test (a Markdown line names ``<web_search>``);
- the fuzz catches every mutation of this list that changes a result, within its 2,000 draws;
- each lazy regex put back (the uncut invoke search, the parameter, tool_use, child-tag,
  tool-as-tag and wrapper regexes): its shapes in the cost test, and the uncut invoke search
  in the chat_step turn test;
- a removed regex constant put back: `test_dead_regexes_are_gone`.

`VAF_XML_RECOVERY_FUZZ_DRAWS` runs a larger seeded draw count outside the suite.
"""

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict

import pytest
import requests

import vaf.core.tool_call_recovery as recovery
from test_local_engine_wiring import _local_agent, _Resp, _Stream
from vaf.core.tool_call_recovery import (extract_xml_tool_call, extract_xml_tool_calls,
                                         strip_tool_call_markup)

# ── the frozen oracle: the regex implementation the walks replace, verbatim ────────────────
# Only `_mk` differs: it returns (type, name, arguments) without the random id.

_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)
_PARAM_RE = re.compile(r'<parameter\s+name="([^"]+)"([^>]*)>(.*?)</parameter>', re.DOTALL)
_TOOL_USE_RE = re.compile(r'<tool_use\b[^>]*\bname="([^"]+)"[^>]*>(.*?)</tool_use>', re.DOTALL)
_CHILD_TAG_RE = re.compile(r"<([A-Za-z_][\w-]*)\s*>(.*?)</\1>", re.DOTALL)
_DSML_TOKEN_RE = re.compile(r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*")
_TOOL_CALLS_WRAP_RE = re.compile(r"</?(?:tool_calls|function_calls|calls)\b[^>]*>")


def _coerce(raw: str, force_string: bool = False) -> Any:
    raw = raw.strip()
    if force_string:
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _child_params(block: str) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    for m in _CHILD_TAG_RE.finditer(block):
        args[m.group(1).strip()] = _coerce(m.group(2))
    return args


def _mk(name: str, args: Dict[str, Any]):
    return ("function", name, json.dumps(args))


def _old_extract(content: str, valid_names=None) -> list:
    if not content:
        return []
    norm = _DSML_TOKEN_RE.sub("", content).replace("｜", "")

    def _ok(name: str) -> bool:
        return bool(name) and (valid_names is None or name in valid_names)

    out: list = []
    for inv in _INVOKE_RE.finditer(norm):
        name = inv.group(1).strip()
        if not _ok(name):
            continue
        args: Dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(inv.group(2)):
            args[pm.group(1).strip()] = _coerce(pm.group(3), 'string="true"' in pm.group(2))
        out.append(_mk(name, args))
    for tu in _TOOL_USE_RE.finditer(norm):
        name = tu.group(1).strip()
        if _ok(name):
            out.append(_mk(name, _child_params(tu.group(2))))
    if out:
        return out
    if valid_names:
        for tool in valid_names:
            for m in re.finditer(rf"<{re.escape(tool)}\b[^>]*>(.*?)</{re.escape(tool)}>",
                                 norm, re.DOTALL):
                out.append(_mk(tool, _child_params(m.group(1))))
    return out


def _wire_json_span(content: str):
    if not content or '"tool_calls"' not in content:
        return None
    start = content.find("{", 0, content.find('"tool_calls"'))
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(content)):
        ch = content[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return None


def _old_strip(content: str) -> str:
    if not content or ("invoke name=" not in content
                       and "tool_use name=" not in content
                       and "DSML" not in content
                       and '"tool_calls"' not in content):
        return content
    text = _DSML_TOKEN_RE.sub("", content).replace("｜", "")
    text = _INVOKE_RE.sub("", text)
    text = _TOOL_USE_RE.sub("", text)
    text = _TOOL_CALLS_WRAP_RE.sub("", text)
    span = _wire_json_span(text)
    if span:
        text = text[:span[0]] + text[span[1]:]
    return text.strip()


# ── comparison ─────────────────────────────────────────────────────────────────────────────

NAME_SETS = [None, ["web_search"], ["read_file", "web_search"], ["a.b", "x-y", "web_search"],
             {"web_search": "tool", "read_file": "tool"}]
NAME_IDS = ["none", "web_search", "two", "dotted", "dict"]


def _calls(calls) -> list:
    """(type, name, arguments) per call, after checking the documented shape and the id."""
    assert isinstance(calls, list)
    for c in calls:
        assert isinstance(c, dict) and set(c) == {"id", "type", "function"}, c
        assert isinstance(c["id"], str) and c["id"].startswith("call_synth_"), c["id"]
        assert isinstance(c["function"]["name"], str) and c["function"]["name"], c
        assert isinstance(c["function"]["arguments"], str), c
    return [(c["type"], c["function"]["name"], c["function"]["arguments"]) for c in calls]


def _assert_same(text, names, ctx="") -> list:
    """The new functions against the oracle for one text and one name list; returns the calls."""
    want = _old_extract(text, names)
    got = _calls(extract_xml_tool_calls(text, names))
    assert got == want, f"{ctx} names={names!r}: {got!r} != {want!r} for {text!r}"
    one = extract_xml_tool_call(text, names)
    assert (None if one is None else _calls([one])[0]) == (want[0] if want else None), ctx
    return want


def _assert_strip_same(text, ctx="") -> None:
    got = strip_tool_call_markup(text)
    want = _old_strip(text)
    assert got == want, f"{ctx}: strip {got!r} != {want!r} for {text!r}"


# ── the corpus ─────────────────────────────────────────────────────────────────────────────

def _dsml_invoke(path, start, end):
    return (f'<｜｜DSML｜｜invoke name="read_file"> '
            f'<｜｜DSML｜｜parameter name="path" string="true">{path}</｜｜DSML｜｜parameter> '
            f'<｜｜DSML｜｜parameter name="start_line" string="false">{start}</｜｜DSML｜｜parameter> '
            f'<｜｜DSML｜｜parameter name="end_line" string="false">{end}</｜｜DSML｜｜parameter> '
            f'</｜｜DSML｜｜invoke>')


def _repeat(head, unit, tail, size):
    return head + unit * max(1, (size - len(head) - len(tail)) // len(unit)) + tail


# The adversarial shapes of the cost test: (head, repeated unit, tail).
SHAPES = {
    "invoke_open": ("", '<invoke name="web_search">', ""),
    "tool_use_open": ("", '<tool_use name="web_search">', ""),
    "invoke_params": ('<invoke name="web_search">', '<parameter name="q">', "</invoke>"),
    "param_heads": ('<invoke name="web_search">', '<parameter name="q" ',
                    "</parameter></invoke>"),
    "tool_tags": ("", "<web_search>", ""),
    "tool_tag_heads": ("", "<web_search ", "</web_search>"),
    "tool_use_children": ('<tool_use name="web_search">', "<a>", "</tool_use>"),
    "tool_tag_children": ("<web_search>", "<a>", "</web_search>"),
    "tool_use_bare_heads": ("", "<tool_use ", "</tool_use>"),
    "wrapper_heads": ("invoke name= ", "<calls ", ""),
    "tool_use_name_heads": ("", '<tool_use name="a" ', "</tool_use>"),
    # A valid block first, then unclosed openers: a closer exists, so a lane that only checks
    # for one at all would still rescan from every later opener.
    "invoke_after_valid": ('<invoke name="web_search">x</invoke>', '<invoke name="web_search">', ""),
    "params_after_valid": ('<invoke name="web_search"><parameter name="q">x</parameter>',
                           '<parameter name="q">', "</invoke>"),
    "tool_use_after_valid": ('<tool_use name="web_search"><q>1</q></tool_use>',
                             '<tool_use name="web_search">', ""),
    "children_after_valid": ('<tool_use name="web_search"><b>1</b>', "<a>", "</tool_use>"),
    "tool_tags_after_valid": ("<web_search>x</web_search>", "<web_search>", ""),
    "wrappers_after_valid": ("invoke name= <calls>", "<calls ", ""),
}

# Rows whose result on the regex implementation is pinned here as well: (text, names,
# expected calls as (name, args) or None, expected strip output or None).
PINNED = {
    "two_names": ('<tool_use name="x" name="y></tool_use>">', None, [("x", {})], None),
    "two_names_with_names": ('<tool_use name="x" name="y></tool_use>">', ["web_search"], [],
                             None),
    "name_with_gt": ('<tool_use name="a>b"><q>1</q></tool_use>', None, [("a>b", {"q": 1})],
                     None),
    "invoke_joins_tool_use": (
        '<tool_<invoke name="a">x</invoke>use name="web_search"><q>1</q></tool_use>',
        None, [("a", {})], ""),
    "child_duplicate": ("<web_search><a>1</a><b>2</b><a>3</a></web_search>", ["web_search"],
                        [("web_search", {"a": 3, "b": 2})], None),
    "child_nested": ("<web_search><a><b>1</b></a></web_search>", ["web_search"],
                     [("web_search", {"a": "<b>1</b>"})], None),
    "child_unclosed": ("<web_search><a>1<b>2</b></web_search>", ["web_search"],
                       [("web_search", {"b": 2})], None),
    "tool_user": ('<tool_user name="web_search"><q>1</q></tool_use>', None, [], None),
    "name_order": ("<web_search></web_search><read_file></read_file>",
                   ["web_search", "read_file"], [("web_search", {}), ("read_file", {})], None),
    "named_dialect_wins": ('<tool_use name="web_search"><q>1</q></tool_use>'
                           "<web_search><q>2</q></web_search>", ["web_search"],
                           [("web_search", {"q": 1})], None),
    "param_inside_value": ('<invoke name="web_search"><parameter name="a">x<parameter name="b">'
                           "y</parameter></invoke>", None,
                           [("web_search", {"a": 'x<parameter name="b">y'})], None),
    "tool_use_inside_body": ('<tool_use name="web_search"><tool_use name="read_file">'
                             "</tool_use>", None, [("web_search", {})], None),
    "tool_tag_nested": ("<web_search><web_search>1</web_search>", ["web_search"],
                        [("web_search", {})], None),
    "overlapping_closer": ("x<a></a</a></a></a>x", ["a></a"], [("a></a", {})], None),
}

CORPUS = {
    "dsml_batch": ("Ich lese die restlichen Abschnitte:\n\n<｜｜DSML｜｜tool_calls> "
                   + " ".join(_dsml_invoke(f"/home/user/{c}.txt", 10 * i, 10 * i + 40)
                              for i, c in enumerate("abcd"))
                   + " </｜｜DSML｜｜tool_calls>"),
    "dsml_spaced_calls": ('Moment.\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="web_search">\n'
                          '<｜｜DSML｜｜ parameter name="query" string="true">wetter berlin'
                          '</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'),
    "morph_tool_use": ('Ich schreibe die Datei.\n<tool_use name="write_to_file" id="t1">'
                       "<path>a.txt</path>\n<content>Hallo\nWelt</content></tool_use> fertig"),
    "tool_as_tag": "<read_file><path>x.py</path><start_line>3</start_line></read_file>",
    "invoke_with_params": ('<function_calls><invoke name="web_search"><parameter name="q" '
                           'string="true">42</parameter><parameter name="n">5</parameter>'
                           '</invoke></function_calls>'),
    "param_inside_param_name": ('<invoke name="web_search"><parameter name="a<parameter '
                                'name="q">v</parameter></invoke>'),
    "param_without_gt": '<invoke name="web_search"><parameter name="q"</invoke>',
    "wire_json_and_invoke": ('{"tool_calls": [{"function": {"name": "web_search"}}]} '
                             '<invoke name="web_search"></invoke>'),
    "dotted_names": "<a.b><x>1</x></a.b><x-y q=1></x-y><a.bc></a.b>",
    "umlaut": '<tool_use name="web_search"><q>ä</q><ä>1</ä></tool_use>',
    **{f"shape_{label}": _repeat(*parts, 2048) for label, parts in SHAPES.items()},
    **{f"pinned_{label}": row[0] for label, row in PINNED.items()},
}


@pytest.mark.parametrize("text", list(CORPUS.values()), ids=list(CORPUS))
def test_corpus_matches_the_old_implementation(text):
    for names in NAME_SETS:
        _assert_same(text, names)
    _assert_strip_same(text)


@pytest.mark.parametrize("label", list(PINNED))
def test_pinned_rows(label):
    text, names, calls, stripped = PINNED[label]
    want = [("function", name, json.dumps(args)) for name, args in calls]
    assert _old_extract(text, names) == want, "the oracle copy drifted from the pinned result"
    assert _assert_same(text, names, label) == want
    if stripped is not None:
        assert _old_strip(text) == stripped
        assert strip_tool_call_markup(text) == stripped


def test_a_corpus_row_recovers_every_dsml_call():
    calls = _assert_same(CORPUS["dsml_batch"], ["read_file"])
    assert [c[1] for c in calls] == ["read_file"] * 4
    assert strip_tool_call_markup(CORPUS["dsml_batch"]) == "Ich lese die restlichen Abschnitte:"


# ── prose is never a call ──────────────────────────────────────────────────────────────────

PROSE = [
    "<div><p>Hallo</p></div>",
    'TEMPLATE = "<invoke name=\\"x\\">"\nprint(TEMPLATE)',
    "Siehe <path>docs/a.md</path> und <a>Link</a>. Der Tag `<web_search>` steht allein, "
    "<b>fett</b> und </a> ohne Gegenstueck.",
    "Morgen frueh schaue ich mir die Rechnung noch einmal in Ruhe an.",
]


@pytest.mark.parametrize("names", NAME_SETS, ids=NAME_IDS)
@pytest.mark.parametrize("text", PROSE, ids=["html", "python", "markdown", "german"])
def test_prose_is_never_a_call(text, names):
    assert extract_xml_tool_calls(text, names) == []
    assert extract_xml_tool_call(text, names) is None
    assert strip_tool_call_markup(text) == text
    assert _old_extract(text, names) == [] and _old_strip(text) == text


# ── cost ───────────────────────────────────────────────────────────────────────────────────

_KB = 1024
# The sizes on the regex implementation took 3 to 27 s; the cubic head 14.4 s at 32 KB, and the
# two mildest quadratic shapes 2.5 and 4.3 s at 256 KB.
_SIZES = {"tool_use_name_heads": 32 * _KB, "tool_tag_heads": 256 * _KB,
          "wrapper_heads": 256 * _KB, "wrappers_after_valid": 256 * _KB}
_COST_BOUND = 0.5


@pytest.mark.parametrize("label", list(SHAPES))
def test_xml_lanes_cost_is_linear(label):
    size = _SIZES.get(label, 128 * _KB)
    text = _repeat(*SHAPES[label], size)
    assert len(text) >= size - 64
    runs = {"xml": lambda: extract_xml_tool_calls(text),
            "xml+n": lambda: extract_xml_tool_calls(text, {"web_search", "read_file"}),
            "strip": lambda: strip_tool_call_markup(text)}
    for call, run in runs.items():
        start = time.perf_counter()
        run()
        took = time.perf_counter() - start
        assert took < _COST_BOUND, \
            f"{label} {call}: {took:.2f} s for {size // _KB} KB, the walk is no longer linear"


# ── through a real chat_step ───────────────────────────────────────────────────────────────

def test_a_turn_with_128k_of_unclosed_openers_stays_fast(monkeypatch, tmp_path):
    """The model answers with one sentence and 128 KB of unclosed ``<invoke name="web_search">``.
    The round reads the reply twice, in the model-health guard and in the text fallback; the
    regex implementation took about 3.2 s for each. The sentence makes it a visible answer: a
    reply of markup alone counts as empty and is retried instead, which reads it once."""
    reply = _repeat("Ich suche das.\n", '<invoke name="web_search">', "", 128 * _KB)
    agent = _local_agent(monkeypatch, tmp_path)
    ran = []
    monkeypatch.setattr(agent.tools["web_search"], "run",
                        lambda **kw: ran.append(kw) or "Ergebnis.", raising=False)
    spent, sizes = [], []
    real = recovery.extract_xml_tool_calls

    def timed(content, valid_names=None):
        start = time.perf_counter()
        try:
            return real(content, valid_names)
        finally:
            spent.append(time.perf_counter() - start)
            sizes.append(len(content or ""))
    monkeypatch.setattr(recovery, "extract_xml_tool_calls", timed)
    requests_seen, chunks = [], []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            requests_seen.append(json)
            return _Stream([{"content": reply if len(requests_seen) == 1 else "Fertig."}])
        return _Resp(500, {})
    monkeypatch.setattr(requests, "post", post)
    agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=chunks.append)
    assert len(requests_seen) == 1, f"{len(requests_seen)} model requests, expected one round"
    big = [n for n in sizes if n >= len(reply)]
    assert len(big) == 2, f"the lane read the 128 KB reply {len(big)} times, expected two"
    assert sum(spent) < 1.0, f"{len(spent)} calls took {sum(spent):.2f} s in the XML lane"
    assert ran == [], "a tool ran for markup without a closer"
    # chat_step closes the stream with a newline of its own.
    assert "".join(c for c in chunks if isinstance(c, str)) == reply + "\n"


# ── the removed regexes ────────────────────────────────────────────────────────────────────

_DEAD = ("_PARAM_RE", "_TOOL_USE_RE", "_CHILD_TAG_RE", "_TOOL_CALLS_WRAP_RE")


def test_dead_regexes_are_gone():
    for name in _DEAD:
        assert not hasattr(recovery, name), f"{name} is back in tool_call_recovery"
    pattern = re.compile(r"\b(?:" + "|".join(_DEAD) + r")\b")
    vaf_dir = Path(__file__).resolve().parents[1] / "vaf"
    hits = [p.relative_to(vaf_dir).as_posix() for p in vaf_dir.rglob("*.py")
            if pattern.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == [], f"a removed regex is referenced again: {hits}"


# ── seeded fuzz against the oracle ─────────────────────────────────────────────────────────

_TOKENS = [
    '<invoke name="web_search">', '<invoke  name="read_file" >', "</invoke>", '<invoke name="',
    '<parameter name="q" string="true">', '<parameter name="n"', '<parameter name="q">',
    "</parameter>", '<tool_use name="web_search">', '<tool_use id="1" name="read_file">',
    "<tool_use>", "<tool_use ", "<tool_user>", "</tool_use>", '<tool_use name="a>b">',
    "<web_search>", '<web_search q="1">', "</web_search>", "<read_file>", "</read_file>",
    "<x-y>", "</x-y>", "<a.b>", "</a.b>", "<a>", "<a >", "</a>", "<_b-1>", "</_b-1>", "<b>",
    "</b>", 'name="', 'name=""', 'name="a>b"', 'name="read_file"', '"', ">", "<", "</", " ",
    "\n", "｜｜DSML｜｜", "|DSML|", '<｜｜DSML｜｜ invoke name="web_search">',
    "</｜｜DSML｜｜ invoke>", '<｜｜DSML｜｜invoke name="read_file">', "</｜｜DSML｜｜parameter>",
    "<calls>", "</calls>", "<tool_calls", "<function_calls>", "</function_calls>",
    '{"tool_calls": [', '"tool_calls"', "{", "}", "1", "true", '"x"', "ä", "Hallo Welt. ",
    "tool_use name=", "invoke name=",
]


# Narrow families for the subtle walks, where the general list rarely builds a hit: a
# <tool_use head with several name attributes and a ">" inside a name, child tags that close,
# nest and stay open, and parameter heads inside other heads.
_FAMILIES = [
    ["<tool_use ", "<tool_use>", "<tool_user ", 'name="web_search" ', 'name="read_file"', 'name="', 'name=""',
     '"', ">", "</tool_use>", " ", "<q>1</q>", "web_search", "x"],
    ["<web_search>", "</web_search>", '<tool_use name="web_search">', "</tool_use>", "<a>",
     "</a>", "<a >", "<b>", "</b>", "<_b-1>", "</_b-1>", "1", "x", "<", ">", "</"],
    ['<invoke name="web_search">', "</invoke>", '<parameter name="q"', '<parameter name="',
     '<parameter name="n" string="true"', "<parameter ", ">", "</parameter>", '"', "1", "x",
     " "],
]


def _draw(rng: random.Random) -> str:
    roll = rng.random()
    tokens = _TOKENS if roll < 0.55 else _FAMILIES[int((roll - 0.55) / 0.15)]
    return "".join(rng.choice(tokens) for _ in range(rng.randint(0, 40)))


def _fuzz(draws: int, seed: int = 0x5EED) -> Dict[str, int]:
    """Runs `draws` seeded inputs through both implementations for every name list."""
    rng = random.Random(seed)
    stats = {"draws": draws, "recovered": 0, "stripped": 0}
    for n in range(draws):
        text = _draw(rng)
        ctx = f"draw {n}"
        got_any = False
        for names in NAME_SETS:
            got_any = bool(_assert_same(text, names, ctx)) or got_any
        stats["recovered"] += got_any
        _assert_strip_same(text, ctx)
        stats["stripped"] += strip_tool_call_markup(text) != text
    return stats


def test_xml_recovery_fuzz_matches_the_old_implementation():
    """2,000 seeded draws (well under a second); `VAF_XML_RECOVERY_FUZZ_DRAWS` runs more."""
    for names in NAME_SETS:
        assert extract_xml_tool_calls(None, names) == []
        assert extract_xml_tool_call(None, names) is None
    assert strip_tool_call_markup(None) is None
    stats = _fuzz(int(os.environ.get("VAF_XML_RECOVERY_FUZZ_DRAWS", "2000")))
    assert stats["recovered"] > stats["draws"] // 10, \
        f"too few draws recovered a call to compare anything: {stats}"
    assert stats["stripped"] > stats["draws"] // 10, stats
