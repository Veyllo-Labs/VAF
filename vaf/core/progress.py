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

Nothing here imports ``fastapi``. ``vaf/core/web_interface.py`` does, at module scope, and
``fastapi`` is an optional extra - so the hop to it is a LATE import inside ``publish``.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

__all__ = ["VAF_LIVE_VIEW_TYPES", "resolve_ui_session_id", "StatePublisher"]


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
    "subagent_update",        # the coder's live editor feed; NOT derivable from any name
    "browser_frame_update",
    "browser_step_update",
})


def resolve_ui_session_id() -> str:
    """Which Web UI session this run feeds, or "" for none. Environment first, IPC second.

    Contract, each choice against its failure mode:

    - The ENVIRONMENT wins because it is the only channel that survives a process
      boundary: a sub-agent child is spawned with ``VAF_SESSION_ID`` in its env and has no
      IPC context of its own. Reversing the order changes WHO RECEIVES the stream whenever
      the thread-local context and the process-global env disagree, which is a routing
      change wearing a refactor's clothes.
    - ``.strip()`` before the truth test: a whitespace-only value is an absent one, and a
      blank string that reaches the transport takes the unscoped global-broadcast branch.
    - The IPC fallback carries its own ``except``. ``subagent_ipc`` is imported late and a
      missing session must never turn a best-effort publisher into a raising one.
    - Returns ``""`` and never ``None``, so every call site reads ``if not sid: return``.
    - NOT used by the research agent, deliberately. That site is env-only today, and adding
      the IPC fallback would switch its emits ON in in-process runs, sourced from a
      process-global id. Turning a stream on is a behaviour change with its own test, not a
      side effect of an extraction.
    """
    sid = os.environ.get("VAF_SESSION_ID", "").strip()
    if sid:
        return sid
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
