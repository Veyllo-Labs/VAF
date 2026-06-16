"""AnthropicProvider compatibility tests.

Pins the native Messages API integration: the streaming-kwarg regression (the SDK's
messages.stream() does not accept a `stream` argument), the OpenAI<->Anthropic
tool roundtrip conversion, system-message consolidation, temperature gating, adaptive
thinking, and the raw-block replay side-channel that keeps a thinking-enabled tool loop
from 400-ing. All tests run with a faked client — no network, no API key.
"""
import json

import pytest

from vaf.core import api_backend
from vaf.core.api_backend import AnthropicProvider


# ── Fakes for the anthropic SDK client ────────────────────────────────────────

class _FakeUsage:
    input_tokens = 11
    output_tokens = 22


class _Delta:
    def __init__(self, dtype, value):
        self.type = dtype
        if dtype == "text_delta":
            self.text = value
        elif dtype == "thinking_delta":
            self.thinking = value


class _Event:
    def __init__(self, delta, etype="content_block_delta"):
        self.type = etype
        self.delta = delta


class _Block:
    """Mimics an SDK content block: attribute access + model_dump()."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _Final:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = _FakeUsage()


class _StreamCtx:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, events, final):
        self._events = events
        self._final = final
        self.stream_kwargs = None
        self.create_kwargs = None

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return _StreamCtx(self._events, self._final)

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self._final


class _FakeClient:
    def __init__(self, events, final):
        self.messages = _FakeMessages(events, final)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture
def cfg(monkeypatch):
    """Hermetic Config.get: returns from a dict, else the passed default."""
    state = {}

    def fake_get(key, default=None):
        return state.get(key, default)

    monkeypatch.setattr(api_backend.Config, "get", staticmethod(fake_get))
    return state


def _provider(events=None, final=None):
    p = AnthropicProvider("dummy-key")
    p.client = _FakeClient(events or [], final or _Final([_Block(type="text", text="ok")]))
    return p


def _drive(provider, messages, model, stream=True, tools=None, temperature=0.7, tool_choice=None):
    return list(provider.chat_completion(messages, temperature, 1024, stream, model, tools, tool_choice))


# ── 1. Streaming regression: no `stream` kwarg to messages.stream() ───────────

def test_stream_does_not_pass_stream_kwarg(cfg):
    p = _provider(events=[_Event(_Delta("text_delta", "hi"))],
                  final=_Final([_Block(type="text", text="hi")]))
    out = _drive(p, [{"role": "user", "content": "hello"}], "claude-sonnet-4-6", stream=True)
    assert "hi" in "".join(out)
    sk = p.client.messages.stream_kwargs
    assert sk is not None, "messages.stream() was never called"
    assert "stream" not in sk, "stream kwarg must not be forwarded to messages.stream()"


# ── 2. Tool roundtrip conversion (pure) ───────────────────────────────────────

def test_tool_roundtrip_conversion():
    p = _provider()
    history = [
        {"role": "user", "content": "search cats"},
        {"role": "assistant", "content": "calling tool", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "web_search", "arguments": '{"query": "cats"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "name": "web_search", "content": "found 3 results"},
    ]
    conv = p._convert_messages_to_anthropic(history)

    assistant = conv[1]
    assert assistant["role"] == "assistant"
    text_block, tool_block = assistant["content"]
    assert text_block == {"type": "text", "text": "calling tool"}
    assert tool_block["type"] == "tool_use"
    assert tool_block["id"] == "call_1"
    assert tool_block["name"] == "web_search"
    assert tool_block["input"] == {"query": "cats"}  # arguments JSON-parsed

    result = conv[2]
    assert result["role"] == "user"
    assert result["content"][0] == {
        "type": "tool_result", "tool_use_id": "call_1", "content": "found 3 results",
    }


def test_tool_result_merge_consecutive():
    p = _provider()
    history = [
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "t", "arguments": "{}"}},
            {"id": "b", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
    ]
    conv = p._convert_messages_to_anthropic(history)
    # Both tool results land in ONE user message (parallel-tool pattern).
    user_turns = [m for m in conv if m["role"] == "user"]
    assert len(user_turns) == 1
    ids = [b["tool_use_id"] for b in user_turns[0]["content"]]
    assert ids == ["a", "b"]


def test_bad_arguments_fall_back_to_empty_input():
    p = _provider()
    history = [{"role": "assistant", "tool_calls": [
        {"id": "x", "function": {"name": "t", "arguments": "{not json"}},
    ]}]
    conv = p._convert_messages_to_anthropic(history)
    assert conv[0]["content"][0]["input"] == {}


def test_empty_plain_assistant_dropped():
    p = _provider()
    conv = p._convert_messages_to_anthropic([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},  # empty -> dropped (Anthropic rejects empty)
    ])
    assert all(m["role"] != "assistant" for m in conv)


# ── 3. Verbatim _anthropic_blocks replay (thinking preservation) ──────────────

def test_verbatim_anthropic_blocks_replay():
    p = _provider()
    raw = [
        {"type": "thinking", "thinking": "let me think", "signature": "sig123"},
        {"type": "tool_use", "id": "tu1", "name": "calc", "input": {"x": 1}},
    ]
    history = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "tu1", "function": {"name": "calc", "arguments": '{"x":1}'}}],
        "_anthropic_blocks": raw,
    }]
    conv = p._convert_messages_to_anthropic(history)
    # Raw blocks (incl. the signed thinking block) are replayed verbatim, not synthesized.
    assert conv[0] == {"role": "assistant", "content": raw}


# ── 4. System consolidation ───────────────────────────────────────────────────

def test_system_consolidation(cfg):
    p = _provider(final=_Final([_Block(type="text", text="x")]))
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "NUDGE"},   # mid-run -> becomes a user turn
        {"role": "user", "content": "again"},
    ]
    _drive(p, messages, "claude-sonnet-4-6", stream=True)
    sk = p.client.messages.stream_kwargs
    # Leading system -> single top-level system (cache-wrapped by default).
    assert sk["system"][0]["text"] == "SYS"
    # Mid-run system nudge appears as a user message, not a lost/overwriting system.
    assert any(m["role"] == "user" and m["content"] == "NUDGE" for m in sk["messages"])


# ── 5. Temperature gating ─────────────────────────────────────────────────────

def test_temperature_omitted_for_no_sampling_model(cfg):
    cfg["anthropic_thinking"] = False  # isolate the rejects-sampling path
    p = _provider()
    _drive(p, [{"role": "user", "content": "hi"}], "claude-opus-4-8", stream=True)
    assert "temperature" not in p.client.messages.stream_kwargs


def test_temperature_sent_when_thinking_disabled(cfg):
    cfg["anthropic_thinking"] = False
    p = _provider()
    _drive(p, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6", stream=True, temperature=0.5)
    assert p.client.messages.stream_kwargs.get("temperature") == 0.5


# ── 6. Adaptive thinking gating ───────────────────────────────────────────────

def test_thinking_param_for_supported_model(cfg):
    p = _provider()
    _drive(p, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6", stream=True)
    sk = p.client.messages.stream_kwargs
    assert sk["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in sk  # thinking on -> no sampling param


def test_thinking_absent_for_unsupported_model(cfg):
    p = _provider()
    _drive(p, [{"role": "user", "content": "hi"}], "claude-haiku-4-5", stream=True)
    sk = p.client.messages.stream_kwargs
    assert "thinking" not in sk
    assert sk.get("temperature") == 0.7  # haiku accepts sampling


def test_supports_thinking_matrix():
    yes = ["claude-sonnet-4-6", "claude-opus-4-8", "claude-opus-4-6", "claude-fable-5"]
    no = ["claude-haiku-4-5", "claude-3-5-sonnet-20241022", "claude-3-opus-20240229"]
    assert all(AnthropicProvider._supports_thinking(m) for m in yes)
    assert not any(AnthropicProvider._supports_thinking(m) for m in no)


def test_rejects_sampling_matrix():
    assert AnthropicProvider._rejects_sampling("claude-opus-4-8")
    assert AnthropicProvider._rejects_sampling("claude-fable-5")
    assert not AnthropicProvider._rejects_sampling("claude-sonnet-4-6")
    assert not AnthropicProvider._rejects_sampling("claude-opus-4-6")


# ── 7. Stop-reason + raw-block emission ───────────────────────────────────────

def test_refusal_stop_reason_yields_message_not_crash(cfg):
    final = _Final([], stop_reason="refusal")
    p = _provider(final=final)
    out = "".join(_drive(p, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6", stream=True))
    assert "declined" in out.lower()


def test_anthropic_blocks_emitted_on_thinking_tool_use(cfg):
    content = [
        _Block(type="thinking", thinking="reason", signature="sig"),
        _Block(type="tool_use", id="tu1", name="web_search", input={"q": "x"}),
    ]
    final = _Final(content, stop_reason="tool_use")
    p = _provider(events=[], final=final)
    out = _drive(p, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
                 stream=True, tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}])
    payloads = [json.loads(o) for o in out if o.startswith("{")]
    assert any("tool_use" in d for d in payloads), "tool_use payload must be emitted"
    blocks_msgs = [d for d in payloads if "_anthropic_blocks" in d]
    assert blocks_msgs, "_anthropic_blocks side-channel must be emitted for thinking+tool_use"
    # The signed thinking block is preserved for verbatim replay.
    types = [b["type"] for b in blocks_msgs[0]["_anthropic_blocks"]]
    assert "thinking" in types and "tool_use" in types
