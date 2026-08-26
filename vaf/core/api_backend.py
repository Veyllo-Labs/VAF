# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF API Backend - Provider System
Implements structured, provider-specific interfaces for AI services.
Uses official SDKs (openai, anthropic, google-genai) for robust interaction.
"""

import os
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Generator, List, Union
from vaf.core.config import Config
from vaf.core.cost import blank_request_usage, cache_usage, cache_usage_from_openai
from vaf.cli.ui import UI

# Configure logging
logger = logging.getLogger("vaf.api_backend")


def consolidate_system_messages(messages: List[Dict]) -> List[Dict]:
    """Make a message list valid for strict LOCAL chat templates (e.g. Qwen, Gemma 4) that require a
    SINGLE system message at the very start.

    - LEADING system turns (everything before the first non-system message) are merged into one leading
      system message.
    - A system message that appears AFTER the conversation has started (a mid-run nudge: empty-retry,
      loop block, plan-required, [TODO STATUS], correction) is converted to a USER turn IN PLACE.
      Hoisting it to the front would lose its "respond to this now" position and leave the turn ending on
      an assistant message, which Qwen rejects with 400 "Assistant response prefill is incompatible with
      enable_thinking". As a user turn it stays in place and the turn ends on a user message.

    Pure + caller-gated (local, non-Gemma). Used by BOTH the main agent (_prepare_messages) and the coder
    (which builds its own clean_history and calls the provider directly, so it never went through the
    agent's consolidation -> Qwen 500 "System message must be at the beginning"). Returns the input
    unchanged when there are no system messages.
    """
    def _text(c):
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        return str(c or "").strip()

    leading: List[str] = []
    rest: List[Dict] = []
    seen_non_system = False
    for m in messages:
        if m.get("role") == "system":
            t = _text(m.get("content"))
            if not t:
                continue
            if seen_non_system:
                rest.append({"role": "user", "content": t})   # mid-run instruction -> user turn
            else:
                leading.append(t)
        else:
            seen_non_system = True
            rest.append(m)
    out: List[Dict] = []
    if leading:
        out.append({"role": "system", "content": "\n\n".join(leading)})
    out.extend(rest)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# OPENAI CHAT/COMPLETIONS REQUEST SHAPE (per model)
# ═══════════════════════════════════════════════════════════════════════════════
# THE shared answer to "which parameters does this model accept", for every lane
# that builds an OpenAI-compatible /v1/chat/completions body. Two lanes do:
# OpenAIProvider below (via the SDK) and the coder's raw HTTP request. Only the
# first one knew any of the rules, so the coder sent a fixed `max_tokens` plus a
# `temperature` to every provider, which is a 400 on the WHOLE gpt-5 family
# ("Unsupported parameter: 'max_tokens' ... Use 'max_completion_tokens'"); an
# OpenAI coder run on any gpt-5 model could not start. A model property has one
# home, not one per caller, so both lanes read it from here.
#
# NOT on the public facade, and the boundary is deliberate rather than an
# oversight. Both measured callers live inside vaf/ (this module and the coder);
# no harness lane and no embedder builds a chat/completions body at all, because
# an embedder reaches a model through Agent, which sits on top of this. That is
# the same reason consolidate_system_messages above is unexported: it is the
# older primitive of exactly this class, shared by exactly these two lanes. The
# day someone writes their own provider call against the framework, that is the
# first measured caller outside vaf/ and earns the export.

# Families that refuse FUNCTION TOOLS on /v1/chat/completions unless
# `reasoning_effort` is sent explicitly as "none". The API's own error names
# /v1/responses as the alternative; that is a different wire format, while
# "none" is the documented escape hatch on this endpoint. The cost is the
# model's server-side reasoning, and only on turns that carry tools - a
# tool-free call (vision description, summary, compaction) keeps it, which is
# why the value is attached to `has_tools` and not to the model alone.
#
# Measured against the live API, one variable at a time, tools + no effort:
#   gpt-5.1 / 5.2 / 5.4 / 5.4-mini / 5.4-nano / 5.5   200
#   gpt-5.6-luna / gpt-5.6-terra / gpt-5.6-sol        400, and 200 with "none"
#   o1 / o3-mini / o4-mini                            200, and 400 with "none"
#                                                     ("does not support 'none'")
#   gpt-4o                                            200, and 400 with "none"
#                                                     ("Unrecognized request argument")
# So the value can be neither omitted for everyone nor sent to every reasoning
# model: it belongs to the family that demands it, and to no other.
_TOOLS_NEED_EFFORT_NONE_SEED = ("gpt-5.6",)

# A family released after this table was written would refuse the same way and
# is recorded here for the rest of the process, at the price of one round trip
# once. Same shape as the `allowed_tools` refusal further down: no registry can
# know a per-MODEL property in advance, so the first refusal teaches it.
_tools_need_effort_none_learned: set = set()


def openai_is_reasoning_model(model: str) -> bool:
    """True for OpenAI reasoning models (o1/o3/o4 series, gpt-5 family), which reject
    `max_tokens` and a non-default `temperature` and want `max_completion_tokens`.

    Matches the o-series only at the start of the bare model name (after any
    `provider/` prefix) so `gpt-4o` / `gpt-4o-mini` are NOT misdetected.
    """
    m = (model or "").lower()
    if "gpt-5" in m:
        return True
    name = m.rsplit("/", 1)[-1]  # strip openrouter-style "openai/" prefix
    return name.startswith(("o1", "o3", "o4"))


def openai_tools_need_effort_none(model: str) -> bool:
    """True when this model only accepts function tools with `reasoning_effort="none"`."""
    m = (model or "").lower()
    return any(fam in m for fam in _TOOLS_NEED_EFFORT_NONE_SEED) or m in _tools_need_effort_none_learned


def note_openai_tools_effort_refusal(model: str, error_text: str) -> bool:
    """Record a "function tools need reasoning_effort=none" refusal for this model.

    Returns True only when the model did NOT already carry the rule, which is what
    makes a caller's retry provably terminate: the second refusal for the same model
    is a different problem and must be reported, not retried.

    Matches the refusal narrowly on BOTH halves of the message. The o-series answers
    "Unsupported value: 'reasoning_effort' does not support 'none'" - the same
    parameter, the opposite meaning - and carries no "function tools", so it can
    never be mistaken for this one.
    """
    err = (error_text or "").lower()
    if "reasoning_effort" not in err or "function tools" not in err:
        return False
    m = (model or "").lower()
    if not m or openai_tools_need_effort_none(m):
        return False
    _tools_need_effort_none_learned.add(m)
    return True


def openai_request_params(provider_name: str, model: str, *, temperature: float,
                          max_tokens: int, has_tools: bool) -> Dict[str, Any]:
    """The per-model parameters an OpenAI-compatible chat/completions body needs.

    Returns the token-limit key, the sampling params where they are accepted, and
    the tool-related switches. The caller adds model/messages/stream itself.

    Reasoning-param gating applies only to the DIRECT OpenAI API. OpenRouter (same
    wire format, different base_url) normalizes around `max_tokens` for every model,
    so sending `max_completion_tokens` there can lose the token limit; DeepSeek,
    Veyllo and local never match the OpenAI ids anyway.
    """
    direct_openai = (provider_name or "").lower() == "openai"
    # A model that demands reasoning_effort IS a reasoning model, so a refusal
    # learned at runtime also settles the token-limit key. Without that coupling a
    # learned family would be sent `reasoning_effort` and `max_tokens` in the same
    # body, and every reasoning model measured so far rejects the second one - the
    # run would die on the parameter it was just taught to avoid dying on.
    reasoning = direct_openai and (openai_is_reasoning_model(model)
                                   or openai_tools_need_effort_none(model))
    params: Dict[str, Any] = {}
    if reasoning:
        # o-series / gpt-5: `max_tokens` and a non-default `temperature` are both a 400.
        params["max_completion_tokens"] = max_tokens
    else:
        params["max_tokens"] = max_tokens
        params["temperature"] = temperature
    if has_tools:
        if direct_openai and openai_tools_need_effort_none(model):
            params["reasoning_effort"] = "none"
        if not reasoning:
            # parallel_tool_calls isn't accepted by all reasoning models; the
            # server-side default already allows parallel calls, so just omit it.
            params["parallel_tool_calls"] = True
    return params


# ═══════════════════════════════════════════════════════════════════════════════
# RATE-LIMIT WAIT PARSING (shared by the SDK lane and the coder's raw HTTP lane)
# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI's limits are per organization and per model, measured per minute (RPM
# and TPM; this account: 500 requests, 200k tokens for the gpt-5.6 family). A
# 429 names its own remedy, but in THREE different places and formats, and the
# old header-only integer parse missed two of them on the live incident:
#
#   Retry-After header        "30" - and the docs allow it to be absent
#   x-ratelimit-reset-*       duration strings: "120ms", "0s", "6m0s" (measured)
#   the error message itself  "... Please try again in 186ms."
#
# The incident this exists for: a TPM 429 whose message said "try again in
# 186ms" surfaced to the user as a lost turn, because there was no header, the
# backoff waited 1s+2s and the attempt budget (2) ran out while the window was
# still saturated. 186ms of patience would have saved the turn.

_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?!s))?\s*(?:(\d+(?:\.\d+)?)\s*s)?\s*(?:(\d+(?:\.\d+)?)\s*ms)?\s*$")
_TRY_AGAIN_RE = re.compile(r"try again in\s+([0-9hms\. ]+?)[\.\s]*(?:$|Visit)", re.IGNORECASE)


def _parse_duration_seconds(raw: str) -> Optional[float]:
    """'186ms' / '6.13s' / '6m0s' / '1h2m' / bare '30' -> seconds, else None."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:  # bare number = seconds (the Retry-After header form, fractions included)
        return float(s)
    except ValueError:
        pass
    m = _DURATION_RE.fullmatch(s)
    if not m or not any(m.groups()):
        return None
    h, mins, secs, ms = m.groups()
    return (float(h or 0) * 3600 + float(mins or 0) * 60
            + float(secs or 0) + float(ms or 0) / 1000.0)


def rate_limit_wait_seconds(headers, message_text: str = "") -> Optional[float]:
    """The wait a 429 asked for, from whichever source carried it, UNCAPPED.

    Sources in order of authority: the Retry-After header, then the "try again
    in <duration>" phrase in the error body. Callers apply their own cap
    (api_retry_after_max) - the cap is policy, the parse is fact, and mixing
    them made the old parser return None for a capped-out value instead of the
    cap. Returns None when neither source yields a number, so the caller falls
    back to exponential backoff.
    """
    try:
        raw = None
        if headers:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is not None:
            got = _parse_duration_seconds(raw)
            if got is not None:
                return max(0.0, got)
        m = _TRY_AGAIN_RE.search(str(message_text or ""))
        if m:
            got = _parse_duration_seconds(m.group(1).strip())
            if got is not None:
                return max(0.0, got)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ABSTRACT BASE PROVIDER
# ════───────────────────────────────────────────────────────────────────────────

class BaseAIProvider(ABC):
    """Abstract base class for all AI providers."""

    # What one reply may cost when the configured figure was refused. This is the
    # value the product shipped with for a long time, so it is known to be
    # accepted everywhere rather than merely guessed to be small enough.
    SAFE_RESPONSE_TOKENS = 8192

    # Parameter names and complaints, kept as two small sets rather than one
    # regex per provider. Every provider spells the cap differently and phrases
    # the refusal differently, but a refusal names the parameter AND says the
    # figure was too big; requiring both is what keeps an unrelated 400 out. The
    # one exception is a window complaint, handled separately below.
    _CAP_PARAMS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
    _CAP_COMPLAINTS = ("too large", "maximum", "at most", "exceed", "context length")

    def __init__(self, provider_name: str, api_key: str):
        self.provider_name = provider_name
        self.api_key = api_key
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = blank_request_usage()
        # Set once a provider has refused the configured cap, so the lost round
        # trip is paid once per process instead of on every later call.
        self.output_cap_refused = False

    def _capped_output(self, max_tokens: int) -> int:
        """The figure to actually send, after any earlier refusal."""
        try:
            if self.output_cap_refused and int(max_tokens) > self.SAFE_RESPONSE_TOKENS:
                return self.SAFE_RESPONSE_TOKENS
            return int(max_tokens)
        except Exception:
            return self.SAFE_RESPONSE_TOKENS

    def _refused_output_cap(self, err_str: str, max_tokens) -> bool:
        """True when this error says the requested output cap was too big.

        Guarded on the figure being above the safe one, so the retry below cannot
        recurse: the second attempt sends a value this test can never match.
        """
        try:
            if int(max_tokens) <= self.SAFE_RESPONSE_TOKENS:
                return False
        except Exception:
            return False
        low = (err_str or "").lower()
        # A window complaint names the WINDOW, not the parameter ("maximum context
        # length is 128000 ... however you requested more"), and a smaller cap is
        # the right remedy there too: it gives the prompt back the room the reply
        # had reserved. If the prompt alone does not fit, the retry fails again
        # and the guard above stops it, so the cost is one round trip, not a loop.
        if any(k in low for k in ("context length", "context window")):
            return True
        return (any(p in low for p in self._CAP_PARAMS)
                and any(c in low for c in self._CAP_COMPLAINTS))

    @abstractmethod
    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = True,
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Union[str, Dict]] = None,  # 'auto', 'none', 'required', or specific function
    ) -> Generator[str, None, None]:
        """Execute a chat completion request."""
        pass

    # ── Shared transient-error retry (429/5xx/timeout), inherited by every provider ─────────────────
    # Retries ONLY request INITIATION (before any token streams), so output is never duplicated.
    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        """True for transient errors worth retrying: HTTP 429 (rate limit), 5xx, timeouts, connection drops."""
        code = getattr(e, "status_code", None) or getattr(e, "status", None) or getattr(e, "code", None)
        if isinstance(code, int) and (code == 429 or 500 <= code < 600):
            return True
        for mod_name in ("openai", "anthropic"):
            try:
                mod = __import__(mod_name)
                types = tuple(
                    getattr(mod, n) for n in
                    ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")
                    if hasattr(mod, n)
                )
                if types and isinstance(e, types):
                    return True
            except Exception:
                pass
        try:  # google-genai: ServerError (5xx) is transient; a 429 ClientError is caught by the code check above
            from google.genai import errors as _g_errors
            if isinstance(e, getattr(_g_errors, "ServerError", ())):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_rate_limit(e: Exception) -> bool:
        """True for a 429 specifically - the retryable error with its own time budget."""
        code = getattr(e, "status_code", None) or getattr(e, "status", None) or getattr(e, "code", None)
        if code == 429:
            return True
        try:
            import openai as _oa
            if isinstance(e, getattr(_oa, "RateLimitError", ())):
                return True
        except Exception:
            pass
        return "rate_limit_exceeded" in str(e) or "rate limit" in str(e).lower()

    @staticmethod
    def _retry_after_seconds(e: Exception) -> Optional[float]:
        """The wait this error asked for - Retry-After header or the "try again in
        <duration>" phrase in its message - capped by api_retry_after_max. None when
        neither source yields a number, so the caller uses exponential backoff."""
        try:
            headers = getattr(getattr(e, "response", None), "headers", None)
            secs = rate_limit_wait_seconds(headers, str(e))
            if secs is None:
                return None
            cap = float(Config.get("api_retry_after_max", 30) or 30)
            return min(secs, cap)
        except Exception:
            return None

    def _with_retry(self, make_request):
        """Run make_request() with a bounded retry on transient errors (429/5xx/timeout). Wraps ONLY
        request initiation (before any token), so it can never duplicate streamed output. Sits on top
        of each SDK's own retries.

        Two budgets, because the two failure kinds recover differently. 5xx/timeouts get
        `api_retry_attempts` COUNTED retries with backoff - a broken server is not made whole by
        patience. A 429 gets a WALL-CLOCK budget (`api_rate_limit_wait_max`, default 60s): the
        provider names the wait itself ("Please try again in 186ms", a Retry-After header), the
        window drains on its own schedule, and counting attempts against it is how a live turn died
        after 3s of patience against a request that asked for 186ms. Per the provider's own
        guidance the named wait is a minimum, so a small random jitter is added on top - concurrent
        lanes (chat plus coder on one org) must not all retry in the same instant."""
        import random as _random
        import time as _time
        max_retries = max(0, int(Config.get("api_retry_attempts", 2) or 0))
        rate_budget = max(0.0, float(Config.get("api_rate_limit_wait_max", 60) or 0))
        attempt = 0
        rate_waited = 0.0
        while True:
            try:
                return make_request()
            except Exception as e:
                if not self._is_retryable_error(e):
                    raise
                if self._is_rate_limit(e):
                    wait = self._retry_after_seconds(e)
                    if wait is None:
                        wait = min(2 ** max(0, min(attempt, 4)), 8)  # 1,2,4,8,8...
                    wait += _random.uniform(0, min(1.0, 0.25 * wait or 0.05))
                    if rate_waited + wait > rate_budget:
                        raise
                    rate_waited += wait
                    attempt += 1  # counted for the backoff curve, not against the budget
                    _time.sleep(wait)
                    continue
                if attempt >= max_retries:
                    raise
                attempt += 1
                wait = self._retry_after_seconds(e)
                if wait is None:
                    wait = min(2 ** (attempt - 1), 4)  # ~1s, 2s, capped 4s
                _time.sleep(wait)

