# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Literal

class BaseTool(ABC):
    """
    Blueprint for all VAF tools.
    Every new tool MUST inherit from this class.
    
    Example for a new tool (my_tool.py):
    -----------------------------------------
    from vaf.tools.base import BaseTool

    class MyTool(BaseTool):
        name        = "my_super_tool"
        description = "Does cool things"

        # Declarative contract — set these on every tool
        permission_level  = "read"   # "read" | "write" | "dangerous" | "system"
        side_effect_class = "none"   # "none" | "reversible" | "irreversible"
        channel_restrictions = ()    # e.g. ("telegram", "whatsapp") to block chat channels
        admin_only = False           # True → blocked for non-admin users entirely

        # 1–3 concrete examples shown to the LLM (provider-agnostic, embedded in description)
        input_examples = [
            {"input": "hello world"},
        ]

        # Restrict to Coder Sub-Agent only (omit or set False for Main Agent)
        # coder_only = True

        parameters = {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Input text"}
            },
            "required": ["input"]
        }

        def run(self, **kwargs):
            input_text = kwargs.get("input", "")
            return f"Processed: {input_text}"
    -----------------------------------------

    The tool will be automatically discovered and loaded!
    """
    
    # ═══════════════════════════════════════════════════════════════════════════
    # REQUIRED ATTRIBUTES
    # ═══════════════════════════════════════════════════════════════════════════
    
    name: str = "base_tool"       # The name used by the model (e.g., "web_search")
    description: str = "Description" # Explanation for the model regarding what the tool does
    
    # ═══════════════════════════════════════════════════════════════════════════
    # OPTIONAL ATTRIBUTES
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Set to True to make this tool available ONLY to the Coder Sub-Agent
    # Useful for: file operations, shell commands, code-specific tools
    coder_only: bool = False

    # JSON Schema for parameters (optional but recommended).
    # Validated at dispatch: common weak-model shape mistakes are repaired before
    # run() is called; `content` / `code` fields are passed through verbatim.
    # See docs/agents/TOOL_INPUT_REPAIR.md.
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": []
    }

    # Optional: 1–3 example calls that illustrate correct usage.
    # Each entry is a dict of parameter name → value (matching the parameters schema).
    # These are injected into the description so every provider/backend benefits.
    # Leave empty (default) to skip.
    #
    # Example:
    #   input_examples: List[Dict[str, Any]] = [
    #       {"query": "current weather in Berlin"},
    #       {"query": "population of Tokyo", "language": "de"},
    #   ]
    input_examples: List[Dict[str, Any]] = []

    # ── Declarative tool contract ────────────────────────────────────────────
    #
    # permission_level controls the confirmation gate in execute_tool():
    #   "read"      — no confirmation needed (default, safe operations)
    #   "write"     — no confirmation by default, but the action changes state
    #   "dangerous" — always prompts the user before execution
    #   "system"    — internal / agent-only tool; skips the confirmation gate
    #                 entirely even when the legacy risky-tool list would gate it.
    #                 Use for tools the agent calls as part of its own plumbing
    #                 (e.g. memory updates, context tools) where a user prompt
    #                 would be disruptive and the action is already controlled.
    permission_level: Literal["read", "write", "dangerous", "system"] = "read"

    # channel_restrictions: sources where this tool is completely blocked.
    # Common values: "telegram", "whatsapp", "discord", "channel" (any chat).
    # The check runs before the tool executes — no confirmation, hard block.
    channel_restrictions: tuple[str, ...] = ()

    # input_aliases: {canonical_param: [synonyms]}. Weak models often use a
    # synonym for a parameter name (write_file: file_path -> path,
    # message -> content). The input-repair layer (R0, see
    # docs/agents/TOOL_INPUT_REPAIR.md) remaps a present synonym to the
    # canonical name before dispatch. Deliberately NOT a JSON-schema keyword:
    # kept off the model-facing `parameters` so a strict provider (Google
    # Gemini) can never reject the whole tool over an unknown schema field.
    input_aliases: Dict[str, List[str]] = {}

    # side_effect_class: impact classification shown in the confirmation prompt.
    #   "none"        — read-only, no external state changed
    #   "reversible"  — state changed but can be undone
    #   "irreversible"— cannot be undone (e.g. sending an email, deleting a file)
    side_effect_class: Literal["none", "reversible", "irreversible"] = "none"

    # whare_wananga_prereqs: tool names that must run FIRST to set up the state this tool needs
    # (e.g. a plan). During Whare Wananga training these prerequisites are added to the (otherwise
    # class-scoped) sandbox and the agent runs them once before probing this tool, so a tool that
    # "wants a plan first" can actually be exercised. Empty = no prerequisites.
    whare_wananga_prereqs: tuple[str, ...] = ()

    # admin_only: when True the tool is blocked for non-admin users at the
    # execute_tool() level — the agent simply cannot call it during a session
    # with a regular user.  This is checked via the session's user_scope_id /
    # user_role, NOT via channel_restrictions (which is channel-based, not
    # role-based).
    #
    # Use this for tools that must never run on behalf of a regular user —
    # e.g. create_agent_tool, which lets the agent write arbitrary Python code
    # to disk.  An admin has explicitly elevated trust; a regular user has not.
    admin_only: bool = False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ABSTRACT METHOD
    # ═══════════════════════════════════════════════════════════════════════════
    
    @abstractmethod
    def run(self, **kwargs) -> str:
        """
        The main execution logic.
        The model calls this function.
        
        Arguments:
            kwargs: Parameters from the model (e.g., query="Berlin")
            
        Returns:
            str: The result string that the model reads.
        """
        pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # UTILITY METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def query_llm(self, messages, max_tokens=300, temperature=0.7, timeout: int = None,
                  provider: str = None, model: str = None,
                  allow_reasoning_fallback: bool = True) -> Optional[str]:
        """
        Unified LLM query method for tools. 
        Supports both API providers and Local mode automatically.
        
        Args:
            messages: List of message dicts {"role": "...", "content": "..."}
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            
        Returns:
            Optional[str]: Generated text or None if failed
        """
        from vaf.core.config import Config
        import requests
        import json
        
        config = Config.load()
        # Explicit provider/model override (e.g. the Whare Wananga teacher): force this exact
        # provider+model, bypassing the configured main model. No global Config mutation.
        _provider_override, _model_override = provider, model
        if _provider_override:
            provider = _provider_override
            if provider != "local":
                model = _model_override or config.get(f"api_model_{provider}", "") or config.get("model", "")
            else:
                model = _model_override or config.get("model", "")
        else:
            provider = config.get("provider", "local")
            # FIX: Use correct model ID for the selected provider
            # (Previously it used 'model' which is the LOCAL model ID, causing 400 errors with APIs)
            if provider != "local":
                model = config.get(f"api_model_{provider}", "")
                # Fallback to generic model setting if provider-specific is empty
                if not model:
                   model = config.get("model", "")
                # Hybrid mode: VAF_TOOL_MODEL overrides the model for tools/sub-agents during workflow runs
                import os as _os
                _tool_model = _os.environ.get("VAF_TOOL_MODEL", "").strip()
                if _tool_model:
                    model = _tool_model
            else:
                # Local provider uses the main model setting
                model = config.get("model", "")
        
        # 0. Validate Model
        if not model:
            # Fallback: Try to get default based on provider
            if provider == "openai": model = "gpt-4o"
            elif provider == "anthropic": model = "claude-sonnet-4-6"
            elif provider == "google": model = "gemini-2.5-flash"
            
            if not model:
                print(f"[WARN] Tool {self.name}: No model configured. Skipping LLM query.")
                return None
        
        # 1. API Backend Mode
        if provider != "local":
            def _execute_query(target_model):
                from vaf.core.api_backend import APIBackendManager
                backend = APIBackendManager(provider)
                response_text = ""
                for chunk in backend.chat_completion(
                    messages=messages, 
                    temperature=temperature, 
                    max_tokens=max_tokens, 
                    stream=True, 
                    model=target_model
                ):
                    # Only skip metadata chunks (tool_calls, finish_reason), NOT actual content.
                    # Document agent and other tools often request JSON - that content must be kept.
                    strip_chunk = chunk.strip()
                    if strip_chunk.startswith("{") and (
                        "tool_calls" in chunk or "tool_use" in chunk or "finish_reason" in chunk
                    ):
                        continue
                    response_text += chunk
                return response_text.strip()

            import concurrent.futures as _cf
            _executor = _cf.ThreadPoolExecutor(max_workers=1)
            _fut = _executor.submit(_execute_query, model)
            try:
                return _fut.result(timeout=timeout)
            except _cf.TimeoutError:
                print(f"[WARN] Tool {self.name}: query_llm timeout after {timeout}s")
                return None
            except Exception as e:
                err_str = str(e).lower()
                # Self-Healing: If Invalid Model (400) or Model Not Found (404), try fallback
                if "400" in err_str or "404" in err_str or "invalid model" in err_str:
                    fallback = "gpt-4o" if provider == "openai" else ("claude-sonnet-4-6" if provider == "anthropic" else "gemini-2.5-flash")
                    if fallback and fallback != model:
                        print(f"[WARN] Tool {self.name}: API Error with model '{model}'. Retrying with fallback '{fallback}'...")
                        _fut2 = _executor.submit(_execute_query, fallback)
                        try:
                            return _fut2.result(timeout=timeout)
                        except _cf.TimeoutError:
                            print(f"[WARN] Tool {self.name}: query_llm fallback timeout after {timeout}s")
                            return None
                        except Exception as e2:
                            print(f"[ERROR] Tool {self.name}: Fallback failed too: {e2}")
                            return None

                print(f"[ERROR] Tool {self.name}: Query failed: {e}")
                return None
            finally:
                _executor.shutdown(wait=False)
        
        # 2. Local Server Mode (Fallback)
        try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                # Qwen-class local models burn the whole (small) token budget
                # on reasoning_content and return EMPTY content for these
                # utility calls (observed live: finish_reason=length,
                # reasoning_len=2691, max_tokens=600 on web_search summaries).
                # Same fix as the voice lane: disable thinking for tool-side
                # utility completions; non-thinking templates ignore the flag.
                "chat_template_kwargs": {"enable_thinking": False},
            }
            res = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=payload,
                timeout=(timeout or 120),  # honour the caller's timeout (was hardcoded 60s)
            )
            if res.status_code == 200:
                data = res.json()
                if "choices" in data and len(data["choices"]) > 0:
                    choice = data["choices"][0] or {}
                    msg = choice.get("message", {}) or {}
                    content = (msg.get("content") or "").strip()
                    if content:
                        return content
                    # Reasoning models (e.g. qwen) put the chain-of-thought in reasoning_content and the
                    # final answer in content. If generation was cut off while still reasoning
                    # (finish_reason="length"), content is empty even though the model produced plenty.
                    # Log the cause, then fall back to the reasoning text -- it already holds the substance
                    # -- instead of returning nothing.
                    # EXCEPTION: callers generating long-form output (e.g. research report
                    # sections) must NEVER receive chain-of-thought as the answer — a run
                    # once filled every report section with "Thinking Process: ...". They
                    # pass allow_reasoning_fallback=False and handle the empty return via
                    # their own retry paths.
                    if not allow_reasoning_fallback:
                        return None
                    reasoning = (msg.get("reasoning_content") or "").strip()
                    try:
                        from vaf.core.log_helper import append_domain_log
                        append_domain_log(
                            "backend",
                            f"query_llm({self.name}) empty content: finish_reason={choice.get('finish_reason')} "
                            f"reasoning_len={len(reasoning)} max_tokens={max_tokens}",
                        )
                    except Exception:
                        pass
                    if reasoning:
                        return reasoning
            return None
        except Exception:
            return None

    def _stream_local_completion(self, messages, max_tokens: int, temperature: float = 0.2,
                                 idle_timeout: int = 75, on_progress=None) -> str:
        """Stream a completion from the LOCAL llama server with an IDLE timeout.

        Same proven mechanism the research agent uses for sections: a reasoning model
        may think as long as tokens keep flowing (reasoning deltas keep the connection
        alive = progress) but its chain-of-thought is NEVER collected — only `content`
        is returned. A fixed total timeout would kill legitimate long reasoning and leave
        the orphaned request occupying the single server slot. Returns '' on any failure.
        """
        import requests
        import json
        from vaf.core.config import Config
        body = {
            "model": Config.get("model", "") or "user-model",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        parts = []
        try:
            with requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=body, stream=True, timeout=(10, idle_timeout),
            ) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines():
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                    if not line or not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        break
                    try:
                        delta = (json.loads(d).get("choices") or [{}])[0].get("delta") or {}
                    except Exception:
                        continue
                    piece = delta.get("content") or ""
                    if piece:
                        parts.append(piece)
                        if on_progress:
                            try:
                                on_progress("".join(parts))
                            except Exception:
                                pass
                    # reasoning_content deltas keep the connection alive but are not collected.
        except Exception as e:
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"{self.name} stream ended: {type(e).__name__}: {str(e)[:120]}")
            except Exception:
                pass
        return "".join(parts).strip()

    def generate_text(self, messages, max_tokens: int, temperature: float = 0.2,
                      idle_timeout: int = 75, api_timeout: int = 240, on_progress=None) -> str:
        """Provider-aware generation that NEVER returns chain-of-thought.

        Local provider streams (reasoning models may think as long as tokens flow); API
        providers use `query_llm(allow_reasoning_fallback=False)` under a wall-clock guard.
        Returns '' on failure/timeout so the caller runs its own fallback. Use a GENEROUS
        `max_tokens` (e.g. 8192): a reasoning model needs room to finish thinking AND emit
        the answer — a tight budget cuts it off mid-reasoning, leaving empty content.
        """
        from vaf.core.config import Config
        if (Config.get("provider", "local") or "local").strip().lower() == "local":
            return self._stream_local_completion(messages, max_tokens, temperature,
                                                 max(15, idle_timeout), on_progress)
        import threading
        holder = {"content": ""}

        def _worker():
            try:
                holder["content"] = self.query_llm(
                    messages=messages, max_tokens=max_tokens, temperature=temperature,
                    allow_reasoning_fallback=False,
                ) or ""
            except Exception:
                holder["content"] = ""

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=max(10, api_timeout))
        if t.is_alive():
            return ""
        return str(holder.get("content") or "").strip()

    @staticmethod
    def sanitize_model_text(raw: str) -> str:
        """Strip <think>…</think> blocks and markdown fences, and reject output that is
        pure chain-of-thought (returns '' so the caller's fallback fires). Mirrors the
        research agent's reasoning-leak guard so a reasoning model's thoughts never end up
        in generated content. JSON/structured output (starts with '{' or '[') is kept."""
        if not raw:
            return ""
        import re as _re
        text = _re.sub(r'<think>[\s\S]*?</think>', '', raw).strip()
        text = _re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = _re.sub(r'\s*```\s*$', '', text).strip()
        if not text:
            return ""
        if text[0] in '{[':
            return text  # structured output — not chain-of-thought
        low = text.lower()
        reasoning_markers = (
            'thinking process', 'okay,', 'okay ', 'let me', "let's", 'alright',
            'the user', 'we need', 'i need ', 'hmm', '1. **analyze',
        )
        if any(low.startswith(mk) for mk in reasoning_markers):
            return ""
        return text

    def get_description_with_examples(self) -> str:
        """Return description with optional input_examples appended as text.

        Provider-agnostic: examples are embedded in the description string so
        every backend (OpenAI, Anthropic, Google, local) sees them without
        requiring special API fields.
        """
        desc = self.description or ""
        examples = getattr(self, "input_examples", None) or []
        if not examples:
            return desc
        import json
        lines = [desc, "", "Examples:"]
        for ex in examples[:3]:  # cap at 3 to avoid bloat
            try:
                lines.append(f"  {self.name}({json.dumps(ex, ensure_ascii=False)})")
            except Exception:
                pass
        return "\n".join(lines)

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool schema for the model (description includes examples)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.get_description_with_examples(),
                "parameters": self.parameters
            }
        }

    def get_contract_metadata(self) -> Dict[str, Any]:
        """Return declarative tool metadata for central policy checks."""
        return {
            "name": self.name,
            "permission_level": getattr(self, "permission_level", "read"),
            "channel_restrictions": list(getattr(self, "channel_restrictions", []) or []),
            "side_effect_class": getattr(self, "side_effect_class", "none"),
        }

    def __repr__(self) -> str:
        return f"<Tool: {self.name}>"


def format_tool_signature(tool, max_chars: int = 180) -> str:
    """Compact call signature from a tool's JSON-schema parameters.

    Shape: ``name(req: type, req2: type, [opt: type])`` - required parameters
    first, optional ones bracketed. Used by search_tools so a discovered tool
    is callable without guessing parameters. Returns "" when the tool declares
    no name or no properties. Fail-safe: never raises (callers sit on the tool
    dispatch path).
    """
    try:
        name = getattr(tool, "name", "") or ""
        params = getattr(tool, "parameters", None) or {}
        props = params.get("properties") or {}
        if not name or not isinstance(props, dict) or not props:
            return ""
        required = [p for p in (params.get("required") or []) if p in props]
        optional = [p for p in props if p not in required]
        parts = [f"{p}: {(props[p] or {}).get('type', 'any')}" for p in required]
        parts += [f"[{p}: {(props[p] or {}).get('type', 'any')}]" for p in optional]
        sig = f"{name}({', '.join(parts)})"
        if len(sig) > max_chars:
            sig = sig[:max_chars - 4] + "...)"
        return sig
    except Exception:
        return ""
