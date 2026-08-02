# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one-shot completion primitive: one collector, one local lane, one API lane.

Every assertion here is a live defect one of the deleted hand-rolls actually had:
metadata frames concatenated into commit messages, error sentinels returned as content,
reasoning burned into empty local answers, hardcoded server URLs that broke in Docker,
a retry that could never fire because the manager yields errors instead of raising.
"""
from pathlib import Path

import pytest

import vaf.core.completion as comp
from vaf.core.completion import (
    collect_stream,
    complete,
    is_metadata_frame,
    strip_think_blocks,
)


class _FakeManager:
    """Ctor-compatible stand-in whose stream is scripted per test."""
    scripts = []          # list of chunk lists; each construction pops the next
    constructed = []      # (provider, model-per-call) records

    def __init__(self, provider, **kwargs):
        type(self).constructed.append(provider)
        self._chunks = type(self).scripts.pop(0) if type(self).scripts else []

    def chat_completion(self, messages, temperature, max_tokens, stream, model,
                        tools=None, tool_choice=None):
        type(self).constructed.append(("call", model))
        yield from self._chunks


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    _FakeManager.scripts = []
    _FakeManager.constructed = []
    import vaf.core.api_backend as ab
    monkeypatch.setattr(ab, "APIBackendManager", _FakeManager)
    yield


def _api(chunks, **kw):
    _FakeManager.scripts = [list(c) for c in chunks] if chunks and isinstance(chunks[0], list) else [list(chunks)]
    return complete("q", provider="openai", model="m1", **kw)


# ── the collector ────────────────────────────────────────────────────────────────────

def test_metadata_frames_never_reach_the_result():
    out = _api(["Fix parser", '{"finish_reason": "stop"}'])
    assert out == "Fix parser"
    assert "finish_reason" not in (out or "")


def test_an_error_sentinel_is_none_not_content():
    assert _api(["[API Error from openai: boom]"]) is None


def test_dict_chunks_contribute_their_content():
    assert collect_stream([{"content": "a"}, "b", {"other": 1}]) == "ab"


def test_think_blocks_are_stripped_by_default():
    assert _api(["<think>plan</think>", "Answer"]) == "Answer"


def test_strip_think_can_be_disabled():
    out = _api(["<think>plan</think>Answer"], strip_think=False)
    assert "<think>" in out


def test_an_unclosed_think_truncates_to_none():
    """A reply that was all reasoning must become None so the caller's fallback fires -
    the stricter attachment-RAG semantics, adopted because one-shot results land in
    commit messages and stored summaries."""
    assert _api(["<think>only reasoning, never closed"]) is None
    assert strip_think_blocks("keep this <think>drop the rest") == "keep this"


# ── the local lane ───────────────────────────────────────────────────────────────────

def _local(monkeypatch, response_json=None, status=200, url_box=None, payload_box=None,
           raise_exc=None, **kw):
    import requests

    def fake_post(url, json=None, timeout=None):
        if url_box is not None:
            url_box.append(url)
        if payload_box is not None:
            payload_box.append(json)
        if raise_exc:
            raise raise_exc

        class _R:
            status_code = status

            def json(self):
                return response_json or {}
        return _R()

    monkeypatch.setattr(requests, "post", fake_post)
    return complete("q", provider="local", **kw)


def test_the_local_payload_disables_thinking_and_uses_the_config_url(monkeypatch):
    """Not hardcoded: the URL comes from Config.get_llama_server_url (Docker-aware) -
    proven by pointing the config helper somewhere else and seeing the request follow."""
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get_llama_server_url",
                        classmethod(lambda cls, ep="": f"http://testhost:9999{ep}"))
    urls, payloads = [], []
    out = _local(monkeypatch, response_json={
        "choices": [{"message": {"content": "ok"}}]
    }, url_box=urls, payload_box=payloads)

    assert out == "ok"
    assert urls == ["http://testhost:9999/v1/chat/completions"]
    assert payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert payloads[0].get("stream") is None       # non-streaming one-shot


def test_reasoning_content_fallback_is_gated(monkeypatch):
    resp = {"choices": [{"message": {"content": "", "reasoning_content": "substance"},
                         "finish_reason": "length"}]}
    assert _local(monkeypatch, response_json=resp) == "substance"
    assert _local(monkeypatch, response_json=resp, allow_reasoning_fallback=False) is None


def test_a_dead_server_is_none_never_a_spawn(monkeypatch):
    assert _local(monkeypatch, raise_exc=ConnectionError("down")) is None


def test_the_module_never_starts_the_server():
    """The auditable half of the ONE-llama-server invariant: no import, no call that
    could load or spawn anything."""
    src = Path(comp.__file__).read_bytes().decode("utf-8")
    for forbidden in ("ServerManager", "load_model", "start_server", "ensure_local_model"):
        assert forbidden not in src, f"completion.py references {forbidden}"


# ── timeout and self-heal ────────────────────────────────────────────────────────────

def test_a_timeout_returns_none(monkeypatch):
    import time

    class _SlowManager(_FakeManager):
        def chat_completion(self, *a, **k):
            time.sleep(0.5)
            yield "too late"

    import vaf.core.api_backend as ab
    monkeypatch.setattr(ab, "APIBackendManager", _SlowManager)
    _SlowManager.scripts = [[]]
    assert complete("q", provider="openai", model="m1", timeout=0.05) is None


def test_the_retry_fires_on_a_sentinel_error_only_with_a_fallback_model():
    """The manager yields errors instead of raising, so the old exception-only retry
    was dead code. The decision now reads the sentinel text."""
    _FakeManager.scripts = [
        ["[API Error from openai: Error code: 400 invalid model]"],
        ["ok"],
    ]
    out = complete("q", provider="openai", model="bad", fallback_model="good")
    assert out == "ok"
    assert ("call", "good") in _FakeManager.constructed

    _FakeManager.scripts = [["[API Error from openai: Error code: 400 invalid model]"]]
    _FakeManager.constructed = []
    assert complete("q", provider="openai", model="bad") is None
    assert _FakeManager.constructed.count(("call", "bad")) == 1   # no second attempt