# ═══════════════════════════════════════════════════════════════════════════════
# OPENAI PROVIDER (also used for DeepSeek & OpenRouter)
# ═══════════════════════════════════════════════════════════════════════════════

class OpenAIProvider(BaseAIProvider):
    """Provider for OpenAI-compatible APIs."""
    
    def __init__(self, provider_name: str, api_key: str, base_url: Optional[str] = None):
        super().__init__(provider_name, api_key)
        try:
            from openai import OpenAI
            # Explicit timeouts: bound connect/write so a large image upload can't hang,
            # but keep `read` generous — reasoning models (o-series / gpt-5) stream for
            # minutes and a short read timeout would cut them off. All overridable via config.
            try:
                import httpx
                _timeout = httpx.Timeout(
                    connect=float(Config.get("api_timeout_connect", 20.0) or 20.0),
                    write=float(Config.get("api_timeout_write", 120.0) or 120.0),
                    read=float(Config.get("api_timeout_read", 600.0) or 600.0),
                    pool=float(Config.get("api_timeout_pool", 20.0) or 20.0),
                )
                self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=_timeout)
            except Exception:
                self.client = OpenAI(api_key=api_key, base_url=base_url)
        except ImportError:
            self.client = None
            logger.error("OpenAI SDK not installed. Please run: pip install openai")

    # Thin alias for the module-level predicate, which is the shared one (the coder's
    # raw-HTTP lane reads the same rules). Kept as a name on the class because that is
    # what the compatibility tests pin.
    _is_reasoning_model = staticmethod(openai_is_reasoning_model)

    def _create_with_retry(self, kwargs: Dict):
        """chat.completions.create with a bounded retry on transient errors (429/5xx/timeout). Initiation-only
        (before any token streams), so it can never duplicate output. Uses the shared BaseAIProvider retry."""
        return self._with_retry(lambda: self.client.chat.completions.create(**kwargs))

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.client:
            yield "[Error] OpenAI SDK missing."
            return

        try:
            # Per-model parameter shape (token-limit key, sampling params,
            # reasoning_effort, parallel_tool_calls). One implementation, shared with
            # the coder's raw-HTTP lane, which builds its own body.
            max_tokens = self._capped_output(max_tokens)
            kwargs = {
                "model": model,
                "messages": messages,
                "stream": stream,
            }
            kwargs.update(openai_request_params(
                self.provider_name, model, temperature=temperature,
                max_tokens=max_tokens, has_tools=bool(tools),
            ))
            if tools:
                kwargs["tools"] = tools
                # tool_choice: 'auto' (default), 'none', 'required', or specific function
                if tool_choice:
                    kwargs["tool_choice"] = tool_choice
            
            if stream:
                # Enable usage for streaming (OpenAI specific)
                kwargs["stream_options"] = {"include_usage": True}
                
                # DeepSeek Reasoner & R1: output primarily in reasoning_content; must yield both
                is_reasoning = False
                response = self._create_with_retry(kwargs)
                for chunk in response:
                    if len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        reasoning_chunk = getattr(delta, "reasoning_content", None) or ""
                        content_chunk = delta.content or ""
                        
                        # Method 1: reasoning_content (DeepSeek Reasoner/R1, extended thinking models)
                        if reasoning_chunk:
                            if not is_reasoning:
                                is_reasoning = True
                                yield "<think>"
                            yield reasoning_chunk
                        
                        # Method 2: content (standard answer field)
                        if content_chunk:
                            if is_reasoning:
                                is_reasoning = False
                                yield "</think>\n\n"
                            yield content_chunk
                        
                        # Handle tool calls
                        if delta.tool_calls:
                            yield json.dumps({"tool_calls": [tc.model_dump() for tc in delta.tool_calls]})
                        
                        # Handle finish reason
                        if chunk.choices[0].finish_reason:
                            self.last_request_usage["finish_reason"] = chunk.choices[0].finish_reason
                            yield json.dumps({"finish_reason": chunk.choices[0].finish_reason})
                    
                    # Handle usage metadata (sent in last chunk)
                    if hasattr(chunk, 'usage') and chunk.usage:
                        self.usage["input_tokens"] += chunk.usage.prompt_tokens
                        self.usage["output_tokens"] += chunk.usage.completion_tokens
                        self.last_request_usage["input_tokens"] = chunk.usage.prompt_tokens
                        self.last_request_usage["output_tokens"] = chunk.usage.completion_tokens
                        self.last_request_usage.update(cache_usage_from_openai(chunk.usage))
                
                if is_reasoning:
                    yield "</think>"
            else:
                response = self._create_with_retry(kwargs)
                msg = response.choices[0].message
                reasoning = getattr(msg, "reasoning_content", None) or ""
                content = msg.content or ""
                
                # DeepSeek Reasoner: answer often in reasoning_content only
                if reasoning:
                    yield "<think>" + reasoning + "</think>\n\n"
                if content:
                    yield content
                
                # Handle tool calls (Reasoner has none)
                tc = getattr(msg, "tool_calls", None)
                if tc:
                    yield json.dumps({"tool_calls": [t.model_dump() for t in tc]})
                
                # Captured, deliberately NOT yielded. The stream lane sends this
                # down the channel, but agent.py's stream=False fallback joins
                # every chunk into the visible reply, so a JSON blob here would
                # be shown to the user as text.
                self.last_request_usage["finish_reason"] = getattr(
                    response.choices[0], "finish_reason", None)

                if response.usage:
                    self.usage["input_tokens"] += response.usage.prompt_tokens
                    self.usage["output_tokens"] += response.usage.completion_tokens
                    self.last_request_usage["input_tokens"] = response.usage.prompt_tokens
                    self.last_request_usage["output_tokens"] = response.usage.completion_tokens
                    self.last_request_usage.update(cache_usage_from_openai(response.usage))
                    
        except Exception as e:
            err_str = str(e)
            # A provider that does not know `allowed_tools` answers 400 and the
            # turn is lost, not merely expensive. The capability is declared per
            # provider and probed, but it can also be per MODEL, which no registry
            # can know in advance. So the first refusal retires the shape for this
            # process and the request is retried plainly: one lost round trip
            # instead of every later one.
            if isinstance(tool_choice, dict) and tool_choice.get("type") == "allowed_tools" \
                    and "tool_choice" in err_str.lower():
                self.allowed_tools_refused = True
                UI.event("Backend", "provider refused allowed_tools; retrying without it",
                         style="warning")
                yield from self.chat_completion(messages, temperature, max_tokens, stream,
                                                model, tools, "auto")
                return
            # Same shape, one level down: a MODEL that refuses function tools unless
            # reasoning_effort is "none". The seed table knows the families measured
            # when it was written; a later one teaches itself here, once per process.
            # The record is module-level rather than on the instance (unlike the cap
            # above) because this is a property of the MODEL, not of the connection,
            # and it has to reach every provider instance in the process.
            if tools and self.provider_name == "openai" \
                    and note_openai_tools_effort_refusal(model, err_str):
                UI.event("Backend", f"{model} refuses tools with reasoning; "
                                    "retrying with reasoning_effort=none", style="warning")
                yield from self.chat_completion(messages, temperature, max_tokens, stream,
                                                model, tools, tool_choice)
                return
            if self._refused_output_cap(err_str, max_tokens):
                self.output_cap_refused = True
                UI.event("Backend", f"{self.provider_name} refused the reply-length cap; "
                                    f"retrying at {self.SAFE_RESPONSE_TOKENS}", style="warning")
                yield from self.chat_completion(messages, temperature,
                                                self.SAFE_RESPONSE_TOKENS, stream,
                                                model, tools, tool_choice)
                return
            UI.error(f"{self.provider_name.upper()} Provider Error: {err_str}")
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"{self.provider_name}_api_error: {err_str}")
            except Exception:
                pass
            yield f"[API Error from {self.provider_name}: {err_str}]"

