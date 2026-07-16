# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF as a library — the public, stable entry door.

This is the thin façade developers build on. It wraps the proven headless
agent path (the same one `vaf prompt` and the background runner use) behind a
small, stable surface that will not churn even as the internal core evolves:

    from vaf import Agent

    agent = Agent(config={"provider": "deepseek"})
    answer = agent.run("Summarise the README in one sentence.")
    print(answer)

Design notes
------------
- `Agent` here is the *façade*. The full internal engine remains importable as
  `vaf.core.agent.Agent` (re-exported below as `CoreAgent`) for advanced use.
- The core engine is imported lazily (inside `run()`), so merely constructing a
  façade `Agent` — or doing `import vaf` — never pays the cost of loading the
  ~9k-line core module and its dependency chain.
- Embedding-safe by default: we set `VAF_NONINTERACTIVE=1` (via `setdefault`, so
  an explicit caller still wins). That makes the tool-confirmation gates return
  an error instead of blocking on stdin/WebSocket — an embedded library must
  never hang waiting for a human. Grant specific tools via the trust mechanisms
  (`mark_trusted_dir`, `set_tool_policy`; persisted in trust.json under the
  platform config dir, e.g. ~/.config/vaf/ on Linux) when you want dangerous
  tools to run unattended.
- Stateful across calls: one façade `Agent` keeps one conversation. Repeated
  `run()` calls continue the same history (multi-turn). Create a new `Agent`
  for an independent conversation.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

# Re-export the full internal engine for advanced users who need direct access.
# Imported lazily on attribute access would be cleaner, but this module is itself
# only imported on first `vaf.Agent` access (see vaf/__init__.py), so importing
# the core here is already deferred far enough.
from vaf.core.agent import Agent as CoreAgent

__all__ = ["Agent", "CoreAgent"]


