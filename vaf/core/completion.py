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
import contextvars
import re
from typing import Optional

_ERROR_SENTINEL = "[API Error from"
# The engine's own failure status: chat_step returns (and streams) it for a backend
# exception or a server it gave up on, and the queue runner writes it for a turn that
# produced nothing.
_ENGINE_ERROR = "[Error] "

# What the engine writes as an assistant message's content in its OWN history when the
# round had no text: a tool round without narration (outside the local server lane, which
# stores None) and the first empty retry that had neither reasoning nor answer. Engine status,
# never words for the person: a reader that takes the newest assistant text of a run as its
# answer passes over these.
THINKING_PLACEHOLDER = "Thinking..."
EMPTY_PLACEHOLDER = "[Empty]"
HISTORY_PLACEHOLDERS = (THINKING_PLACEHOLDER, EMPTY_PLACEHOLDER)


def is_error_reply(text) -> bool:
    """True when a turn's text is a backend failure handed back as text, not an answer.

    `chat_step` does not raise for a failing provider. It returns its `[Error] ...` status,
    and the backend streams a provider's failure as a content chunk, `[API Error from
    <provider>: <raw answer>]`, which can follow text an earlier round already streamed. Both
    reach the turn's answer and carry internal detail (file paths, store ids, the provider's
    raw answer, a masked key). Judge the whole text you are about to show or send; a reply
    that quotes either marker counts as an error too, the safe side for a reader who must
    not see internals.

    Named boundary: the collectors that test ONE chunk of a stream for the provider
    sentinel (`_collect` here, `_first_chunk` in the failover chain, the vision, voice and
    memory-answer lanes) stay chunk tests. The engine marker is a whole-text signal; at
    chunk level it would fire on content that merely starts a chunk with it.

    The engine marker with no detail after it counts too when a cleanup stripped its
    trailing blank (a text that ends in `[Error]`): a messenger's outbound text is
    stripped, and the bare word would otherwise reach a contact."""
    if not isinstance(text, str) or not text:
        return False
    return (_ERROR_SENTINEL in text or _ENGINE_ERROR in text
            or text.rstrip().endswith(_ENGINE_ERROR.rstrip()))


def is_stop_note(text) -> bool:
    """True when a text IS a loop-protection stop note: it begins with the
    `[LOOP_PROTECTION]` marker (`vaf.markers.LOOP_PROTECTION`), after the warning sign the
    engine puts in front. A reply that mentions the marker further in, explaining what such
    a stop means, is an answer and not a stop."""
    if not isinstance(text, str):
        return False
    from vaf.markers import LOOP_PROTECTION
    head = text.lstrip().removeprefix("⚠").removeprefix("️").lstrip()
    return head.startswith(LOOP_PROTECTION)


def split_control_head(text) -> tuple:
    """`(marker, rest)` for a turn's return value that begins with one of the engine's two
    control heads, `[ASYNC_ACK]` or `[SYSTEM_LOG_ONLY]` (`vaf.markers`), after leading
    whitespace; `rest` is everything after the marker. `("", text)` with the text unchanged
    for every other text, `("", "")` for None.

    Only the head counts: a reply that quotes a marker further in, explaining it, is text.
    These two are status for the host, never words for the person: the engine hands them back
    in its return channel and never streams them, and its own history holds the
    acknowledgement without the marker. `[Generation stopped by user]` and
    `[LOOP_PROTECTION]` are not control heads: the person sees both as written while the turn
    runs, so a stored copy keeps them too."""
    if text is None:
        return "", ""
    text = text if isinstance(text, str) else str(text)
    from vaf.markers import ASYNC_ACK, SYSTEM_LOG_ONLY
    head = text.lstrip()
    for marker in (ASYNC_ACK, SYSTEM_LOG_ONLY):
        if head.startswith(marker):
            return marker, head[len(marker):]
    return "", text


def is_failure_chunk(chunk) -> bool:
    """True for the content chunk a provider yields in place of an answer when it fails: the
    `[API Error from <provider>: ...]` sentinel, which api_backend yields as one whole chunk,
    also after text the round already streamed. The chunk-level question a stream reader asks
    (`chat_step` marks its round failed on it, so the text is never committed as the model's
    answer); `is_error_reply` is the whole-text one. A chunk that merely quotes the sentinel
    further in is content."""
    return isinstance(chunk, str) and chunk.startswith(_ERROR_SENTINEL)


def is_metadata_frame(chunk: str) -> bool:
    """True for the JSON control frames the backend stream interleaves with content.

    Only frames, never content: document tools legitimately request JSON output, so the
    check requires both the "{" shape and one of the control markers.
    """
    stripped = chunk.strip()
    return stripped.startswith("{") and (
        "tool_calls" in chunk or "tool_use" in chunk or "finish_reason" in chunk
    )