# ═══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC PROVIDER
# ═══════════════════════════════════════════════════════════════════════════════

class AnthropicProvider(BaseAIProvider):
    """Provider for Anthropic Claude models (native Messages API)."""

    # Models that support adaptive thinking (substring match on the lower-cased id).
    # Excludes Haiku 4.5 (no adaptive thinking) and legacy claude-3.x.
    _THINKING_MODELS = ("sonnet-4-6", "opus-4-6", "opus-4-7", "opus-4-8", "fable", "mythos")
    # Models that reject sampling params (temperature/top_p/top_k) — 400 if sent.
    _NO_SAMPLING_MODELS = ("opus-4-7", "opus-4-8", "fable", "mythos")

    def __init__(self, api_key: str):
        super().__init__("anthropic", api_key)
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=api_key)
        except ImportError:
            self.client = None
            logger.error("Anthropic SDK not installed. Please run: pip install anthropic")

    @staticmethod
    def _convert_content(content) -> Any:
        """Convert OpenAI multimodal content list to Anthropic format.

        OpenAI image block: {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
        Anthropic image block: {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}}
        """
        if isinstance(content, str):
            return content
        result = []
        for block in content:
            if block.get("type") == "text":
                result.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image_url":
                url = block["image_url"]["url"]
                if url.startswith("data:"):
                    header, b64_data = url.split(",", 1)
                    mime_type = header.split(":")[1].split(";")[0]
                    result.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime_type, "data": b64_data},
                    })
        return result

    @classmethod
    def _supports_thinking(cls, model: str) -> bool:
        m = (model or "").lower()
        return any(p in m for p in cls._THINKING_MODELS)

    @classmethod
    def _rejects_sampling(cls, model: str) -> bool:
        m = (model or "").lower()
        return any(p in m for p in cls._NO_SAMPLING_MODELS)

    def _convert_messages_to_anthropic(self, messages: List[Dict]) -> List[Dict]:
        """Convert VAF's OpenAI-format history (already system-stripped) to native
        Anthropic message blocks.

        - assistant + tool_calls  -> content list: optional text block + tool_use blocks
          (arguments JSON-parsed; defensive fallback {}).
        - assistant + _anthropic_blocks -> replayed VERBATIM (preserves thinking blocks +
          signatures so a thinking-enabled tool loop doesn't 400 on the next turn).
        - role:"tool"             -> user turn with a tool_result block; consecutive results
          are merged into ONE user message (Anthropic parallel-tool pattern).
        - plain user/assistant    -> _convert_content (keeps image conversion).
        Empty plain-assistant turns are dropped (Anthropic rejects empty content).
        """
        out: List[Dict] = []
        for m in messages:
            role = m.get("role")

            if role == "assistant":
                raw_blocks = m.get("_anthropic_blocks")
                if raw_blocks:
                    out.append({"role": "assistant", "content": raw_blocks})
                    continue

                tool_calls = m.get("tool_calls")
                if tool_calls:
                    blocks: List[Dict] = []
                    text = m.get("content")
                    if isinstance(text, str) and text.strip():
                        blocks.append({"type": "text", "text": text})
                    for tc in tool_calls:
                        fn = tc.get("function", {}) or {}
                        args = fn.get("arguments", "{}")
                        try:
                            parsed = json.loads(args) if isinstance(args, str) else (args or {})
                        except Exception:
                            parsed = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_{os.urandom(4).hex()}",
                            "name": fn.get("name", ""),
                            "input": parsed,
                        })
                    out.append({"role": "assistant", "content": blocks})
                else:
                    converted = self._convert_content(m.get("content", ""))
                    # Drop empty assistant turns — Anthropic rejects empty content.
                    if isinstance(converted, str) and not converted.strip():
                        continue
                    if isinstance(converted, list) and not converted:
                        continue
                    out.append({"role": "assistant", "content": converted})

            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": str(m.get("content", "")),
                }
                prev = out[-1] if out else None
                if (
                    prev and prev.get("role") == "user"
                    and isinstance(prev.get("content"), list)
                    and prev["content"]
                    and isinstance(prev["content"][0], dict)
                    and prev["content"][0].get("type") == "tool_result"
                ):
                    prev["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})

            else:  # user (and any unexpected role) -> user text/multimodal
                out.append({"role": "user", "content": self._convert_content(m.get("content", ""))})

        return out

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.client:
            yield "[Error] Anthropic SDK missing."
            return

        max_tokens = self._capped_output(max_tokens)

        # 1. Consolidate system messages: leading system turns -> one top-level system;
        #    mid-run system nudges -> in-place user turns (reuses the shared helper).
        consolidated = consolidate_system_messages(messages)
        system_msg = ""
        rest: List[Dict] = []
        for m in consolidated:
            if m.get("role") == "system":
                c = m.get("content")
                system_msg = c if isinstance(c, str) else ""
            else:
                rest.append(m)

        # 2. Convert remaining messages (tool_calls/role:tool -> tool_use/tool_result).
        anthropic_messages = self._convert_messages_to_anthropic(rest)

        try:
            kwargs = {
                "model": model,
                "messages": anthropic_messages,
                "max_tokens": max_tokens,
            }

            # 3. System prompt + optional prompt caching (auto-caches the stable prefix).
            if system_msg:
                use_cache = Config.get_bool("anthropic_prompt_cache", True)
                if use_cache:
                    kwargs["system"] = [{
                        "type": "text", "text": system_msg,
                        "cache_control": {"type": "ephemeral"},
                    }]
                else:
                    kwargs["system"] = system_msg

            # 4. Adaptive thinking (config-gated, supported models only).
            thinking_on = Config.get_bool("anthropic_thinking", True)
            thinking_active = thinking_on and self._supports_thinking(model)
            if thinking_active:
                kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

            # 5. Sampling: omit temperature when thinking is on (requires temp=1) or the
            #    model rejects sampling params (Opus 4.7/4.8, Fable/Mythos -> 400).
            if not thinking_active and not self._rejects_sampling(model):
                kwargs["temperature"] = temperature

            # 6. Tools (OpenAI -> Anthropic schema).
            if tools:
                anthropic_tools = []
                for t in tools:
                    if t.get("type") == "function":
                        func = t["function"]
                        anthropic_tools.append({
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                        })
                if anthropic_tools:
                    kwargs["tools"] = anthropic_tools
                    if tool_choice in ("required", "any"):
                        kwargs["tool_choice"] = {"type": "any"}
                    elif tool_choice == "none":
                        kwargs["tool_choice"] = {"type": "none"}
                    elif isinstance(tool_choice, dict):
                        fn = tool_choice.get("function", {})
                        if fn.get("name"):
                            kwargs["tool_choice"] = {"type": "tool", "name": fn["name"]}

            if stream:
                in_think = False
                _stream_cm = self._with_retry(lambda: self.client.messages.stream(**kwargs))
                with _stream_cm as response:
                    for event in response:
                        if event.type != "content_block_delta":
                            continue
                        delta = event.delta
                        dtype = getattr(delta, "type", None)
                        if dtype == "thinking_delta":
                            if not in_think:
                                in_think = True
                                yield "<think>"
                            yield delta.thinking
                        elif dtype == "text_delta":
                            if in_think:
                                in_think = False
                                yield "</think>\n\n"
                            yield delta.text
                    if in_think:
                        yield "</think>"

                    final_msg = response.get_final_message()
                    yield from self._emit_final(final_msg, thinking_active)
            else:
                response = self._with_retry(lambda: self.client.messages.create(**kwargs))
                for content_block in response.content:
                    if content_block.type == "thinking":
                        yield "<think>" + getattr(content_block, "thinking", "") + "</think>\n\n"
                    elif content_block.type == "text":
                        yield content_block.text
                yield from self._emit_final(response, thinking_active)

        except Exception as e:
            err_str = str(e)
            if self._refused_output_cap(err_str, max_tokens):
                self.output_cap_refused = True
                UI.event("Backend", f"anthropic refused the reply-length cap; "
                                    f"retrying at {self.SAFE_RESPONSE_TOKENS}", style="warning")
                yield from self.chat_completion(messages, temperature,
                                                self.SAFE_RESPONSE_TOKENS, stream,
                                                model, tools, tool_choice)
                return
            UI.error(f"Anthropic Provider Error: {err_str}")
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"anthropic_api_error: {err_str}")
            except Exception:
                pass
            yield f"[API Error from anthropic: {err_str}]"

    def _emit_final(self, final_msg, thinking_active: bool) -> Generator[str, None, None]:
        """Shared finalize step for streaming and non-streaming: usage, stop_reason,
        tool_use payloads, and raw-block side-channel for thinking-loop replay."""
        # Usage
        try:
            self.usage["input_tokens"] += final_msg.usage.input_tokens
            self.usage["output_tokens"] += final_msg.usage.output_tokens
            self.last_request_usage["input_tokens"] = final_msg.usage.input_tokens
            self.last_request_usage["output_tokens"] = final_msg.usage.output_tokens
            # in_input=False: Anthropic's `input_tokens` EXCLUDES both cache
            # figures, while every OpenAI-shaped provider includes them. Getting
            # that backwards does not misdraw a chart, it produces a wrong bill.
            # The per-TTL split under `usage.cache_creation` is deliberately not
            # read: this adapter sends `{"type": "ephemeral"}` with no `ttl`, so
            # every write it can produce is a five-minute one and a single price
            # multiplier is exact. Read it the day a `ttl` is sent.
            self.last_request_usage.update(cache_usage(
                getattr(final_msg.usage, "cache_read_input_tokens", None),
                getattr(final_msg.usage, "cache_creation_input_tokens", None),
                in_input=False))
        except Exception:
            pass

        stop_reason = getattr(final_msg, "stop_reason", None)
        self.last_request_usage["finish_reason"] = stop_reason
        if stop_reason == "refusal":
            details = getattr(final_msg, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            yield (
                "[Anthropic declined this request for safety reasons"
                + (f" (category: {category})" if category else "")
                + ".]"
            )
            return

        # Tool use: emit each call (drives VAF's tool execution) and, when a thinking
        # block is present, the raw assistant blocks so the next turn can replay them
        # verbatim (else Anthropic 400s "thinking blocks must be preserved").
        content_blocks = getattr(final_msg, "content", []) or []
        has_tool_use = any(getattr(b, "type", None) == "tool_use" for b in content_blocks)
        has_thinking = any(getattr(b, "type", None) == "thinking" for b in content_blocks)
        for b in content_blocks:
            if getattr(b, "type", None) == "tool_use":
                yield json.dumps({"tool_use": b.model_dump()})
        if has_tool_use and thinking_active and has_thinking:
            try:
                raw = [b.model_dump() for b in content_blocks]
                yield json.dumps({"_anthropic_blocks": raw})
            except Exception:
                pass

        if stop_reason == "pause_turn":
            # Server-tool pause: VAF declares no server tools here, so this is rare.
            # Surface a hint rather than silently ending.
            yield json.dumps({"finish_reason": "pause_turn"})

# ═══════════════════════════════════════════════════════════════════════════════
# GOOGLE GEMINI PROVIDER
# ═══════════════════════════════════════════════════════════════════════════════

class GoogleProvider(BaseAIProvider):
    """Provider for Google Gemini models (native google-genai SDK)."""

    # Models with built-in thinking (surfaced as thought parts). Gemini 2.0 and
    # earlier have no thinking.
    _THINKING_MODELS = ("gemini-2.5", "gemini-3")

    def __init__(self, api_key: str):
        super().__init__("google", api_key)
        try:
            from google import genai
            self.genai = genai
            self.client = genai.Client(api_key=api_key)
        except ImportError:
            self.client = None
            logger.error("google-genai SDK missing. Please run: pip install google-genai")

    @classmethod
    def _supports_thinking(cls, model: str) -> bool:
        m = (model or "").lower()
        return any(p in m for p in cls._THINKING_MODELS)

    @staticmethod
    def _build_contents(messages, types, b64):
        """Convert VAF's OpenAI-format history (system already stripped) to Gemini
        `Content` objects, including the tool roundtrip:
        - assistant + tool_calls -> role 'model' with function_call parts (+ text)
        - role:'tool'            -> role 'user' with a function_response part
        - user/assistant text    -> text / image parts
        Empty turns are skipped (Gemini rejects empty parts).
        """
        contents = []
        for m in messages:
            role = m.get("role")

            if role == "tool":
                name = m.get("name") or "tool"
                result = str(m.get("content", ""))
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_function_response(name=name, response={"result": result})],
                ))
                continue

            if role == "assistant":
                parts = []
                text = m.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(types.Part.from_text(text=text))
                elif isinstance(text, list):
                    for b in text:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                            parts.append(types.Part.from_text(text=b["text"]))
                for tc in (m.get("tool_calls") or []):
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments", "{}")
                    try:
                        parsed = json.loads(args) if isinstance(args, str) else (args or {})
                    except Exception:
                        parsed = {}
                    parts.append(types.Part.from_function_call(name=fn.get("name", ""), args=parsed))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            # user (and any unexpected role)
            content = m.get("content")
            parts = []
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(types.Part.from_text(text=block["text"]))
                    elif block.get("type") == "image_url":
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            header, data = url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            parts.append(types.Part.from_bytes(data=b64.b64decode(data), mime_type=mime))
            elif content:
                parts.append(types.Part.from_text(text=str(content)))
            if parts:
                contents.append(types.Content(role="user", parts=parts))
        return contents

    @staticmethod
    def _iter_parts(resp):
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return []
        content = getattr(cands[0], "content", None)
        if not content:
            return []
        return getattr(content, "parts", None) or []

    def _record_usage(self, resp):
        try:
            cand = (getattr(resp, "candidates", None) or [None])[0]
            self.last_request_usage["finish_reason"] = getattr(
                cand, "finish_reason", None)
        except Exception:
            pass
        um = getattr(resp, "usage_metadata", None)
        if not um:
            return
        inp = getattr(um, "prompt_token_count", 0) or 0
        out = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        self.usage["input_tokens"] += inp
        self.usage["output_tokens"] += out
        self.last_request_usage["input_tokens"] = inp
        self.last_request_usage["output_tokens"] = out
        # `prompt_token_count` already includes the cached part, and Gemini
        # charges no cache-write premium, so a zero write here is the truth
        # rather than a gap.
        self.last_request_usage.update(cache_usage(
            getattr(um, "cached_content_token_count", None), None, in_input=True))

    def _tool_call_payload(self, fc):
        return json.dumps({"tool_calls": [{
            "index": 0,
            "id": getattr(fc, "id", None) or f"call_{fc.name}",
            "type": "function",
            "function": {"name": fc.name, "arguments": json.dumps(dict(fc.args or {}))},
        }]})

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.client:
            yield "[Error] google-genai SDK missing."
            return

        max_tokens = self._capped_output(max_tokens)

        from google.genai import types
        import base64 as _b64

        # 1. Consolidate system messages (leading -> system_instruction; mid-run -> user turns).
        consolidated = consolidate_system_messages(messages)
        system_instruction = None
        rest: List[Dict] = []
        for m in consolidated:
            if m.get("role") == "system":
                c = m.get("content")
                if isinstance(c, str):
                    system_instruction = c
            else:
                rest.append(m)

        # 2. Build contents (incl. tool roundtrip).
        contents = self._build_contents(rest, types, _b64)

        # 3. Tools (OpenAI -> Gemini function declarations; raw JSON schema).
        gtools = None
        if tools:
            decls = []
            for t in tools:
                if t.get("type") == "function":
                    func = t["function"]
                    decls.append(types.FunctionDeclaration(
                        name=func["name"],
                        description=func.get("description", ""),
                        parameters_json_schema=func.get("parameters", {"type": "object", "properties": {}}),
                    ))
            if decls:
                gtools = [types.Tool(function_declarations=decls)]

        # 4. tool_choice -> FunctionCallingConfig (AUTO is the default, so only set non-auto).
        tool_config = None
        if gtools and tool_choice and tool_choice != "auto":
            mode, allowed = None, None
            if tool_choice in ("required", "any"):
                mode = "ANY"
            elif tool_choice == "none":
                mode = "NONE"
            elif isinstance(tool_choice, dict):
                fn = tool_choice.get("function", {})
                if fn.get("name"):
                    mode, allowed = "ANY", [fn["name"]]
            if mode:
                tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode=mode, allowed_function_names=allowed))

        # 5. Thinking (config-gated, supported models only) — surface thought summaries.
        thinking_on = Config.get_bool("google_thinking", True)
        thinking_config = None
        if thinking_on and self._supports_thinking(model):
            thinking_config = types.ThinkingConfig(include_thoughts=True)

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_instruction or None,
            tools=gtools,
            tool_config=tool_config,
            thinking_config=thinking_config,
        )

        try:
            if stream:
                in_think = False
                last = None
                for chunk in self._with_retry(lambda: self.client.models.generate_content_stream(
                    model=model, contents=contents, config=config
                )):
                    last = chunk
                    for part in self._iter_parts(chunk):
                        if getattr(part, "thought", False) and getattr(part, "text", None):
                            if not in_think:
                                in_think = True
                                yield "<think>"
                            yield part.text
                        elif getattr(part, "function_call", None):
                            if in_think:
                                in_think = False
                                yield "</think>\n\n"
                            yield self._tool_call_payload(part.function_call)
                        elif getattr(part, "text", None):
                            if in_think:
                                in_think = False
                                yield "</think>\n\n"
                            yield part.text
                if in_think:
                    yield "</think>"
                self._record_usage(last)
            else:
                resp = self._with_retry(lambda: self.client.models.generate_content(
                    model=model, contents=contents, config=config
                ))
                for part in self._iter_parts(resp):
                    if getattr(part, "thought", False) and getattr(part, "text", None):
                        yield "<think>" + part.text + "</think>\n\n"
                    elif getattr(part, "function_call", None):
                        yield self._tool_call_payload(part.function_call)
                    elif getattr(part, "text", None):
                        yield part.text
                self._record_usage(resp)

        except Exception as e:
            err_str = str(e)
            if self._refused_output_cap(err_str, max_tokens):
                self.output_cap_refused = True
                UI.event("Backend", f"google refused the reply-length cap; "
                                    f"retrying at {self.SAFE_RESPONSE_TOKENS}", style="warning")
                yield from self.chat_completion(messages, temperature,
                                                self.SAFE_RESPONSE_TOKENS, stream,
                                                model, tools, tool_choice)
                return
            UI.error(f"Google Provider Error: {err_str}")
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"google_api_error: {err_str}")
            except Exception:
                pass
            yield f"[API Error from google: {err_str}]"

