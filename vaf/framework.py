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
  (`mark_trusted_dir`, `set_tool_policy`, ~/.vaf/trust.json) when you want
  dangerous tools to run unattended.
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
        "openrouter"), ``model``, ``api_key_<provider>``, ``n_ctx``,
        ``temperature``. See vaf/core/config.py for the full schema.
    verbose:
        Forwarded to the core agent (extra diagnostic output).
    """

    def __init__(self, config: Optional[dict] = None, *, verbose: bool = False):
        self._config = dict(config) if config else None
        self._verbose = verbose
        self._agent: Optional[CoreAgent] = None
        # Embedding-safe default: never block on a human. `setdefault` lets an
        # explicit `VAF_NONINTERACTIVE=0` from the caller take precedence.
        os.environ.setdefault("VAF_NONINTERACTIVE", "1")

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
            agent.init_chat()
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

        result = self.core.chat_step(prompt, stream_callback=_sink)
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