# Leaked tool-call JSON at the end of a reply, each pattern cutting to the end of the text:
# a whole array or object ([{"tool_calls": ...}], {"name": ..., "arguments": ...}) and the
# fragments a confused model leaves (", "name": ..., "}, "type": "function", "index": 0}]}).
# Each starts at a literal, never at a run of blanks, so none rescans a run from every blank.
_TOOL_CALL_TAILS = tuple(re.compile(p, re.DOTALL | re.MULTILINE) for p in (
    # The bracket or the brace starts it, not the blank before: a leading `\s*` was retried
    # from every blank of a long run (11 s on 64 KB of spaces); that blank goes with the
    # caller's final strip.
    r'(?:\[\s*)?\{\s*"tool_calls":.*$',
    r'\{\s*"name":\s*"[^"]*",\s*"arguments":.*$',
    r'\{\s*"index":\s*\d+,\s*"id":.*$',
    r'",\s*"name":\s*"[^"]*",\s*"type":\s*"function".*$',
    r'"\}\s*,\s*"type":\s*"function".*$',
    r'",\s*"name":\s*"[^"]*"\}\s*,\s*"type":\s*"function".*$',
    # A trailing fragment, with or without its closing quote. The blanks before the end go
    # with the quote only: `[^"]*` already takes them when the quote is missing, and a `\s*`
    # behind an optional quote rescanned the rest of a blank run from every blank (39 s on
    # 64 KB).
    r',\s*"name":\s*"[^"]*(?:"\s*)?$',
    r'\}\s*,\s*\{\s*"index":\s*\d+.*$',
    r'\}\s*,\s*\{\s*"name":\s*"[^"]*".*$',
    r'\]\s*\}\s*$',
    r'\}\s*\]\s*\}\s*$',
))


def strip_tool_call_tails(text: str) -> str:
    """Leaked tool-call JSON off the end of a model's text, by the one pattern list the
    reply cleaner (`Agent._sanitize_response`) and the messenger chain
    (`headless_runner._strip_tool_calls_json`) share. Not stripped: the caller strips."""
    for pattern in _TOOL_CALL_TAILS:
        text = pattern.sub("", text)
    return text


# <thinking> is the same marker: the chat's parseThinkBlocks (web/app/page.tsx) reads it so.
_THINK_TAG = re.compile(r"<(/?)think(?:ing)?>", re.IGNORECASE)

# Case-sensitive on purpose: the models that emit this family write it in lowercase only.
_REDACTED_OPEN = "<redacted_reasoning>"
_REDACTED_CLOSE = "</redacted_reasoning>"


def strip_think_blocks(text: str, *, greedy: bool = False) -> str:
    """Remove the reasoning a model MARKED from its text, strictly: THE rule for every
    reader that must not pass a model's reasoning on (a reply, a messenger send, speech,
    a stored memory, a one-shot result).

    Three marks: ``<think>`` and ``<thinking>`` in any case, like the models write them,
    and ``<redacted_reasoning>`` (lowercase). Closed blocks are removed, each closed at
    its first closer; an UNCLOSED opener truncates from the marker to the end (a text
    that is all reasoning must become empty, not leak); stray close markers are dropped.
    A tag that only forms once a block is dropped ("<" + block + "think>") is a marker
    too and cuts the text there: removed text can never re-form a block. The result is
    stripped; a falsy input comes back as it is.

    ``greedy``: one think block from the FIRST opener to the LAST closer, for a turn's
    reply, where a weak model sometimes quotes a ``</think>`` mid-reasoning (an earlier
    reply) and a block closed at the first one would leak the rest of its reasoning. A
    tag that forms where that slice joins the text ("a</th" + block + "ink>b") is read by
    the walk like any other: a closer there is dropped as a stray one ("ab"), an opener
    cuts.

    The order of the passes: redacted blocks go first, because a ``<think>`` the model
    quotes inside one would otherwise cut the text before the block's closer, leak the
    block and lose the answer; then the think blocks. A redacted tag that only formed once
    a think block was dropped ("<redacted_" + block + "reasoning>"), or a redacted closer
    with no opener, gets a second pass in which a stray closer is dropped and an opener
    still left without its closer cuts the text; a think tag that pass joins
    ("<thi" + closer + "nk>") is read once more, so it cuts too.

    Named boundaries: blocks of the two kinds that CROSS each other, a think tag inside a
    redacted block pairing with one outside it
    (``<redacted_reasoning><think></redacted_reasoning>Q</think>``), have no right
    reading; the redacted block goes first and Q is kept as text (about 2 in 1,000
    adversarial fuzz draws, all of this class). A redacted block the model never closed
    cuts the text to its end even when a think closer follows it
    (``<redacted_reasoning>plan</think>Answer.`` is empty): a think closer quoted inside
    such a block cannot be told from one that ends it, so the reasoning side wins. Only a
    ``<think>`` opener BEFORE the redacted one pairs with that closer, which the greedy
    reading does (``<think>a</think><redacted_reasoning>b</think>Answer`` is ``Answer``
    greedy, as the chat shows it, and empty plain). A stray ``</redacted_reasoning>`` that
    the second think pass itself joins stays as text: a closer opens nothing, so no
    reasoning follows it out.

    Linear in the text: a fixed number of passes, none of which rescans what it already
    read. A lazy ``<think>.*?</think>`` rescans to the end from every unclosed opener
    (9.7 s on 128 KB of them), and a pass per joined tag rescans once per tag (9.8 s on
    128 KB of nested joined closers).
    """
    if not text:
        return text
    t = drop_redacted_blocks(text, cut_unclosed=False)
    t = _strip_think_tags(t, greedy)
    if _REDACTED_OPEN in t or _REDACTED_CLOSE in t:
        t = _strip_think_tags(drop_redacted_blocks(t, cut_unclosed=True), greedy)
        t = t.split(_REDACTED_OPEN, 1)[0]  # an opener that this cleanup itself joined
    return t.strip()


