"""
VAF API Backend - Provider System
Implements structured, provider-specific interfaces for AI services.
Uses official SDKs (openai, anthropic, google-genai) for robust interaction.
"""

import os
import json
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Generator, List, Union
from vaf.core.config import Config
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
# ABSTRACT BASE PROVIDER
# ════───────────────────────────────────────────────────────────────────────────

class BaseAIProvider(ABC):
    """Abstract base class for all AI providers."""
    
    def __init__(self, provider_name: str, api_key: str):
        self.provider_name = provider_name
        self.api_key = api_key
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = {"input_tokens": 0, "output_tokens": 0}

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

# ═══════════════════════════════════════════════════════════════════════════════
# OPENAI PROVIDER (also used for DeepSeek & OpenRouter)
# ═══════════════════════════════════════════════════════════════════════════════

class OpenAIProvider(BaseAIProvider):
    """Provider for OpenAI-compatible APIs."""
    
    def __init__(self, provider_name: str, api_key: str, base_url: Optional[str] = None):
        super().__init__(provider_name, api_key)
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        except ImportError:
            self.client = None
            logger.error("OpenAI SDK not installed. Please run: pip install openai")

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        """OpenAI reasoning models (o1/o3/o4 series, gpt-5 family) reject `max_tokens`
        and a non-default `temperature` (only the default 1 is allowed). They require
        `max_completion_tokens` instead. Detect them so we can gate those params.

        Matches the o-series only at the start of the bare model name (after any
        `provider/` prefix) so `gpt-4o` / `gpt-4o-mini` are NOT misdetected.
        """
        m = (model or "").lower()
        if "gpt-5" in m:
            return True
        name = m.rsplit("/", 1)[-1]  # strip openrouter-style "openai/" prefix
        return name.startswith(("o1", "o3", "o4"))

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.client:
            yield "[Error] OpenAI SDK missing."
            return

        try:
            # Reasoning-param gating applies only to the DIRECT OpenAI API. OpenRouter
            # (same provider class, different base_url) normalizes around max_tokens for
            # every model — sending max_completion_tokens there can lose the token limit,
            # so let OpenRouter normalize. DeepSeek/local: ids never match o-series anyway.
            reasoning_model = self.provider_name == "openai" and self._is_reasoning_model(model)
            # Prepare arguments
            kwargs = {
                "model": model,
                "messages": messages,
                "stream": stream,
            }
            if reasoning_model:
                # o-series / gpt-5: use max_completion_tokens; omit temperature (only the
                # default is accepted). Sending max_tokens or temperature here -> HTTP 400.
                kwargs["max_completion_tokens"] = max_tokens
            else:
                kwargs["max_tokens"] = max_tokens
                kwargs["temperature"] = temperature
            if tools:
                kwargs["tools"] = tools
                if not reasoning_model:
                    # parallel_tool_calls isn't accepted by all reasoning models; the
                    # server-side default already allows parallel calls, so just omit it.
                    kwargs["parallel_tool_calls"] = True

                # tool_choice: 'auto' (default), 'none', 'required', or specific function
                if tool_choice:
                    kwargs["tool_choice"] = tool_choice
            
            if stream:
                # Enable usage for streaming (OpenAI specific)
                kwargs["stream_options"] = {"include_usage": True}
                
                # DeepSeek Reasoner & R1: output primarily in reasoning_content; must yield both
                is_reasoning = False
                response = self.client.chat.completions.create(**kwargs)
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
                            yield json.dumps({"finish_reason": chunk.choices[0].finish_reason})
                    
                    # Handle usage metadata (sent in last chunk)
                    if hasattr(chunk, 'usage') and chunk.usage:
                        self.usage["input_tokens"] += chunk.usage.prompt_tokens
                        self.usage["output_tokens"] += chunk.usage.completion_tokens
                        self.last_request_usage["input_tokens"] = chunk.usage.prompt_tokens
                        self.last_request_usage["output_tokens"] = chunk.usage.completion_tokens
                
                if is_reasoning:
                    yield "</think>"
            else:
                response = self.client.chat.completions.create(**kwargs)
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
                
                if response.usage:
                    self.usage["input_tokens"] += response.usage.prompt_tokens
                    self.usage["output_tokens"] += response.usage.completion_tokens
                    self.last_request_usage["input_tokens"] = response.usage.prompt_tokens
                    self.last_request_usage["output_tokens"] = response.usage.completion_tokens
                    
        except Exception as e:
            err_str = str(e)
            UI.error(f"{self.provider_name.upper()} Provider Error: {err_str}")
            try:
                from vaf.core.domain_log import append_domain_log
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
                use_cache = Config.get("anthropic_prompt_cache", True)
                use_cache = use_cache if isinstance(use_cache, bool) else \
                    str(use_cache).strip().lower() in ("1", "true", "yes", "on")
                if use_cache:
                    kwargs["system"] = [{
                        "type": "text", "text": system_msg,
                        "cache_control": {"type": "ephemeral"},
                    }]
                else:
                    kwargs["system"] = system_msg

            # 4. Adaptive thinking (config-gated, supported models only).
            thinking_on = Config.get("anthropic_thinking", True)
            thinking_on = thinking_on if isinstance(thinking_on, bool) else \
                str(thinking_on).strip().lower() in ("1", "true", "yes", "on")
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
                with self.client.messages.stream(**kwargs) as response:
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
                response = self.client.messages.create(**kwargs)
                for content_block in response.content:
                    if content_block.type == "thinking":
                        yield "<think>" + getattr(content_block, "thinking", "") + "</think>\n\n"
                    elif content_block.type == "text":
                        yield content_block.text
                yield from self._emit_final(response, thinking_active)

        except Exception as e:
            err_str = str(e)
            UI.error(f"Anthropic Provider Error: {err_str}")
            try:
                from vaf.core.domain_log import append_domain_log
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
        except Exception:
            pass

        stop_reason = getattr(final_msg, "stop_reason", None)
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
        um = getattr(resp, "usage_metadata", None)
        if not um:
            return
        inp = getattr(um, "prompt_token_count", 0) or 0
        out = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        self.usage["input_tokens"] += inp
        self.usage["output_tokens"] += out
        self.last_request_usage["input_tokens"] = inp
        self.last_request_usage["output_tokens"] = out

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
        thinking_on = Config.get("google_thinking", True)
        thinking_on = thinking_on if isinstance(thinking_on, bool) else \
            str(thinking_on).strip().lower() in ("1", "true", "yes", "on")
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
                for chunk in self.client.models.generate_content_stream(
                    model=model, contents=contents, config=config
                ):
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
                resp = self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
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
            UI.error(f"Google Provider Error: {err_str}")
            try:
                from vaf.core.domain_log import append_domain_log
                append_domain_log("backend", f"google_api_error: {err_str}")
            except Exception:
                pass
            yield f"[API Error from google: {err_str}]"

