# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Recover tool calls a model emitted as text instead of via structured tool_calls.

Some models (notably DeepSeek v4) intermittently emit a real tool call as assistant
CONTENT - in Claude-style XML (``<invoke name="X"><parameter name="P">V</parameter></invoke>``)
or wrapped in their own special tokens (``<｜｜DSML｜｜invoke name="X">…``) - instead of the
structured ``tool_calls`` field. Without recovery the raw markup is shown to the user and the
call never runs. Both the coding agent and the main agent route content through this on their
"no structured tool_calls" fallback path.

The token form varies by serving stack. One live leak (a Veyllo-served model) put a SPACE
between the token and the tag name, ``<｜｜DSML｜｜ invoke name="X">`` with ``</｜｜DSML｜｜ parameter>``
closers, and wrapped the batch in a bare ``calls`` instead of ``tool_calls``. Stripping the
token alone left ``< invoke ...>``, which no dialect matched: three calls never ran and the
raw block was the reply the user saw. The token regex therefore swallows the whitespace
after the token, and the wrapper strip knows ``calls`` and ``function_calls`` too.

The XML lanes read in linear time (n log n with the lookups): every opener, ``>`` and closer is
found once and looked up by position, never rescanned from each opener. The lazy regexes they
replace rescanned to the end from every unclosed opener, 27 s for 128 KB of unclosed child tags
and about 15 minutes for 128 KB of ``<tool_use name="a" `` heads, twice per generation round.
"""
from __future__ import annotations

import json
import os
import re
import time
from bisect import bisect_left
from typing import Any, Dict, List, Optional

# Dialect 1 - Anthropic/Claude: <invoke name="X"> ... <parameter name="P">V</parameter> ... </invoke>
# Only ever run up to the end of the last </invoke> (_invoke_matches): every match ends at a
# closer, and an opener past the last one would otherwise rescan the rest of the text.
_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)
# The head of <parameter name="P" ...>V</parameter>; the rest is walked (_parameters).
_PARAM_HEAD_RE = re.compile(r'<parameter\s+name="([^"]+)"')
# Dialect 2 - Morph <tool_use name="X" [id="..."]>: params are TAG-named children (<path>V</path>).
# The start of the tag and every name attribute; the head between them is walked (_tool_use_blocks).
_TOOL_USE_START_RE = re.compile(r"<tool_use\b")
_NAME_ATTR_RE = re.compile(r'\bname="')
# Direct child tags used as parameters in Dialects 2 & 3 (tag name = parameter name): the opener
# <name> and the closer </name>, paired by name in _child_params.
_CHILD_OPEN_RE = re.compile(r"<([A-Za-z_][\w-]*)\s*>")
_CHILD_CLOSE_RE = re.compile(r"</([A-Za-z_][\w-]*)>")
# DeepSeek wraps tags in its special-token delimiter (fullwidth pipe U+FF5C): <｜｜DSML｜｜invoke ...>.
# The trailing \s* eats the space some stacks put between the token and the tag name
# (<｜｜DSML｜｜ invoke ...>, </｜｜DSML｜｜ parameter>): without it the plain-XML regexes below see
# "< invoke" and match nothing. Only whitespace directly after the token is consumed.
_DSML_TOKEN_RE = re.compile(r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*")


class _Positions:
    """The sorted start positions of one needle in a text, found once, and the first one at or
    after a given position by bisection.

    Every walk in this module pairs an opener with the first ``>`` or the first closer after it.
    Searching forward from each opener is what made the lazy regexes quadratic: thousands of
    unclosed openers each scanned to the same distant ``>`` or to the end. Here each needle is
    found in one pass and every lookup is a bisection, so a walk costs n log n.

    Private to this module. ``completion.drop_redacted_blocks`` and
    ``web_server._legacy_dim_pairs`` walk an opener and a closer by hand too, but each with one
    fixed pair and semantics of its own (a cut at an unclosed opener, a closer with
    alternatives), already linear and tested. The helper moves to a shared module when a third
    module needs such a walk; the text lanes of ``Agent._text_tool_calls`` are the likely next.
    """

    __slots__ = ("_at",)

    def __init__(self, at: List[int]):
        self._at = at

    @classmethod
    def of(cls, text: str, needle: str) -> "_Positions":
        # Stepping by one, not by the needle's length: ">", '"' and the fixed closers cannot
        # overlap themselves, but a tool closer can when the caller's tool name holds "<" and
        # ">" (</a></a> for the name "a></a"), and the regex finds such a closer too.
        at = []
        i = text.find(needle)
        while i >= 0:
            at.append(i)
            i = text.find(needle, i + 1)
        return cls(at)

    def first_at(self, pos: int) -> int:
        """The first position at or after ``pos``, or -1."""
        k = bisect_left(self._at, pos)
        return self._at[k] if k < len(self._at) else -1


def _cut(text: str, spans) -> str:
    """``text`` without the given (start, end) spans, which are in order and disjoint."""
    parts = []
    pos = 0
    for start, end in spans:
        parts.append(text[pos:start])
        pos = end
    if not parts:
        return text
    parts.append(text[pos:])
    return "".join(parts)


def _invoke_matches(text: str):
    """The ``_INVOKE_RE`` matches of ``text``, in order.

    The search stops at the end of the last ``</invoke>``. Every match ends at a closer, so the
    matches are the same; an opener after the last closer is no longer tried against the whole
    rest of the text.
    """
    last = text.rfind("</invoke>")
    if last < 0:
        return iter(())
    return _INVOKE_RE.finditer(text, 0, last + len("</invoke>"))


def _parameters(body: str):
    """(name, attributes, value) of every ``<parameter name="P" ...>V</parameter>`` in an
    invoke body, in order; the matches of ``<parameter\\s+name="([^"]+)"([^>]*)>(.*?)</parameter>``.

    The head ends at the first ``>`` after the name and the value at the first closer after
    that. Every ``<parameter`` position is tried, also one inside an earlier opener's name, as
    a regex attempt at that position would be; positions inside a match are skipped.
    """
    gts = closers = None
    p = body.find("<parameter")
    while p >= 0:
        head = _PARAM_HEAD_RE.match(body, p)
        if head is None:
            p = body.find("<parameter", p + 1)
            continue
        if gts is None:
            gts = _Positions.of(body, ">")
            closers = _Positions.of(body, "</parameter>")
        g = gts.first_at(head.end())
        if g < 0:
            return
        c = closers.first_at(g + 1)
        if c < 0:
            # A later opener's head ends later, so its ">" and its closer would too.
            return
        yield head.group(1), body[head.end():g], body[g + 1:c]
        # The next attempt starts after the match, so an opener inside its value is skipped.
        p = body.find("<parameter", c + len("</parameter>"))


def _tool_use_blocks(text: str):
    """(start, end, name, body) of every ``<tool_use ... name="X" ...>body</tool_use>`` block,
    in order; the matches of ``<tool_use\\b[^>]*\\bname="([^"]+)"[^>]*>(.*?)</tool_use>``.

    The head runs to the first ``>`` (g1) and the regex backtracks over it from the right, so
    the LAST ``name="`` attribute before g1 that leads to a closer wins. Only that last one can
    have a name running past g1 (``name="a>b"``) and so its own ``>``: every earlier one's name
    ends at the next attribute's quote, so its ``>`` is g1. That leaves two checks per block,
    the last attribute and the last earlier one with a non-empty name.
    """
    if "<tool_use" not in text:
        return
    gts = _Positions.of(text, ">")
    quotes = _Positions.of(text, '"')
    closers = _Positions.of(text, "</tool_use>")
    attrs = [m.start() for m in _NAME_ATTR_RE.finditer(text)]
    # named[i]: the index of the last attribute at or before i whose name is not empty, or -1.
    named = []
    last = -1
    for i, s in enumerate(attrs):
        if text[s + 6:s + 7] not in ("", '"'):
            last = i
        named.append(last)
    taken = 0
    for m in _TOOL_USE_START_RE.finditer(text):
        if m.start() < taken:
            continue
        g1 = gts.first_at(m.end())
        if g1 < 0 or closers.first_at(g1 + 1) < 0:
            # A later block's head ends at this ">" or a later one, and needs a closer after it.
            return
        hi = bisect_left(attrs, g1) - 1
        if hi < 0 or attrs[hi] < m.end():
            continue
        hit = None
        s = attrs[hi]
        q = quotes.first_at(s + 6)
        if q > s + 6:
            g2 = gts.first_at(q + 1)
            c = closers.first_at(g2 + 1) if g2 >= 0 else -1
            if c >= 0:
                hit = (s, q, g2, c)
        if hit is None and hi > 0:
            j = named[hi - 1]
            if j >= 0 and attrs[j] >= m.end():
                s = attrs[j]
                hit = (s, quotes.first_at(s + 6), g1, closers.first_at(g1 + 1))
        if hit is None:
            continue
        s, q, g2, c = hit
        taken = c + len("</tool_use>")
        yield m.start(), taken, text[s + 6:q], text[g2 + 1:c]


def _coerce(raw: str, force_string: bool = False) -> Any:
    """Coerce a text parameter value: keep strings, else JSON-parse (int/float/bool/object)."""
    raw = raw.strip()
    if force_string:
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _child_params(block: str) -> Dict[str, Any]:
    """Parse tag-named child parameters (<path>V</path>, <content>V</content>, ...).

    The matches of ``<([A-Za-z_][\\w-]*)\\s*>(.*?)</\\1>``: each opener closes at the first
    ``</name>`` of its own name after it. An opener whose name never closes is skipped and the
    walk goes on, because another name can still close; an opener inside a match is skipped.
    A name that comes twice keeps its first place and its last value.
    """
    args: Dict[str, Any] = {}
    if "</" not in block:
        return args
    by_name: Dict[str, List[int]] = {}
    for m in _CHILD_CLOSE_RE.finditer(block):
        by_name.setdefault(m.group(1), []).append(m.start())
    closers = {name: _Positions(at) for name, at in by_name.items()}
    taken = 0
    for m in _CHILD_OPEN_RE.finditer(block):
        name = m.group(1)
        if m.start() < taken or name not in closers:
            continue
        c = closers[name].first_at(m.end())
        if c < 0:
            continue
        args[name.strip()] = _coerce(block[m.end():c])
        taken = c + len(name) + 3
    return args


def _tool_tag_blocks(text: str, tool: str, gts: _Positions):
    """The body of every ``<TOOL ...>body</TOOL>`` block, in order; the matches of
    ``<TOOL\\b[^>]*>(.*?)</TOOL>``. ``gts`` holds the ``>`` positions of ``text``."""
    head = re.compile("<" + re.escape(tool) + r"\b")
    opener = "<" + tool
    closer = "</" + tool + ">"
    closers = None
    p = text.find(opener)
    while p >= 0:
        h = head.match(text, p)
        if h is None:
            p = text.find(opener, p + 1)
            continue
        g = gts.first_at(h.end())
        if g < 0:
            return
        if closers is None:
            closers = _Positions.of(text, closer)
        c = closers.first_at(g + 1)
        if c < 0:
            # A later opener's ">" is this one or a later one, so it has no closer either.
            return
        yield text[g + 1:c]
        # The next attempt starts after the match, so an opener inside its body is skipped.
        p = text.find(opener, c + len(closer))


def _mk(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Synthetic id: the model leaked this call as TEXT, no provider-issued id
    # exists. The call_synth_ prefix marks it so providers that only accept
    # their own ids on replay (Veyllo) get the exchange downgraded to plain
    # text pre-send; the random tail keeps two recoveries within the same
    # second unique (the old extracted_<epoch> ids collided).
    return {
        "id": f"call_synth_{int(time.time())}_{os.urandom(2).hex()}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def extract_xml_tool_calls(content: str, valid_names=None) -> list:
    """Recover EVERY tool call a model emitted as XML/text (not structured tool_calls).

    Handles three dialects:
      1. Anthropic/Claude   ``<invoke name="X"><parameter name="P">V</parameter></invoke>``
         (and DeepSeek's ``<｜｜DSML｜｜invoke …>`` token-wrapped variant).
      2. Morph ``<tool_use``  ``<tool_use name="X" id="…"><path>V</path><content>V</content></tool_use>``
      3. Morph tool-as-tag    ``<write_to_file><path>V</path>…</write_to_file>`` where the tool
         name IS the tag - only tried when ``valid_names`` is given (needed to tell a tool tag
         from ordinary markup in file content).

    ALL matches, not the first. A model that batches - four ``<invoke>`` blocks inside one
    ``<｜｜DSML｜｜tool_calls>`` wrapper is what DeepSeek actually emits - used to have three of
    them dropped, and `strip_tool_call_markup` then erased the evidence from the text. The user
    saw a sentence, one file was read, three were not, and nothing anywhere said so. Silently
    doing a quarter of the work is worse than showing raw markup.

    An entry whose name is not in ``valid_names`` is SKIPPED, and the rest still run. This
    differs from the wire-JSON recovery on purpose: that format is one object, so a foreign
    name makes the whole object suspect, while these are independent sibling blocks and
    refusing all of them for one bad neighbour would drop real work - the very defect above.

    Returns a list of OpenAI-style tool_call dicts (empty when nothing matched). Parameter
    values are coerced from text: an Anthropic ``string="true"`` attribute forces a string,
    otherwise values are JSON-parsed with a string fallback.

    Cost: each opener, ``>`` and closer is found once (see ``_Positions``), so a reply full of
    unclosed openers is read in n log n. The lazy regexes this replaced rescanned to the end
    from every unclosed opener: 3.2 s for 128 KB of ``<invoke name="x">``, 27 s for 128 KB of
    unclosed child tags in one ``<tool_use>``, about 15 minutes for 128 KB of
    ``<tool_use name="a" `` heads without ``>``, and the main agent runs this twice per
    generation round, holding the interpreter lock throughout. The results are those of the
    regexes, match for match. Named boundaries: dialect 3 is one pass per tool name (T * n,
    8 ms for 152 names on 128 KB of prose), because the result follows the order of the names;
    the reply length itself is not capped, and neither is the index: a list of positions costs
    about 36 bytes per needle found, 150 MB for a reply of 4 MB of ``>`` (5 MB at 128 KB). An
    ``array('l')`` would hold 35 MB there, but built 1.5 times and looked up 2.3 times slower.
    """
    if not content:
        return []
    # Strip any special-token wrapper (｜｜DSML｜｜ / |DSML| / stray fullwidth pipes) -> plain XML.
    norm = _DSML_TOKEN_RE.sub("", content).replace("｜", "")

    def _ok(name: str) -> bool:
        return bool(name) and (valid_names is None or name in valid_names)

    out: list = []

    # 1. Anthropic <invoke name>/<parameter name>
    for inv in _invoke_matches(norm):
        name = inv.group(1).strip()
        if not _ok(name):
            continue
        args: Dict[str, Any] = {}
        for pname, attrs, value in _parameters(inv.group(2)):
            args[pname.strip()] = _coerce(value, 'string="true"' in attrs)
        out.append(_mk(name, args))

    # 2. Morph <tool_use name="X" ...> with tag-named params
    for _start, _end, name, body in _tool_use_blocks(norm):
        name = name.strip()
        if _ok(name):
            out.append(_mk(name, _child_params(body)))

    if out:
        return out

    # 3. Morph tool-as-tag <TOOLNAME>...</TOOLNAME> - only with a known-tools allowlist.
    # Last, and only when the named dialects found nothing: a bare tool-named tag is the
    # shape most easily confused with ordinary markup inside file content. One pass per name,
    # in the caller's order, which is the order of the result.
    if valid_names:
        gts = _Positions.of(norm, ">")
        for tool in valid_names:
            for body in _tool_tag_blocks(norm, tool, gts):
                out.append(_mk(tool, _child_params(body)))
    return out


def extract_xml_tool_call(content: str, valid_names=None) -> Optional[Dict[str, Any]]:
    """First recovered XML tool call, or None. See :func:`extract_xml_tool_calls`.

    Kept for callers that genuinely want one call. New code should use the plural
    form: a model that batched four calls means all four.
    """
    calls = extract_xml_tool_calls(content, valid_names)
    return calls[0] if calls else None


def _wire_json_span(content: str):
    """(start, end) of the bare {"tool_calls": [...]} object in content, or None."""
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


def extract_wire_json_tool_calls(content: str, valid_names=None):
    """Recover tool calls a model leaked as a BARE OpenAI-wire JSON object in
    its text: ``{"tool_calls": [{"id": ..., "function": {"name", "arguments"},
    "type": "function"}]}`` (first live sighting: Veyllo echoing the wire
    format as content - the turn ended as think-only, the UI hid the raw JSON,
    and the user saw an agent that "thought but never answered").

    Returns a list of OpenAI-style tool_call dicts, or []. A provider-issued
    id in the leak is PRESERVED - Veyllo's stateful backend accepts only its
    own ids on replay, so re-minting would break the follow-up exchange; only
    a missing id gets a synthetic one. When ``valid_names`` is given, every
    recovered name must be known - one foreign name refuses the WHOLE leak
    (half-executing a hallucinated batch is worse than executing none of it).

    A MALFORMED item refuses the whole leak the same way: an item that is not
    an object, a ``function`` that is not an object, or a name that is not a
    non-empty string (also without ``valid_names``, the coder's call shape:
    a number used to be dispatched under its repr). The text then stays in
    the reply as written. The item loop once assumed dicts, so a reply holding
    ``{"tool_calls": [1]}`` raised out of ``chat_step`` and ended the turn.
    Arguments that are a JSON value but not an object or a string (``0``,
    ``false``, ``[1]``) are passed on as JSON TEXT, so ``decode_arguments``
    answers the call in its own tool message with the type it got; they used
    to collapse to ``"{}"`` (the tool ran with no arguments) or to a Python
    repr (``"True"``, refused as a parse error instead of by type). An id that
    is not a non-empty string gets a synthetic one.

    Contract: for any ``str`` or ``None`` input this never raises and returns
    a list of ``{"id": non-empty str, "type": "function", "function":
    {"name": non-empty str, "arguments": str}}``. The guards are explicit type
    checks rather than a catch around the loop, which would also hide a real
    bug in the valid path.
    """
    span = _wire_json_span(content)
    if not span:
        return []
    start, end = span
    try:
        data = json.loads(content[start:end])
    except Exception:
        return []
    # The span opens with "{" and closes at its matching "}", so a successful
    # decode is always an object.
    calls = data.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return []
    out = []
    for c in calls:
        if not isinstance(c, dict):
            return []
        fn = c.get("function")
        if not isinstance(fn, dict):
            return []
        name = fn.get("name")
        if not isinstance(name, str):
            return []
        name = name.strip()
        if not name:
            return []
        if valid_names is not None and name not in valid_names:
            return []
        args = fn.get("arguments")
        if args is None or args == "":
            args = "{}"
        elif not isinstance(args, str):
            # dict, list, number or bool: JSON text, never a Python repr, so the
            # dispatch judges the value the model actually wrote.
            try:
                args = json.dumps(args)
            except (TypeError, ValueError, RecursionError):
                return []
        leaked_id = c.get("id")
        leaked_id = leaked_id.strip() if isinstance(leaked_id, str) else ""
        out.append({
            "id": leaked_id or f"call_synth_{int(time.time())}_{os.urandom(2).hex()}",
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    return out


# The batch wrapper: DeepSeek's `tool_calls`, Anthropic's `function_calls`, and the bare
# `calls` one live leak used. Only reached when the content carries call markup (the guard
# in strip_tool_call_markup), so a `<calls>` tag in ordinary prose is never touched.
# The tag runs to the first ">" after this start (_wrapper_spans).
_TOOL_CALLS_WRAP_START_RE = re.compile(r"</?(?:tool_calls|function_calls|calls)\b")


def _wrapper_spans(text: str):
    """(start, end) of every ``</?(?:tool_calls|function_calls|calls)\\b[^>]*>`` tag, in order."""
    gts = None
    taken = 0
    for m in _TOOL_CALLS_WRAP_START_RE.finditer(text):
        if m.start() < taken:
            continue
        if gts is None:
            gts = _Positions.of(text, ">")
        g = gts.first_at(m.end())
        if g < 0:
            return
        taken = g + 1
        yield m.start(), taken


def strip_tool_call_markup(content: str) -> str:
    """Remove leaked XML/DSML tool-call markup from displayable/persisted assistant text.

    Also removes a bare OpenAI-wire ``{"tool_calls": [...]}`` object (the leak
    extract_wire_json_tool_calls recovers): once the call is executed, the raw
    JSON in the text is noise the user must never see.

    When a tool call is recovered from content (extract_xml_tool_call), the raw
    ``<invoke>…</invoke>`` / ``<tool_use>…</tool_use>`` / ``<｜｜DSML｜｜tool_calls>…`` markup is
    still in the assistant text - it must not be shown in the UI or fed back to the model next
    turn. This strips those blocks and leaves ordinary text intact. No-op (returns the input
    unchanged) when there is no such markup, so legitimate text - including a stray fullwidth
    pipe - is never touched.
    """
    if not content or ("invoke name=" not in content
                       and "tool_use name=" not in content
                       and "DSML" not in content
                       and '"tool_calls"' not in content):
        return content
    text = _DSML_TOKEN_RE.sub("", content).replace("｜", "")
    # Each stage reads what the stage before left: a removed <invoke> block can join the
    # text around it into a <tool_use> block, and that one goes too.
    text = _cut(text, (m.span() for m in _invoke_matches(text)))
    text = _cut(text, ((s, e) for s, e, _name, _body in _tool_use_blocks(text)))
    text = _cut(text, _wrapper_spans(text))
    span = _wire_json_span(text)
    if span:
        text = text[:span[0]] + text[span[1]:]
    return text.strip()