def drop_redacted_blocks(text: str, *, cut_unclosed: bool) -> str:
    """Every ``<redacted_reasoning>`` block out, each closed at its first closer, in one walk.

    Without ``cut_unclosed`` an opener without a closer is kept as text, and so is a closer
    without an opener: the exact output of a lazy
    ``<redacted_reasoning>.*?</redacted_reasoning>`` (the web chat's stored copy keeps
    everything else as written). With ``cut_unclosed`` such an opener cuts the text there
    and a stray closer is dropped (`strip_think_blocks`'s second pass).

    Linear: the next opener and the next closer are each found once and kept until the walk
    passes them, where the lazy regex rescans to the end from every unclosed opener (about
    4 s on 128 KB of them).
    """
    kept, pos = [], 0
    if not cut_unclosed:
        while True:
            start = text.find(_REDACTED_OPEN, pos)
            if start < 0:
                break
            end = text.find(_REDACTED_CLOSE, start + len(_REDACTED_OPEN))
            if end < 0:
                break  # no closer follows: no later opener can be closed either
            kept.append(text[pos:start])
            pos = end + len(_REDACTED_CLOSE)
        kept.append(text[pos:])
        return "".join(kept)
    opener, closer = text.find(_REDACTED_OPEN), text.find(_REDACTED_CLOSE)
    while True:
        if closer >= 0 and (opener < 0 or closer < opener):
            # A closer with no opener before it. An opener holds no "<" after its first
            # character, so the cached opener still lies at or after the new position.
            kept.append(text[pos:closer])
            pos = closer + len(_REDACTED_CLOSE)
            closer = text.find(_REDACTED_CLOSE, pos)
            continue
        if opener < 0:
            break
        kept.append(text[pos:opener])
        if closer < 0:
            return "".join(kept)  # never closed: the rest is reasoning
        pos = closer + len(_REDACTED_CLOSE)
        opener = text.find(_REDACTED_OPEN, pos)
        closer = text.find(_REDACTED_CLOSE, pos)
    kept.append(text[pos:])
    return "".join(kept)


def split_think_blocks(text: str) -> tuple:
    """(visible, blocks): the inner texts of the closed ``<think>``/``<thinking>`` blocks, in
    order, and the text with each such block, tags included, replaced by one space.

    Each block closes at its FIRST closer and openers inside it are ignored; an unclosed
    opener and stray closers stay in the visible text as written. For the readers that
    compare a reply with its own reasoning rather than remove it (the echo check, the
    compose lane's leak check). One walk, where the lazy ``findall`` it replaces rescans
    to the end from every unclosed opener.
    """
    text = text or ""
    visible, blocks, pos, start = [], [], 0, None
    for m in _THINK_TAG.finditer(text):
        if start is None:
            if not m.group(1):
                start = m
        elif m.group(1):
            visible.append(text[pos:start.start()])
            visible.append(" ")
            blocks.append(text[start.end():m.start()])
            pos, start = m.end(), None
    visible.append(text[pos:])
    return "".join(visible), blocks


