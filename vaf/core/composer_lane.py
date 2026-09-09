# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Composer's IO: its settings, the memory lookup, and the ONE tool-less call.

`vaf/core/composer.py` is pure and builds the prompt; this module is what a route
does with it. Every window that offers the Composer (mail, the messenger windows)
goes through here, so the containment lives in exactly one place:

ONE model call, NO TOOLS. That is the containment, not the prompt wording:
conversation text is attacker-controlled and the phishing scorer never reads
bodies, so an injected instruction must be unable to DO anything. With tools=None
the worst it can achieve is a bad draft, which the user reads before sending.
Anything added here that hands this lane a tool, an op, or a send breaks that
property - guarded by tests/test_mail_composer_guards.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

from vaf.core.config import Config, get_local_admin_scope_id
from vaf.core.cost import usage_context

logger = logging.getLogger("vaf.core.composer_lane")

#: Strong refs to in-flight generation tasks: the event loop keeps only weak task
#: references, so a task nothing else holds can be collected mid-stream.
INFLIGHT_TASKS: set = set()

#: How long to wait for a cold local model before giving up. A several-GB GGUF
#: maps from disk in tens of seconds on a cold cache; failing at 10s would just be
#: the old "not running" message with extra steps.
_LOCAL_MODEL_WAIT_S = 90


