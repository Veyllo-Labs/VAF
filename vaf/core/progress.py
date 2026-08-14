# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One gate in front of every live agent view, so a dropped frame is a decision.

Five sub-agent tools feed a live window in the Web UI - coder, research, document,
librarian, browser - through six publisher blocks and eight emit methods. Four of the six
say in their own docstrings that they copy another one: "Mirrors the coder's coder_state
pattern", "Mirrors research's _emit_research_state", "resolves the session id like the
document agent". They do not, quite. This module is the thing all four were describing.

MEASURED BEFORE BUILDING. Every clause below has a number behind it, taken from the tree
as it stood:

- 8 emit methods on ``web_interface``. 5 of them have BYTE-IDENTICAL seven-line bodies
  differing in exactly one string literal; the other 3 repeat the same six-line transport
  fork under a different payload build. 48 lines of fork expressing one rule.
- 3 verbatim copies of the same "environment ``VAF_SESSION_ID``, then the IPC context"
  block (coder, document, librarian), plus a fourth env-ONLY variant in research that no
  comment marks as different, plus a fifth site (browser) that resolves once at run setup
  and threads the value down five call frames.
- 4 different throttles for 6 publishers: a hash with no clock (coder state), a 0.35s
  clock with no hash (coder code), 0.4s AND a hash (research, document), and nothing at
  all (librarian, browser).
- ``force`` means two different things in two adjacent files. In research it bypasses the
  clock and NOT the hash; in document it bypasses both. Neither file says so.

THE WIRE IS THE CONTRACT AND IT IS TAKEN APART FIELD BY FIELD ON THE OTHER SIDE.
``web/app/page.tsx`` rebuilds every one of these payloads key by key. A field that is
added, renamed or not forwarded is not an error anywhere; it is absent. That has already
happened twice in this repo, to ``diffs`` and to ``activity``. So this module builds no
payload, renames no key, injects no field and normalises no value. It decides only WHETHER
to send.

WHAT LOOKS LIKE A FREE FIELD IS A LOADED GUN. On the sub-agent bridge the payload is
validated by ``web_server.SubAgentStreamUpdate``, which declares typed optional fields with
``extra="allow"``. Unknown keys ride through untyped; a DECLARED key at the wrong type is a
ValidationError, and ``_post_to_parent`` has no ``raise_for_status``, so the POST is
swallowed and every event of that run disappears - in the bridge lane only, which is the
lane the coder, the browser and every workflow child actually run in. ``progress`` is
declared ``Optional[int]`` there and the coder's own progress reads ``"Task 1/3"``. That is
why this class has no ``progress`` and no ``agent_type`` constructor argument: not unused,
absent.

NO SESSION ID IS NOT "UNSCOPED". Below the emit methods, ``_push_session_update`` falls
through to ``push_update`` on a falsy session id, and ``push_update`` broadcasts to EVERY
connected client. Four of the five state publishers guard against that today by returning
early, and that guard is load-bearing, not defensive: one user's source file, one user's
research topic and one user's filesystem listing all ride these streams. ``publish``
therefore refuses a falsy session id before it touches any bookkeeping.

THE PUBLISHER DOES NOT RESOLVE THE SESSION, ON PURPOSE. Resolution stays at the call site,
above the payload build, because at two sites the payload build is the expensive part: the
coder's is 6+ ``git`` subprocesses per emit and the librarian's is a filesystem rescan that
WRITES a cache file. A publisher that resolved internally would force the build to happen
first and pay that cost on every run with no viewer at all.

PER RUN, NEVER PER PROCESS. The dedup cell and the clock live on the instance, and the
instance belongs to the run. The frontend resets a view's state at every sub-agent task
start, so a publisher that outlived its run would suppress run 2's first frame as a
duplicate of run 1's last - and that first frame is the one that opens the window.

NAMED BOUNDARY: deliberately NOT on the public facade, and neither is
``WebInterface.emit_agent_state``. The framework half of this round is real - before it, an
agent built on VAF could feed a live view only by picking one of eight hardcoded emitters
named after VAF's own agents; now it declares its own wire type and calls one primitive.
What is not earned is the EXPORT. Two measurements say so: zero embedders have asked, and
the shipped Web UI has no handler for a type it does not know, so exporting the publisher
alone would hand out half a contract and no way to complete it. The export is one lazy
``__getattr__`` branch on the day an embedder ships a view consumer of their own.

THE SECOND HALF OF THIS MODULE ANSWERS A DIFFERENT QUESTION AND TAKES A DIFFERENT ROAD.
``set_run_progress`` / ``read_run_progress`` carry "how far along is this run" - two
integers, for a terminal task line rather than a web window. They do NOT ride the view
transport above, and they do not ride the agent's event sink either, because in the shipped
default a sub-agent is a CHILD PROCESS: it is constructed bare, with no agent object and
therefore no sink, and a callable does not cross a process boundary. The one up-channel
that does is the IPC task record, which the parent already polls per session. So progress
is parked in memory here and stamped onto that record by the heartbeat write that already
happens every 3 seconds - measured added cost: none, because the record is rewritten
either way.