# ═══════════════════════════════════════════════════════════════════════════════
# FACTORY & MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class APIBackendManager:
    """Refactored Manager using provider-specific classes."""
    
    def __init__(self, provider: str):
        self.provider_name = provider
        self.config = Config.load()
        self.api_key = Config.get_api_key(provider)
        self.provider = self._create_provider()
        self.session_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = {"input_tokens": 0, "output_tokens": 0}

    def _create_provider(self) -> BaseAIProvider:
        # Local/Ollama provider doesn't need an API key
        if self.provider_name == "local":
            # OpenAI-compatible endpoint of the local server. Default to VAF's own llama-server
            # (port 8080, Docker/env-aware via get_llama_server_url) -- NOT Ollama's 11434, which is
            # nothing in a stock VAF install and made the browser agent fail with a connection error.
            # An explicit "local_api_url" (e.g. a real Ollama on :11434) still wins.
            local_url = Config.get("local_api_url", "") or Config.get_llama_server_url("/v1")
            return OpenAIProvider("local", "ollama", base_url=local_url)

        if not self.api_key:
            raise ValueError(f"API key missing for {self.provider_name}")

        if self.provider_name == "openai":
            return OpenAIProvider("openai", self.api_key)
        elif self.provider_name == "anthropic":
            return AnthropicProvider(self.api_key)
        elif self.provider_name == "google":
            return GoogleProvider(self.api_key)
        elif self.provider_name == "deepseek":
            return OpenAIProvider("deepseek", self.api_key, base_url="https://api.deepseek.com/v1")
        elif self.provider_name == "openrouter":
            return OpenAIProvider("openrouter", self.api_key, base_url="https://openrouter.ai/api/v1")
        else:
            raise ValueError(f"Unsupported provider: {self.provider_name}")

    def chat_completion(self, messages, temperature=0.7, max_tokens=4096, stream=True, model=None, tools=None, tool_choice=None):
        """Unified entry point for chat completion.
        
        Args:
            tool_choice: Control tool usage - 'auto' (default), 'none', 'required', 
                        or {'type': 'function', 'function': {'name': '...'}} for specific tool
        """
        # Determine model — defaults derive from Config.PROVIDER_MODELS (single source).
        default_models = {p: m["default"] for p, m in Config.PROVIDER_MODELS.items()}
        default_models["local"] = "llama3"
        if not model:
            # Read fresh from disk so mid-session model changes (via Settings) take effect immediately
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

        # Execute via provider
        for chunk in self.provider.chat_completion(messages, temperature, max_tokens, stream, model, tools, tool_choice):
            # Sync usage stats back to manager
            self.session_usage["input_tokens"] = self.provider.usage["input_tokens"]
            self.session_usage["output_tokens"] = self.provider.usage["output_tokens"]
            self.last_request_usage["input_tokens"] = self.provider.last_request_usage["input_tokens"]
            self.last_request_usage["output_tokens"] = self.provider.last_request_usage["output_tokens"]
            yield chunk

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
                resp = _req.get(
                    "https://openrouter.ai/api/v1/models",
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

        # Static table — substring match
        for pattern, ctx in APIBackendManager._CTX_TABLE:
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
        selection to consider the strongest AVAILABLE model, not only the configured one."""
        import requests
        from vaf.core.config import Config
        try:
            key = Config.get_api_key(provider)
        except Exception:
            key = ""
        if not key:
            return []
        try:
            if provider == "openai":
                r = requests.get("https://api.openai.com/v1/models",
                                 headers={"Authorization": f"Bearer {key}"}, timeout=10)
                if r.status_code == 200:
                    return sorted(m["id"] for m in r.json().get("data", [])
                                  if any(x in m["id"] for x in ("gpt", "o1", "o3", "o4")))
            elif provider == "anthropic":
                r = requests.get("https://api.anthropic.com/v1/models",
                                 headers={"X-Api-Key": key, "anthropic-version": "2023-06-01"}, timeout=10)
                if r.status_code == 200:
                    return [m["id"] for m in r.json().get("data", []) if m.get("id")]
            elif provider == "deepseek":
                r = requests.get("https://api.deepseek.com/models",
                                 headers={"Authorization": f"Bearer {key}"}, timeout=10)
                if r.status_code == 200:
                    return [m["id"] for m in r.json().get("data", []) if m.get("id")]
            elif provider == "google":
                r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                                 params={"key": key, "pageSize": 1000}, timeout=10)
                if r.status_code == 200:
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
                r = requests.get("https://openrouter.ai/api/v1/models",
                                 headers={"Authorization": f"Bearer {key}"}, timeout=10)
                if r.status_code == 200:
                    return [m["id"] for m in r.json().get("data", []) if m.get("id")][:50]
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