class Agent:
    """Stable, embeddable façade over the VAF agent runtime.

    Parameters
    ----------
    config:
        Optional dict of config overrides merged on top of ~/.vaf/config.json
        for this instance only (nothing is written to disk). Common keys:
        ``provider`` ("local" | "openai" | "anthropic" | "google" | "deepseek" |
        "openrouter" | "veyllo"), ``model``, ``api_key_<provider>``, ``n_ctx``,
        ``temperature``. See vaf/core/config.py for the full schema.
    verbose:
        Forwarded to the core agent (extra diagnostic output).
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        verbose: bool = False,
        user_scope: Optional[str] = None,
    ):
        self._config = dict(config) if config else None
        self._verbose = verbose
        self._agent: Optional[CoreAgent] = None
        self._pending_tools: list = []
        # Multi-tenant identity (docs/EMBEDDING.md "Multi-tenant embedding"):
        # user_scope is an ASSERTION by the embedder - the library performs no
        # authentication (the process boundary is the trust boundary, same as
        # the gateway trusting itself). Validate at the boundary: a bad value
        # must fail HERE, loudly, never fall back to the machine owner's data.
        self._user_scope = None
        self._scope_username: Optional[str] = None
        if user_scope is not None:
            import uuid as _uuid

            try:
                self._user_scope = _uuid.UUID(str(user_scope))
            except (ValueError, AttributeError, TypeError):
                raise ValueError(
                    "user_scope must be a valid UUID string"
                ) from None
        # Embedding-safe default: never block on a human. `setdefault` lets an
        # explicit `VAF_NONINTERACTIVE=0` from the caller take precedence.
        os.environ.setdefault("VAF_NONINTERACTIVE", "1")

    def _bind_identity(self, agent: "CoreAgent") -> None:
        """Bind scope AND username together onto the engine.

        Username resolution goes through the same helper the background lanes
        use: the real account name when the scope maps to a local user, else
        a synthetic per-scope name - NEVER the literal "admin", which the
        engine's injection sites would otherwise fall back to (that fallback
        stamps the admin's identity into a foreign tenant's artifacts).
        """
        if self._scope_username is None:
            username = None
            try:
                from vaf.core.thinking_mode import _resolve_username_for_scope

                username = _resolve_username_for_scope(self._user_scope)
            except Exception:
                username = None
            self._scope_username = (
                username or f"scope_{str(self._user_scope).replace('-', '')[:8]}"
            )
        agent._current_user_scope_id = self._user_scope
        agent._current_username = self._scope_username

    def add_tool(self, tool) -> None:
        """Register a BaseTool instance for THIS Agent instance only.

        Call before the first ``run()`` / ``.core`` access: the system prompt
        is built once at engine build, and the per-instance wiring runs there
        too, so the facade rejects later additions rather than leaving the
        tool half-visible. A tool with the same name as an existing one wins
        (same last-write semantics as the entry-point loader).

        Raises RuntimeError after the engine was built, TypeError for
        non-BaseTool values, ValueError for coder-only tools (per-instance
        tools target the main agent).
        """
        from vaf.tools.base import BaseTool

        if self._agent is not None:
            raise RuntimeError(
                "add_tool() must be called before the first run()/.core access"
            )
        if not isinstance(tool, BaseTool):
            raise TypeError("add_tool() expects a BaseTool instance")
        if getattr(tool, "coder_only", False):
            raise ValueError(
                "coder_only tools cannot be registered on the main agent"
            )
        self._pending_tools.append(tool)

    @property
    def core(self) -> CoreAgent:
        """The underlying core agent, constructed and chat-initialised on first use."""
        if self._agent is None:
            # register_signals=False is mandatory when embedding: signal handlers
            # may only be installed on the main thread, so a host worker thread
            # would otherwise crash.
            agent = CoreAgent(
                verbose=self._verbose,
                register_signals=False,
                config_overrides=self._config,
            )
            # Per-instance tools go in BEFORE init_chat so the system prompt
            # and the tool schemas the model sees include them. Mirror the
            # engine's own post-load wiring passes so these tools behave
            # exactly like entry-point tools (registry handle, state provider).
            for tool in self._pending_tools:
                agent.tools[tool.name] = tool
                if hasattr(tool, "available_tools"):
                    try:
                        tool.available_tools = agent.tools
                    except Exception:
                        pass
                try:
                    if hasattr(tool, "create_state_provider"):
                        provider = tool.create_state_provider()
                        if provider:
                            agent.state_registry.register(f"tool_{tool.name}", provider)
                except Exception:
                    pass
            # Identity BEFORE init_chat: the system prompt is built from it
            # (user context, memory seed, last interaction).
            if self._user_scope is not None:
                self._bind_identity(agent)
            agent.init_chat()
            # Local mode has no lazy load inside chat_step: without a backend
            # the turn aborts ("Agent not initialized") and run() would return
            # an empty string. Mirror chat_step's own guard and load here, so
            # the first run() downloads/starts (or reuses) the one local
            # llama server exactly like the CLI lanes do.
            if agent.api_backend is None and not agent.llm and not agent.use_server:
                agent.load_model()
            self._agent = agent
        return self._agent

    def run(self, prompt: str, on_token: Optional[Callable[[str], None]] = None) -> str:
        """Send one message and return the final assistant answer (reasoning stripped).

        Parameters
        ----------
        prompt:
            The user message.
        on_token:
            Optional streaming callback; receives text deltas as they arrive.
            Note: for reasoning models these deltas may include the model's
            ``<think>...</think>`` block. The returned value is always the
            cleaned final answer regardless.

        Why we also capture the stream
        ------------------------------
        ``chat_step`` returns a cleaned copy of the raw turn text, but for
        reasoning models (e.g. DeepSeek) the visible answer is streamed while the
        return value collapses to a placeholder ("..."). So we accumulate the
        stream and, when the direct return is empty/degenerate, fall back to the
        reasoning-stripped streamed text. For non-reasoning models the two agree.
        """
        buf: list[str] = []

        def _sink(s):
            if isinstance(s, str) and s:
                buf.append(s)
            if on_token is not None:
                on_token(s)

        core = self.core
        # Re-assert identity every turn: session loads rebind identity from
        # session metadata unconditionally, so a bound scope must be
        # re-applied (same pattern as the headless runner). Cheap after the
        # first call (username is cached).
        if self._user_scope is not None:
            self._bind_identity(core)
        result = core.chat_step(prompt, stream_callback=_sink)
        result = result.strip() if isinstance(result, str) else ""

        streamed = "".join(buf)
        cleaned_stream = ""
        if streamed:
            try:
                cleaned_stream = self.core._clean_reasoning(streamed).strip()
            except Exception:
                cleaned_stream = streamed.strip()

        # A bare ellipsis (or empty) is the core's placeholder when the real
        # answer was streamed instead of returned — prefer the streamed answer.
        degenerate = (not result) or set(result) <= {".", "…"}
        if degenerate and cleaned_stream:
            return cleaned_stream
        return result or cleaned_stream
