# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Bounded, stop-aware execution of a blocking callable.

The problem this solves: tools and in-process sub-agents are called synchronously
in the single worker thread (e.g. `result = tool.run(**args)` in the workflow
engine and in `Agent.execute_tool`). If one of them blocks forever, the whole VAF
backend freezes and the Stop button does nothing, because `should_stop` is only
polled *between* turns, never *during* a tool call.

`run_bounded()` runs the callable on a daemon worker thread and waits for it with a
hard deadline and a frequent stop-check. If the deadline passes or stop is
requested, the caller is freed immediately and gets a clear sentinel string back
instead of hanging.

Python caveat: a thread cannot be force-killed. On timeout/stop we *abandon* the
worker thread (it keeps running in the background until it finishes on its own),
but the caller — and therefore the whole backend — stays responsive. Genuinely
unkillable work belongs in a child process (run it out of process so it can be killed).
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Callable, Optional

_log = logging.getLogger(__name__)

# Sentinels returned (not raised) when a call is aborted. Kept as recognizable
# string prefixes so callers (agent / workflow engine) can detect them in a result.
TIMEOUT_PREFIX = "[VAF_TOOL_TIMEOUT]"
STOPPED_PREFIX = "[VAF_TOOL_STOPPED]"


def is_abort_sentinel(value) -> bool:
    """True if `value` is a run_bounded timeout/stop sentinel string."""
    s = str(value or "")
    return s.startswith(TIMEOUT_PREFIX) or s.startswith(STOPPED_PREFIX)


# Tools that manage their OWN cancellation + lifecycle and are legitimately long-running,
# so they must NOT be wrapped by run_bounded — a hard timeout would abandon them mid-work
# while they are actively making progress (the abandoned thread keeps running).
#   - browser_agent: runs an asyncio browser session for minutes; has its own _stop_monitor
#     polling TaskQueue.should_stop + browser-use max_steps/max_failures internal limits.
#   - create_agent_workflow / execute_workflow: orchestrators that run an already per-step
#     bounded, stop-aware WorkflowEngine internally (bounding them again double-bounds).
#   - python_sandbox: runs code in a Docker container and already supervises itself — its
#     persistent-container path is a stop-aware poll loop with its own deadline that kills the
#     docker exec (and the in-container process) the moment Stop is requested. Wrapping it in
#     run_bounded would instead *abandon* the thread, and the abandoned thread can lose the
#     should_stop flag to clear_stop before it gets to kill the exec. Self-supervising keeps the
#     poll loop running in the worker thread, where should_stop is still set, so the kill is prompt.
#   - coding_agent: a real edit on a large file legitimately takes many minutes; the agentic
#     loop governs itself (idle-based safety timeout + stuck-detection + a final_commit on every
#     exit path) and polls TaskQueue.should_stop each iteration so the Stop button breaks it
#     cleanly. A flat run_bounded timeout would instead ABANDON it mid-edit — leaving the file
#     half-written and telling the user to "try a smaller task" while the coder was making progress.
SELF_SUPERVISED_TOOLS = frozenset({
    "browser_agent",
    "create_agent_workflow",
    "execute_workflow",
    "python_sandbox",
    "coding_agent",
})


def agent_timeout_seconds(tool_name: str) -> float:
    """
    Wall-clock budget (seconds) for a single in-line tool / sub-agent call, used by the
    bounded wait in both the agent and the workflow engine. Per-agent so a fast filesystem
    agent isn't forced to make the user wait the full research budget.
    """
    from vaf.core.config import Config
    if tool_name == "librarian_agent":
        # Filesystem ops should return fast; if they don't they're stuck on a huge tree
        # or a hung mount, and a long wait helps nobody.
        return float(Config.get("librarian_timeout_seconds", 60))
    if tool_name == "browser_agent":
        # Browsing is legitimately slow (page loads, multi-step). Generous budget so a
        # normal task is never cut off, but bounded so a hung browser can't block a
        # workflow forever. (browser-use also caps itself via max_steps.)
        return float(Config.get("browser_timeout_seconds", 300))
    if tool_name in ("coding_agent", "research_agent", "document_agent"):
        return float(Config.get("subagent_timeout_seconds", 300))
    return float(Config.get("tool_timeout_seconds", 120))


def run_bounded(
    fn: Callable[[], object],
    *,
    timeout: float,
    stop_check: Optional[Callable[[], bool]] = None,
    poll: float = 0.5,
    label: str = "tool",
) -> object:
    """
    Run ``fn()`` on a worker thread; never block the caller longer than ``timeout``
    seconds, and abort early as soon as ``stop_check()`` returns True.

    Returns:
        - whatever ``fn()`` returned, on normal completion, OR
        - a sentinel string (``TIMEOUT_PREFIX``/``STOPPED_PREFIX`` …) when aborted.

    Re-raises any exception raised by ``fn`` in the caller's thread (so existing
    try/except around the original call keeps working unchanged).
    """
    timeout = max(1.0, float(timeout))
    poll = max(0.05, float(poll))

    box: dict = {}
    done = threading.Event()

    # Run the tool inside a COPY of the caller's context so context-locals (notably the current
    # session id, see subagent_ipc) propagate into this worker thread. A bare threading.Thread
    # otherwise starts with a fresh context and would fall back to the process-global session id —
    # which is wrong under concurrent workers. The copy also means an *abandoned* worker (freed on
    # timeout/stop but still running) keeps its OWN session context, so its late writes are tagged
    # with the right session instead of whatever a later turn set globally.
    _ctx = contextvars.copy_context()

    def _worker():
        try:
            box["value"] = _ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 — preserved and re-raised below
            box["error"] = exc
        finally:
            done.set()

    t = threading.Thread(target=_worker, name=f"vaf-bounded-{label}", daemon=True)
    t.start()

    deadline = time.monotonic() + timeout
    while True:
        if done.wait(timeout=poll):
            if "error" in box:
                raise box["error"]  # re-raise in caller, exact type preserved
            return box.get("value", "")

        # Worker still running — check cooperative stop first, then the deadline.
        if stop_check is not None:
            try:
                stop = bool(stop_check())
            except Exception:
                stop = False
            if stop:
                _log.warning(
                    "[BoundedRun] '%s' cancelled by stop request after %.1fs "
                    "(worker thread abandoned)", label, time.monotonic() - (deadline - timeout)
                )
                return (
                    f"{STOPPED_PREFIX} '{label}' was cancelled by the user before it "
                    f"finished. The step was aborted so the system stays responsive."
                )

        if time.monotonic() >= deadline:
            _log.warning(
                "[BoundedRun] '%s' timed out after %.0fs (worker thread abandoned)",
                label, timeout,
            )
            return (
                f"{TIMEOUT_PREFIX} '{label}' did not finish within {int(timeout)}s and "
                f"was abandoned to keep the system responsive. Try a smaller/simpler task."
            )
