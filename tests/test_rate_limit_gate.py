# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The request gate for providers that publish their per-key request window.

The Veyllo API names the window on every answer, streams and the 429 included:
x-ratelimit-remaining-requests / -reset-requests ("41s"), and retry-after on a
429. VAF must send nothing while remaining is 0 until the reset has passed, and
after a 429 wait exactly retry-after. Measured before the gate, against a 429
that kept answering retry-after 41: one SDK call sent 6 requests and waited
194 s against a 60 s budget, and VAF's own capped wait (30 s) sent 11 s early.

All answers are fake (the network is httpx's HTTPTransport.handle_request
replaced, below every client the product builds) and the clock is fake: no
request leaves the machine and no test sleeps.
"""
import ast
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import vaf.core.rate_limit_gate as rl
from vaf.core.rate_limit_gate import RateLimitGate, RateLimitHeld, rate_limit_gate, send_gated

ROOT = pathlib.Path(__file__).resolve().parents[1]

CLOSED = {"x-ratelimit-limit-requests": "180", "x-ratelimit-remaining-requests": "0",
          "x-ratelimit-reset-requests": "41s"}
OPEN = {"x-ratelimit-limit-requests": "180", "x-ratelimit-remaining-requests": "5",
        "x-ratelimit-reset-requests": "41s"}


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self._lock = threading.Lock()

    def monotonic(self):
        return self.t

    def sleep(self, s):
        with self._lock:
            self.sleeps.append(float(s))
            self.t += float(s)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    # Before AND after: the STT tests elsewhere share key literals, and a gate left
    # closed here would hold their requests.
    rl.reset_gates()
    c = FakeClock()
    monkeypatch.setattr(rl, "_monotonic", c.monotonic)
    monkeypatch.setattr(rl, "_sleep", c.sleep)
    monkeypatch.setattr(time, "sleep", c.sleep)
    yield c
    rl.reset_gates()


@pytest.fixture
def config(monkeypatch):
    values = {"api_retry_attempts": 2, "api_rate_limit_wait_max": 60, "api_retry_after_max": 30}
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: values.get(k, d)))
    return values


@pytest.fixture
def network(monkeypatch, clock):
    """Every request that reaches the network: (fake time, path)."""
    net = {"log": [], "answer": None}

    def handle_request(self, request):
        net["log"].append((clock.t, request.url.path))
        return net["answer"](request, len(net["log"]))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return net


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def gate_log():
    handler = _Lines()
    logger = logging.getLogger("vaf.rate_limit_gate")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(old)


def _gate(clock):
    return RateLimitGate("Veyllo", clock=clock.monotonic, sleep=clock.sleep)


def _completion(text="ok"):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "veyllo-chat",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


_SSE = (b'data: {"id":"a","object":"chat.completion.chunk","created":0,"model":"m",'
        b'"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
        b"data: [DONE]\n\n")


def _ok(headers, stream):
    def answer(request, n):
        if stream:
            return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"},
                                  content=_SSE)
        return httpx.Response(200, headers=headers, json=_completion())
    return answer


def _too_many(retry_after):
    def answer(request, n):
        return httpx.Response(429, headers={**CLOSED, "retry-after": str(retry_after)},
                              json={"error": {"type": "rate_limit_exceeded",
                                              "code": "rate_limit_exceeded",
                                              "message": "limit"}})
    return answer


def _chat(provider, stream=False):
    return "".join(provider.chat_completion([{"role": "user", "content": "hi"}], 0.7, 16,
                                            stream, "veyllo-chat", None))


def _veyllo(key="vk-test-key"):
    from vaf.core.api_backend import OpenAIProvider
    return OpenAIProvider("veyllo", key, base_url="https://api.example.test/v1")


# ── AC1: every contract header is read, and the 429 wait is not capped ────────

@pytest.mark.parametrize("status,headers,expected", [
    (200, {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "41s"}, 41.0),
    (200, {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1m5s"}, 65.0),
    (200, {"X-RateLimit-Remaining-Requests": "0", "X-RateLimit-Reset-Requests": "41s"}, 41.0),
    (200, {"x-ratelimit-remaining-requests": "3", "x-ratelimit-reset-requests": "41s"}, 0.0),
    (429, {"retry-after": "41"}, 41.0),
    (429, {"x-ratelimit-reset-requests": "12s"}, 12.0),
    (200, {"x-ratelimit-remaining-requests": "0"}, 60.0),
    (429, {}, 0.0),
    (200, None, 0.0),
    (200, {"x-ratelimit-remaining-requests": "zero", "x-ratelimit-reset-requests": "soon"}, 0.0),
    (429, {"retry-after": "Wed, 21 Oct"}, 0.0),
    (None, "not headers", 0.0),
    # A 429 waits what it names, even with remaining 0 and no reset beside it.
    (429, {"retry-after": "5", "x-ratelimit-remaining-requests": "0"}, 5.0),
    (429, {"retry-after": "5", "x-ratelimit-remaining-requests": "0",
           "x-ratelimit-reset-requests": "41s"}, 5.0),
    # Outside the one-minute contract: at most ten windows.
    (429, {"retry-after": "86400"}, 600.0),
    (429, {"retry-after": "1e308"}, 600.0),
    (200, {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "3h"}, 600.0),
])
def test_observe_parses_every_contract_header(clock, config, status, headers, expected):
    gate = _gate(clock)
    gate.observe(status, headers)
    assert gate.closed_for() == pytest.approx(expected)


_HOSTILE_PROBE = r"""
import time
from vaf.core.rate_limit_gate import RateLimitGate, rate_limit_wait_seconds
K = 16 * 1024
cases = [
    lambda: rate_limit_wait_seconds({"retry-after": "1h" + " " * K + "x"}, ""),
    lambda: RateLimitGate("Veyllo").observe(200, {
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "1h" + " " * K + "x"}),
    lambda: rate_limit_wait_seconds(None, "try again in 1h" + " " * K + "hh"),
    lambda: rate_limit_wait_seconds(None, "try again in 1 " * (K // 15)),
]
worst = 0.0
for case in cases:
    t = time.perf_counter()
    case()
    worst = max(worst, time.perf_counter() - t)
print(worst)
"""


def test_hostile_duration_values_parse_in_milliseconds():
    """A 16 KiB header or error body built to make the duration parser backtrack.

    The regex engine holds the GIL while it matches, so a slow match stops every
    thread in the process, the web server included. Run in a child process with a
    timeout, so a regression fails here instead of hanging the suite.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH", "")) if p)
    try:
        out = subprocess.run([sys.executable, "-c", _HOSTILE_PROBE], env=env, cwd=str(ROOT),
                             capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("parsing a 16 KiB hostile duration did not finish in 30 s")
    assert out.returncode == 0, out.stderr[-2000:]
    worst = float(out.stdout.strip().splitlines()[-1])
    assert worst < 0.25, f"slowest hostile parse took {worst:.3f} s"


def test_the_body_phrase_still_reads_real_messages():
    assert rl.rate_limit_wait_seconds(None, "Please try again in 1m 5s. Visit x") == pytest.approx(65.0)
    assert rl.rate_limit_wait_seconds(None, "Rate limited, try again in 20s.") == pytest.approx(20.0)
    assert rl._parse_duration_seconds("1h 2m") == pytest.approx(3720.0)
    assert rl._parse_duration_seconds("4" * 40) is None


def test_an_answer_without_headers_closes_nothing(clock):
    gate = _gate(clock)
    assert send_gated(gate, lambda: SimpleNamespace(status_code=200), max_wait=60).status_code == 200
    assert gate.closed_for() == 0


# ── AC2: the deadline only moves forward ──────────────────────────────────────

def test_deadline_only_moves_forward(clock):
    gate = _gate(clock)
    gate.observe(200, CLOSED)
    clock.t = 10.0
    gate.observe(200, {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "5s"})
    assert gate.closed_for() == pytest.approx(31.0)
    clock.t = 41.0
    assert gate.closed_for() == 0


# ── AC3: one admin line per closed window ─────────────────────────────────────

def test_one_log_line_per_closed_window(clock, gate_log):
    gate = _gate(clock)
    for t in (0.0, 0.3, 0.6):
        clock.t = t
        gate.observe(200, CLOSED)
    threads = [threading.Thread(target=gate.wait, args=(100,)) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert gate.closed_for() == 0
    assert gate_log == ["Veyllo API limit reached, continuing in 41 s"]
    # The window reopened; the next closed one is announced again.
    gate.observe(429, {"retry-after": "12"})
    assert gate_log[1:] == ["Veyllo API limit reached, continuing in 12 s"]


def test_a_deadline_creeping_forward_is_announced_once(clock, gate_log):
    """Answers of one window seconds apart move the deadline in small steps;
    together they pass 1 s, each alone does not."""
    gate = _gate(clock)
    for t in (0.0, 0.6, 1.2, 1.8, 2.4):
        clock.t = t
        gate.observe(200, CLOSED)
    assert gate.closed_for() == pytest.approx(41.0)
    assert gate_log == ["Veyllo API limit reached, continuing in 41 s"]


# ── AC4: one gate per key in the process, Veyllo only, no raw key kept ────────

def test_gate_registry_shared_per_key_and_veyllo_only(config):
    from vaf.core.provider_registry import PROVIDER_SPECS
    key = "vk-secret-registry-key"
    barrier = threading.Barrier(2)
    got = []

    def grab():
        barrier.wait(5)
        got.append(rate_limit_gate("veyllo", key))

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert got[0] is not None and got[0] is got[1]
    assert rate_limit_gate("Veyllo", key) is got[0]
    assert _veyllo(key)._rate_gate is _veyllo(key)._rate_gate is got[0]
    assert rate_limit_gate("veyllo", key + "-2") is not got[0]

    for provider in ("openai", "deepseek", "openrouter", "anthropic", "google", "local", "nope"):
        assert rate_limit_gate(provider, key) is None, provider
    assert rate_limit_gate("veyllo", "") is None
    assert rate_limit_gate("veyllo", None) is None

    gate = got[0]
    assert key not in repr(gate) and key not in repr(vars(gate))
    assert key not in repr(rl._GATES)
    assert [n for n, s in PROVIDER_SPECS.items() if s.request_limit_headers] == ["veyllo"]


# ── AC5: the SDK lane sends nothing while remaining is 0 ──────────────────────

@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_sdk_lane_holds_after_remaining_zero(config, network, clock, stream):
    provider = _veyllo()
    network["answer"] = lambda req, n: _ok(CLOSED if n == 1 else OPEN, stream)(req, n)
    assert _chat(provider, stream) == "ok"
    assert _chat(provider, stream) == "ok"
    assert [t for t, _ in network["log"]] == [0.0, 41.0]
    assert all(path.endswith("/chat/completions") for _, path in network["log"])


# ── AC6: a 429 is waited out exactly, inside the budget ───────────────────────

def test_sdk_lane_waits_exactly_retry_after(config, network, clock):
    provider = _veyllo()
    network["answer"] = _too_many(41)
    out = _chat(provider)
    assert out.startswith("[API Error from veyllo:"), out
    assert [t for t, _ in network["log"]] == [0.0, 41.0]
    assert sum(clock.sleeps) <= 60.0


# ── AC7: a hold past the budget sends nothing more ────────────────────────────

def test_sdk_lane_over_budget_sends_once(config, network, clock):
    provider = _veyllo()
    network["answer"] = _too_many(120)
    out = _chat(provider)
    assert out.startswith("[API Error from veyllo:"), out
    assert len(network["log"]) == 1
    assert all(s <= 60.0 for s in clock.sleeps) and sum(clock.sleeps) <= 60.0
    # The window is still closed: the next call answers locally, nothing is sent.
    assert _chat(provider).startswith("[API Error from veyllo:")
    assert len(network["log"]) == 1


# ── AC8: instances with one key share the window, another key does not ────────

def test_two_instances_one_key_share_the_window(config, network, clock):
    a, b, c = _veyllo("vk-one"), _veyllo("vk-one"), _veyllo("vk-two")
    network["answer"] = lambda req, n: _ok(CLOSED if n == 1 else OPEN, False)(req, n)
    assert _chat(a) == "ok"
    assert _chat(c) == "ok"
    assert _chat(b) == "ok"
    assert [t for t, _ in network["log"]] == [0.0, 0.0, 41.0]


# ── AC9: every other provider keeps the SDK's own client ──────────────────────

def test_other_providers_unchanged(config, network, clock):
    import openai
    from vaf.core.api_backend import OpenAIProvider
    p = OpenAIProvider("openai", "sk-test", base_url="https://api.example.test/v1")
    assert p._rate_gate is None
    assert not isinstance(p.client._client, rl.GatedHttpxClient)
    assert p.client.max_retries == openai.DEFAULT_MAX_RETRIES
    network["answer"] = _ok(CLOSED, False)
    assert _chat(p) == "ok" and _chat(p) == "ok"
    assert [t for t, _ in network["log"]] == [0.0, 0.0]
    assert clock.sleeps == []


def test_the_veyllo_client_is_gated_without_sdk_retries():
    import openai
    p = _veyllo()
    assert isinstance(p.client._client, rl.GatedHttpxClient)
    assert p.client._client.rate_gate is p._rate_gate
    assert p.client.max_retries == 0
    assert p.client._client._transport._pool._max_connections == \
        openai.DEFAULT_CONNECTION_LIMITS.max_connections


# ── AC10: send_gated waits, refuses, and observes ─────────────────────────────

def test_send_gated_waits_raises_and_observes(clock):
    gate = _gate(clock)
    calls = []

    def fn(headers=None):
        calls.append(clock.t)
        return SimpleNamespace(status_code=200, headers=headers or {})

    gate.observe(429, {"retry-after": "10"})
    send_gated(gate, fn, max_wait=60)
    assert clock.sleeps == [10.0] and calls == [10.0]

    gate.observe(429, {"retry-after": "90"})
    with pytest.raises(RateLimitHeld) as held:
        send_gated(gate, fn, max_wait=60)
    assert held.value.seconds == pytest.approx(90.0)
    assert "nothing was sent" in str(held.value)
    assert calls == [10.0] and clock.sleeps == [10.0]

    assert send_gated(None, fn, max_wait=0).status_code == 200
    assert len(calls) == 2

    clock.t = 200.0
    send_gated(gate, lambda: fn(CLOSED), max_wait=0)
    assert gate.closed_for() == pytest.approx(41.0)


def test_send_gated_reports_each_sleep_before_it(clock):
    gate = _gate(clock)
    gate.observe(429, {"retry-after": "10"})
    seen = []
    send_gated(gate, lambda: SimpleNamespace(status_code=200, headers={}), max_wait=60,
               on_sleep=lambda s: seen.append((clock.t, s)))
    assert seen == [(0.0, 10.0)] and clock.sleeps == [10.0]


# ── AC11: the coder waits the gate's hold, and every chat POST is gated ───────

def test_coder_rate_limit_wait_exact_for_gate(clock):
    from vaf.tools.coder import coder_rate_limit_wait
    gate = _gate(clock)
    gate.observe(429, {"retry-after": "41"})
    assert coder_rate_limit_wait(gate, 41.0, 30.0) == pytest.approx(41.0)
    assert coder_rate_limit_wait(None, 41.0, 30.0) == pytest.approx(30.0)
    assert coder_rate_limit_wait(None, 1.0, 30.0) == pytest.approx(1.25)
    assert coder_rate_limit_wait(None, None, 30.0) == pytest.approx(2.25)
    clock.t = 50.0  # the gate is open again: the parsed wait applies
    assert coder_rate_limit_wait(gate, 1.0, 30.0) == pytest.approx(1.25)


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr if not isinstance(f.value, ast.Name) else f"{f.value.id}.{f.attr}"
    return f.id if isinstance(f, ast.Name) else ""


def _coder_tree():
    return ast.parse((ROOT / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8"))


def _stream_send(tree):
    """The try statement around the coder's streamed chat request."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) \
                    and _call_name(stmt.value) == "send_gated" \
                    and any(isinstance(t, ast.Name) and t.id == "stream_response"
                            for t in stmt.targets):
                return node, stmt.value
    raise AssertionError("the streamed chat request is not sent through send_gated")


def test_coder_stream_ends_cleanly_when_the_window_stays_closed():
    try_node, call = _stream_send(_coder_tree())
    handlers = [h for h in try_node.handlers
                if isinstance(h.type, ast.Name) and h.type.id == "RateLimitHeld"]
    assert len(handlers) == 1, "the stream must catch RateLimitHeld itself"
    returns = [n for n in ast.walk(handlers[0]) if isinstance(n, ast.Return)]
    assert returns, "a held window must end the run, not fall through to a send"
    assert "nothing was sent" in ast.unparse(returns[0])
    # The wait before sending is shown and counted against the 429 budget.
    assert [k.arg for k in call.keywords] == ["max_wait", "on_sleep"]
    assert "rate_limit_waited" in ast.unparse(call.keywords[0].value)
    hook = call.keywords[1].value
    assert isinstance(hook, ast.Name) and hook.id == "_on_gate_sleep"


def _gate_sleep_hook():
    hooks = [n for n in ast.walk(_coder_tree())
             if isinstance(n, ast.FunctionDef) and n.name == "_on_gate_sleep"]
    assert len(hooks) == 1
    return hooks[0]


def test_coder_gate_wait_counts_against_the_429_budget():
    hook = _gate_sleep_hook()
    targets = [ast.unparse(t) for n in ast.walk(hook) if isinstance(n, ast.Assign)
               for t in n.targets]
    assert "loop.rate_limit_waited" in targets


def test_a_failing_display_cannot_stop_the_coder_wait():
    """The hook runs inside gate.wait, before the sleep: anything it raises would
    abort the wait and the run, so its whole body is guarded."""
    body = [stmt for stmt in _gate_sleep_hook().body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Try), "the whole hook must be in one try"
    guarded = body[0]
    assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in guarded.handlers)
    assert any(isinstance(n, ast.Assign) for n in guarded.body)


def test_coder_429_wait_comes_from_the_gate():
    calls = [n for n in ast.walk(_coder_tree())
             if isinstance(n, ast.Call) and _call_name(n) == "coder_rate_limit_wait"]
    assert len(calls) == 1
    first = calls[0].args[0]
    assert isinstance(first, ast.Name) and first.id == "_rate_gate"
    assert _call_name(calls[0].args[1]) == "rate_limit_wait_seconds"


def test_every_coder_chat_post_is_gated():
    tree = ast.parse((ROOT / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    posts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and _call_name(n) == "requests.post" and n.args
             and isinstance(n.args[0], ast.Name) and n.args[0].id == "_llm_chat_url"]
    assert len(posts) == 3

    for post in posts:
        lam = parents.get(post)
        assert isinstance(lam, ast.Lambda), f"line {post.lineno}: not wrapped in a lambda"
        outer = parents.get(lam)
        assert isinstance(outer, ast.Call) and _call_name(outer) == "send_gated" \
            and lam in outer.args, f"line {post.lineno}: the chat POST bypasses send_gated"
        assert isinstance(outer.args[0], ast.Name) and outer.args[0].id == "_rate_gate"


# ── AC12: speech-to-text never waits and never sends into a closed window ─────

def _stt_config(monkeypatch, key):
    import vaf.core.speech_api as sa
    from vaf.core.config import Config
    cfg = {"speech_stt_provider": "veyllo"}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: cfg.get(k, d)))
    monkeypatch.setattr(Config, "get_api_key",
                        classmethod(lambda cls, p: key if p == "veyllo" else ""))
    monkeypatch.setattr(sa, "_domain_log", lambda msg: None)
    return sa


def test_veyllo_stt_held_sends_nothing(monkeypatch, clock):
    sa = _stt_config(monkeypatch, "vk-stt")
    posts = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: posts.append(a) or None)
    rate_limit_gate("veyllo", "vk-stt").observe(200, CLOSED)
    assert sa.transcribe(b"RIFFdata") == (None, None)
    assert posts == [] and clock.sleeps == []


def test_veyllo_stt_observes(monkeypatch, clock):
    sa = _stt_config(monkeypatch, "vk-stt")
    posts = []

    def fake_post(url, **kw):
        posts.append(url)
        return SimpleNamespace(status_code=200, headers=httpx.Headers(CLOSED),
                               json=lambda: {"text": "guten tag", "language": "de"})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert sa.transcribe(b"RIFFdata") == ("guten tag", "de")
    assert len(posts) == 1
    assert rate_limit_gate("veyllo", "vk-stt").closed_for() == pytest.approx(41.0)


# ── AC13: a new lane that reads the Veyllo endpoint must use the gate ──────────

# Modules that name the Veyllo API endpoint without sending a gated request,
# each with its reason.
_UNGATED_VEYLLO_READERS = {
    "vaf/core/config.py": "the default value of veyllo_base_url",
    "vaf/core/provider_registry.py": "the registry row; it sends nothing",
    "vaf/api/auth_routes.py": "the setup key check: a /v1/models listing with a key "
                              "that is not stored yet (named boundary in rate_limit_gate.py)",
}


def test_every_veyllo_url_reader_uses_the_gate():
    import re
    pattern = re.compile(r"veyllo_base_url|VEYLLO_DEFAULT_BASE|api\.veyllo\.app")
    # An import, not a mention: a comment naming the module would pass a substring check.
    uses_gate = re.compile(r"^\s*from vaf\.core\.rate_limit_gate import ", re.MULTILINE)
    offenders = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        src = path.read_bytes().decode("utf-8", "replace")
        if not pattern.search(src) or rel in _UNGATED_VEYLLO_READERS:
            continue
        if not uses_gate.search(src):
            offenders.append(rel)
    assert offenders == [], f"reads the Veyllo endpoint without the request gate: {offenders}"
    for rel in _UNGATED_VEYLLO_READERS:
        assert (ROOT / rel).is_file(), f"stale allowlist entry: {rel}"


# ── AC14: local mode never meets a gate ───────────────────────────────────────

def test_coder_local_mode_has_no_gate():
    assert rate_limit_gate("local", "ollama") is None
    assert rate_limit_gate("veyllo", "") is None  # the coder's local branch key
    src = (ROOT / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")
    assert "_rate_gate = rate_limit_gate(_provider, _llm_api_key) if _is_api_mode else None" in src
