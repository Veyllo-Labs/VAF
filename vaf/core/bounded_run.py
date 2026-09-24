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
import math
import threading
import time
from typing import Callable, Optional

_log = logging.getLogger(__name__)

# Thread-local cancellation flag for the CURRENT bounded worker thread.
# run_bounded sets the event when it abandons the worker (user stop or
# timeout); abandoning cannot kill a Python thread, so long-running tools
# poll cancel_requested() at their loop boundaries and exit early instead
# of crawling on as zombies (which, in local mode, keep occupying the one
# llama-server with dead work).
_thread_cancel = threading.local()


def cancel_requested() -> bool:
    """True inside a bounded worker whose run was stopped or timed out.

    Safe to call from anywhere: outside a bounded worker it returns False.
    Long-running tools should check this between units of work (per search
    result, per page fetch, per summary call) and return early.
    """
    ev = getattr(_thread_cancel, "event", None)
    return bool(ev is not None and ev.is_set())

# Sentinels returned (not raised) when a call is aborted. Kept as recognizable
# string prefixes so callers (agent / workflow engine) can detect them in a result.
TIMEOUT_PREFIX = "[VAF_TOOL_TIMEOUT]"
STOPPED_PREFIX = "[VAF_TOOL_STOPPED]"


def is_abort_sentinel(value) -> bool:
    """True if `value` is a run_bounded timeout/stop sentinel string."""
    s = str(value or "")
    return s.startswith(TIMEOUT_PREFIX) or s.startswith(STOPPED_PREFIX)


def default_timeout_seconds() -> float:
    """The wall-clock budget of a tool call whose tool declares none (``tool_timeout_seconds``)."""
    from vaf.core.config import Config
    return float(Config.get("tool_timeout_seconds", 120))


def tool_budget_seconds(tool, args: dict | None = None) -> float:
    """How long the dispatcher waits for ONE call of ``tool`` with ``args``.

    The tool declares it (``BaseTool.timeout_seconds``, or ``budget_seconds(args)`` when the
    budget follows the call's own arguments); without a declaration it is the default. This
    used to be a list of tool NAMES here (librarian 60 s, browser 1800 s, the sub-agents
    300 s, everything else 120 s), which a tool registered by an embedder could never join,
    and which cut host_bash at 120 s while it accepted a 300-second command. A declaration
    that fails or answers nonsense falls back to the default rather than to "wait forever" -
    which includes infinity and NaN: the deadline arithmetic would never be reached with
    either, so the call could only ever end by Stop.
    """
    declared = None
    fn = getattr(tool, "budget_seconds", None)
    try:
        declared = fn(dict(args or {})) if callable(fn) else getattr(tool, "timeout_seconds", None)
    except Exception:
        declared = None
    try:
        value = float(declared) if declared is not None else None
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return default_timeout_seconds()
    return value


def is_self_supervised(tool) -> bool:
    """Whether ``tool`` governs its own lifetime and must not be wrapped (``BaseTool.self_supervised``)."""
    return bool(getattr(tool, "self_supervised", False))


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
    cancel_ev = threading.Event()

    # Run the tool inside a COPY of the caller's context so context-locals (notably the current
    # session id, see subagent_ipc) propagate into this worker thread. A bare threading.Thread
    # otherwise starts with a fresh context and would fall back to the process-global session id —
    # which is wrong under concurrent workers. The copy also means an *abandoned* worker (freed on
    # timeout/stop but still running) keeps its OWN session context, so its late writes are tagged
    # with the right session instead of whatever a later turn set globally.
    _ctx = contextvars.copy_context()

    def _worker():
        # Expose the cancellation flag to THIS worker thread: abandoning a
        # thread does not kill it, so long-running tools (web_search) poll
        # cancel_requested() at loop boundaries and exit early - otherwise a
        # zombie worker keeps crawling and keeps occupying the single local
        # llama-server with dead work (live incident 2026-07-16).
        _thread_cancel.event = cancel_ev
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
                cancel_ev.set()  # cooperative: the worker exits at its next checkpoint
                _log.warning(
                    "[BoundedRun] '%s' cancelled by stop request after %.1fs "
                    "(worker thread abandoned)", label, time.monotonic() - (deadline - timeout)
                )
                return (
                    f"{STOPPED_PREFIX} '{label}' was cancelled by the user before it "
                    f"finished. The step was aborted so the system stays responsive."
                )

        if time.monotonic() >= deadline:
            cancel_ev.set()  # cooperative: the worker exits at its next checkpoint
            _log.warning(
                "[BoundedRun] '%s' timed out after %.0fs (worker thread abandoned)",
                label, timeout,
            )
            return (
                f"{TIMEOUT_PREFIX} '{label}' did not finish within {int(timeout)}s and "
                f"was abandoned to keep the system responsive. Try a smaller/simpler task."
            )