class LocalModelUnavailable(RuntimeError):
    """The local llama server could not be brought up for this request. Only raised
    after actually trying to load it - the client message is a last resort, not the
    first response to a cold model."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def settings() -> Dict[str, Any]:
    """The Composer's knobs. The `mail_composer_*` keys govern the Composer in
    every window it appears in: they were named when the mail window was the only
    one, and a second set of keys for the same lane would only let the two drift."""
    from vaf.core import composer
    return {
        "enabled": bool(Config.get("mail_composer_enabled", True)),
        "budget": composer.clamp_budget(Config.get("mail_composer_max_context_chars", 12000), 12000),
        "per_msg": max(200, int(Config.get("mail_composer_max_message_chars", 4000) or 4000)),
        "max_messages": max(1, int(Config.get("mail_composer_max_messages", 8) or 8)),
        "max_tokens": max(64, int(Config.get("mail_composer_max_output_tokens", 2500) or 2500)),
        "memory": bool(Config.get("mail_composer_memory_enabled", True)),
        "mailbox": bool(Config.get("mail_composer_mailbox_search_enabled", False)),
    }


def knowledge(user_scope_id: Optional[str], instruction: str, fallback: str = "", *,
              caller: str, chat_key: Optional[str] = None) -> str:
    """The user's own long-term memory, retrieved for this request.

    A messenger window may name the chat's own memory namespace (``chat_key``, the
    session id of that chat): the draft then also knows what the agent learned in that
    chat when it answered there before. The mail window never names one, so a mail
    draft can never reach a contact's namespace.

    The SAME lane the main agent and the voice agent use - `turn_memory_context`
    with the user's scope - and called the same way they call it: unconditionally,
    gated only on `memory_enabled`, with `memory_rag_k` (the main agent's own key)
    rather than a number invented here. A composer that only sometimes remembers
    who you are is worse than one that never does, because you cannot tell which
    run you got.

    Query: the user's instruction, which is their prompt for this turn, exactly as
    `task.input_text` is the main agent's. With no instruction it falls back to
    what the caller hands in (the subject of the mail being answered, the name of
    the chat), so drafting without typing anything still gets memory. That fallback
    is conversation-controlled text and therefore lets it influence WHICH of the
    user's memories are retrieved - a narrower version of what the main agent
    already does with any message it is handed. Deliberate; the
    `mail_composer_memory_enabled` switch turns the whole lane off.
    """
    if not Config.get("memory_enabled", True):
        return ""
    scope = (user_scope_id or "").strip() or get_local_admin_scope_id()
    if not scope:
        return ""                      # user isolation: never search unscoped
    query = (instruction or "").strip() or (fallback or "").strip()
    if not query:
        return ""
    try:
        from uuid import UUID

        from vaf.memory.rag import turn_memory_context
        return turn_memory_context(query, user_scope_id=UUID(str(scope)), caller=caller,
                                   chat_key=chat_key)
    except Exception as e:  # pragma: no cover - memory is optional infrastructure
        logger.info("composer: memory lookup unavailable: %s", e)
        return ""


def local_model_is_cold() -> bool:
    """True when this request will have to wait for a local model to load. Purely
    for telling the user WHY nothing is happening: a silent 90-second "writing ..."
    is indistinguishable from a hang."""
    if (Config.get("provider", "local") or "local").strip() != "local":
        return False
    try:
        import requests as _rq
        return _rq.get(f"{Config.get_llama_server_url()}/health", timeout=2).status_code != 200
    except Exception:
        return True


def ensure_local_model() -> None:
    """Ask the running agent to load the local model, the way every other
    non-chat lane does (`agent.load_model()` in automations, thinking runs and the
    headless runner). Best effort: if there is no agent instance (CLI-only, or the
    web app is still starting) we fall through to the health wait, which is also
    what happens when someone else is already loading it."""
    try:
        from vaf.core.web_interface import get_web_interface
        agent = getattr(get_web_interface(), "agent_instance", None)
        if agent is None or getattr(agent, "provider", "local") != "local":
            return
        logger.info("composer: local model not ready, requesting load")
        agent.load_model(skip_download_check=True)
    except Exception as e:  # pragma: no cover - never let this break the request
        logger.warning("composer: could not request a model load: %s", e)


def stream_completion(messages: List[Dict[str, str]], max_tokens: int, temperature: float,
                      *, lane: str = "mail") -> Iterator[str]:
    """Yield text chunks from one tool-less completion, booked on `lane`.

    Provider resolution mirrors the tool lane (vaf/tools/base.py query_llm) rather
    than inventing a second rule: provider from config, provider-specific model
    with a fallback to the generic one. Local mode streams from the single llama
    server - the same one everything else uses, never a second inference.
    """
    with usage_context(lane=lane):
        provider = (Config.get("provider", "local") or "local").strip()
        if provider != "local":
            model = Config.get(f"api_model_{provider}", "") or Config.get("model", "")
            from vaf.core.api_backend import APIBackendManager
            backend = APIBackendManager(provider)
            for chunk in backend.chat_completion(
                    messages=messages, temperature=temperature, max_tokens=max_tokens,
                    stream=True, model=model, tools=None, tool_choice=None):
                s = chunk if isinstance(chunk, str) else ""
                # metadata frames carry no prose; keep everything else verbatim
                if s.strip().startswith("{") and ("tool_calls" in s or "finish_reason" in s):
                    continue
                if s:
                    yield s
            return

        import json as _json

        import requests as _rq

        # Local mode: the single llama server may be down, or up but still mapping a
        # multi-GB model. Telling the user to go start it is not an answer - the chat
        # lane does not do that either, it just loads the model (automations, thinking
        # runs and the headless runner all call agent.load_model()). So: try to bring it
        # up ourselves, wait for /health, and only report a failure if that does not
        # work. The load is idempotent and reuses a healthy server.
        base = Config.get_llama_server_url()

        def _healthy() -> bool:
            try:
                return _rq.get(f"{base}/health", timeout=3).status_code == 200
            except _rq.RequestException:
                return False

        if not _healthy():
            ensure_local_model()
            # Weights map from disk; a cold start of a several-GB GGUF takes a while,
            # and answering "not running" ten seconds in would be the same unhelpful
            # message with extra steps.
            deadline = time.monotonic() + _LOCAL_MODEL_WAIT_S
            while time.monotonic() < deadline:
                if _healthy():
                    break
                time.sleep(1.5)
            else:
                raise LocalModelUnavailable("local_unavailable")

        payload = {
            "model": Config.get("model", ""), "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature, "stream": True,
            # Qwen-class local models otherwise spend the whole budget on reasoning
            # and return empty content (same fix as the tool and voice lanes).
            "chat_template_kwargs": {"enable_thinking": False},
        }
        with _rq.post(f"{base}/v1/chat/completions", json=payload,
                      stream=True, timeout=(10, 300)) as res:
            if res.status_code == 503:
                raise LocalModelUnavailable("local_loading")
            res.raise_for_status()
            # requests defaults text/* without an explicit charset to ISO-8859-1, which
            # turns every umlaut into mojibake ("moechte" arriving as two bytes shown as
            # "mA¶chte"). llama-server streams UTF-8; say so before decoding.
            res.encoding = "utf-8"
            for line in res.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = _json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                piece = delta.get("content") or ""
                if piece:
                    yield piece


async def sse_events(messages: List[Dict[str, str]], *, meta: Dict[str, Any],
                     max_tokens: int, temperature: float,
                     stream: Callable[..., Iterator[str]], log_name: str = "composer",
                     ) -> AsyncIterator[str]:
    """The Composer's response as SSE frames, the same in every window.

    Frames, in order: `meta` (what was read, so the panel can say so), an optional
    `notice` (`local_loading` while a cold local model maps from disk), then text
    frames, each carrying the FULL cleaned text so far, then `error` or `end`.
    `stream` is the route's chunk source (`stream_completion` bound to its usage
    lane; tests substitute it), run in a worker thread because provider IO never
    belongs on the event loop.

    Cumulative rather than delta frames, deliberately: clean_output has to see the
    whole buffer (a <think> block closes mid-stream, a code fence is only
    recognisable once its opening line is complete), so a delta would leak the
    scratchpad into the user's compose box and then try to take it back.
    """
    import json as _json

    from vaf.core import composer

    yield f"event: meta\ndata: {_json.dumps(meta)}\n\n"
    produced = False
    if await asyncio.to_thread(local_model_is_cold):
        yield f"event: notice\ndata: {_json.dumps('local_loading')}\n\n"
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _pump():
        try:
            for piece in stream(messages, max_tokens, temperature):
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", piece))
        except LocalModelUnavailable as e:
            logger.info("%s: local model not ready (%s)", log_name, e.code)
            loop.call_soon_threadsafe(queue.put_nowait, ("error", e.code))
        except Exception as e:                       # noqa: BLE001 - reported to the client
            logger.warning("%s: generation failed: %s", log_name, e)
            loop.call_soon_threadsafe(queue.put_nowait, ("error", "failed"))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", ""))

    task = asyncio.create_task(asyncio.to_thread(_pump))
    INFLIGHT_TASKS.add(task)
    task.add_done_callback(INFLIGHT_TASKS.discard)
    buffered = ""
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "done":
                # The stream ended without one usable frame. A thinking model that
                # spent the whole output budget on reasoning is the usual cause, and
                # the person must be told THAT rather than shown an empty box that
                # looks like a hang.
                if not produced and buffered.strip():
                    yield f"event: error\ndata: {_json.dumps('reasoning_only')}\n\n"
                break
            if kind == "error":
                yield f"event: error\ndata: {_json.dumps(payload)}\n\n"
                break
            buffered += payload
            cleaned = composer.clean_output(buffered)
            if cleaned and not reasoning_leaked(buffered, cleaned):
                produced = True
                yield f"data: {_json.dumps(cleaned)}\n\n"
    finally:
        task.cancel()
    yield "event: end\ndata: {}\n\n"


_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def reasoning_leaked(raw: str, cleaned: str) -> bool:
    """True when the text left after cleaning is the model's own scratchpad.

    Measured on the Veyllo gateway (a DeepSeek-dialect thinking model): when the
    output budget runs out INSIDE the reasoning, the gateway closes the reasoning
    and then sends that same reasoning once more as the answer content. The tags
    are stripped as designed, the copy is not, and a compose box full of "The user
    wants me to ..." was the live result. No request parameter switches the
    thinking off there (`enable_thinking`, `thinking.type=disabled` and
    `reasoning_effort=none` were all probed and ignored), so the leak is caught by
    shape: the answer equals, or is a prefix of, what stood inside the think block.
    """
    thoughts = " ".join(m.strip() for m in _THINK_BLOCK.findall(raw or "") if m.strip())
    if not thoughts or not cleaned:
        return False
    head = cleaned.strip()[:200]
    return len(head) >= 40 and thoughts.startswith(head)
