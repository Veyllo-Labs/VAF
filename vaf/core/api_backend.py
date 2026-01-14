"""
API Backend Manager for VAF
Supports multiple AI providers: OpenAI, Anthropic, DeepSeek, Google AI Studio, OpenRouter

Best Practices Implemented:
- Unified interface for all providers
- Streaming support
- Proper error handling
- Timeout management
- Rate limit awareness
"""

import os
import json
import requests
from typing import Optional, Dict, Any, Generator, List
from vaf.core.config import Config
from vaf.cli.ui import UI


class APIBackendManager:
    """
    Unified API backend manager for multiple AI providers.
    Converts between different API formats to provide a consistent interface.
    """
    
    PROVIDER_CONFIGS = {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "chat_endpoint": "/chat/completions",
            "header_key": "Authorization",
            "header_format": "Bearer {api_key}",
            "default_model": "gpt-4o",
            "supports_streaming": True,
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com/v1",
            "chat_endpoint": "/messages",
            "header_key": "x-api-key",
            "header_format": "{api_key}",
            "default_model": "claude-3-5-sonnet-20241022",
            "version_header": "2023-06-01",
            "supports_streaming": True,
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com/v1",
            "chat_endpoint": "/chat/completions",
            "header_key": "Authorization",
            "header_format": "Bearer {api_key}",
            "default_model": "deepseek-chat",
            "supports_streaming": True,
        },
        "google": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "chat_endpoint": "/models/{model}:generateContent",
            "header_key": "x-goog-api-key",
            "header_format": "{api_key}",
            "default_model": "gemini-1.5-flash",  # Free tier, fast & capable
            "supports_streaming": True,
            "stream_endpoint": "/models/{model}:streamGenerateContent",
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "chat_endpoint": "/chat/completions",
            "header_key": "Authorization",
            "header_format": "Bearer {api_key}",
            "default_model": "anthropic/claude-3.5-sonnet",
            "supports_streaming": True,
        },
    }
    
    def __init__(self, provider: str):
        """
        Initialize API backend for a specific provider.
        
        Args:
            provider: Provider name (openai, anthropic, deepseek, google, openrouter)
            
        Raises:
            ValueError: If provider is unsupported or API key is missing
        """
        self.provider = provider
        self.config = Config.load()
        
        if provider not in self.PROVIDER_CONFIGS:
            raise ValueError(f"Unsupported provider: {provider}. Supported: {list(self.PROVIDER_CONFIGS.keys())}")
        
        self.provider_config = self.PROVIDER_CONFIGS[provider]
        
        # Get API key using secure method
        self.api_key = Config.get_api_key(provider)
        
        if not self.api_key:
            raise ValueError(f"API key not set for provider: {provider}. Use settings menu to configure.")
    
    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = True,
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
    ) -> Generator[str, None, None]:
        """
        Unified chat completion method supporting streaming.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            stream: Enable streaming response
            model: Override default model
            tools: Tool definitions for function calling (if supported)
            
        Yields:
            Text chunks from the API response
        """
        if not model:
            model = self.config.get(f"api_model_{self.provider}", self.provider_config["default_model"])
        
        # Route to provider-specific implementation
        if self.provider == "anthropic":
            yield from self._anthropic_chat(messages, temperature, max_tokens, stream, model, tools)
        elif self.provider == "google":
            yield from self._google_chat(messages, temperature, max_tokens, stream, model, tools)
        else:
            # OpenAI-compatible (OpenAI, DeepSeek, OpenRouter)
            yield from self._openai_compatible_chat(messages, temperature, max_tokens, stream, model, tools)
    
    def _openai_compatible_chat(
        self, 
        messages: List[Dict], 
        temperature: float, 
        max_tokens: int, 
        stream: bool, 
        model: str,
        tools: Optional[List[Dict]] = None
    ) -> Generator[str, None, None]:
        """
        OpenAI-compatible API format (OpenAI, DeepSeek, OpenRouter)
        """
        url = self.provider_config["base_url"] + self.provider_config["chat_endpoint"]
        
        headers = {
            self.provider_config["header_key"]: self.provider_config["header_format"].format(api_key=self.api_key),
            "Content-Type": "application/json",
        }
        
        # OpenRouter specific headers (Best Practice: Identify application)
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/veyllolabs/vaf"
            headers["X-Title"] = "Veyllo Agent Framework"
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        
        # Enable usage reporting for streaming (OpenAI standard)
        if stream:
            payload["stream_options"] = {"include_usage": True}
        
        # Add tools if provided (function calling)
        if tools:
            payload["tools"] = tools
        
        try:
            response = requests.post(
                url, 
                headers=headers, 
                json=payload, 
                stream=stream, 
                timeout=120  # Best Practice: Long timeout for streaming
            )
            response.raise_for_status()
            
            if stream:
                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        yield content
                                    
                                    # Handle tool calls if present
                                    tool_calls = delta.get("tool_calls")
                                    if tool_calls:
                                        # For now, yield as JSON (agent will parse)
                                        yield json.dumps({"tool_calls": tool_calls})
                                    
                                    # Handle finish_reason
                                    finish_reason = chunk["choices"][0].get("finish_reason")
                                    if finish_reason:
                                        yield json.dumps({"finish_reason": finish_reason})
                                
                                # Handle usage stats (last chunk)
                                if "usage" in chunk:
                                    usage = chunk["usage"]
                                    self.session_usage["input_tokens"] += usage.get("prompt_tokens", 0)
                                    self.session_usage["output_tokens"] += usage.get("completion_tokens", 0)
                                    
                            except json.JSONDecodeError:
                                continue
            else:
                data = response.json()
                if "choices" in data and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message", {})
                    content = message.get("content", "")
                    if content:
                        yield content
                    
                    # Handle tool calls
                    tool_calls = message.get("tool_calls")
                    if tool_calls:
                        yield json.dumps({"tool_calls": tool_calls})
                
                # Handle usage stats
                if "usage" in data:
                    usage = data["usage"]
                    self.session_usage["input_tokens"] += usage.get("prompt_tokens", 0)
                    self.session_usage["output_tokens"] += usage.get("completion_tokens", 0)
        
        except requests.exceptions.Timeout:
            UI.error(f"API request timed out for {self.provider}")
            yield ""
        except requests.exceptions.HTTPError as e:
            error_code = e.response.status_code
            error_text = e.response.text[:200]
            
            # Non-retryable errors: Don't yield empty, raise exception to break retry loop
            if error_code in [400, 401, 403, 429]:  # Bad Request, Unauthorized, Forbidden, Rate Limit
                UI.error(f"{self.provider.upper()} API request failed: {error_code} - {error_text}")
                raise  # Re-raise to break retry loop
            
            # Other errors: yield empty to allow retry
            UI.error(f"API request failed: {error_code} - {error_text}")
            yield ""
        except Exception as e:
            UI.error(f"API request failed: {e}")
            yield ""
    
    def _anthropic_chat(
        self, 
        messages: List[Dict], 
        temperature: float, 
        max_tokens: int, 
        stream: bool, 
        model: str,
        tools: Optional[List[Dict]] = None
    ) -> Generator[str, None, None]:
        """
        Anthropic-specific API format (Claude)
        Best Practice: Anthropic has different message format and streaming structure
        """
        url = self.provider_config["base_url"] + self.provider_config["chat_endpoint"]
        
        headers = {
            self.provider_config["header_key"]: self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": self.provider_config["version_header"],
        }
        
        # Convert messages format (Anthropic separates system messages)
        system_msg = ""
        converted_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                converted_messages.append(msg)
        
        payload = {
            "model": model,
            "messages": converted_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        
        if system_msg:
            payload["system"] = system_msg
        
        # Add tools if provided (Anthropic supports function calling)
        if tools:
            payload["tools"] = tools
        
        try:
            response = requests.post(
                url, 
                headers=headers, 
                json=payload, 
                stream=stream, 
                timeout=120
            )
            response.raise_for_status()
            
            if stream:
                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data = line[6:]
                            try:
                                chunk = json.loads(data)
                                
                                # Anthropic streaming format
                                if chunk.get("type") == "content_block_delta":
                                    delta = chunk.get("delta", {})
                                    content = delta.get("text", "")
                                    if content:
                                        yield content
                                
                                # Handle usage stats (Anthropic)
                                elif chunk.get("type") == "message_start":
                                    # Input tokens are in message_start
                                    usage = chunk.get("message", {}).get("usage", {})
                                    self.session_usage["input_tokens"] += usage.get("input_tokens", 0)
                                    
                                elif chunk.get("type") == "message_delta":
                                    # Output tokens are in message_delta
                                    usage = chunk.get("usage", {})
                                    self.session_usage["output_tokens"] += usage.get("output_tokens", 0)
                                    
                                    delta = chunk.get("delta", {})
                                    stop_reason = delta.get("stop_reason")
                                    if stop_reason:
                                        # Map to openai format for consistency
                                        finish_reason = "length" if stop_reason == "max_tokens" else stop_reason
                                        yield json.dumps({"finish_reason": finish_reason})
                                
                                # Handle tool use
                                elif chunk.get("type") == "content_block_start":
                                    block = chunk.get("content_block", {})
                                    if block.get("type") == "tool_use":
                                        yield json.dumps({"tool_use": block})
                                
                                # Handle stop reason
                                elif chunk.get("type") == "message_delta":
                                    delta = chunk.get("delta", {})
                                    stop_reason = delta.get("stop_reason")
                                    if stop_reason:
                                        # Map to openai format for consistency
                                        finish_reason = "length" if stop_reason == "max_tokens" else stop_reason
                                        yield json.dumps({"finish_reason": finish_reason})
                            except json.JSONDecodeError:
                                continue
            else:
                data = response.json()
                if "content" in data and len(data["content"]) > 0:
                    for block in data["content"]:
                        if block.get("type") == "text":
                            yield block.get("text", "")
                        elif block.get("type") == "tool_use":
                            yield json.dumps({"tool_use": block})
                
                # Handle usage stats
                if "usage" in data:
                    usage = data["usage"]
                    self.session_usage["input_tokens"] += usage.get("input_tokens", 0)
                    self.session_usage["output_tokens"] += usage.get("output_tokens", 0)
        
        except requests.exceptions.Timeout:
            UI.error(f"Anthropic API request timed out")
            yield ""
        except requests.exceptions.HTTPError as e:
            error_code = e.response.status_code
            error_text = e.response.text[:200]
            
            # Non-retryable errors: Don't yield empty, raise exception to break retry loop
            if error_code in [400, 401, 403, 429]:  # Bad Request, Unauthorized, Forbidden, Rate Limit
                UI.error(f"Anthropic API request failed: {error_code} - {error_text}")
                raise  # Re-raise to break retry loop
            
            # Other errors: yield empty to allow retry
            UI.error(f"Anthropic API request failed: {error_code} - {error_text}")
            yield ""
        except Exception as e:
            UI.error(f"Anthropic API request failed: {e}")
            yield ""
    
    def _google_chat(
        self, 
        messages: List[Dict], 
        temperature: float, 
        max_tokens: int, 
        stream: bool, 
        model: str,
        tools: Optional[List[Dict]] = None
    ) -> Generator[str, None, None]:
        """
        Google AI Studio specific format (Gemini)
        Best Practice: Google uses different message structure and auth method
        Supports function calling via tools parameter
        """
        if stream:
            endpoint = self.provider_config.get("stream_endpoint", self.provider_config["chat_endpoint"])
        else:
            endpoint = self.provider_config["chat_endpoint"]
        
        endpoint = endpoint.format(model=model)
        url = self.provider_config["base_url"] + endpoint
        
        # Google uses API key as query parameter (their design choice)
        url += f"?key={self.api_key}"
        if stream:
            url += "&alt=sse"
        
        headers = {
            "Content-Type": "application/json",
        }
        
        # Convert messages to Google format
        contents = []
        system_instruction = None
        
        for msg in messages:
            if msg["role"] == "system":
                system_instruction = msg["content"]
            else:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": msg["content"]}]
                })
        
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }
        
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        
        # Add tools if provided (Google function calling format)
        if tools:
            def sanitize_schema(schema: Dict) -> Dict:
                """Recursively remove 'additionalProperties' from schema."""
                if not isinstance(schema, dict):
                    return schema
                new_schema = {}
                for k, v in schema.items():
                    if k == "additionalProperties":
                        continue
                    if isinstance(v, dict):
                        new_schema[k] = sanitize_schema(v)
                    elif isinstance(v, list):
                        new_schema[k] = [sanitize_schema(i) if isinstance(i, dict) else i for i in v]
                    else:
                        new_schema[k] = v
                return new_schema

            # Convert OpenAI tool format to Google format
            google_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    parameters = func.get("parameters", {"type": "object", "properties": {}})
                    google_tools.append({
                        "name": func["name"],
                        "description": func.get("description", ""),
                        "parameters": sanitize_schema(parameters)
                    })
            
            if google_tools:
                payload["tools"] = [{"functionDeclarations": google_tools}]
        
        try:
            response = requests.post(
                url, 
                headers=headers, 
                json=payload, 
                stream=stream,
                timeout=120
            )
            response.raise_for_status()
            
            if stream:
                # Track usage for this request (Google sends cumulative usage)
                last_usage = None
                
                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data = line[6:]
                            try:
                                chunk = json.loads(data)
                                
                                # Track usage
                                if "usageMetadata" in chunk:
                                    last_usage = chunk["usageMetadata"]
                                
                                if "candidates" in chunk and len(chunk["candidates"]) > 0:
                                    candidate = chunk["candidates"][0]
                                    if "content" in candidate:
                                        parts = candidate["content"].get("parts", [])
                                        for part in parts:
                                            if "text" in part:
                                                yield part["text"]
                                            elif "functionCall" in part:
                                                # Google function calling format
                                                func_call = part["functionCall"]
                                                # Convert to OpenAI-compatible format
                                                tool_call_json = json.dumps({
                                                    "tool_calls": [{
                                                        "id": f"call_{func_call['name']}_{hash(json.dumps(func_call.get('args', {})))}",
                                                        "type": "function",
                                                        "function": {
                                                            "name": func_call["name"],
                                                            "arguments": json.dumps(func_call.get("args", {}))
                                                        }
                                                    }]
                                                })
                                                yield tool_call_json
                            except json.JSONDecodeError:
                                continue
                
                # Update session usage with final stats
                if last_usage:
                    self.session_usage["input_tokens"] += last_usage.get("promptTokenCount", 0)
                    self.session_usage["output_tokens"] += last_usage.get("candidatesTokenCount", 0)
            else:
                data = response.json()
                
                # Handle usage stats
                if "usageMetadata" in data:
                    usage = data["usageMetadata"]
                    self.session_usage["input_tokens"] += usage.get("promptTokenCount", 0)
                    self.session_usage["output_tokens"] += usage.get("candidatesTokenCount", 0)
                
                if "candidates" in data and len(data["candidates"]) > 0:
                    candidate = data["candidates"][0]
                    if "content" in candidate:
                        parts = candidate["content"].get("parts", [])
                        for part in parts:
                            if "text" in part:
                                yield part["text"]
                            elif "functionCall" in part:
                                # Google function calling format
                                func_call = part["functionCall"]
                                # Convert to OpenAI-compatible format
                                tool_call_json = json.dumps({
                                    "tool_calls": [{
                                        "id": f"call_{func_call['name']}_{hash(json.dumps(func_call.get('args', {})))}",
                                        "type": "function",
                                        "function": {
                                            "name": func_call["name"],
                                            "arguments": json.dumps(func_call.get("args", {}))
                                        }
                                    }]
                                })
                                yield tool_call_json
        
        except requests.exceptions.Timeout:
            UI.error(f"Google API request timed out")
            yield ""
        except requests.exceptions.HTTPError as e:
            error_code = e.response.status_code
            error_text = e.response.text[:200]
            
            # Non-retryable errors: Don't yield empty, raise exception to break retry loop
            if error_code in [400, 401, 403, 429]:  # Bad Request, Unauthorized, Forbidden, Rate Limit
                UI.error(f"Google API request failed: {error_code} - {error_text}")
                raise  # Re-raise to break retry loop
            
            # Other errors: yield empty to allow retry
            UI.error(f"Google API request failed: {error_code} - {error_text}")
            yield ""
        except Exception as e:
            UI.error(f"Google API request failed: {e}")
            yield ""
    
    @staticmethod
    def test_connection(provider: str) -> bool:
        """
        Best Practice: Test API key validity before using it.
        For Google: Fetches available models first, then tests with a real model.
        
        Args:
            provider: Provider name
            
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Special handling for Google: Fetch models first to get a valid model name
            if provider == "google":
                try:
                    models = APIBackendManager._fetch_google_models()
                    if not models:
                        raise ValueError("No models available")
                    # Use first available model for testing
                    test_model = models[0]
                except Exception:
                    # Fallback to common working model
                    test_model = "gemini-pro"
            else:
                manager = APIBackendManager(provider)
                # Use provider's default model for testing
                test_model = manager.provider_config["default_model"]
            
            # Now test with the model
            manager = APIBackendManager(provider)
            result = list(manager.chat_completion(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
                stream=False,
                model=test_model
            ))
            return bool(result and any(result))
        except Exception as e:
            UI.event("Test", f"Connection test failed: {e}", style="error")
            return False
    
    @staticmethod
    def get_available_models(provider: str) -> List[str]:
        """
        Get list of available models for a provider.
        Best Practice: Fetch dynamically from API when possible, with fallback to static list.
        
        Args:
            provider: Provider name
            
        Returns:
            List of model names
        """
        try:
            # Try to fetch models dynamically from API
            if provider == "openai":
                return APIBackendManager._fetch_openai_models()
            elif provider == "anthropic":
                return APIBackendManager._fetch_anthropic_models()
            elif provider == "google":
                return APIBackendManager._fetch_google_models()
            elif provider == "openrouter":
                return APIBackendManager._fetch_openrouter_models()
            elif provider == "deepseek":
                return APIBackendManager._fetch_deepseek_models()
        except Exception:
            # Fallback to static list if API fetch fails
            pass
        
        # Fallback: Static model lists (used if API fetch fails or not implemented)
        models = {
            "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4-turbo-preview", "gpt-3.5-turbo"],
            "anthropic": [
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022", 
                "claude-3-opus-20240229",
                "claude-3-sonnet-20240229",
                "claude-3-haiku-20240307"
            ],
            "deepseek": ["deepseek-chat", "deepseek-coder"],
            "google": ["gemini-pro", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-pro-vision"],
            "openrouter": [
                "anthropic/claude-3.5-sonnet",
                "openai/gpt-4o",
                "google/gemini-pro-1.5",
                "meta-llama/llama-3.1-405b-instruct"
            ],
        }
        
        return models.get(provider, [])
    
    @staticmethod
    def _fetch_openai_models() -> List[str]:
        """Fetch available models from OpenAI API."""
        from vaf.core.config import Config
        
        api_key = Config.get_api_key("openai")
        if not api_key:
            raise ValueError("No API key")
        
        try:
            response = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            models = []
            
            # Filter for chat models only (gpt-4, gpt-3.5)
            for model in data.get("data", []):
                model_id = model.get("id", "")
                if any(x in model_id for x in ["gpt-4", "gpt-3.5"]):
                    models.append(model_id)
            
            # Sort by name (most recent first)
            models.sort(reverse=True)
            return models[:15]  # Limit to top 15
            
        except Exception:
            raise
    
    @staticmethod
    def _fetch_anthropic_models() -> List[str]:
        """Fetch available models from Anthropic API."""
        # Anthropic doesn't have a public models endpoint yet
        # Return curated list of known models
        return [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229", 
            "claude-3-haiku-20240307"
        ]
    
    @staticmethod
    def _fetch_google_models() -> List[str]:
        """Fetch available models from Google AI Studio API."""
        from vaf.core.config import Config
        
        # Fallback list of known free/stable models (if API call fails)
        fallback_models = [
            "gemini-1.5-flash",      # Fast, free tier available
            "gemini-1.5-flash-8b",   # Smaller, faster
            "gemini-1.5-pro",        # More capable, free tier
            "gemini-2.0-flash-exp",  # Latest experimental
            "gemini-pro",            # Legacy stable
            "gemini-pro-vision",     # Vision support
        ]
        
        api_key = Config.get_api_key("google")
        if not api_key:
            return fallback_models
        
        try:
            response = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            models = []
            
            # Filter for generative models
            for model in data.get("models", []):
                name = model.get("name", "")
                # Extract model ID (e.g., "models/gemini-1.5-pro" -> "gemini-1.5-pro")
                if "/" in name:
                    model_id = name.split("/")[-1]
                    # Filter for gemini models that support generateContent
                    supported_methods = model.get("supportedGenerationMethods", [])
                    if "generateContent" in supported_methods and "gemini" in model_id:
                        models.append(model_id)
            
            # If API returned models, use them; otherwise use fallback
            return models if models else fallback_models
            
        except Exception:
            # If API call fails (e.g., rate limit), return fallback list
            return fallback_models
    
    @staticmethod
    def _fetch_openrouter_models() -> List[str]:
        """Fetch available models from OpenRouter API."""
        from vaf.core.config import Config
        
        api_key = Config.get_api_key("openrouter")
        if not api_key:
            raise ValueError("No API key")
        
        try:
            response = requests.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            models = []
            
            # Get model IDs
            for model in data.get("data", []):
                model_id = model.get("id", "")
                if model_id:
                    models.append(model_id)
            
            # Sort by popularity (if available) or alphabetically
            models.sort()
            return models[:30]  # Limit to top 30
            
        except Exception:
            raise
    
    @staticmethod
    def _fetch_deepseek_models() -> List[str]:
        """Fetch available models from DeepSeek API."""
        from vaf.core.config import Config
        
        api_key = Config.get_api_key("deepseek")
        if not api_key:
            raise ValueError("No API key")
        
        try:
            response = requests.get(
                "https://api.deepseek.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            models = []
            
            # Get model IDs
            for model in data.get("data", []):
                model_id = model.get("id", "")
                if model_id:
                    models.append(model_id)
            
            return models
            
        except Exception:
            raise