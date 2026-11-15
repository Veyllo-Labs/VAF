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
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional

# Dialect 1 - Anthropic/Claude: <invoke name="X"> ... <parameter name="P">V</parameter> ... </invoke>
_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)
_PARAM_RE = re.compile(r'<parameter\s+name="([^"]+)"([^>]*)>(.*?)</parameter>', re.DOTALL)
# Dialect 2 - Morph <tool_use name="X" [id="..."]>: params are TAG-named children (<path>V</path>).
_TOOL_USE_RE = re.compile(r'<tool_use\b[^>]*\bname="([^"]+)"[^>]*>(.*?)</tool_use>', re.DOTALL)
# Direct child tags used as parameters in Dialects 2 & 3 (tag name = parameter name).
_CHILD_TAG_RE = re.compile(r"<([A-Za-z_][\w-]*)\s*>(.*?)</\1>", re.DOTALL)
# DeepSeek wraps tags in its special-token delimiter (fullwidth pipe U+FF5C): <｜｜DSML｜｜invoke ...>.
# The trailing \s* eats the space some stacks put between the token and the tag name
# (<｜｜DSML｜｜ invoke ...>, </｜｜DSML｜｜ parameter>): without it the plain-XML regexes below see
# "< invoke" and match nothing. Only whitespace directly after the token is consumed.
_DSML_TOKEN_RE = re.compile(r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*")


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
    """Parse tag-named child parameters (<path>V</path>, <content>V</content>, ...)."""
    args: Dict[str, Any] = {}
    for m in _CHILD_TAG_RE.finditer(block):
        args[m.group(1).strip()] = _coerce(m.group(2))
    return args


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
    """
    if not content:
        return []
    # Strip any special-token wrapper (｜｜DSML｜｜ / |DSML| / stray fullwidth pipes) -> plain XML.
    norm = _DSML_TOKEN_RE.sub("", content).replace("｜", "")

    def _ok(name: str) -> bool:
        return bool(name) and (valid_names is None or name in valid_names)

    out: list = []

    # 1. Anthropic <invoke name>/<parameter name>
    for inv in _INVOKE_RE.finditer(norm):
        name = inv.group(1).strip()
        if not _ok(name):
            continue
        args: Dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(inv.group(2)):
            args[pm.group(1).strip()] = _coerce(pm.group(3), 'string="true"' in pm.group(2))
        out.append(_mk(name, args))

    # 2. Morph <tool_use name="X" ...> with tag-named params
    for tu in _TOOL_USE_RE.finditer(norm):
        name = tu.group(1).strip()
        if _ok(name):
            out.append(_mk(name, _child_params(tu.group(2))))

    if out:
        return out

    # 3. Morph tool-as-tag <TOOLNAME>...</TOOLNAME> - only with a known-tools allowlist.
    # Last, and only when the named dialects found nothing: a bare tool-named tag is the
    # shape most easily confused with ordinary markup inside file content.
    if valid_names:
        for tool in valid_names:
            for m in re.finditer(rf"<{re.escape(tool)}\b[^>]*>(.*?)</{re.escape(tool)}>",
                                 norm, re.DOTALL):
                out.append(_mk(tool, _child_params(m.group(1))))
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
_TOOL_CALLS_WRAP_RE = re.compile(r"</?(?:tool_calls|function_calls|calls)\b[^>]*>")


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
    text = _INVOKE_RE.sub("", text)
    text = _TOOL_USE_RE.sub("", text)
    text = _TOOL_CALLS_WRAP_RE.sub("", text)
    span = _wire_json_span(text)
    if span:
        text = text[:span[0]] + text[span[1]:]
    return text.strip()
