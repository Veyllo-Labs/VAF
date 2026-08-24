# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One completion - one prompt in, one text out. No tools, no history, no conversation.

THE MEASUREMENT BEHIND THIS MODULE: ~22 places hand-rolled the same single-shot call.
`BaseTool.query_llm` was the best of them and now delegates here; six external copies
(the three identical `call_local_llm` functions in the git/debug/generate CLIs, the
coder-template classifier, the memory-RAG answerer, the attachment-RAG summarizer) are
deleted outright. The hand-rolls disagreed about correctness, and every disagreement was
a live defect: collectors without the metadata-frame filter concatenated
`{"finish_reason": ...}` JSON into commit messages; collectors without the sentinel
check returned "[API Error from ...]" strings as CONTENT; local calls without
`chat_template_kwargs {enable_thinking: false}` burned the whole token budget on
reasoning and returned empty text; six sites hardcoded 127.0.0.1:8080 and broke in
Docker. One collector, one local lane, one API lane - every consumer gets all of the
fixes at once.

THE CONTRACT: `complete()` returns `Optional[str]` and NEVER raises, never returns an
error sentinel or a metadata frame as content. Callers format their own error strings.
`<think>` blocks are stripped by default (one-shot results land in commit messages and
stored summaries - leaking chain of thought there is the documented incident class);
the stricter semantics win: an UNCLOSED `<think>` truncates to the end, so a reply that
was all reasoning becomes None and the caller's fallback fires.

THE ONE-LLAMA-SERVER INVARIANT, auditable by the import list: this module never imports
the server manager and never starts, loads or downloads anything. The local lane only
TALKS to the one server that may already exist (`Config.get_llama_server_url`, which is
Docker-aware); a dead server is None, not a spawn.