# ═══════════════════════════════════════════════════════════════════════════════
# FACTORY & MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class APIBackendManager:
    """Refactored Manager using provider-specific classes."""
    
    def __init__(self, provider: str, *, config: Optional[dict] = None,
                 caller_config: Optional[dict] = None):
        # `config` is the merged config an embedder handed in; `caller_config` is their raw
        # override dict, which the shared resolver treats as the highest-precedence source.
        # It used to be a single pre-extracted api_key, which reached this constructor and
        # nothing else - the failover chain and model discovery below ask the resolver
        # themselves, so an embedder's chain could never find a key. Passing the dict is
        # what makes those work; with both None (product mode) nothing changes.
        from vaf.core.api_keys import resolve_api_key
        self.provider_name = provider
        self.caller_config = caller_config
        # Still needed, and NOT about the key: `_embedded` decides whether `api_model_*` is
        # read from the programmatic config or re-read from disk each turn. Dropping it while
        # reworking the key path would have quietly taken an embedder's model choice away.
        self._embedded = config is not None
        self.config = config if config is not None else Config.load()
        self.api_key = resolve_api_key(provider, caller_config)
        self.provider = self._create_provider()
        self.session_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = blank_request_usage()
        self._failover_pinned_idx = 0  # sticky link when failover_return_to_primary is off
        # provider name -> monotonic deadline until which that link is treated as dead
        # and skipped. Empty means every link is a candidate. See _skip_dead_links.
        self._failover_open_until = {}
        # Optional structured-event callback (set via Agent.set_event_sink):
        # emits llm_start/llm_end around every chat completion. None = off.
        self.event_sink = None

    def _emit_event(self, evt: dict) -> None:
        """Send one event to the sink; a raising sink never breaks the call."""
        sink = getattr(self, "event_sink", None)
        if callable(sink):
            try:
                sink(evt)
            except Exception:
                pass

    def _create_provider(self) -> BaseAIProvider:
        # Provider set, endpoints and key requirements come from the single
        # source of truth (vaf/core/provider_registry.py); this factory only
        # maps a spec to the right provider class. Error messages and their
        # order are pinned by tests/test_provider_factory_pinning.py.
        from vaf.core.provider_registry import (
            KIND_ANTHROPIC_SDK,
            KIND_GOOGLE_SDK,
            get_spec,
            resolve_sdk_base_url,
        )

        spec = get_spec(self.provider_name)

        if spec is not None and not spec.needs_api_key:
            # Local/Ollama lane: no key needed (the dummy bearer is ignored).
            # Base URL: explicit local_api_url wins, else VAF's own llama-server
            # (port 8080, Docker/env-aware) - never Ollama's 11434 default.
            base_url = resolve_sdk_base_url(spec.name, self.config)
            return OpenAIProvider(spec.name, spec.dummy_api_key or "", base_url=base_url)

        if not self.api_key:
            raise ValueError(f"API key missing for {self.provider_name}")

        if spec is None:
            raise ValueError(f"Unsupported provider: {self.provider_name}")

        if spec.kind == KIND_ANTHROPIC_SDK:
            return AnthropicProvider(self.api_key)
        if spec.kind == KIND_GOOGLE_SDK:
            return GoogleProvider(self.api_key)
        return OpenAIProvider(
            spec.name, self.api_key, base_url=resolve_sdk_base_url(spec.name, self.config)
        )

    # ══════════════════════════════════════════════════════════════════════════
    # FAILOVER  (Settings → Advanced → Failover)
    # Wraps the single-provider call with an optional provider chain: if the
    # primary is unreachable/errors *before the first token*, the request is
    # retried against a backup API and/or a local model. With failover off
    # (default) this layer is bypassed and behaviour is byte-identical to before.
    # ══════════════════════════════════════════════════════════════════════════

    # level → ordered fallback links appended after the primary
    _FAILOVER_LINKS = {
        "off": [],
        "basic": ["local"],
        "balanced": ["backup", "local"],
        "maximum": ["backup", "local"],
    }

    def _failover_cfg(self, key, default):
        """Read a failover setting, preferring this manager's own (embedded/agent) config,
        falling back to the global Config. Returns ``default`` when unset/None."""
        try:
            if isinstance(self.config, dict) and self.config.get(key) is not None:
                return self.config.get(key)
        except Exception:
            pass
        val = Config.get(key, default)
        return default if val is None else val

    def _build_failover_chain(self, model):
        """Ordered provider chain for this request as a list of (manager, model) tuples,
        primary first. Empty / duplicate / key-less links are dropped. Returns just the
        primary (length 1) whenever failover is off or nothing valid can be added."""
        level = str(self._failover_cfg("failover_level", "off") or "off").lower()
        chain = [(self, model)]
        wanted = self._FAILOVER_LINKS.get(level, [])
        if not wanted:
            return chain
        seen = {self.provider_name}
        for kind in wanted:
            try:
                if kind == "backup":
                    bp = str(self._failover_cfg("failover_backup_provider", "") or "").strip()
                    # The caller's overrides travel into the fallback too - this is the
                    # line that made an embedder's failover chain structurally dead.
                    from vaf.core.api_keys import resolve_api_key as _resolve
                    if not bp or bp == "local" or bp in seen or not _resolve(bp, self.caller_config):
                        continue
                    bm = str(self._failover_cfg("failover_backup_model", "") or "").strip() or None
                    chain.append((APIBackendManager(bp, config=self.config,
                                                    caller_config=self.caller_config), bm))
                    seen.add(bp)
                elif kind == "local":
                    if "local" in seen:
                        continue
                    lm = str(self._failover_cfg("failover_local_model", "") or "").strip() or None
                    chain.append((APIBackendManager("local", config=self.config), lm))
                    seen.add("local")
            except Exception as e:
                logger.warning(f"[failover] could not add {kind} link: {e}")
        return chain

    @staticmethod
    def _classify_failure(failure) -> str:
        """Best-effort bucket for a failure string/exception:
        client_error | timeout | rate_limit | server_error | connection | unknown."""
        s = str(failure).lower()
        if "429" in s or "rate limit" in s or "ratelimit" in s or "too many requests" in s:
            return "rate_limit"
        # 4xx CLIENT errors are a problem with the REQUEST, not a provider outage. Failing over to a
        # different provider cannot help (the same request fails everywhere) and — worse for stateful
        # gateways like Veyllo — it forwards provider-bound tool_call ids to a provider that cannot honor
        # them, turning one 400 into a cascade. Surface the real error instead. (429 is handled above.)
        if (any(f"error code: {c}" in s for c in ("400", "401", "403", "404", "422"))
                or "invalid_request_error" in s or "bad request" in s):
            return "client_error"
        if "timeout" in s or "timed out" in s:
            return "timeout"
        if "reset by peer" in s or "connection" in s or "unreachable" in s or "refused" in s:
            return "connection"
        if any(c in s for c in ("500", "502", "503", "504", "529", "server error", "internal server", "overloaded")):
            return "server_error"
        return "unknown"

    @staticmethod
    def _messages_have_provider_bound_tool_calls(messages) -> bool:
        """True if the conversation carries tool_call ids bound to the CURRENT provider — an assistant
        message with `tool_calls`, or a role:`tool` result referencing a tool_call_id. Such ids are NOT
        portable across providers (a stateful gateway like Veyllo issues its own ids and 400s on foreign
        ones), so failing over mid-tool-sequence only yields another error. Used to suppress failover there."""
        try:
            for m in (messages or []):
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                if role == "assistant" and m.get("tool_calls"):
                    return True
                if role == "tool":
                    return True
        except Exception:
            pass
        return False

    def _should_failover_on(self, failure) -> bool:
        """Whether a failure may trigger failover, honoring the failover_triggers config.
        Connection/unknown failures always do; an empty trigger list means 'any'."""
        bucket = self._classify_failure(failure)
        if bucket == "client_error":
            return False  # a 4xx is a request problem; another provider won't fix it and gets foreign state
        if bucket in ("connection", "unknown"):
            return True
        triggers = self._failover_cfg("failover_triggers", []) or []
        if not triggers:
            return True
        return bucket in triggers

    # ── The dead-link breaker ────────────────────────────────────────────────
    #
    # Failing over answers "the primary is down"; on its own it never answers
    # "is it back yet", and the two failure modes it left behind were measured:
    #
    #   * With failover_return_to_primary ON (the default) the chain restarts at
    #     the primary on EVERY request, so recovery is automatic but each request
    #     pays the primary's failure again first. Measured against a primary that
    #     hangs: three requests, three attempts, one full failover_timeout_s of
    #     pure waiting each (30s by default).
    #   * With it OFF the pin is permanent. Measured: five requests after the
    #     primary had recovered, zero attempts on it. Nothing ever probed it.
    #
    # One piece of state fixes both: when a link fails for an outage reason it is
    # remembered as dead until a deadline, and while that deadline stands the link
    # is skipped rather than paid for. When it expires the link is simply a
    # candidate again, so the next ORDINARY request is the probe (half-open) - no
    # timer, no background traffic, no tokens spent, and it works in every process
    # that holds a manager rather than only in the one that runs a scheduler.
    #
    # Deliberate: no separate liveness ping. `test_connection()` spends a real
    # completion (tokens, and it would recurse through this very chain) and
    # `list_models()` answers [] both for "provider down" and for "provider has no
    # discovery URL", so neither is a liveness signal a switch may rest on.

    # Injectable so the cooldown is testable without sleeping through it.
    _clock = staticmethod(time.monotonic)

    def _recheck_after_s(self) -> float:
        """Seconds a failed link stays skipped. 0 disables skipping entirely, which
        restores the pre-breaker behaviour exactly."""
        try:
            return max(0.0, float(self._failover_cfg("failover_recheck_after_s", 300) or 0))
        except Exception:
            return 0.0

    def _mark_link_dead(self, provider_name: str) -> None:
        cooldown = self._recheck_after_s()
        if cooldown <= 0:
            return
        if not isinstance(getattr(self, "_failover_open_until", None), dict):
            self._failover_open_until = {}
        self._failover_open_until[provider_name] = self._clock() + cooldown
        logger.info(f"[failover] {provider_name} is down; skipping it for {cooldown:.0f}s")

    def _mark_link_alive(self, provider_name: str) -> None:
        store = getattr(self, "_failover_open_until", None)
        if isinstance(store, dict) and store.pop(provider_name, None) is not None:
            logger.info(f"[failover] {provider_name} answered again; back in the chain")

    def _walk_pin_back(self, pinned, chain):
        """Move the sticky pin back over every earlier link whose recheck window has
        passed.

        Only relevant with failover_return_to_primary OFF, where the pin is what keeps
        the chain on the link that last worked. Measured before this existed: five
        requests after the primary had recovered, zero attempts on it - the pin made
        "stay on the working link until it also fails" mean forever, because the
        working link never fails. Expiring the breaker alone does not help there: the
        primary becomes a candidate again but sits BEHIND the healthy backup in the
        order, so it is never reached.

        With failover_recheck_after_s at 0 the pin stays permanent, which is the
        behaviour this setting had before.
        """
        if pinned <= 0 or self._recheck_after_s() <= 0:
            return pinned
        store = getattr(self, "_failover_open_until", None) or {}
        now = self._clock()
        while pinned > 0 and now >= store.get(chain[pinned - 1][0].provider_name, 0.0):
            pinned -= 1
        return pinned

    def _skip_dead_links(self, order, chain, messages):
        """Drop the positions in `order` whose link is still cooling down.

        Two things it must never do, both load-bearing:

        * Never skip EVERYTHING. If the whole chain is cooling down the request
          still has to be attempted somewhere, so the full order is returned and
          the call fails the way it did before rather than vanishing.
        * Never skip while the outbound history carries provider-bound tool_call
          ids. Skipping is a silent hand-over to the next provider, and that is
          precisely the cascade `_messages_have_provider_bound_tool_calls` exists
          to prevent: a stateful gateway 400s on ids it never issued. Mid-tool
          sequence the chain is walked in full, breaker or not.
        """
        if self._recheck_after_s() <= 0:
            return order
        store = getattr(self, "_failover_open_until", None)
        if not store:
            return order
        if self._messages_have_provider_bound_tool_calls(messages):
            return order
        now = self._clock()
        live = [i for i in order if now >= store.get(chain[i][0].provider_name, 0.0)]
        if not live:
            return order
        if len(live) < len(order):
            skipped = ", ".join(chain[i][0].provider_name for i in order if i not in live)
            logger.info(f"[failover] skipping {skipped}: still cooling down after a failure")
        return live

    def _first_chunk(self, gen, deadline):
        """Pull the first chunk of a provider generator. Returns (chunk, None) on success,
        or (None, failure) where failure is an exception or the '[API Error from …]'
        sentinel the providers yield. With a deadline (seconds, non-last links only) a
        slow first token counts as a failure so we can fail over to the next provider."""
        try:
            if deadline:
                import concurrent.futures as _futures
                ex = _futures.ThreadPoolExecutor(max_workers=1)
                try:
                    first = ex.submit(lambda: next(gen)).result(timeout=deadline)
                except _futures.TimeoutError:
                    return None, f"timeout after {deadline:.0f}s"
                finally:
                    ex.shutdown(wait=False)
            else:
                first = next(gen)
        except StopIteration:
            return "", None
        except Exception as e:  # any pre-first-token error is a failover candidate
            return None, e
        if isinstance(first, str) and first.startswith("[API Error from "):
            return None, first
        return first, None

    def _stream_link(self, link_mgr, first, gen):
        """Yield the buffered first chunk then the rest of a link's stream, mirroring the
        link's token usage back onto this manager so the agent sees correct counts."""
        try:
            if first:
                yield first
            for chunk in gen:
                yield chunk
        finally:
            if link_mgr is not self:
                try:
                    lr = dict(link_mgr.last_request_usage or {})
                    # Merged over the blank shape, so a link that reports only
                    # the two token counts still leaves this manager holding a
                    # complete record rather than the previous call's remains.
                    self.last_request_usage.update({**blank_request_usage(), **lr})
                    self.session_usage["input_tokens"] += lr.get("input_tokens", 0)
                    self.session_usage["output_tokens"] += lr.get("output_tokens", 0)
                except Exception:
                    pass

    def chat_completion(self, messages, temperature=0.7, max_tokens=4096, stream=True, model=None, tools=None, tool_choice=None):
        """Public chat-completion entry point. Transparently adds automatic provider
        failover when configured (Settings → Advanced → Failover); with failover off it
        delegates straight to the single-provider path and is byte-identical to before.

        When an event sink is attached (Agent.set_event_sink), one llm_start /
        llm_end pair wraps the whole call: llm_end carries duration_ms, ok
        (False = errored OR abandoned before completion) and a best-effort
        usage snapshot (the serving provider's last request; may lag one call
        behind when a failover link served the response)."""
        import time as _time

        _sink = callable(getattr(self, "event_sink", None))
        if _sink:
            self._emit_event(
                {"type": "llm_start", "provider": self.provider_name, "model": model}
            )
        # THE accounting point for the whole product. Every lane - chat, coder,
        # sub-agents, vision, voice, memory compaction, the mail composer, the
        # browser agent - reaches a model through this method, so recording here
        # is what makes "every call is counted" true by construction instead of
        # by nine call sites remembering to. It replaced a per-turn hook in the
        # agent that counted the chat lane only; everything else was invisible.
        _before = dict(self.session_usage or {})
        _t0 = _time.monotonic()
        _ok = False
        # Kept as a running COUNT, never as accumulated text: this exists only
        # to size a fallback, and holding a whole response in memory to do it
        # would cost more than the number is worth.
        _out_units = 0
        try:
            for _chunk in self._chat_completion_impl(
                messages, temperature, max_tokens, stream, model, tools, tool_choice
            ):
                if isinstance(_chunk, str) and _chunk:
                    _out_units += _chunk.count(" ") + 1
                yield _chunk
            _ok = True
        finally:
            self._record_call_usage(_before, model, messages=messages, out_units=_out_units)
            if _sink:
                self._emit_event(
                    {
                        "type": "llm_end",
                        "provider": self.provider_name,
                        "model": model,
                        "duration_ms": int((_time.monotonic() - _t0) * 1000),
                        "ok": _ok,
                        "usage": dict(self.last_request_usage or {}),
                    }
                )

    def _record_call_usage(self, before: dict, model: Optional[str],
                           *, messages=None, out_units: int = 0) -> None:
        """Book what THIS call added, once, into the spend ledger.

        Measured as the DELTA of the running session total, not by comparing
        `last_request_usage` against a snapshot. That comparison looked
        equivalent and silently dropped every call whose token counts happened
        to match the previous one exactly - which is not rare at all, since the
        utility lanes send near-identical prompts back to back. Measured against
        a provider's own dashboard it was a percent-scale undercount.

        The running total only ever grows, so an identical repeat still moves it,
        while a call that reported nothing (an error, an abandoned generator)
        moves it by zero and is correctly not billed.
        """
        try:
            after = dict(self.session_usage or {})
            _in = int(after.get("input_tokens") or 0) - int(before.get("input_tokens") or 0)
            _out = int(after.get("output_tokens") or 0) - int(before.get("output_tokens") or 0)
            if _in < 0 or _out < 0:
                # The counter was reset under us (a provider swap). Fall back to
                # what the last request reported rather than booking a negative.
                _lr = dict(self.last_request_usage or {})
                _in, _out = int(_lr.get("input_tokens") or 0), int(_lr.get("output_tokens") or 0)
            from vaf.core.cost import record_call

            # The per-call record, carried to the LAST hop. The capture sites and
            # the sync above are worth nothing if the figures are dropped here:
            # this is where every lane that reaches a model through the manager
            # (chat, sub-agents, vision, voice, compaction, mail) turns into a
            # ledger entry and into what the spend cap reads. An unreported call
            # carries the blank shape, so `cache_measured` is False and it stays
            # out of the hit-rate denominator instead of entering it as a 0% hit.
            _cache = dict(self.last_request_usage or {})
            # Pulled out by name rather than read back out of `cache` inside the
            # recorder: `cache` answers what the prompt cost, this answers why the
            # response stopped, and reading one out of the other hides both.
            _finish = _cache.get("finish_reason")

            _model = str(model or self.config.get(f"api_model_{self.provider_name}", "") or "")
            if not (_in or _out):
                # The call happened - we know the lane, the provider and the
                # model, because those are decided before it goes out. What is
                # missing is the provider's own report, which an aborted or
                # failed stream never sends. Counted with zero tokens so the
                # difference against an invoice is a visible number rather than
                # a silent gap somebody would later paper over with a margin.
                # We know WHO called and WHERE it went - those are decided
                # before the request leaves. Only the provider's own count is
                # missing, so a rough one is put in its place and marked as
                # such, everywhere: in the log line, in the ledger, and in the
                # share of the total that is estimated. Better a stated
                # approximation than a hole nobody can size.
                from vaf.core.cost import estimate_tokens_roughly

                _in_est = 0
                for _m in (messages or []):
                    try:
                        _in_est += estimate_tokens_roughly(str((_m or {}).get("content") or ""))
                    except Exception:
                        continue
                if _in_est or out_units:
                    record_call(self.provider_name, _model, _in_est, int(out_units),
                                reported=False, estimated=True, cache=_cache,
                                finish_reason=_finish)
                else:
                    record_call(self.provider_name, _model, 0, 0, reported=False,
                                cache=_cache, finish_reason=_finish)
                return
            record_call(self.provider_name, _model, _in, _out, cache=_cache,
                        finish_reason=_finish)
        except Exception:
            pass  # accounting must never break a call

    def _chat_completion_impl(self, messages, temperature=0.7, max_tokens=4096, stream=True, model=None, tools=None, tool_choice=None):
        chain = self._build_failover_chain(model)
        if len(chain) <= 1:
            yield from self._chat_single(messages, temperature, max_tokens, stream, model, tools, tool_choice)
            return

        return_primary = bool(self._failover_cfg("failover_return_to_primary", True))
        pinned = 0 if return_primary else max(0, min(getattr(self, "_failover_pinned_idx", 0), len(chain) - 1))
        pinned = self._walk_pin_back(pinned, chain)
        order = list(range(pinned, len(chain))) + list(range(0, pinned))
        order = self._skip_dead_links(order, chain, messages)
        try:
            timeout_s = float(self._failover_cfg("failover_timeout_s", 0) or 0)
        except Exception:
            timeout_s = 0.0

        last_failure = None
        for pos, idx in enumerate(order):
            link_mgr, link_model = chain[idx]
            is_last = pos == len(order) - 1
            gen = link_mgr._chat_single(messages, temperature, max_tokens, stream, link_model, tools, tool_choice)
            deadline = timeout_s if (timeout_s > 0 and not is_last) else None
            first, failure = self._first_chunk(gen, deadline)
            if failure is not None:
                last_failure = failure
                # An outage arms the breaker whether or not we actually move on:
                # the next request must not pay for this link again either way.
                # A 4xx does not - that is the request, not the provider.
                _outage = self._should_failover_on(failure)
                if _outage:
                    self._mark_link_dead(link_mgr.provider_name)
                _can_failover = not is_last and _outage
                if _can_failover and self._messages_have_provider_bound_tool_calls(messages):
                    # Mid-tool-sequence: the history carries this provider's tool_call ids, which the next
                    # provider cannot honor. Don't cascade — surface the primary's error.
                    logger.info(f"[failover] {link_mgr.provider_name} failed ({self._classify_failure(failure)}) but conversation has provider-bound tool_call ids — NOT failing over")
                    _can_failover = False
                if _can_failover:
                    logger.info(f"[failover] {link_mgr.provider_name} failed ({self._classify_failure(failure)}); trying next provider")
                    continue
                yield failure if isinstance(failure, str) else f"[API Error from {link_mgr.provider_name}: {failure}]"
                return
            self._failover_pinned_idx = 0 if return_primary else idx
            self._mark_link_alive(link_mgr.provider_name)
            if link_mgr is not self:
                logger.info(f"[failover] serving response from {link_mgr.provider_name}")
            yield from self._stream_link(link_mgr, first, gen)
            return

        if isinstance(last_failure, str):
            yield last_failure
        elif last_failure is not None:
            yield f"[API Error: {last_failure}]"

    def _chat_single(self, messages, temperature=0.7, max_tokens=4096, stream=True, model=None, tools=None, tool_choice=None):
        """Execute one chat completion against THIS manager's single provider (no failover).

        Args:
            tool_choice: Control tool usage - 'auto' (default), 'none', 'required',
                        or {'type': 'function', 'function': {'name': '...'}} for specific tool
        """
        # Determine model — defaults derive from Config.PROVIDER_MODELS (single source).
        default_models = {p: m["default"] for p, m in Config.PROVIDER_MODELS.items()}
        default_models["local"] = "llama3"
        # A forwarded local/blank model id must never reach a cloud provider. The local model default
        # is "auto" (config.py), and some sub-agents/tools forward the local "model" key verbatim; for
        # non-local providers normalise "auto"/blank to "unset" so it resolves to api_model_{provider}
        # below (incl. DeepSeek's deepseek-auto routing) instead of shipping "auto" -> HTTP 400
        # "model does not exist". The GGUF guardrail below only catches local *file* ids, not "auto".
        if model and self.provider_name != "local" and str(model).strip().lower() in ("", "auto"):
            model = None
        if not model:
            if getattr(self, "_embedded", False):
                # Embedded: honour api_model from the programmatic config (no Settings UI here)
                model = self.config.get(f"api_model_{self.provider_name}", default_models.get(self.provider_name, "gpt-4o"))
            else:
                # Product: read fresh from disk so mid-session model changes (via Settings) take effect immediately
                live_config = Config.load()
                model = live_config.get(f"api_model_{self.provider_name}", default_models.get(self.provider_name, "gpt-4o"))
        # Guardrail: when using API providers, a stale local GGUF model value can be passed
        # (e.g. "Veyllo/VQ-1_Instruct-q4_k_m"), which causes provider errors and long retry loops.
        # In that case, force provider-specific model from config/default.
        elif self.provider_name != "local":
            model_s = str(model).strip().lower()
            looks_like_local_model = (
                model_s.endswith(".gguf")
                or "vq-1" in model_s
                or "instruct-q" in model_s
                or model_s.startswith("veyllo/")
            )
            if looks_like_local_model:
                model = self.config.get(
                    f"api_model_{self.provider_name}",
                    default_models.get(self.provider_name, "gpt-4o"),
                )

        # DeepSeek Auto mode: flash for main chat, pro model for tools/workflows/compaction.
        # Also resolves when VAF_TOOL_MODEL is set to "deepseek-auto" (e.g. subagent_model config).
        if self.provider_name == "deepseek" and str(model or "").lower() == "deepseek-auto":
            _pro_context = (
                os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip() in ("1", "true", "yes")
                or os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes")
                or os.environ.get("VAF_COMPACTION_IN_PROGRESS", "").strip() in ("1", "true", "yes")
                or os.environ.get("VAF_BACKGROUND_PRO", "").strip() in ("1", "true", "yes")
                or os.environ.get("VAF_TOOL_MODEL", "").strip().lower() == "deepseek-auto"
            )
            if _pro_context:
                # Use explicit subagent_model if configured, but never "deepseek-auto" (would recurse)
                _sa = self.config.get("subagent_model", "").strip()
                model = (_sa if _sa and _sa.lower() != "deepseek-auto" else None) or "deepseek-v4-pro"
            else:
                model = "deepseek-v4-flash"

        # DeepSeek Reasoner/R1: no function calling support; API returns 400 if tools passed
        if self.provider_name == "deepseek" and model:
            m = (model or "").lower()
            if "reasoner" in m or "-r1" in m:
                tools = None
                tool_choice = "none"

        # DeepSeek tool_choice restriction (UNIVERSAL — applies to every caller routed through this
        # manager: main agent streaming/non-stream/fallback, thinking-mode forced nodes, sub-agents).
        # DeepSeek (both deepseek-v4-flash and deepseek-v4-pro, internally reasoning models) reject
        # tool_choice="required" and specific function-forcing dicts with HTTP 400 ("does not support
        # this tool_choice"); only "auto"/"none" are accepted. Downgrade any forcing form to "auto" so
        # forced-tool callers degrade gracefully — those callers already carry a prompt-level imperative
        # to emit the tool call. "auto"/"none"/no-tools are left untouched. Runs AFTER the reasoner guard
        # so its "none" is not re-touched. (coder.py keeps its own equivalent guard because it bypasses
        # this manager and posts to the provider over HTTP directly.)
        if self.provider_name == "deepseek" and tools and tool_choice is not None:
            if (isinstance(tool_choice, str) and tool_choice.strip().lower() == "required") \
                    or isinstance(tool_choice, dict):
                tool_choice = "auto"

        # A per-call figure that is only ever WRITTEN, never reset, reports the
        # previous call's numbers for any call the provider says nothing about -
        # a stream that carries no usage chunk, a request that fails after the
        # last one succeeded. Both sides start blank here, which is the single
        # funnel: `provider.chat_completion` has exactly one caller and the
        # provider classes are built nowhere but the factory above.
        self.provider.last_request_usage = blank_request_usage()
        self.last_request_usage = blank_request_usage()

        # Execute via provider
        try:
            for chunk in self.provider.chat_completion(messages, temperature, max_tokens, stream, model, tools, tool_choice):
                # Sync usage stats back to manager. `update` rather than two named
                # assignments: a field the provider learns to report must not need a
                # second edit here to survive the trip (it did, twice).
                self._sync_usage_from_provider()
                yield chunk
        finally:
            # AND once after the stream ends, which is the sync that actually
            # carries the numbers. A provider reports its usage in a trailing
            # chunk that carries no choices, so it records the figures and yields
            # NOTHING for them: the per-chunk sync above runs for the last chunk
            # that had content and never again. Measured before this line
            # existed, three calls in a row booked 3/4 (an estimate), then 10/2,
            # then 10/4 while the provider had reported 10/2, 10/4, 10/2 - every
            # call billed the previous call's tokens, and the first one billed a
            # guess. The per-chunk sync stays because a reader watching a live
            # stream wants the running figure; this one is what makes the final
            # one true. In `finally`, so an abandoned generator settles too.
            self._sync_usage_from_provider()

    def _sync_usage_from_provider(self) -> None:
        """Copy the provider's counters onto this manager, whatever they hold."""
        try:
            self.session_usage["input_tokens"] = self.provider.usage["input_tokens"]
            self.session_usage["output_tokens"] = self.provider.usage["output_tokens"]
            self.last_request_usage.update(self.provider.last_request_usage)
        except Exception:
            pass  # accounting must never break a call

    def chat_completion_stream(self, messages, temperature=0.7, max_tokens=4096, model=None, tools=None, tool_choice=None):
        """Streaming chat completion - alias for chat_completion with stream=True."""
        return self.chat_completion(messages, temperature, max_tokens, stream=True, model=model, tools=tools, tool_choice=tool_choice)

    # ── Context window lookup ─────────────────────────────────────────────────

    # Static table: substring patterns (lower-case) → context window in tokens.
    # Ordered from most-specific to least-specific; first match wins.
    _CTX_TABLE: list[tuple[str, int]] = [
        # OpenAI
        ("gpt-4o",          128_000),
        ("gpt-4-turbo",     128_000),
        ("gpt-4-32k",        32_768),
        ("gpt-4",             8_192),
        ("gpt-3.5-turbo-16",16_385),
        ("gpt-3.5",           4_096),
        ("o1-mini",         128_000),
        ("o1",              200_000),
        ("o3",              200_000),
        ("o4",              200_000),
        # Anthropic — Claude 4 family (Sonnet/Opus/Fable/Mythos) is 1M; Haiku 4.5 + legacy 3.x = 200K
        ("claude-haiku-4",  200_000),
        ("claude-sonnet-4",1_000_000),
        ("claude-opus-4",  1_000_000),
        ("claude-fable",   1_000_000),
        ("claude-mythos",  1_000_000),
        ("claude",          200_000),
        # Google
        ("gemini-3",      1_048_576),
        ("gemini-2.5",    1_048_576),
        ("gemini-2.0",    1_048_576),
        ("gemini-1.5-pro",2_097_152),
        ("gemini-1.5",    1_048_576),
        ("gemini",        1_048_576),
        # DeepSeek — all V4 models: 1M input context, 64K max output
        ("deepseek-v4",   1_000_000),
        ("deepseek",      1_000_000),
        # Mistral
        ("mistral-large",   131_072),
        ("mistral-small",   131_072),
        ("codestral",       256_000),
        ("mistral",          32_000),
        # Meta / Llama
        ("llama-3.1",       131_072),
        ("llama-3.2",       131_072),
        ("llama-3.3",       131_072),
        ("llama",            32_000),
        # Qwen
        ("qwen2.5-72",      131_072),
        ("qwen2.5",         131_072),
        ("qwen",             32_000),
    ]

    # Module-level cache: openrouter model id → context_length
    _openrouter_ctx_cache: dict[str, int] = {}

    def get_model_context_window(self, model: str | None = None) -> int:
        """
        Return the context window (in tokens) for *model* on this provider.

        Lookup order:
          1. OpenRouter → fetch /v1/models once per process and cache.
          2. Static table (substring match, longest-specific first).
          3. Fallback: 128 000.
        """
        if not model:
            model = self.config.get(f"api_model_{self.provider_name}", "") or ""

        model_lc = model.lower()

        # OpenRouter: live API gives exact context_length per model
        if self.provider_name == "openrouter":
            if model_lc in APIBackendManager._openrouter_ctx_cache:
                return APIBackendManager._openrouter_ctx_cache[model_lc]
            try:
                import requests as _req
                from vaf.core.provider_registry import models_discovery as _md
                _or_url = (_md("openrouter") or ("https://openrouter.ai/api/v1/models", "bearer"))[0]
                resp = _req.get(
                    _or_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=5,
                )
                if resp.ok:
                    for m in resp.json().get("data", []):
                        mid = (m.get("id") or "").lower()
                        ctx = m.get("context_length") or 0
                        if mid and ctx:
                            APIBackendManager._openrouter_ctx_cache[mid] = int(ctx)
                    if model_lc in APIBackendManager._openrouter_ctx_cache:
                        return APIBackendManager._openrouter_ctx_cache[model_lc]
            except Exception:
                pass  # Fall through to static table

        return APIBackendManager.static_context_window(model_lc)

    @classmethod
    def static_context_window(cls, model: str) -> int:
        """Context window from the static table alone - no instance, no network.

        The instance method above owns the live OpenRouter lookup and then lands
        here, so the table is read in ONE place. Callers that have no backend
        instance (the settings surfaces that offer a context budget, an embedder
        sizing its own UI) get the same answer without constructing a backend.
        """
        model_lc = (model or "").lower()
        for pattern, ctx in cls._CTX_TABLE:
            if pattern in model_lc:
                return ctx

        return 128_000  # Safe default

    @staticmethod
    def get_available_models(provider: str) -> List[str]:
        """Static fallback list for UI dropdowns — sourced from Config.PROVIDER_MODELS
        (single source). Used when no live /v1/models fetch is available."""
        if provider == "local":
            return ["llama3", "mistral", "codellama"]
        return Config.get_fallback_models(provider)

    @staticmethod
    def list_models(provider: str) -> List[str]:
        """Live-fetch the available chat model IDs for `provider` from its API, or [] on any error.
        Sync + hard fail-safe; the API key is read from Config. Used by Whare Wananga's teacher
        selection to consider the strongest AVAILABLE model, not only the configured one.

        Discovery URL + auth kind come from the provider registry (single source
        of truth); the response FILTERING below stays provider-specific."""
        import requests
        from vaf.core.config import Config
        try:
            key = Config.get_api_key(provider)
        except Exception:
            key = ""
        if not key:
            return []
        try:
            from vaf.core.provider_registry import models_discovery

            disc = models_discovery(provider)
            if disc is None:
                # No remote listing for this provider (e.g. local): today's
                # empty-result behavior.
                return []
            url, auth = disc
            headers = {}
            params = {}
            if auth == "bearer":
                headers["Authorization"] = f"Bearer {key}"
            elif auth == "x-api-key":
                headers["X-Api-Key"] = key
                headers["anthropic-version"] = "2023-06-01"
            elif auth == "query-key":
                params["key"] = key
            if provider == "google":
                params["pageSize"] = 1000
            r = requests.get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                if provider == "openai":
                    return sorted(m["id"] for m in r.json().get("data", [])
                                  if any(x in m["id"] for x in ("gpt", "o1", "o3", "o4")))
                elif provider == "google":
                    out = []
                    for m in r.json().get("models", []):
                        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                            continue
                        mid = m.get("baseModelId") or m.get("name", "")
                        if mid.startswith("models/"):
                            mid = mid.split("/", 1)[1]
                        if mid and mid not in out:
                            out.append(mid)
                    return sorted(out)
                elif provider == "openrouter":
                    return [m["id"] for m in r.json().get("data", []) if m.get("id")][:50]
                else:
                    # anthropic, deepseek, veyllo and any future
                    # OpenAI-compatible provider: plain id list.
                    ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
                    if provider == "veyllo":
                        # /v1/models also lists veyllo-transcribe (STT); keep chat only.
                        from vaf.core.provider_registry import is_veyllo_chat_model
                        ids = [mid for mid in ids if is_veyllo_chat_model(mid)]
                    return ids
        except Exception:
            return []
        return []

    @staticmethod
    def test_connection(provider: str) -> bool:
        """Test API connectivity."""
        try:
            mgr = APIBackendManager(provider)
            # Short test call
            res = list(mgr.chat_completion([{"role": "user", "content": "hi"}], max_tokens=5, stream=False))
            return len(res) > 0
        except Exception:
            return False
