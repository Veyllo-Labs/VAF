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

    # JSON Schema for parameters (optional but recommended)
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

    # side_effect_class: impact classification shown in the confirmation prompt.
    #   "none"        — read-only, no external state changed
    #   "reversible"  — state changed but can be undone
    #   "irreversible"— cannot be undone (e.g. sending an email, deleting a file)
    side_effect_class: Literal["none", "reversible", "irreversible"] = "none"

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
    
    def query_llm(self, messages, max_tokens=300, temperature=0.7) -> Optional[str]:
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
            elif provider == "anthropic": model = "claude-3-5-sonnet-20240620"
            elif provider == "google": model = "gemini-1.5-pro"
            
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

            try:
                return _execute_query(model)
            except Exception as e:
                err_str = str(e).lower()
                # Self-Healing: If Invalid Model (400) or Model Not Found (404), try fallback
                if "400" in err_str or "404" in err_str or "invalid model" in err_str:
                    fallback = "gpt-4o" if provider == "openai" else ("claude-3-5-sonnet-20240620" if provider == "anthropic" else "gemini-1.5-pro")
                    if fallback and fallback != model:
                        print(f"[WARN] Tool {self.name}: API Error with model '{model}'. Retrying with fallback '{fallback}'...")
                        try:
                            return _execute_query(fallback)
                        except Exception as e2:
                            print(f"[ERROR] Tool {self.name}: Fallback failed too: {e2}")
                            return None
                
                print(f"[ERROR] Tool {self.name}: Query failed: {e}")
                return None
        
        # 2. Local Server Mode (Fallback)
        try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            res = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=payload,
                timeout=60
            )
            if res.status_code == 200:
                data = res.json()
                if "choices" in data and len(data["choices"]) > 0:
                    return data["choices"][0]["message"].get("content", "").strip()
            return None
        except Exception:
            return None

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