The remaining direct `chat_completion` call sites are streaming or multi-turn lanes
with their own collectors, frozen in tests/test_completion_call_baseline.py. The 11
one-shot hand-rolls inside vaf/core/agent.py are the named follow-up recorded there.
"""
from typing import Optional

_ERROR_SENTINEL = "[API Error from"


def is_metadata_frame(chunk: str) -> bool:
    """True for the JSON control frames the backend stream interleaves with content.

    Only frames, never content: document tools legitimately request JSON output, so the
    check requires both the "{" shape and one of the control markers.
    """
    stripped = chunk.strip()
    return stripped.startswith("{") and (
        "tool_calls" in chunk or "tool_use" in chunk or "finish_reason" in chunk
    )


def strip_think_blocks(text: str) -> str:
    """Remove model reasoning from a one-shot result, strictly.

    Closed ``<think>...</think>`` blocks are removed; an UNCLOSED ``<think>`` truncates
    from the marker to the end (a result that is all reasoning must become empty, not
    leak); stray close markers are dropped. Case-insensitive, like the models are.
    """
    import re

    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    lower = text.lower()
    open_idx = lower.find("<think>")
    if open_idx != -1:
        text = text[:open_idx]
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def _collect(chunks) -> tuple:
    """(text, error_text): concatenate content chunks; an error sentinel poisons the call.

    Dict chunks contribute their "content" field (some providers yield dicts on the
    non-stream path); metadata frames are skipped; a sentinel makes the TEXT None while
    the raw error is kept for the caller's retry decision.
    """
    parts = []
    error_text = None
    for chunk in chunks:
        if isinstance(chunk, dict):
            piece = chunk.get("content") or ""
            if piece:
                parts.append(piece)
            continue
        if not isinstance(chunk, str) or not chunk:
            continue
        if _ERROR_SENTINEL in chunk:
            error_text = chunk
            continue
        if is_metadata_frame(chunk):
            continue
        parts.append(chunk)
    if error_text is not None:
        return None, error_text
    return "".join(parts).strip(), None


def collect_stream(chunks) -> Optional[str]:
    """The shared collector alone: text, or None when the stream carried an error."""
    text, _error = _collect(chunks)
    return text


def _local_complete(messages, model, max_tokens, temperature, timeout,
                    allow_reasoning_fallback, caller) -> Optional[str]:
    """One non-streaming call against the ONE local llama server. Never starts it."""
    import requests

    from vaf.core.config import Config

    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Qwen-class local models burn the whole (small) token budget on
        # reasoning_content and return EMPTY content for utility calls. Same fix as
        # the voice lane: disable thinking; non-thinking templates ignore the flag.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if model:
        payload["model"] = model
    try:
        res = requests.post(
            Config.get_llama_server_url("/v1/chat/completions"),
            json=payload,
            timeout=(timeout or 120),
        )
        if res.status_code != 200:
            return None
        data = res.json()
        # The local server reports usage like any other OpenAI-compatible one.
        # It costs no money, but it IS a model call, and a usage view that shows
        # only the paid ones cannot answer "what did this machine do".
        try:
            _u = data.get("usage") or {}
            _in, _out = int(_u.get("prompt_tokens") or 0), int(_u.get("completion_tokens") or 0)
            if _in or _out:
                from vaf.core.cost import cache_usage_from_openai, record_call
                # llama-server reports prefix reuse under `timings`, not in
                # `usage`, so this reads as unmeasured and the local lane stays
                # out of the hit-rate denominator instead of dragging it down
                # with a zero it never claimed. Reading `timings` needs a second
                # path with its own failure mode and buys nothing here: the lane
                # is free, so the number moves no money and trips no cap.
                record_call("local", str(data.get("model") or "local"), _in, _out,
                            cache=cache_usage_from_openai(_u))
        except Exception:
            pass
        choices = data.get("choices") or []
        if not choices:
            return None
        choice = choices[0] or {}
        msg = choice.get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        if content:
            return content
        # Reasoning models put the substance in reasoning_content when the budget ran
        # out mid-think. Long-form callers must never receive chain of thought as the
        # answer and pass allow_reasoning_fallback=False.
        if not allow_reasoning_fallback:
            return None
        reasoning = (msg.get("reasoning_content") or "").strip()
        try:
            from vaf.core.log_helper import append_domain_log
            append_domain_log(
                "backend",
                f"complete({caller}) empty content: finish_reason={choice.get('finish_reason')} "
                f"reasoning_len={len(reasoning)} max_tokens={max_tokens}",
            )
        except Exception:
            pass
        return reasoning or None
    except Exception:
        return None                      # dead server = no answer, NEVER a spawn


def _api_complete(backend, provider, model, messages, max_tokens, temperature,
                  timeout, fallback_model, caller) -> Optional[str]:
    """One streamed call through APIBackendManager, bounded, with one self-heal retry."""
    import concurrent.futures as _cf

    def _run(target_model):
        mgr = backend
        if mgr is None:
            from vaf.core.api_backend import APIBackendManager
            mgr = APIBackendManager(provider)
        return _collect(mgr.chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            model=target_model,
        ))

    def _retryable(err_str: str) -> bool:
        low = err_str.lower()
        return "400" in low or "404" in low or "invalid model" in low

    executor = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        try:
            text, error_text = executor.submit(_run, model).result(timeout=timeout)
        except _cf.TimeoutError:
            print(f"[WARN] complete({caller}): timeout after {timeout}s")
            return None
        except Exception as e:
            text, error_text = None, str(e)

        if text is not None:
            return text or None

        # Self-heal ONCE on an invalid-model answer. The manager yields errors as
        # sentinel strings and almost never raises, so the decision reads the
        # SENTINEL TEXT too - the old exception-only retry was dead code.
        if (error_text and _retryable(error_text)
                and fallback_model and fallback_model != model):
            print(f"[WARN] complete({caller}): model '{model}' rejected, "
                  f"retrying with '{fallback_model}'")
            try:
                text, error_text = executor.submit(_run, fallback_model).result(timeout=timeout)
                if text is not None:
                    return text or None
            except Exception:
                pass
        if error_text:
            print(f"[WARN] complete({caller}): backend error: {str(error_text)[:200]}")
        return None
    finally:
        executor.shutdown(wait=False)


def complete(messages, *, provider: Optional[str] = None, model: Optional[str] = None,
             max_tokens: int = 512, temperature: float = 0.2,
             timeout: Optional[float] = None, strip_think: bool = True,
             allow_reasoning_fallback: bool = True,
             fallback_model: Optional[str] = None, backend=None,
             caller: str = "") -> Optional[str]:
    """One completion. Text or None - never an exception, never an error sentinel.

    ``messages`` is a string (wrapped as one user message) or a messages list.
    ``provider`` None resolves the configured one; ``model`` None lets the API manager
    resolve ``api_model_<provider>`` itself. ``backend`` reuses an existing
    APIBackendManager (the engine passes its own, which carries embedded caller keys
    and the event sink). ``timeout`` bounds the API wait (None = unbounded, the
    query_llm contract) and the local request (None = 120s).
    """
    # Label the lane from the caller this function is ALREADY given, so a tool
    # that reaches a model is counted under its own name instead of inheriting
    # whatever turn it happened to run inside. `caller` is "tool:<name>" from
    # BaseTool.query_llm and a plain lane elsewhere.
    from vaf.core.cost import usage_context as _usage_context

    _lane = (caller or "").strip() or None
    with _usage_context(lane=_lane):
        return _complete_inner(
            messages, provider=provider, model=model, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout, strip_think=strip_think,
            allow_reasoning_fallback=allow_reasoning_fallback,
            fallback_model=fallback_model, backend=backend, caller=caller)


def _complete_inner(messages, *, provider=None, model=None, max_tokens: int = 512,
                    temperature: float = 0.2, timeout=None, strip_think: bool = True,
                    allow_reasoning_fallback: bool = True, fallback_model=None,
                    backend=None, caller: str = "") -> Optional[str]:
    """The body of :func:`complete`; see there. Split out so the lane label wraps
    every path, including the local one that never touches the backend manager."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    if provider is None:
        from vaf.core.config import Config
        provider = Config.get("provider", "local") or "local"

    if provider != "local" or backend is not None:
        text = _api_complete(backend, provider, model, messages, max_tokens,
                             temperature, timeout, fallback_model, caller)
    else:
        if not model:
            from vaf.core.config import Config
            model = Config.get("model", "") or ""
        text = _local_complete(messages, model, max_tokens, temperature, timeout,
                               allow_reasoning_fallback, caller)

    if text and strip_think:
        text = strip_think_blocks(text)
    return text or None
