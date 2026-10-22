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

`run_bounded_all()` bounds several independent calls at once and keeps the answers that
arrived in time; `DaemonPool` is the executor for calls that may hang, with a thread cap
that holds across all its callers.
"""
from __future__ import annotations

import contextvars
import logging
import math
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from typing import Any, Callable, Dict, Hashable, List, Mapping, Optional, Tuple

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

def cancel_check() -> Callable[[], bool]:
    """``cancel_requested`` of THIS worker, as a callable any thread may poll.

    The flag is per thread, so a helper thread that watches a child process on the worker's
    behalf (the worker itself is blocked reading the child's output) takes this from the
    worker first. Outside a bounded worker it never answers True.
    """
    ev = getattr(_thread_cancel, "event", None)
    return ev.is_set if ev is not None else (lambda: False)


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


class DaemonPool(Executor):
    """At most ``workers`` daemon threads behind the ``Executor`` interface, started as work
    arrives.

    For calls that may never return, such as a DNS lookup nobody answers: the workers of a
    ``ThreadPoolExecutor`` are joined when the interpreter exits, so one such call holds up
    the exit of the process it ran in, a CLI command that has already printed its answer
    too (measured: 3.04 s for a 3 s call, 0.24 s here). The cap holds across every caller
    of one pool, so calls that hang occupy at most ``workers`` threads however often they
    are made, and later calls wait in the queue. A future that has not started can be
    cancelled and is then never run. Each call runs in a copy of the submitter's context,
    as ``run_bounded``'s does.

    A worker is counted once it started: one that could not start would otherwise hold its
    place under the cap for good, and a pool whose every start failed would never run
    anything again (measured). When no worker runs, the failure reaches the caller and its
    call is cancelled; when one does, the call waits for it.
    """

    def __init__(self, workers: int, name: str) -> None:
        self._workers = max(1, int(workers))
        self._name = name
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        self._queue.put((future, contextvars.copy_context(), fn, args, kwargs))
        with self._lock:
            if len(self._threads) < self._workers:
                worker = threading.Thread(target=self._work, daemon=True,
                                          name=f"{self._name}-{len(self._threads)}")
                try:
                    worker.start()
                except BaseException:
                    if not self._threads:
                        future.cancel()
                        raise
                    _log.warning("[DaemonPool] '%s' could not start another worker; %d run",
                                 self._name, len(self._threads))
                else:
                    self._threads.append(worker)
        return future

    def _work(self) -> None:
        while True:
            future, ctx, fn, args, kwargs = self._queue.get()
            if not future.set_running_or_notify_cancel():
                continue  # cancelled while it waited: never run
            try:
                result = ctx.run(fn, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - handed to the future's owner
                future.set_exception(exc)
            else:
                future.set_result(result)


class InFlight:
    """The calls of ``run_bounded_all`` still running, by key, each with the deadline of the
    caller that started it. Shared by the callers whose calls mean the same thing for the
    same key (a lookup of one name), so a key runs once at a time.

    ``join_wait`` caps how long a caller waits for a call it joined (None: no cap of its own;
    the limits ``run_bounded_all`` names still hold). A quick answer still reaches it, while
    a call that ends late, or fails late, costs only the caller that started it: with lookups
    that failed after 1.5 s, every invitation that joined one waited those 1.5 s, and the
    invitations of other accounts queued behind them (measured)."""

    def __init__(self, join_wait: Optional[float] = None) -> None:
        self._lock = threading.Lock()
        self._calls: Dict[Hashable, Any] = {}
        self.join_wait = None if join_wait is None else max(0.0, float(join_wait))


def _timed(fn: Callable[[], Any]) -> Tuple[Any, float]:
    """``fn()`` and the moment it returned, taken where the call ends: every caller that
    shares the future reads the same time, and none has to register anything on it."""
    value = fn()
    return value, time.monotonic()


def run_bounded_all(calls: Mapping[Hashable, Callable[[], Any]], *, timeout: float,
                    pool: Executor, in_flight: Optional[InFlight] = None) -> Dict[Hashable, Any]:
    """Run every call on ``pool`` and return, keyed like ``calls``, the answers that arrived
    within ``timeout`` seconds in all.

    ``run_bounded`` bounds ONE call and answers a tool's timeout text; this is for several
    independent calls whose answers count each on its own, so the ones that arrived in time
    are kept. A call that raised is left out, and so is one that was still queued (without
    ``in_flight`` it is cancelled, so it never starts; with one it stays queued, below) or
    still running when the time was up (it is abandoned like ``run_bounded``'s worker and
    finishes on its own). Give it a ``DaemonPool`` of its own when the calls can hang, so
    their threads stay capped and never hold up the exit.

    With ``in_flight``, a key whose call another caller started and has not finished, running
    or queued, is not started again; this caller joins it instead. A call still queued when
    the time is up stays queued for whoever asks next, instead of being cancelled, so at
    most one call per key waits. Measured before: every later caller waited the full time
    again, because it started the calls cancelled from the queue once more (another
    account's invitation waited 32 s at 60 connections).

    A key counts only if its call finished by the key's own limit: for a call this caller
    started, its own deadline; for a call it joined, the earliest of its own deadline, the
    deadline of the caller that started it and, with ``InFlight(join_wait=...)``, the time it
    joined plus ``join_wait``. A key whose limit passed is dropped, and the caller returns
    once every key is answered or dropped. One stop time for all keys let an answer count
    after its own limit (a joined call answered after its starter's deadline was kept when
    another key's limit was later), and a caller that joined a hung call and started one
    that answered at once waited its full time instead of ``join_wait``. The time a call
    finished is taken where it ends (``_timed``), not when this caller next looks: a call
    that finished after its limit, just before the loop looked again, used to count.
    """
    now = time.monotonic()
    deadline = now + max(0.0, float(timeout))
    futures: Dict[Hashable, Future] = {}
    until: Dict[Hashable, float] = {}
    started = set()
    if in_flight is None:
        for key, fn in calls.items():
            futures[key], until[key] = pool.submit(_timed, fn), deadline
            started.add(key)
    else:
        with in_flight._lock:
            for key, fn in calls.items():
                running = in_flight._calls.get(key)
                if running is not None and not running[0].done():
                    futures[key], until[key] = running
                    if in_flight.join_wait is not None:
                        until[key] = min(until[key], now + in_flight.join_wait)
                    continue
                futures[key], until[key] = pool.submit(_timed, fn), deadline
                in_flight._calls[key] = (futures[key], deadline)
                started.add(key)
    for key in until:
        until[key] = min(until[key], deadline)
    answers: Dict[Hashable, Any] = {}
    pending = dict(futures)
    while pending:
        for key in [k for k, future in pending.items() if future.done()]:
            future = pending.pop(key)
            if not future.cancelled() and future.exception() is None:
                value, finished = future.result()
                if finished <= until[key]:
                    answers[key] = value
        moment = time.monotonic()
        for key in [k for k in pending if until[k] <= moment]:
            pending.pop(key)  # its limit passed before its call finished
        if pending:
            nearest = min(until[k] for k in pending)
            wait(list(pending.values()), timeout=max(0.0, nearest - moment),
                 return_when=FIRST_COMPLETED)
    if in_flight is None:
        for key in started:
            if key not in answers:
                futures[key].cancel()
    return answers
