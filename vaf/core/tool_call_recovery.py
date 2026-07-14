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
_DSML_TOKEN_RE = re.compile(r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}")


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


def extract_xml_tool_call(content: str, valid_names=None) -> Optional[Dict[str, Any]]:
    """Recover a single tool call that a model emitted as XML/text (not structured tool_calls).

    Handles three dialects, in order:
      1. Anthropic/Claude   ``<invoke name="X"><parameter name="P">V</parameter></invoke>``
         (and DeepSeek's ``<｜｜DSML｜｜invoke …>`` token-wrapped variant).
      2. Morph ``<tool_use``  ``<tool_use name="X" id="…"><path>V</path><content>V</content></tool_use>``
      3. Morph tool-as-tag    ``<write_to_file><path>V</path>…</write_to_file>`` where the tool
         name IS the tag - only tried when ``valid_names`` is given (needed to tell a tool tag
         from ordinary markup in file content).

    Returns an OpenAI-style tool_call dict, or None. When ``valid_names`` is provided the
    recovered name must be one of them. Parameter values are coerced from text: an Anthropic
    ``string="true"`` attribute forces a string, otherwise values are JSON-parsed with a string
    fallback.
    """
    if not content:
        return None
    # Strip any special-token wrapper (｜｜DSML｜｜ / |DSML| / stray fullwidth pipes) -> plain XML.
    norm = _DSML_TOKEN_RE.sub("", content).replace("｜", "")

    def _ok(name: str) -> bool:
        return bool(name) and (valid_names is None or name in valid_names)

    # 1. Anthropic <invoke name>/<parameter name>
    inv = _INVOKE_RE.search(norm)
    if inv and _ok(inv.group(1).strip()):
        args: Dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(inv.group(2)):
            args[pm.group(1).strip()] = _coerce(pm.group(3), 'string="true"' in pm.group(2))
        return _mk(inv.group(1).strip(), args)

    # 2. Morph <tool_use name="X" ...> with tag-named params
    tu = _TOOL_USE_RE.search(norm)
    if tu and _ok(tu.group(1).strip()):
        return _mk(tu.group(1).strip(), _child_params(tu.group(2)))

    # 3. Morph tool-as-tag <TOOLNAME>...</TOOLNAME> - only with a known-tools allowlist.
    if valid_names:
        for tool in valid_names:
            m = re.search(rf"<{re.escape(tool)}\b[^>]*>(.*?)</{re.escape(tool)}>", norm, re.DOTALL)
            if m:
                return _mk(tool, _child_params(m.group(1)))
    return None


_TOOL_CALLS_WRAP_RE = re.compile(r"</?tool_calls\b[^>]*>")


def strip_tool_call_markup(content: str) -> str:
    """Remove leaked XML/DSML tool-call markup from displayable/persisted assistant text.

    When a tool call is recovered from content (extract_xml_tool_call), the raw
    ``<invoke>…</invoke>`` / ``<tool_use>…</tool_use>`` / ``<｜｜DSML｜｜tool_calls>…`` markup is
    still in the assistant text - it must not be shown in the UI or fed back to the model next
    turn. This strips those blocks and leaves ordinary text intact. No-op (returns the input
    unchanged) when there is no such markup, so legitimate text - including a stray fullwidth
    pipe - is never touched.
    """
    if not content or ("invoke name=" not in content
                       and "tool_use name=" not in content
                       and "DSML" not in content):
        return content
    text = _DSML_TOKEN_RE.sub("", content).replace("｜", "")
    text = _INVOKE_RE.sub("", text)
    text = _TOOL_USE_RE.sub("", text)
    text = _TOOL_CALLS_WRAP_RE.sub("", text)
    return text.strip()
