"""
VAF API Backend - Provider System
Implements structured, provider-specific interfaces for AI services.
Uses official SDKs (openai, anthropic, google-generativeai) for robust interaction.
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

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.client:
            yield "[Error] OpenAI SDK missing."
            return

        try:
            # Prepare arguments
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": stream,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["parallel_tool_calls"] = True  # Allow multiple tools in one response
                
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
            UI.error(f"{self.provider_name.upper()} Provider Error: {e}")
            yield ""

# ═══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC PROVIDER
# ═══════════════════════════════════════════════════════════════════════════════

class AnthropicProvider(BaseAIProvider):
    """Provider for Anthropic Claude models."""
    
    def __init__(self, api_key: str):
        super().__init__("anthropic", api_key)
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=api_key)
        except ImportError:
            self.client = None
            logger.error("Anthropic SDK not installed. Please run: pip install anthropic")

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.client:
            yield "[Error] Anthropic SDK missing."
            return

        # Convert format: extract system message
        system_msg = ""
        filtered_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                filtered_messages.append(m)

        try:
            kwargs = {
                "model": model,
                "messages": filtered_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": stream,
            }
            if system_msg:
                kwargs["system"] = system_msg
            if tools:
                # Convert OpenAI tools to Anthropic format
                anthropic_tools = []
                for t in tools:
                    if t["type"] == "function":
                        func = t["function"]
                        anthropic_tools.append({
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                        })
                kwargs["tools"] = anthropic_tools

            if stream:
                with self.client.messages.stream(**kwargs) as response:
                    for text in response.text_stream:
                        yield text
                    
                    # Finalize usage stats
                    final_msg = response.get_final_message()
                    self.usage["input_tokens"] += final_msg.usage.input_tokens
                    self.usage["output_tokens"] += final_msg.usage.output_tokens
                    self.last_request_usage["input_tokens"] = final_msg.usage.input_tokens
                    self.last_request_usage["output_tokens"] = final_msg.usage.output_tokens
                    
                    # Handle tool use if any
                    for tool_use in response.get_final_message().content:
                        if hasattr(tool_use, 'type') and tool_use.type == "tool_use":
                            yield json.dumps({"tool_use": tool_use.model_dump()})
            else:
                response = self.client.messages.create(**kwargs)
                for content_block in response.content:
                    if content_block.type == "text":
                        yield content_block.text
                    elif content_block.type == "tool_use":
                        yield json.dumps({"tool_use": content_block.model_dump()})
                
                self.usage["input_tokens"] += response.usage.input_tokens
                self.usage["output_tokens"] += response.usage.output_tokens
                self.last_request_usage["input_tokens"] = response.usage.input_tokens
                self.last_request_usage["output_tokens"] = response.usage.output_tokens
                
        except Exception as e:
            UI.error(f"Anthropic Provider Error: {e}")
            yield ""

# ═══════════════════════════════════════════════════════════════════════════════
# GOOGLE GEMINI PROVIDER
# ═══════════════════════════════════════════════════════════════════════════════

class GoogleProvider(BaseAIProvider):
    """Provider for Google Gemini models."""
    
    def __init__(self, api_key: str):
        super().__init__("google", api_key)
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            self.sdk = genai
        except ImportError:
            self.sdk = None
            logger.error("Google GenerativeAI SDK missing. Please run: pip install google-generativeai")

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice=None):
        if not self.sdk:
            yield "[Error] Google GenerativeAI SDK missing."
            return

        try:
            # Convert messages to Gemini format
            contents = []
            system_instruction = None
            for m in messages:
                if m["role"] == "system":
                    system_instruction = m["content"]
                else:
                    role = "user" if m["role"] == "user" else "model"
                    contents.append({"role": role, "parts": [m["content"]]})

            # Configure tools
            google_tools = None
            if tools:
                google_tools = []
                for t in tools:
                    if t["type"] == "function":
                        func = t["function"]
                        # Deep copy and sanitize parameters
                        params = json.loads(json.dumps(func.get("parameters", {"type": "object"})))
                        if "additionalProperties" in params: del params["additionalProperties"]
                        
                        google_tools.append({
                            "function_declarations": [{
                                "name": func["name"],
                                "description": func.get("description", ""),
                                "parameters": params
                            }]
                        })

            client = self.sdk.GenerativeModel(
                model_name=model,
                system_instruction=system_instruction,
                tools=google_tools
            )

            config = self.sdk.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens
            )

            if stream:
                response = client.generate_content(contents, generation_config=config, stream=True)
                for chunk in response:
                    if chunk.text:
                        yield chunk.text
                    
                    # Handle function calls
                    for part in chunk.candidates[0].content.parts:
                        if hasattr(part, 'function_call') and part.function_call:
                            # Convert to OpenAI compatible format
                            yield json.dumps({
                                "tool_calls": [{
                                    "index": 0, # Since Gemini streams one candidate part at a time usually, but for parallel safe to expect one-by-one or handle list
                                    "id": f"call_{part.function_call.name}",
                                    "type": "function",
                                    "function": {
                                        "name": part.function_call.name,
                                        "arguments": json.dumps({k: v for k, v in part.function_call.args.items()})
                                    }
                                }]
                            })
                
                # Usage stats
                usage = response.usage_metadata
                self.usage["input_tokens"] += usage.prompt_token_count
                self.usage["output_tokens"] += usage.candidates_token_count
                self.last_request_usage["input_tokens"] = usage.prompt_token_count
                self.last_request_usage["output_tokens"] = usage.candidates_token_count
            else:
                response = client.generate_content(contents, generation_config=config)
                if response.text:
                    yield response.text
                
                # Stats
                usage = response.usage_metadata
                self.usage["input_tokens"] += usage.prompt_token_count
                self.usage["output_tokens"] += usage.candidates_token_count
                self.last_request_usage["input_tokens"] = usage.prompt_token_count
                self.last_request_usage["output_tokens"] = usage.candidates_token_count
                
        except Exception as e:
            UI.error(f"Google Provider Error: {e}")
            yield ""

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
            # Use OpenAI-compatible provider for Ollama
            local_url = Config.get("local_api_url", "http://localhost:11434/v1")
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
        # Determine model
        default_models = {
            "openai": "gpt-4o",
            "anthropic": "claude-3-5-sonnet-20241022",
            "deepseek": "deepseek-chat",
            "google": "gemini-1.5-flash",
            "openrouter": "anthropic/claude-3.5-sonnet",
            "local": "llama3",
        }
        if not model:
            model = self.config.get(f"api_model_{self.provider_name}", default_models.get(self.provider_name, "gpt-4o"))
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
        # Anthropic – all current models share 200 K
        ("claude",          200_000),
        # Google
        ("gemini-2.5",    1_048_576),
        ("gemini-2.0",    1_048_576),
        ("gemini-1.5-pro",2_097_152),
        ("gemini-1.5",    1_048_576),
        ("gemini",        1_048_576),
        # DeepSeek — deepseek-chat → V4-Flash, deepseek-reasoner → V4-Flash (thinking)
        # All current models: 1M input context, 384K max output
        ("deepseek-v4",   1_000_000),
        ("deepseek-chat", 1_000_000),
        ("deepseek-reasoner", 1_000_000),
        ("deepseek-coder",   128_000),
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
        """Legacy static list for UI dropdowns (can be extended to use providers)."""
        models = {
            "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
            "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229"],
            "deepseek": ["deepseek-chat", "deepseek-coder", "deepseek-reasoner"],
            "google": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash-exp"],
            "openrouter": ["anthropic/claude-3.5-sonnet", "openai/gpt-4o"],
            "local": ["llama3", "mistral", "codellama"]
        }
        return models.get(provider, [])

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
