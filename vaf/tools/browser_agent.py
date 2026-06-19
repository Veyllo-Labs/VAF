"""
VAF Browser Agent Tool
======================
Autonomous browser automation via browser-use + Playwright/Chromium.

The tool wraps browser-use's Agent loop, powered by VAF's own configured LLM
(local Ollama, OpenAI, Anthropic, DeepSeek, …). browser-use handles DOM
parsing, element selection and multi-step navigation; VAF's agent decides
*when* to use this tool and *what task* to hand it.

Install (once, after pip install -r requirements.txt):
    playwright install --with-deps chromium
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
import threading
from typing import Any, Optional

from vaf.tools.base import BaseTool


# ── 0. On-demand vision helpers ───────────────────────────────────────────────
# Vision is only called when browser-use sends a screenshot (use_vision='auto'),
# or when the agent explicitly calls describe_page_visually().
# This avoids paying vision-token cost on every DOM-only step.

def _model_supports_vision(provider: str, model: str) -> bool:
    """Mirror of agent.py's _model_supports_vision — kept in sync manually."""
    if provider == "anthropic":
        return True
    if provider == "google":
        return True
    if provider == "openai":
        return any(k in model for k in ("gpt-4o", "gpt-4-turbo", "gpt-4-vision", "o1", "o3"))
    if provider == "deepseek":
        return False
    if provider == "openrouter":
        return any(k in model for k in ("gpt-4o", "claude-3", "gemini", "vision", "vl", "llava", "pixtral"))
    return True  # local / unknown: pass through


def _call_vision(image_url: str, prompt: str, max_tokens: int = 512) -> Optional[str]:
    """
    Send a screenshot to the configured vision backend with a custom prompt.
    Returns the response text, or None if no vision backend is available.
    """
    try:
        from vaf.core.config import Config
        from vaf.core.api_backend import APIBackendManager

        vision_provider = Config.get("vision_provider", "").strip()
        vision_model = Config.get("vision_model", "").strip() or None

        if not vision_provider:
            return None

        backend = APIBackendManager(vision_provider)
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }]
        text = ""
        for chunk in backend.chat_completion(
            msgs, model=vision_model, temperature=0.1,
            max_tokens=max_tokens, stream=True,
        ):
            if isinstance(chunk, str):
                text += chunk
        return text.strip() or None
    except Exception as _e:
        logging.getLogger(__name__).debug("vision call failed: %s", _e)
        return None


def _call_vision_for_screenshot(image_url: str) -> Optional[str]:
    """Generic page description — used by describe_page_visually action."""
    return _call_vision(
        image_url,
        prompt=(
            "You are helping a browser automation agent. "
            "Describe what is visible on this screenshot concisely: "
            "page title/heading, main content, any forms, buttons, errors, "
            "or CAPTCHA/challenge elements. Be specific and brief."
        ),
        max_tokens=512,
    )


def _call_vision_for_captcha(image_url: str, category: str) -> Optional[str]:
    """
    Analyze a reCAPTCHA grid screenshot and return which tile indices to click.
    Grid size is detected from the image — not assumed.
    """
    prompt = (
        f"This is a reCAPTCHA image challenge screenshot.\n"
        f"It shows a grid of image tiles. First, count the rows and columns yourself "
        f"from what you see — do NOT assume a fixed size.\n"
        f"The task is to select all tiles that contain: \"{category}\"\n\n"
        f"Number the tiles 0 to (rows×cols − 1), left-to-right, top-to-bottom.\n"
        f"Examine EACH tile carefully.\n\n"
        f"Respond with ONLY JSON — no prose, no markdown fences:\n"
        f'{{"tiles": [<matching indices>], "rows": <rows you counted>, '
        f'"cols": <cols you counted>, "confidence": "high"|"medium"|"low"}}'
    )
    raw = _call_vision(image_url, prompt=prompt, max_tokens=150)
    if not raw:
        return None

    try:
        cleaned = raw.strip()
        if "```" in cleaned:
            parts = cleaned.split("```")
            cleaned = parts[1].lstrip("json").strip() if len(parts) > 1 else cleaned
        start, end = cleaned.find("{"), cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            obj = json.loads(cleaned[start:end])
            tiles = obj.get("tiles", [])
            rows = obj.get("rows", "?")
            cols = obj.get("cols", "?")
            conf = obj.get("confidence", "unknown")
            grid_desc = f"{rows}×{cols}" if rows != "?" else "unknown size"
            if tiles:
                return (
                    f"[reCAPTCHA analysis — category: '{category}']\n"
                    f"Detected grid: {grid_desc}  |  Confidence: {conf}\n"
                    f"Tiles to click (0-indexed, left→right, top→bottom): {tiles}\n"
                    f"Click each listed tile, then click 'Verify'."
                )
            else:
                return (
                    f"[reCAPTCHA analysis — category: '{category}']\n"
                    f"Detected grid: {grid_desc}  |  No matching tiles found (confidence: {conf}).\n"
                    f"Click 'Skip' if available, otherwise click 'Verify' and wait for a new challenge."
                )
    except Exception:
        pass
    return f"[reCAPTCHA vision response]\n{raw}"


