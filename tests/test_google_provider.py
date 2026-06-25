# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""GoogleProvider compatibility tests (native google-genai SDK).

Pins the migration off the deprecated google-generativeai package: the OpenAI<->Gemini
tool roundtrip (tool_calls -> function_call parts, role:"tool" -> function_response),
system-message consolidation, thinking gating, tool_config mapping, streaming with
thought parts, and usage accounting. Builds REAL google.genai types but fakes the client
(no network, no API key).
"""
import json

import pytest

from vaf.core import api_backend
from vaf.core.api_backend import GoogleProvider
from google.genai import types


# ── Fake genai client (captures model/contents/config; returns canned parts) ───

class _FC:
    def __init__(self, name, args, id=None):
        self.name = name
        self.args = args
        self.id = id


class _Part:
    def __init__(self, text=None, function_call=None, thought=False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class _Usage:
    prompt_token_count = 5
    candidates_token_count = 7
    thoughts_token_count = 3


class _Resp:
    def __init__(self, parts):
        cand = type("C", (), {"content": type("Ct", (), {"parts": parts})()})()
        self.candidates = [cand]
        self.usage_metadata = _Usage()


class _Models:
    def __init__(self, parts):
        self._parts = parts
        self.kwargs = None

    def generate_content(self, **kw):
        self.kwargs = kw
        return _Resp(self._parts)

    def generate_content_stream(self, **kw):
        self.kwargs = kw
        return iter([_Resp(self._parts)])


class _Client:
    def __init__(self, parts):
        self.models = _Models(parts)


@pytest.fixture
def cfg(monkeypatch):
    state = {}
    monkeypatch.setattr(api_backend.Config, "get",
                        staticmethod(lambda k, d=None: state.get(k, d)))
    return state


def _provider(parts=None):
    p = GoogleProvider("dummy-key")
    p.client = _Client(parts if parts is not None else [_Part(text="ok")])
    return p


def _drive(p, messages, model, stream=True, tools=None, tool_choice=None, temperature=0.7):
    out = list(p.chat_completion(messages, temperature, 2048, stream, model, tools, tool_choice))
    return out, p.client.models.kwargs


# ── Thinking-model detection ──────────────────────────────────────────────────

def test_supports_thinking_matrix():
    assert GoogleProvider._supports_thinking("gemini-2.5-flash")
    assert GoogleProvider._supports_thinking("gemini-3.5-flash")
    assert not GoogleProvider._supports_thinking("gemini-2.0-flash")
    assert not GoogleProvider._supports_thinking("gemini-1.5-pro")


# ── Tool roundtrip in _build_contents (pure, real types) ──────────────────────

def test_build_contents_tool_roundtrip():
    import base64
    p = _provider()
    msgs = [
        {"role": "user", "content": "search cats"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "function": {"name": "web_search", "arguments": '{"q": "cats"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "3 hits"},
    ]
    contents = p._build_contents(msgs, types, base64)

    assert contents[0].role == "user"
    # assistant turn -> model role, function_call part
    assert contents[1].role == "model"
    fc_parts = [pt for pt in contents[1].parts if pt.function_call]
    assert fc_parts and fc_parts[0].function_call.name == "web_search"
    assert dict(fc_parts[0].function_call.args) == {"q": "cats"}
    # tool result -> user role, function_response part
    assert contents[2].role == "user"
    fr = contents[2].parts[0].function_response
    assert fr.name == "web_search"
    assert fr.response == {"result": "3 hits"}


def test_build_contents_skips_empty_assistant():
    import base64
    p = _provider()
    contents = p._build_contents(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}],
        types, base64,
    )
    assert all(c.role != "model" for c in contents)


def test_build_contents_image_part():
    import base64
    p = _provider()
    data_uri = "data:image/png;base64," + base64.b64encode(b"hello").decode()
    contents = p._build_contents(
        [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
        types, base64,
    )
    parts = contents[0].parts
    assert any(getattr(pt, "inline_data", None) for pt in parts)


# ── System consolidation ──────────────────────────────────────────────────────

def test_system_consolidation(cfg):
    p = _provider()
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "NUDGE"},   # mid-run -> user turn
        {"role": "user", "content": "again"},
    ]
    _, kw = _drive(p, messages, "gemini-2.5-flash")
    assert kw["config"].system_instruction == "SYS"
    # mid-run nudge survives as a user content
    texts = [pt.text for c in kw["contents"] if c.role == "user" for pt in c.parts if pt.text]
    assert "NUDGE" in texts


# ── Thinking gating ───────────────────────────────────────────────────────────

def test_thinking_config_for_supported_model(cfg):
    p = _provider()
    _, kw = _drive(p, [{"role": "user", "content": "hi"}], "gemini-2.5-flash")
    tc = kw["config"].thinking_config
    assert tc is not None and tc.include_thoughts is True


def test_thinking_absent_for_unsupported_model(cfg):
    p = _provider()
    _, kw = _drive(p, [{"role": "user", "content": "hi"}], "gemini-2.0-flash")
    assert kw["config"].thinking_config is None


def test_thinking_disabled_by_config(cfg):
    cfg["google_thinking"] = False
    p = _provider()
    _, kw = _drive(p, [{"role": "user", "content": "hi"}], "gemini-2.5-flash")
    assert kw["config"].thinking_config is None


# ── tool_config mapping ───────────────────────────────────────────────────────

def test_tool_choice_required_maps_to_any(cfg):
    p = _provider()
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    _, kw = _drive(p, [{"role": "user", "content": "hi"}], "gemini-2.5-flash",
                   tools=tools, tool_choice="required")
    fcc = kw["config"].tool_config.function_calling_config
    assert str(fcc.mode).endswith("ANY")


def test_tool_choice_auto_omits_tool_config(cfg):
    p = _provider()
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    _, kw = _drive(p, [{"role": "user", "content": "hi"}], "gemini-2.5-flash",
                   tools=tools, tool_choice="auto")
    assert kw["config"].tool_config is None
    assert kw["config"].tools  # tools still declared


# ── Streaming: thought parts wrapped, function calls emitted, usage recorded ───

def test_streaming_thought_and_tool_and_usage(cfg):
    parts = [
        _Part(text="thinking hard", thought=True),
        _Part(function_call=_FC("web_search", {"q": "x"}, id="fc1")),
        _Part(text="final answer"),
    ]
    p = _provider(parts)
    out, _ = _drive(p, [{"role": "user", "content": "hi"}], "gemini-2.5-flash")
    blob = "".join(out)
    assert "<think>thinking hard</think>" in blob
    assert "final answer" in blob
    payloads = [json.loads(o) for o in out if o.startswith("{")]
    tool = [d for d in payloads if "tool_calls" in d]
    assert tool and tool[0]["tool_calls"][0]["function"]["name"] == "web_search"
    # usage: output = candidates(7) + thoughts(3)
    assert p.last_request_usage == {"input_tokens": 5, "output_tokens": 10}