WHY THERE IS NO PROGRESS EVENT AND NO PROCESS-WIDE SINK, since both were planned. No
producer has a sink to emit on; the terminal consumer already polls the record every tick,
so an event buys it nothing; and ``docs/OBSERVABILITY.md`` publishes both a closed event
set and a strict per-call ordering that an unpaired event fired from another thread would
break. A process-wide PUSH sink is also the wrong shape here: the two process-wide hooks
this repo has accepted are PULL resolvers that take an identity as an argument, while a
push sink carries none and cannot be filtered by whoever registered it - in a process
serving N tenants as N threads, that is a leak by construction. The event becomes earned
the day a consumer exists that cannot poll.

Nothing here imports ``fastapi``. ``vaf/core/web_interface.py`` does, at module scope, and
``fastapi`` is an optional extra - so the hop to it is a LATE import inside ``publish``.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

__all__ = [
    "VAF_LIVE_VIEW_TYPES",
    "resolve_ui_session_id",
    "StatePublisher",
    "set_run_progress",
    "read_run_progress",
]


# The wire types VAF's own live views ride on. Declared here so a CI guard can pin them
# against the handlers in web/app/page.tsx: this file and that switch are two halves of one
# contract written in two languages, and nothing else connects them. An embedder's own view
# type is deliberately NOT required to be in here.
VAF_LIVE_VIEW_TYPES = frozenset({
    "coder_state",
    "research_state",
    "document_state",
    "librarian_state",
    "browser_state",
    "learn_state",            # batched document learning: batch N of M for the banner
    "subagent_update",        # the coder's live editor feed; NOT derivable from any name
    "browser_frame_update",
    "browser_step_update",
})


def resolve_ui_session_id() -> str:
    """Which Web UI session this run feeds, or "" for none.

    A thin adapter over ``subagent_ipc.get_current_session_id()``, which answers per
    CONTEXT and falls back to ``VAF_SESSION_ID`` only when a context was never told - the
    child-process case. This function exists for the return TYPE: publishers read
    ``if not sid: return``, and an empty string keeps that idiom honest where ``None``
    would have to be special-cased at every call site.

    Contract, each choice against its failure mode:

    - It does NOT read the environment itself, and used to. Reading it first was the pivot
      of a cross-tenant defect: three tool-dispatching lanes are threads in one process,
      the parent wrote that variable on every dispatch and never restored it, and a
      scheduled run - which belongs to no web session at all - would find a live chat
      turn's id here and publish one tenant's content into another tenant's browser.
    - ``""`` and never ``None``, and a whitespace-only answer is also ``""``. A blank
      string that reaches the transport takes the unscoped global-broadcast branch, so
      empty is not automatically the safe direction; the publishers refuse it explicitly.
    """
    try:
        from vaf.core.subagent_ipc import get_current_session_id

        return get_current_session_id() or ""
    except Exception:
        return ""