def _strip_think_tags(text: str, greedy: bool) -> str:
    """The think half of `strip_think_blocks`: closed blocks out, an unclosed opener cuts,
    stray tags dropped, a tag joined by a dropped block cuts. At most three passes."""
    if greedy:
        tags = [(m.start(), m.end(), bool(m.group(1))) for m in _THINK_TAG.finditer(text)]
        first_open = next((t for t in tags if not t[2]), None)
        last_close = next((t for t in reversed(tags) if t[2]), None)
        if first_open and last_close and last_close[0] > first_open[0]:
            # Before the block only stray closers; after it no closer at all, so an
            # opener there is unclosed: both are handled by the walk below.
            text = text[:first_open[0]] + text[last_close[1]:]
    kept, pos, inside = [], 0, False
    for m in _THINK_TAG.finditer(text):
        if not inside:
            kept.append(text[pos:m.start()])  # the tag is dropped: a stray closer or an opener
            inside = not m.group(1)
        elif m.group(1):
            inside = False
        pos = m.end()
    if not inside:
        kept.append(text[pos:])
    out = "".join(kept)
    joined = _THINK_TAG.search(out)
    if joined:
        out = out[:joined.start()]
    return out.strip()


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


# Where a caller that passed `errors=` hears why there was no answer (see complete()).
_ERRORS: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "complete_errors", default=None)


def _note_error(text: str) -> None:
    sink = _ERRORS.get()
    if sink is not None:
        sink.append(str(text)[:300])


def _local_complete(messages, model, max_tokens, temperature, timeout,
                    allow_reasoning_fallback, caller) -> Optional[str]:
    """One non-streaming call against the ONE local llama server. Never starts it.

    None on an installation without local models (inference_profile = api): a server
    started before a switch may still be running until the switch has stopped it (or a
    stop failed), and no completion may run on it meanwhile."""
    import requests

    from vaf.core import inference_profile
    from vaf.core.config import Config
    if not inference_profile.local_inference_allowed():
        inference_profile.refuse(f"the local model ({caller})")
        return None

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
                # Why it stopped is read HERE rather than below, where it
                # already feeds a domain log: that log only fires when the
                # content came back empty, so a reply cut off mid-sentence left
                # no trace at all. This lane is free, but a free answer that
                # stops half way is still a broken one.
                _fin = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
                record_call("local", str(data.get("model") or "local"), _in, _out,
                            cache=cache_usage_from_openai(_u), finish_reason=_fin)
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
        from vaf.core.local_engine import reasoning_text
        reasoning = reasoning_text(msg).strip()
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
    except Exception as exc:
        _note_error(f"local server: {exc}")
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

    # In the caller's context (call_with_deadline): the call keeps its lane, account and
    # background run, which a bare pool worker would lose.
    from vaf.core.bounded_run import call_with_deadline

    try:
        text, error_text = call_with_deadline(_run, model, timeout=timeout, label="complete")
    except _cf.TimeoutError:
        print(f"[WARN] complete({caller}): timeout after {timeout}s")
        _note_error(f"timeout after {timeout}s")
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
            text, error_text = call_with_deadline(_run, fallback_model, timeout=timeout,
                                                  label="complete")
            if text is not None:
                return text or None
        except Exception:
            pass
    if error_text:
        print(f"[WARN] complete({caller}): backend error: {str(error_text)[:200]}")
        _note_error(error_text)
    return None


def complete(messages, *, provider: Optional[str] = None, model: Optional[str] = None,
             max_tokens: int = 512, temperature: float = 0.2,
             timeout: Optional[float] = None, strip_think: bool = True,
             allow_reasoning_fallback: bool = True,
             fallback_model: Optional[str] = None, backend=None,
             caller: str = "", errors: Optional[list] = None) -> Optional[str]:
    """One completion. Text or None - never an exception, never an error sentinel.

    ``messages`` is a string (wrapped as one user message) or a messages list.
    ``provider`` None resolves the configured one; ``model`` None lets the API manager
    resolve ``api_model_<provider>`` itself. ``backend`` reuses an existing
    APIBackendManager (the engine passes its own, which carries embedded caller keys
    and the event sink). ``timeout`` bounds the API wait (None = unbounded, the
    query_llm contract) and the local request (None = 120s).

    ``errors``: a list that hears WHY there was no answer - the provider's error, a
    timeout, a dead local server - so a caller can tell "the provider failed" from "the
    model answered nothing" (an empty answer adds nothing to it). The code audit needs
    that: it asks an empty answer again in smaller parts, which only repeats a failure.
    """
    # Label the lane from the caller this function is ALREADY given, so a tool
    # that reaches a model is counted under its own name instead of inheriting
    # whatever turn it happened to run inside. `caller` is "tool:<name>" from
    # BaseTool.query_llm and a plain lane elsewhere.
    from vaf.core.cost import usage_context as _usage_context

    _lane = (caller or "").strip() or None
    token = _ERRORS.set(errors) if errors is not None else None
    try:
        with _usage_context(lane=_lane):
            return _complete_inner(
                messages, provider=provider, model=model, max_tokens=max_tokens,
                temperature=temperature, timeout=timeout, strip_think=strip_think,
                allow_reasoning_fallback=allow_reasoning_fallback,
                fallback_model=fallback_model, backend=backend, caller=caller)
    finally:
        if token is not None:
            _ERRORS.reset(token)


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