def _build_browser_controller():
    """
    Build a browser-use Controller with vision-powered actions.

    Actions:
      describe_page_visually  — generic page description (stuck, unclear DOM)
      solve_captcha_challenge — targeted reCAPTCHA tile analysis → returns click indices
    """
    from browser_use import Controller
    import base64

    controller = Controller()

    @controller.action(
        "Visually describe the current page using a screenshot. "
        "Use for general page understanding when DOM text is insufficient. "
        "For reCAPTCHA image grids, use solve_captcha_challenge instead."
    )
    async def describe_page_visually(browser_session) -> str:  # type: ignore[misc]
        try:
            shot = await browser_session.take_screenshot(
                format="jpeg", quality=72, full_page=False
            )
            b64 = base64.b64encode(shot).decode()
            desc = _call_vision_for_screenshot(f"data:image/jpeg;base64,{b64}")
            if desc:
                return f"[Visual description of current page]\n{desc}"
            return (
                "Vision API not configured. "
                "Configure a Vision Model in VAF Settings → AI & Model to enable this."
            )
        except Exception as e:
            return f"Screenshot failed: {e}"

    @controller.action(
        "Solve a reCAPTCHA image tile challenge using computer vision. "
        "Call this IMMEDIATELY when you see a grid of images asking you to select specific tiles "
        "(buses, traffic lights, crosswalks, bicycles, fire hydrants, etc.). "
        "The grid can be any size — the vision model detects it automatically. "
        "Provide the exact category text shown above the grid (e.g. 'traffic lights', 'buses'). "
        "Returns the tile indices (0-indexed, left-to-right, top-to-bottom) to click."
    )
    async def solve_captcha_challenge(category: str, browser_session) -> str:  # type: ignore[misc]
        try:
            # Higher quality for tile content analysis
            shot = await browser_session.take_screenshot(
                format="jpeg", quality=88, full_page=False
            )
            b64 = base64.b64encode(shot).decode()
            result = _call_vision_for_captcha(f"data:image/jpeg;base64,{b64}", category)
            if result:
                return result
            return (
                "Vision API not configured — cannot analyze CAPTCHA tiles. "
                "Configure a Vision Model in VAF Settings → AI & Model."
            )
        except Exception as e:
            return f"CAPTCHA analysis failed: {e}"

    return controller


# ── 1. Async-to-sync bridge ───────────────────────────────────────────────────
# Copied verbatim from vaf/tools/context_tools.py so there is no cross-tool
# import dependency. Creates a fresh OS thread + event loop to avoid
# "Event loop is closed" / "attached to a different loop" errors when called
# from VAF's synchronous tool-dispatch path.

def _run_async_in_new_loop(coro):
    """Run a coroutine in a new thread with its own event loop."""
    result = [None]
    exception = [None]

    def _thread_run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result[0] = loop.run_until_complete(coro)
        except Exception as e:
            exception[0] = e
        finally:
            loop.close()

    t = threading.Thread(target=_thread_run)
    t.start()
    t.join()
    if exception[0]:
        raise exception[0]
    return result[0]


# ── 2. Global browser concurrency gate ───────────────────────────────────────
# One shared Chromium container → limit parallel sessions to avoid memory
# exhaustion and tab interference.  Default: 1 (serialised).
# Override via env var:  VAF_BROWSER_MAX_PARALLEL=2
import os as _os
_BROWSER_MAX_PARALLEL = max(1, int(_os.environ.get("VAF_BROWSER_MAX_PARALLEL", "1")))
_BROWSER_SEMAPHORE = threading.Semaphore(_BROWSER_MAX_PARALLEL)
_BROWSER_QUEUE_TIMEOUT = 120  # seconds a caller will wait before giving up

# ── 3. Browser-use step log capture ──────────────────────────────────────────

_STEP_LOG_SKIP = (
    'selector_map', 'elements_str', 'DOMRect', 'getBoundingClientRect',
    'innerHTML', 'textContent', 'visibility_ratio', 'xpath', 'outerHTML',
    '"index":', '"tag_name":', '"attributes":', '"is_visible":',
)