class StatePublisher:
    """One live view's gate: is anyone listening, is this frame due, is it new.

    Owns exactly four things - the wire type, the clock, the dedup cell and the late hop
    to the transport. It does not build the payload, does not resolve the session, does not
    swallow and does not add a single key.

    Contract, each choice against its failure mode:

    - ``msg_type`` IS the literal type the frontend switches on, and the receiver uses it
      undecorated. Deriving it (``f"{kind}_state"``) would rename ``subagent_update``, the
      type the coder's live editor feed rides on, into one that matches no branch in
      page.tsx - and that chain has no default branch, so the pane goes dark silently.
    - ``state`` is ALREADY BUILT by the caller and is sent as-is. The key renames, the
      list snapshots and the tail caps are wire contract belonging to the agent that knows
      why; a transform hook here would be an adapter, and a key-mapping layer would be the
      drop trap rebuilt in Python.
    - A falsy ``session_id`` returns ``False`` having touched NOTHING - not the clock, not
      the hash. Sending unscoped is a global broadcast of one user's content, and leaving
      the bookkeeping alone is why the first frame after a session appears goes out at once
      instead of landing inside a window opened by a send that never happened.
    - ``force`` bypasses the CLOCK and never the HASH. They answer different questions: the
      clock asks "is it too soon", which a caller can legitimately override; the hash asks
      "does the receiver already have this", which a caller cannot know better.
    - Both cells are stamped BEFORE the send, so an emit that raises still consumes its
      window and still burns its hash. That is today's behaviour at all three hash sites,
      and it is the safe direction: a failing transport must not be retried at full loop
      speed with a payload carrying several large diffs.
    - The dedup sentinel is ``None``, never ``0``. ``hash()`` can legitimately return 0,
      and with hash randomisation that would be a dropped frame nobody can reproduce.
    - ``dedupe`` is OFF by default and ``min_interval`` is 0.0 by default: a publisher that
      silently drops frames by default is the wrong default for a live view.
    - Per-instance state, and the instance belongs to the RUN. Two concurrent in-process
      runs must not share one budget, and a publisher outliving its run suppresses the next
      run's first frame - the frame that opens the window.
    - Does NOT catch exceptions. Every caller already has a blanket swallow; a second one
      here would make ``False`` mean both "gated" and "failed", and the telemetry that
      gates on the return value would start claiming emits that never happened.
    - Returns ``True`` only when the emit was actually handed to the transport, which is
      what makes ``if pub.publish(...) and lg:`` an honest telemetry gate.
    """

    __slots__ = ("msg_type", "min_interval", "dedupe", "_last_at", "_last_hash")

    def __init__(self, msg_type: str, *, min_interval: float = 0.0, dedupe: bool = False):
        self.msg_type = msg_type
        self.min_interval = float(min_interval)
        self.dedupe = bool(dedupe)
        self._last_at = 0.0
        self._last_hash: Optional[int] = None

    def publish(self, state: Optional[dict], *, session_id: str = "",
                force: bool = False) -> bool:
        """Send one frame if it is due, new and addressed. True only when it went out.

        Order is load-bearing: session, then clock, then hash, then stamp, then send. A
        hash computed before the clock spends a ``json.dumps`` of the whole state on frames
        that were never eligible; a stamp written after the send lets a raising transport be
        retried at full loop speed.
        """
        if not session_id:
            # A room-ordered child may run with NO session at all (the runner's
            # room frame binds no chat) - measured live: the whole editor/code
            # stream of such a run died here while its status events flowed.
            # The ordering room is a full address of its own: the child's
            # bridge stamps it from VAF_ROOM_ID and the endpoint routes it to
            # the room's tenant. Only a frame with NEITHER anchor goes nowhere.
            import os
            if not os.environ.get("VAF_ROOM_ID", "").strip():
                return False
        now = time.time()
        if not force and self.min_interval and (now - self._last_at) < self.min_interval:
            return False
        if self.dedupe:
            state_hash = hash(json.dumps(state or {}, sort_keys=True, default=str))
            if state_hash == self._last_hash:
                return False
            self._last_hash = state_hash
        self._last_at = now
        from vaf.core.web_interface import get_web_interface

        get_web_interface().emit_agent_state(self.msg_type, state, session_id=session_id)
        return True


# ── sub-agent run progress: two integers, carried by a write that already happens ──

_run_progress: Optional[tuple] = None


def set_run_progress(done: int, total: int) -> None:
    """This run is at ``done`` of ``total`` planned units. In memory, no I/O, no clock.

    A sub-agent child cannot hand a callable back to its parent, so its progress travels
    the only up-channel that survives a process boundary: the IPC task record. This
    function does not write it. It parks two integers where the heartbeat thread - which
    already rewrites that record every 3 seconds - picks them up on its next pulse.

    Contract, each choice against its failure mode:

    - ARMED ONLY INSIDE A SUB-AGENT CHILD, and the key is ``VAF_IN_SUBAGENT_TERMINAL``.
      A module-level cell is per-RUN only where the process IS the run, which holds for
      the child and fails for the parent: with ``parallel_main_workers > 1`` the headless
      runner serves N tenants as N THREADS in ONE process, and two concurrent in-process
      sub-agent runs would take turns overwriting one cell under two different users.
      Outside a child this is a no-op, so no parent lane can arm it by accident.
      ``VAF_TASK_ID`` would be the wrong key: the workflow engine sets it process-globally
      in the PARENT, which would arm the cell in exactly the multi-tenant process this
      guard exists to exclude.
    - No throttle, no dedup, no lock, no file. The transport's own 3-second cadence IS the
      throttle, which is why a producer may call this on every count change without a
      budget: this is a tuple assignment, against a mutation of a shared JSON file whose
      guard degrades to an unlocked read-modify-write under contention.
    - Two integers and nothing else. No phase, no title, no stage string. The record they
      land on is read UNFILTERED by the runner's sub-agent loop and attributed to the
      current worker's session when the record carries none, so anything on it derived
      from a user's prompt is a cross-user leak with a longer fuse.
    - Coerces to ``int`` and floors at 0. The value crosses a JSON boundary into a file
      several readers parse; a float, a numpy int or a ``None`` out of a producer's
      arithmetic would either widen the record's type surface or raise inside a swallow.
    - Never raises. Producers call this from inside their own loops, and a progress helper
      must not become the reason a coding run dies.
    """
    global _run_progress
    if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        _run_progress = (max(0, int(done)), max(0, int(total)))
    except Exception:
        return


def read_run_progress() -> Optional[tuple]:
    """The counts this run last parked, or ``None`` if it never parked any.

    Reading does NOT consume. The heartbeat pulses every 3 seconds and the producer may
    go minutes without a count change; take-semantics would blank the record between
    changes and the display would flicker back to nothing.

    ``None`` is a real answer and must reach the writer as ``None``: it means "this agent
    does not report progress", which three of the five sub-agents legitimately are, and
    the writer leaves the record's fields untouched for it.
    """
    return _run_progress