class _BrowserStepLogger(logging.Handler):
    """
    Captures browser-use agent INFO+ log lines into a queue.
    The screenshot loop drains the queue and emits each line to the WebUI console.
    Using a queue avoids calling async code from a sync logging context.
    """

    def __init__(self, q: _queue.Queue) -> None:
        super().__init__(level=logging.INFO)
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if len(msg) > 350:            # skip DOM dumps / huge payloads
                return
            if any(kw in msg for kw in _STEP_LOG_SKIP):
                return
            self._q.put_nowait(msg)
        except Exception:
            pass


# ── 3. VAFLLMBridge ───────────────────────────────────────────────────────────

class VAFLLMBridge:
    """
    Implements browser-use's BaseChatModel protocol using VAF's APIBackendManager.

    browser-use calls:  await llm.ainvoke(messages, output_format=SomePydanticModel)
    This class:         delegates to APIBackendManager.chat_completion() (sync,
                        streaming) via asyncio.run_in_executor — correct for code
                        already running inside an event loop (created by
                        _run_async_in_new_loop above).

    Required protocol surface:
        model: str
        provider: str  (property)
        name: str      (property)
        model_name: str (property, legacy compat)
        _verified_api_keys: bool = False
    """

    _verified_api_keys: bool = False

    def __init__(self, model: str, provider_name: str, session_id: Optional[str] = None) -> None:
        self.model = model
        self._provider_name = provider_name
        self._session_id = session_id

    # ── Protocol properties ───────────────────────────────────────────────────

    @property
    def provider(self) -> str:
        return self._provider_name

    @property
    def name(self) -> str:
        return self.model

    @property
    def model_name(self) -> str:
        return self.model

    # ── Core async method called by browser-use ───────────────────────────────

    async def ainvoke(
        self,
        messages: list,
        output_format: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Async entry point.

        When output_format is None  → plain text response wrapped in ChatInvokeCompletion.
        When output_format is given → JSON schema injected into prompt, response parsed
                                      into the Pydantic model, wrapped in ChatInvokeCompletion.
        """
        try:
            from browser_use.llm.views import ChatInvokeCompletion
        except ImportError:
            # Fallback: return a simple wrapper if browser-use version differs
            class ChatInvokeCompletion:  # type: ignore[no-redef]
                def __init__(self, completion, usage=None):
                    self.completion = completion
                    self.usage = usage

        raw = self._to_dicts(messages)
        max_tokens = 8192 if output_format is not None else 4096

        if output_format is not None:
            raw = self._inject_schema(raw, output_format)

        # run_in_executor: runs blocking APIBackendManager inside the already-running
        # event loop without blocking its scheduler.
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._call_sync, raw, max_tokens)

        if output_format is not None:
            parsed = self._parse(text, output_format)
            return ChatInvokeCompletion(completion=parsed, usage=None)

        return ChatInvokeCompletion(completion=text, usage=None)

    # ── Synchronous LLM call (runs in thread pool) ────────────────────────────

    def _call_sync(self, messages: list[dict], max_tokens: int) -> str:
        from vaf.core.api_backend import APIBackendManager
        backend = APIBackendManager(self._provider_name)
        text = ""
        for chunk in backend.chat_completion(
            messages=messages,
            temperature=0.1,        # low temp → more reliable structured JSON
            max_tokens=max_tokens,
            stream=True,
            model=self.model,
        ):
            # Honour user stop request — abort this LLM call immediately
            if self._session_id:
                try:
                    from vaf.core.task_queue import TaskQueue
                    if TaskQueue().should_stop(self._session_id):
                        raise InterruptedError("Browser agent stopped by user.")
                except InterruptedError:
                    raise
                except Exception:
                    pass

            s = chunk.strip()
            if s.startswith("{") and (
                "tool_calls" in chunk or "finish_reason" in chunk
            ):
                continue
            text += chunk
        return text.strip()

    # ── Message conversion ────────────────────────────────────────────────────

    def _to_dicts(self, messages: list) -> list[dict]:
        """
        Convert browser-use BaseMessage objects → VAF/OpenAI dicts.

        Image handling (on-demand vision):
        - If main provider supports native vision → pass image_url blocks directly.
        - Else if vision_provider configured → call vision API → inject text description.
        - Else → skip images (DOM-only fallback).
        """
        native_vision = _model_supports_vision(self._provider_name, self.model)
        result = []
        for m in messages:
            role = str(getattr(m, "role", "user"))
            content = getattr(m, "content", "") or ""
            if isinstance(content, list):
                text_parts: list[str] = []
                img_urls: list[str] = []

                for p in content:
                    if getattr(p, "type", "") == "image_url":
                        # Extract URL from various browser-use object shapes
                        img = getattr(p, "image_url", None)
                        if isinstance(img, dict):
                            img_url = img.get("url", "")
                        elif isinstance(img, str):
                            img_url = img
                        else:
                            img_url = str(img or "")
                        if img_url:
                            img_urls.append(img_url)
                    else:
                        text_parts.append(getattr(p, "text", str(p)))

                if img_urls:
                    if native_vision:
                        # Pass images natively — provider handles them directly
                        blocks: list = []
                        if text_parts:
                            blocks.append({"type": "text", "text": "\n".join(text_parts)})
                        for url in img_urls:
                            blocks.append({"type": "image_url", "image_url": {"url": url}})
                        result.append({"role": role, "content": blocks})
                        continue
                    else:
                        # Vision fallback: describe each image, inject as text
                        for url in img_urls:
                            desc = _call_vision_for_screenshot(url)
                            if desc:
                                text_parts.append(f"[Vision: {desc}]")
                        # fall through to plain-text content below

                content = "\n".join(text_parts)
            result.append({"role": role, "content": str(content)})
        return result

    # ── Structured output helpers ─────────────────────────────────────────────

    def _inject_schema(self, messages: list[dict], output_format: Any) -> list[dict]:
        """Append JSON schema instruction to the last user message."""
        schema_str = json.dumps(output_format.model_json_schema(), indent=2)
        instruction = (
            "\n\nRespond with ONLY valid JSON matching this schema exactly "
            "(no prose, no markdown fences):\n" + schema_str
        )
        msgs = list(messages)
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"] + instruction}
        else:
            msgs.append({"role": "user", "content": instruction})
        return msgs

    def _parse(self, text: str, output_format: Any) -> Any:
        """
        Parse LLM response into the Pydantic model.
        Tries: full text → outermost {…} block → lenient dict coercion.
        """
        cleaned = text.strip()

        # Strip ```json … ``` fences
        if "```" in cleaned:
            parts = cleaned.split("```")
            if len(parts) > 1:
                cleaned = parts[1].lstrip("json").strip()

        # Attempt 1: full cleaned text
        try:
            return output_format.model_validate_json(cleaned)
        except Exception:
            pass

        # Attempt 2: outermost { … } block
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            fragment = cleaned[start:end]
            try:
                return output_format.model_validate_json(fragment)
            except Exception:
                pass
            try:
                return output_format.model_validate(json.loads(fragment))
            except Exception:
                pass

        raise ValueError(
            f"VAFLLMBridge: cannot parse {output_format.__name__} from LLM output.\n"
            f"Raw text (first 400 chars): {text[:400]}"
        )


# ── 5. Factory ────────────────────────────────────────────────────────────────

def _build_vaf_bridge(session_id: Optional[str] = None) -> VAFLLMBridge:
    """
    Read VAF config and return a configured VAFLLMBridge.
    Mirrors the model-selection logic in BaseTool.query_llm() and
    APIBackendManager.chat_completion() (api_backend.py:472-484).
    """
    from vaf.core.config import Config
    config = Config.load()
    provider = config.get("provider", "local")

    if provider == "local":
        model = config.get("model", "llama3")
    else:
        model = config.get(f"api_model_{provider}", "")
        if not model:
            model = config.get("model", "llama3")

    # Guard: never pass a local GGUF filename to a cloud API provider
    if provider != "local" and (
        model.endswith(".gguf")
        or "vq-1" in model.lower()
        or "instruct-q" in model.lower()
        or model.lower().startswith("veyllo/")
    ):
        _defaults = {
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-6",
            "deepseek": "deepseek-v4-flash",
            "google": "gemini-1.5-flash",
            "openrouter": "anthropic/claude-3.5-sonnet",
        }
        model = _defaults.get(provider, "gpt-4o")

    return VAFLLMBridge(model=model, provider_name=provider, session_id=session_id)


# ── 6. BrowserAgentTool ───────────────────────────────────────────────────────

class BrowserAgentTool(BaseTool):
    """
    Autonomous browser agent — controls a real headless Chromium browser.

    Use this tool when:
    • The target page requires JavaScript to render (React, Vue, SPA)
    • You need to click, scroll, fill forms, or navigate multi-page flows
    • Login/authentication walls must be passed
    • web_search or webfetch return incomplete/static content

    For simple fact lookups prefer web_search (faster, no browser overhead).
    """

    name = "browser_agent"
    permission_level = "write"
    side_effect_class = "irreversible"
    channel_restrictions = ("telegram", "whatsapp", "discord")

    description = (
        "Controls a real Chromium browser to complete multi-step web tasks. "
        "Use when: the page requires JavaScript (React/SPA), you need to click/scroll/fill forms, "
        "you need to log in, or web_search/webfetch don't work for the content. "
        "Provide a plain-language task; the agent navigates autonomously and returns the result.\n\n"
        "SESSION MODES — choose carefully:\n"
        "- Default (persistent=false): fresh browser, no cookies, no login state. "
        "Use for all general browsing, research, and data extraction tasks. "
        "This is the correct choice for ~95% of tasks.\n"
        "- Persistent (persistent=true, session='name'): cookies and login state are saved to disk "
        "and restored on the next call with the same session name. "
        "ONLY use this when the user explicitly wants to log in to a specific site and reuse that login later. "
        "Login credentials passed to this mode are stored in ~/.vaf/browser_sessions/{name}.json — "
        "warn the user if they are about to store credentials for a sensitive site."
    )

    input_examples = [
        {"task": "Go to news.ycombinator.com and return the top 5 story titles with scores"},
        {"task": "Search PyPI for 'browser-use' and return the latest version number"},
        {
            "task": "Go to https://app.example.com/login, log in with user@x.com / pass123, "
                    "navigate to /reports, extract the monthly summary table",
            "allowed_domains": ["app.example.com"],
            "persistent": True,
            "session": "app-example",
        },
    ]

    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Plain-language description of the browser task. "
                    "Be specific: include the URL, what data to extract, "
                    "and any credentials or steps required."
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": "Maximum browser steps before stopping. Default: 25, max: 100.",
                "default": 25,
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional domain whitelist (security sandbox). "
                    "Example: ['github.com', 'pypi.org']. "
                    "Omit for unrestricted browsing."
                ),
            },
            "persistent": {
                "type": "boolean",
                "description": (
                    "If true, cookies and login state are saved after the task and restored "
                    "on the next call with the same session name. Use for sites that require "
                    "login (e.g. 'Log in to Tipico and check my balance'). Default: false."
                ),
                "default": False,
            },
            "session": {
                "type": "string",
                "description": (
                    "Named persistent session. Only used when persistent=true. "
                    "Use a descriptive name like 'tipico', 'amazon', 'banking'. "
                    "Each name has its own independent cookie store. Default: 'default'."
                ),
                "default": "default",
            },
        },
        "required": ["task"],
    }

    # ── Public run() — synchronous entry point ────────────────────────────────

    def run(self, **kwargs) -> str:
        task = (kwargs.get("task") or "").strip()
        if not task:
            return "Error: task parameter is required."

        # Optionally run as a separate, killable CHILD PROCESS. Workflows opt in via
        # VAF_SPAWN_BROWSER_SUBAGENT so a long browser run can be supervised/killed cleanly
        # instead of abandoning an un-killable in-process thread. The child sets
        # VAF_IN_SUBAGENT_TERMINAL=1 and runs browser-use in-process there (no re-spawn);
        # it streams live frames + writes its result back through the IPC queue. Standalone
        # callers (no flag) keep the existing in-process behaviour unchanged.
        import os as _os, sys as _sys
        if (_os.environ.get("VAF_SPAWN_BROWSER_SUBAGENT", "").strip().lower() in ("1", "true", "yes")
                and _os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() not in ("1", "true", "yes")):
            try:
                import shlex as _shlex
                from vaf.core.platform import Platform
                from vaf.core.subagent_ipc import get_ipc, get_current_session_id
                ipc = get_ipc()
                task_id = ipc.create_task("browser_agent", task_description=task)
                # Session/task context goes into the CHILD env only (not the parent's global env),
                # so concurrent workers don't clobber each other's session.
                _sid = get_current_session_id()
                _sub_env = {"VAF_TASK_ID": task_id, "VAF_AGENT_TYPE": "browser_agent"}
                if _sid:
                    _sub_env["VAF_SESSION_ID"] = _sid
                _parts = [_sys.executable, '-m', 'vaf.main', 'subagent', 'run', 'browser_agent',
                          '--task', task, '--task-id', task_id]
                if Platform.is_windows():
                    _cmd = ' '.join((f'"{p}"' if (' ' in p or '"' in p) else p) for p in _parts)
                else:
                    _cmd = ' '.join(_shlex.quote(str(p)) for p in _parts)
                if Platform.open_new_terminal(_cmd, title=f"VAF Browser Agent [{task_id}]", extra_env=_sub_env):
                    ipc.mark_task_running(task_id)
                    return (f"[SUBAGENT_ASYNC:{task_id}:browser_agent] "
                            f"Browser agent running as a child process. Task: {task[:80]}...")
                ipc.cancel_task(task_id)   # spawn failed → fall through to in-process
            except Exception:
                pass   # any spawn error → fall through to in-process below

        max_steps = max(1, min(int(kwargs.get("max_steps") or 25), 100))
        allowed_domains: Optional[list] = kwargs.get("allowed_domains") or None
        persistent: bool = bool(kwargs.get("persistent") or False)
        session: str = str(kwargs.get("session") or "default").strip() or "default"

        # ── Concurrency gate ──────────────────────────────────────────────────
        # Serialises access to the shared Chromium container.
        # Multiple users (different sessions) each get their own slot; excess
        # callers wait up to _BROWSER_QUEUE_TIMEOUT seconds before giving up.
        acquired = _BROWSER_SEMAPHORE.acquire(timeout=_BROWSER_QUEUE_TIMEOUT)
        if not acquired:
            return (
                f"Browser agent is busy — all {_BROWSER_MAX_PARALLEL} slot(s) are in use. "
                f"Please try again in a moment."
            )
        try:
            return _run_async_in_new_loop(
                self._run_browser(
                    task=task,
                    max_steps=max_steps,
                    allowed_domains=allowed_domains,
                    persistent=persistent,
                    session=session,
                )
            )
        except ImportError as e:
            return (
                "Error: browser-use is not installed.\n"
                "Run the following commands to enable this tool:\n"
                "  pip install browser-use playwright\n"
                "  playwright install --with-deps chromium\n"
                f"\nDetails: {e}"
            )
        except Exception as e:
            return f"Error (browser_agent): {type(e).__name__}: {e}"
        finally:
            _BROWSER_SEMAPHORE.release()

    # ── Internal async implementation ─────────────────────────────────────────

    async def _run_browser(
        self,
        task: str,
        max_steps: int,
        allowed_domains: Optional[list],
        persistent: bool = False,
        session: str = "default",
    ) -> str:
        import os
        from browser_use import Agent
        from browser_use.browser.session import BrowserSession
        from browser_use.browser.profile import BrowserProfile

        # Resolve the full WebSocket debugger URL from the CDP base endpoint.
        # Chromium requires the full path (ws://.../devtools/browser/{uuid}), not just the base.
        cdp_url = self._resolve_cdp_url(
            os.environ.get("VAF_BROWSER_CDP_URL", "http://localhost:9222")
        )

        # ── Persistent session: resolve cookie store path ─────────────────────
        session_file: Optional[str] = None
        if persistent:
            sessions_dir = os.path.join(os.path.expanduser("~"), ".vaf", "browser_sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            # Sanitise session name: only alphanumeric, dash, underscore
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session)
            session_file = os.path.join(sessions_dir, f"{safe_name}.json")

        session_kwargs: dict = {"cdp_url": cdp_url}
        if allowed_domains:
            session_kwargs["allowed_domains"] = allowed_domains
        if session_file and os.path.exists(session_file):
            session_kwargs["storage_state"] = session_file

        # ── Session ID (needed for stop-check and WebUI broadcast) ───────────
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            _session_id = get_current_session_id()
        except Exception:
            _session_id = None

        browser = BrowserSession(**session_kwargs)

        # ── Stealth: inject anti-bot evasions via CDP init script ────────────
        # The JS payload is vendored directly in _stealth_payload.js (no runtime
        # dependency on playwright-stealth). This eliminates supply-chain risk:
        # the script is reviewed once at vendor time and never changes without
        # an explicit VAF code review.
        #
        # Source: playwright-stealth 2.0.3 (MIT) — Mattwmaster58
        # SHA-256: 5601b9ccfd7d97c538daec0d097dfc7939faa73ad41c9a20a579269111709e32
        # Patches: navigator.webdriver, chrome runtime, WebGL vendor,
        #          hardware concurrency, plugins, user-agent data, permissions.
        try:
            _stealth_js_path = os.path.join(os.path.dirname(__file__), "_stealth_payload.js")
            with open(_stealth_js_path, "r", encoding="utf-8") as _f:
                _stealth_js = _f.read()
            await browser.start()
            await browser._cdp_add_init_script(_stealth_js)
        except Exception:
            pass  # stealth file missing or CDP not ready — degrade gracefully

        agent = Agent(
            task=task,
            llm=_build_vaf_bridge(session_id=_session_id),
            browser_session=browser,
            controller=_build_browser_controller(),
            max_failures=3,
            use_vision=False,       # screenshots go only through describe_page_visually action
            enable_planning=False,
            use_thinking=False,
        )

        # ── Browser-use log capture ────────────────────────────────────────────
        log_queue: _queue.Queue = _queue.Queue()
        log_handler = _BrowserStepLogger(log_queue)
        _bu_logger = logging.getLogger("browser_use")
        _bu_logger.addHandler(log_handler)

        # ── Wrap agent.run() as a cancellable task ─────────────────────────────
        agent_task = asyncio.create_task(agent.run(max_steps=max_steps))
        stop_screenshots = asyncio.Event()

        screenshot_task = asyncio.create_task(
            self._screenshot_loop(browser, _session_id, stop_screenshots, log_queue, agent, task, max_steps)
        )
        stop_monitor_task = asyncio.create_task(
            self._stop_monitor(_session_id, agent, agent_task, stop_screenshots)
        )

        try:
            history = await agent_task
        except (asyncio.CancelledError, InterruptedError):
            return "Browser task stopped by user."
        finally:
            stop_screenshots.set()
            stop_monitor_task.cancel()
            _bu_logger.removeHandler(log_handler)
            try:
                await asyncio.wait_for(screenshot_task, timeout=3.0)
            except Exception:
                pass
            # ── Save persistent session cookies before closing ────────────────
            if session_file:
                try:
                    await browser.export_storage_state(output_path=session_file)
                except Exception:
                    pass
            try:
                await browser.stop()
            except Exception:
                pass

        return self._extract_result(history)

    # ── Stop monitor ─────────────────────────────────────────────────────────

    @staticmethod
    async def _stop_monitor(
        session_id: Optional[str],
        agent,
        agent_task: asyncio.Task,
        done_event: asyncio.Event,
    ) -> None:
        """
        Poll TaskQueue.should_stop() every 0.5 s. When the user presses Stop:

          1. agent.stop() — browser-use's own cooperative stop. It sets
             state.stopped and unblocks the agent's pause event, so the run halts
             cleanly at the next step boundary. This is the reliable path: a bare
             asyncio cancel cannot interrupt a blocking LLM call that runs in the
             executor thread, and browser-use can swallow a single CancelledError
             mid-step and keep going to max_steps.
          2. agent_task.cancel() — fast unblock once the cooperative stop has had a
             tick to take effect (covers awaits that ignore the stop flag).

        We keep polling until the run actually ends instead of returning after a
        single attempt, so a swallowed cancel can't leave the browser running.
        """
        if not session_id:
            return
        try:
            from vaf.core.task_queue import TaskQueue
            tq = TaskQueue()
            stop_signaled = False
            while not done_event.is_set() and not agent_task.done():
                if tq.should_stop(session_id):
                    if not stop_signaled:
                        # Cooperative stop first — graceful halt at next step.
                        try:
                            if hasattr(agent, "stop"):
                                agent.stop()
                        except Exception:
                            pass
                        stop_signaled = True
                    elif not agent_task.done():
                        # Cooperative stop didn't end it within a tick — force-unblock.
                        agent_task.cancel()
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    # ── Live screenshot loop ──────────────────────────────────────────────────

    @staticmethod
    def _build_browser_state(agent, task: str, url: str, max_steps: int) -> dict:
        """Derive the browser window's dock state (task, step, action plan, visited URLs,
        vision) from the browser-use agent history. Best-effort and defensive across
        browser-use versions; never raises and never disturbs the run."""
        def _verb(name: str) -> str:
            n = (name or "").lower()
            if any(k in n for k in ("go_to_url", "open_tab", "search_google", "navigate", "go_back")):
                return "nav"
            if "click" in n:
                return "click"
            if any(k in n for k in ("input_text", "type", "fill", "send_keys")):
                return "type"
            if "scroll" in n:
                return "scroll"
            return "read"  # extract_content / done / get_* / wait / default

        def _atext(name: str, params) -> str:
            label = (name or "").replace("_", " ").strip() or "Aktion"
            try:
                if isinstance(params, dict):
                    if params.get("url"):
                        return f"Öffne {params['url']}"
                    if params.get("query"):
                        return f"Suche „{params['query']}\""
                    if "index" in params:
                        return f"{label} [{params['index']}]"
                    if params.get("text"):
                        return f"Tippe „{str(params['text'])[:40]}\""
            except Exception:
                pass
            return label[:1].upper() + label[1:]

        actions: list = []
        history_urls: list = []
        step_n = 0
        vision = "auto"
        try:
            hist = getattr(agent, "history", None) or getattr(getattr(agent, "state", None), "history", None)
            steps = getattr(hist, "history", None) or []
            step_n = getattr(getattr(agent, "state", None), "n_steps", 0) or len(steps)
            try:
                raw_urls = hist.urls() if hasattr(hist, "urls") else []
            except Exception:
                raw_urls = []
            for u in (raw_urls or []):
                if u and u != "about:blank" and (not history_urls or history_urls[-1] != u):
                    history_urls.append(u)
            recent = list(steps)[-30:]
            for i, h in enumerate(recent):
                mo = getattr(h, "model_output", None)
                acts = getattr(mo, "action", None) or []
                first = acts[0] if acts else None
                name, params = "", {}
                if first is not None:
                    try:
                        dumped = first.model_dump(exclude_none=True)
                    except Exception:
                        try:
                            dumped = first.dict(exclude_none=True)
                        except Exception:
                            dumped = {}
                    if dumped:
                        name, params = next(iter(dumped.items()))
                text = ""
                cs = getattr(mo, "current_state", None)
                ng = getattr(cs, "next_goal", None) if cs else None
                if isinstance(ng, str) and ng.strip():
                    text = ng.strip()
                if not text:
                    text = _atext(name, params)
                if len(text) > 160:
                    text = text[:157] + "…"
                actions.append({"verb": _verb(name), "text": text,
                                "status": "active" if i == len(recent) - 1 else "done"})
                if "describe" in (name or "").lower() or "captcha" in (name or "").lower():
                    vision = "aktiv"
        except Exception:
            pass

        if not history_urls and url:
            history_urls = [url]
        return {
            "task": task or "",
            "url": url or (history_urls[-1] if history_urls else ""),
            "status": "running",
            "step": int(step_n or len(actions)),
            "maxSteps": int(max_steps or 0),
            "vision": vision,
            "actions": actions,
            "history": history_urls[-20:],
        }

    @staticmethod
    async def _screenshot_loop(
        browser_session,
        session_id: Optional[str],
        stop_event: asyncio.Event,
        log_queue: Optional[_queue.Queue] = None,
        agent=None,
        task: str = "",
        max_steps: int = 0,
    ) -> None:
        """
        Every ~1.5 s:
          1. Drain browser-use log queue → emit step lines to WebUI console
          2. Take a JPEG screenshot → emit browser_frame_update to WebUI live view
        """
        import base64
        _slog = logging.getLogger(__name__)
        try:
            from vaf.core.web_interface import get_web_interface
            wi = get_web_interface()
        except Exception:
            _slog.warning("[BrowserFrames] no web_interface — live view disabled (session=%s)", session_id)
            return

        _frame_count = 0
        _slog.info("[BrowserFrames] screenshot loop START session=%s", session_id)
        while not stop_event.is_set():
            # ── Drain step log queue ──────────────────────────────────────────
            if log_queue is not None:
                while True:
                    try:
                        line = log_queue.get_nowait()
                        wi.emit_browser_step(line, session_id)
                    except _queue.Empty:
                        break

            # ── Screenshot ────────────────────────────────────────────────────
            try:
                shot = await browser_session.take_screenshot(
                    format="jpeg", quality=55, full_page=False
                )
                url = ""
                try:
                    url = await browser_session.get_current_page_url() or ""
                except Exception:
                    pass
                wi.emit_browser_frame(base64.b64encode(shot).decode(), url, session_id)
                _frame_count += 1
                if _frame_count <= 2 or _frame_count % 5 == 0:
                    _slog.info("[BrowserFrames] emitted frame #%d session=%s (%d bytes) url=%s",
                               _frame_count, session_id, len(shot), url[:80])
                # Structured dock state (task / step / action plan / history) — best-effort,
                # must never disturb the frame stream or the browser run.
                try:
                    if agent is not None:
                        wi.emit_browser_state(
                            BrowserAgentTool._build_browser_state(agent, task, url, max_steps),
                            session_id,
                        )
                except Exception:
                    pass
            except Exception as _e:
                _slog.warning("[BrowserFrames] screenshot/emit failed session=%s: %s", session_id, _e)

            # Wait 1.5 s or until stop_event — whichever comes first
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.5)
                break
            except asyncio.TimeoutError:
                pass

        _slog.info("[BrowserFrames] screenshot loop EXIT session=%s (emitted %d frames)",
                   session_id, _frame_count)

    # ── CDP URL resolution ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_cdp_url(base: str) -> str:
        """
        Fetch /json/version and return the full webSocketDebuggerUrl.
        Accepts both http:// and ws:// base URLs.
        """
        import urllib.request as _urlreq

        http_base = base.replace("ws://", "http://").replace("wss://", "https://")
        url = http_base.rstrip("/") + "/json/version"
        try:
            with _urlreq.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            ws_url = data["webSocketDebuggerUrl"]
            # Ensure the hostname matches what the host can reach
            # (Chromium may report its internal container hostname)
            import re
            ws_url = re.sub(r"ws://[^/]+", f"ws://{http_base.split('//')[1].split('/')[0]}", ws_url)
            return ws_url
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach browser container at {http_base}.\n"
                f"Is `vaf-browser` running? Check: docker ps | grep vaf-browser\n"
                f"Details: {e}"
            ) from e

    # ── Result extraction ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_result(history) -> str:
        # Preferred: agent's own final_result() call
        final = history.final_result() if hasattr(history, "final_result") else None
        if final:
            return f"Browser task completed.\n\nResult:\n{final}"

        # Fallback: last 3 extracted_content values from action results
        action_results = (
            history.action_results() if hasattr(history, "action_results") else []
        )
        contents = [
            r.extracted_content
            for r in action_results[-3:]
            if getattr(r, "extracted_content", None)
        ]
        if contents:
            return "Browser task completed.\n\nExtracted:\n" + "\n---\n".join(contents)

        return "Browser task completed (no extractable result produced)."
