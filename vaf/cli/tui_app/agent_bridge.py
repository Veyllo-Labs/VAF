# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The single agent lane behind the full-screen app.

One rule carries this module: there is NO second implementation of a turn. The
classic lane's functions are imported and driven through duck-typed surfaces -
`_process_agent_message` (streaming incl. the think-state machine, web
mirroring, session persistence) and `_check_subagent_results` (the exactly-once
drain with its atomic paused-workflow claim) run byte-identically here. The
bridge adds only what an app-mode UI needs around them: one worker lane so
turns and drains never interleave (chat_step is not reentrant, and Rule 4.3's
exactly-once delivery forbids concurrent drains), an event-sink dispatch, and
the gate responder.

THE GATE RESPONDER, and the long-standing gap it closes: the agent's
`_ask_user_about_gate` (the decide hook the tool caller is built with) waits on
`web_interface.register_gate(session_id)` for up to 300 seconds whenever a
session id is bound - which the interactive lane always has. With no browser
watching, every confirmation silently timed out to "cancel". The TUI
subscribes to the `gate_required` sink event, shows its gate screen, and
answers through the SAME `resolve_gate` contract the web UI uses - no change
in the agent, and the terminal finally has a responder.

All callbacks into the UI go through the `events` object the app injects; its
implementations marshal onto the UI thread themselves (tests pass a plain
recorder, so nothing here needs a running app).
"""
import contextlib
import queue
import re
import threading
import time
from typing import Optional

from vaf.core.identity_binding import bind_identity, resolve_owner_identity


class _StreamSurface:
    """The 5-attribute duck `_process_agent_message` renders through.

    The classic callback prints two styles only: `"dim"` for think-stream text
    and `f"bold {tui.primary}"` for answer text - routing on "dim" is therefore
    the entire think/answer split, and a test pins it.
    """

    primary = "#ffffff"          # sentinel; only ever interpolated into a style string

    def __init__(self, bridge: "AgentBridge") -> None:
        self._bridge = bridge
        self.console = self

    def print(self, text="", end="\n", markup=True, style="", **_kw) -> None:
        text = str(text)
        if not text:
            return
        if "dim" in (style or ""):
            self._bridge.events.agent_think(text)
        else:
            self._bridge.note_streaming_started()
            self._bridge.events.agent_chunk(text)

    def spinner(self, message: str = ""):
        bridge = self._bridge

        @contextlib.contextmanager
        def _ctx():
            bridge.events.presence("thinking", "")
            yield

        return _ctx()

    def newline(self) -> None:
        pass

    def error(self, message: str) -> None:
        self._bridge.events.event_note("Error", str(message), "error")


class _DrainSurface:
    """What `_check_subagent_results` renders through: result Panels and notes."""

    primary = "#ffffff"

    def __init__(self, bridge: "AgentBridge") -> None:
        self._bridge = bridge
        self.console = self

    def print(self, *renderables, **_kw) -> None:
        for r in renderables:
            if isinstance(r, str):
                if r.strip():
                    self._bridge.events.system_note(r.strip())
            else:
                self._bridge.events.renderable(r)

    def info(self, message: str) -> None:
        self._bridge.events.event_note("Info", str(message), "info")

    def error(self, message: str) -> None:
        self._bridge.events.event_note("Error", str(message), "error")

    def warning(self, message: str) -> None:
        self._bridge.events.event_note("Warning", str(message), "warning")


# The classic lane's @file inliner, ported byte-faithfully (source of truth:
# the "File attachments" block in run.py's interactive loop, directly above the
# _process_agent_message call): same regex, same wrapper text with the full
# user-typed path, any file size, strict utf-8, and a visible error with the
# literal token kept when the read fails.
# The optional drive-letter group is what makes a Windows absolute path work:
# without it "@C:\\Users\\me\\note.txt" matched only "C", because the colon is not
# in the character class. A single letter before the colon is required, so an
# ordinary "@name:value" still captures just "name" as it always did.
_ATTACH_RE = re.compile(r"@((?:[A-Za-z]:)?[\w\./\\-]+)")


def _inline_attachments(text: str, on_error=None) -> str:
    def _read(match):
        path = match.group(1)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return f"\n\n--- FILE: {path} ---\n{content}\n----------------\n"
        except Exception as exc:
            if on_error is not None:
                on_error(f"Failed to attach {path}: {exc}")
            return match.group(0)

    return _ATTACH_RE.sub(_read, text) if "@" in text else text


def _write_crash_log():
    """Append the current traceback to the dated crash log; return its path.

    The classic loop does this for every unexpected turn error (the same
    crash_<date>.log file, through the same append_crash_log writer). In app
    mode a printed traceback is worse than useless - it lands under the
    alternate screen - so the file IS the artifact, and only its path goes
    into the transcript. Returns None if even logging failed.
    """
    import traceback
    from vaf.core.log_helper import append_crash_log
    return append_crash_log("vaf run, app lane", traceback.format_exc())


def _bind_owner(agent) -> None:
    """Bind the machine owner onto the agent. Two callers: boot, and every session load.

    `load_session_context` assigns the identity stored in the session's own
    metadata, so both places that load a session have to put the owner back.
    This lane is single-user by construction - there is no tenant to distinguish
    and no token to consult, so the configured local admin IS the caller.

    Fail-closed on an unconfigured scope: bind NOTHING rather than a guess, and
    keep the username COUPLED to the scope. Three stores decide account
    ownership on the name alone, so the owner's name over a foreign scope would
    reach the owner's credentials.
    """
    try:
        identity = resolve_owner_identity()
        if identity.scope is None:
            return
        bind_identity(agent, identity)
    except Exception:
        pass


class AgentBridge:
    """Owns the agent lane; translates engine callbacks into UI events."""

    def __init__(self, agent, session, session_mgr, events,
                 web_interface_getter=None) -> None:
        self.agent = agent
        self.session = session
        self.session_mgr = session_mgr
        self.events = events
        self._get_web = web_interface_getter or self._default_web_getter
        # A hand-rolled daemon lane, deliberately NOT a ThreadPoolExecutor:
        # concurrent.futures joins its (non-daemon) workers in an atexit hook,
        # so quitting mid-turn would freeze the closed terminal until the turn
        # finished - up to the full 300 s gate wait. A daemon worker lets the
        # process exit; shutdown() below unblocks the gate case explicitly.
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._lane_loop, daemon=True,
                                        name="vaf-tui-lane")
        self._worker.start()
        self._busy = False
        self._tools_ran = False
        self._streaming = False
        self._ended_in_success = False
        # Single-flight for `repair`: a second run would fight the first one for
        # the same containers, and compose is only idempotent one caller at a time.
        self._repair_running = False
        # Set while the app is tearing down, so no further queue work is claimed.
        # See begin_stopping(): the lane is FIFO, so a drain closure queued just
        # before the app exited still runs during teardown.
        self._stopping = threading.Event()
        # One voice capture at a time; the Event is its cooperative cancel.
        self._listen_stop = None
        self.farewell = ""            # printed by run_tui after the screen is free
        self.history: list = []       # (HH:MM, user text) for the history screen

    @staticmethod
    def _default_web_getter():
        from vaf.cli.cmd.run import get_web_interface
        return get_web_interface()

    # ── lane plumbing ────────────────────────────────────────────────────────────────
    def _stamp_session_ctx(self) -> None:
        """Tell THIS thread which session it serves.

        The session id lives in a ContextVar, and a ContextVar set on the main
        thread (boot) is invisible to independently started threads - each one
        begins with a fresh context. Every bridge-owned thread that touches
        session-scoped state must therefore stamp itself. The live failure
        this fixes: tools executed on the lane read `None`, so working-memory
        writes landed in the legacy GLOBAL store while the plan gate read the
        (empty) session store - and bounced a fully-planned model until its
        loop-cap gave up.
        """
        try:
            from vaf.core.subagent_ipc import set_current_session_id
            set_current_session_id(str(self.session.id))
        except Exception:
            pass

    def _lane_loop(self) -> None:
        # Every turn and every submitted job runs on THIS thread: it must know
        # its session before the first tool executes.
        self._stamp_session_ctx()
        while True:
            fn = self._queue.get()
            if fn is None:
                return
            fn()

    def _submit(self, fn) -> None:
        def _wrapped():
            self._busy = True
            self._ended_in_success = False
            try:
                fn()
            except Exception as exc:               # a broken turn must not kill the app
                where = _write_crash_log()
                detail = f"turn failed: {exc}"
                self.events.event_note("Error", detail, "error")
                if where:
                    self.events.event_note("Error", f"traceback saved to {where}", "error")
            finally:
                self._busy = False
                if self._ended_in_success:
                    # Let the avatar's success ring render, then settle to idle.
                    timer = threading.Timer(1.6,
                                            lambda: self.events.presence("idle", ""))
                    timer.daemon = True
                    timer.start()
                else:
                    self.events.presence("idle", "")

        self._queue.put(_wrapped)

    @property
    def busy(self) -> bool:
        return self._busy

    # ── turns ────────────────────────────────────────────────────────────────────────
    def submit_turn(self, text: str) -> None:
        self.history.append((time.strftime("%H:%M"), text))
        self.events.turn_started(text)
        self._submit(lambda: self._run_turn(text))

    def _run_turn(self, text: str, *, inline_attachments: bool = True) -> None:
        """One turn. `inline_attachments` is False for text nobody typed.

        The `@path` inliner is a PROMPT affordance: it reads arbitrary files off
        disk into the prompt, which is a fair bargain for text a human put in the
        prompt box. A timer's text is authored by the model's own `set_timer` call
        and arrives through the queue, so running the inliner on it would turn an
        agent-authored string into a file read nobody asked for.
        """
        self._tools_ran = False
        self._streaming = False
        self.events.presence("thinking", "")
        self._barge_in_stop()

        # Pre-turn drain, exactly where the classic loop does it: results that
        # arrived while the user typed are summarized BEFORE the new turn.
        self._drain_once()

        expanded = (
            _inline_attachments(
                text, on_error=lambda msg: self.events.event_note("Error", msg, "error"))
            if inline_attachments else text
        )
        # Caller-side contract, exactly like the classic loop: the USER message
        # is added here; _process_agent_message adds the assistant half.
        #
        # What gets STORED is what the user typed, not the @file expansion - the
        # same split the web lane makes (it saves the plain input and enriches only
        # the copy handed to the turn). Storing the expansion put whole files into
        # the session JSON under role "user", and anything that later reads other
        # chats - Cross Chat Hint, session search, an export - would then be reading
        # the contents of `@~/.env` as if the user had typed it.
        self.session.add_message("user", text)

        from vaf.cli.cmd.run import _process_agent_message
        self.events.agent_message_start()
        _process_agent_message(self.agent, expanded, _StreamSurface(self), self.session)
        self.events.agent_message_done()

        self._refresh_context()
        self.events.turn_finished(self._tools_ran)
        if self._tools_ran:
            self._ended_in_success = True
            self.events.presence("success", "")

    # ── the drain (exactly-once territory - imported, never re-implemented) ─────────
    def drain_tick(self) -> None:
        """Called by an app timer; enqueues a drain only when the lane is idle."""
        if self._busy:
            return
        self._submit(self._drain_and_summarize)

    def _drain_and_summarize(self) -> None:
        found = self._drain_once()
        if found:
            self._refresh_context()

    def _drain_once(self):
        from vaf.cli.cmd.run import _check_subagent_results
        surface = _DrainSurface(self)
        found = _check_subagent_results(surface, self.agent)
        if found:
            self._summarize_results(found, surface)
        return found

    # ── this process's task queue (its only producer is a fired timer) ──────────────
    def queue_tick(self) -> None:
        """Hand the process TaskQueue to the lane. Touches the queue not at all here.

        Contract, each choice against its failure mode:

        - NOTHING queue-related happens on this thread. `TaskQueue.get()` keys its
          claim on `threading.get_ident()`, and the stale-claim reaper reclaims only
          NUMERIC keys of DEAD threads. Textual's loop thread lives to interpreter
          exit, so a claim taken here would sit in the in-flight map for the life of
          the process. Release-on-next-get cannot heal it either: that fires when the
          same worker key asks again, and this key never would.
        - Its own 1.0 s interval, deliberately not folded into the 2.5 s sub-agent
          drain beside it. They answer different questions - "did a child finish"
          polls a shared file, "did something wake me" pops an in-memory heap - and a
          timer the user set for 60 seconds must not inherit the file poll's cadence.
        - Gated on `_busy`, which keeps the claim's lifetime and the tick in step.
          That only holds because the turn runs INLINE below; see there.
        """
        if self._stopping.is_set() or self._busy:
            return
        self._submit(self._drain_queue_once)

    def _drain_queue_once(self) -> None:
        """Take at most one task for THIS session and turn it, here, on this thread.

        Contract, each choice against its failure mode:

        - The turn runs INLINE, never through `self._submit`. `_wrapped` clears
          `_busy` in its finally the moment this returns, so a re-submitted turn
          would leave `_busy` False while the claim is still held - and the next
          tick's `get()` releases this worker's previous claim at the top and can
          then pop a second task for the same session. That release is a repair for
          consumers that forget `task_done()`; leaning on it here would turn it into
          a way to run two turns at once.
        - `task_done()` in a `finally`, never left to that self-heal. After the lane
          takes its stop sentinel there is no next `get()` for this key, so the final
          drain's claim would be the one nobody ever releases.
        - `_stopping` first, and again after `get()`. The lane is FIFO and returns
          only on the sentinel, so a closure queued just before the app exited still
          runs during teardown; past the first check it would otherwise start a full
          model turn against a released terminal, with every event dropped.
        - "My session" is `self.session.id`, NEVER `get_current_session_id()`. This
          thread was never told: the session context defaults to a sentinel, a fresh
          thread inherits an empty context, and no lane writes the environment
          variable any more - so that call answers None here and would drop every
          timer. And never `task.source`: a timer set from this very app carries
          "web", because the timer tool falls back to it and nothing under `vaf/cli`
          sets the chat source.
        - A dropped task is GONE, and the note says so rather than implying a retry.
          The scheduler removed the timer from its store before firing, and that
          store does not survive the process.
        - `timeout=0` is one non-blocking attempt, not a poll: the deadline is
          computed first and the pop is attempted before the expiry check, so the
          condition variable is never waited on and the lane is never held.

        NAMED BOUNDARY: no `__CMD__` handling. All four producers of those tasks live
        in the web server, and an explicit `--web` routes to the modern lane, so no
        such task can reach this process. The branch lands when a producer does.
        """
        if self._stopping.is_set():
            return
        from vaf.core.task_queue import TaskQueue

        tq = TaskQueue()
        task = tq.get(timeout=0)
        if task is None:
            return
        try:
            if self._stopping.is_set():
                self.events.event_note(
                    "Timer", "a timer fired while VAF was quitting - it is gone "
                             "(timers live in memory and do not survive a restart)",
                    "warning")
                return
            mine = str(getattr(self.session, "id", "") or "")
            if str(task.session_id or "") != mine:
                self.events.event_note(
                    "Timer",
                    f"a timer for another session ({str(task.session_id or '')[:12]}) "
                    f"fired here and was dropped - it is gone, not deferred",
                    "warning")
                return
            text = str(task.input_text or "")
            if not text.strip():
                return
            if (task.metadata or {}).get("timer"):
                # Same concept and same trigger the web lane already ships: it emits
                # the wake text as its own card before the turn, gated on this very
                # metadata flag. One vocabulary, two lanes. `_emit`, never a direct
                # call - an events object that predates this callback stays quiet
                # instead of raising into the turn.
                self._emit("wake_message", text, "timer")
            self._run_turn(text, inline_attachments=False)
        finally:
            tq.task_done(task=task)

    def begin_stopping(self) -> None:
        """No new queue work from here on. Called from the UI thread, app still alive.

        `shutdown()` is too late for this and always was: it runs from `run_tui`'s
        finally, AFTER `app.run()` returned, so no interval callback could fire past
        it anyway. What has to be stopped is the drain closure already sitting in the
        lane's FIFO ahead of the stop sentinel.
        """
        self._stopping.set()

    def _summarize_results(self, found_results, surface) -> None:
        """The classic summary turn, ported byte-faithfully. Source of truth:
        the `if found_results:` block in run.py's interactive loop (directly
        after its `_check_subagent_results` call) - inline there, not
        importable."""
        self.events.presence("talking", "")
        try:
            user_lang = "auto"
            for msg in reversed(getattr(self.agent, "history", [])):
                if msg.get("role") == "user":
                    user_lang = self.agent._detect_user_language(msg.get("content", ""))
                    break

            native_lang = self.agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
            combined_results = "\n\n---\n\n".join(r[:1000] for r in found_results)

            if user_lang == "de":
                instruction_prompt = (
                    f"Hier sind die Ergebnisse der Sub-Agenten:\n\n"
                    f"{combined_results}\n\n"
                    f"Bitte erstelle eine KURZE ZUSAMMENFASSUNG dieser Ergebnisse für den Benutzer auf DEUTSCH.\n"
                    f"Konzentriere dich auf den Inhalt (was wurde gefunden/getan).\n"
                    f"Bleib prägnant aber informativ.\n"
                    f"Du kannst `read_file` nutzen, wenn du den Inhalt sehen musst.\n"
                    f"ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                )
            else:
                instruction_prompt = (
                    f"The sub-agent(s) have completed their tasks.\n\n"
                    f"**RESULTS:**\n{combined_results}\n\n"
                    f"Please provide a BRIEF SUMMARY of these results for the user in {native_lang}.\n"
                    f"Focus on the content (what was found/done).\n"
                    f"Keep it concise but informative.\n"
                    f"You may use `read_file` if you need to see the content before summarizing.\n"
                    f"RESPOND EXCLUSIVELY IN {native_lang.upper()}."
                )

            self.events.agent_message_start()
            self.agent.chat_step(
                instruction_prompt,
                stream_callback=lambda t: self.events.agent_chunk(str(t)),
                skip_input=False,
                disable_workflows=True,
                disable_tools=False,
            )
            self.events.agent_message_done()
        except Exception as exc:
            self.events.event_note("Error", f"result summary failed: {exc}", "error")

    # ── engine event sink ───────────────────────────────────────────────────────────
    def on_sink_event(self, evt: dict) -> None:
        """Wired via agent.set_event_sink; called on the agent lane thread."""
        try:
            etype = evt.get("type", "")
            if etype == "tool_start":
                self._tools_ran = True
                args = evt.get("args") or {}
                preview = ", ".join(f"{k}={str(v)[:24]}" for k, v in list(args.items())[:3])
                self.events.tool_start(str(evt.get("tool", "")), preview)
                self.events.presence("working", str(evt.get("tool", "")))
            elif etype == "tool_end":
                ms = evt.get("duration_ms")
                duration = f"{ms / 1000:.1f}s" if isinstance(ms, (int, float)) else ""
                self.events.tool_end(str(evt.get("tool", "")),
                                     bool(evt.get("ok", True)), duration,
                                     str(evt.get("result", "") or ""))
                self.events.presence("thinking", "")
            elif etype == "gate_required":
                self.events.presence("waiting", "needs your permission")
                # The arguments were dropped here: the modal asked the user to
                # approve a tool NAME. The preview is already neutralized and
                # redacted by the gate, and the screen escapes it again.
                _notes = []
                if evt.get("args_preview_neutralized"):
                    _notes.append(f"{evt['args_preview_neutralized']} hidden char(s) shown")
                if evt.get("args_preview_redacted"):
                    _notes.append(f"{evt['args_preview_redacted']} secret(s) redacted")
                if evt.get("args_preview_truncated"):
                    _notes.append("truncated")
                if evt.get("command_categories"):
                    _notes.append(", ".join(evt["command_categories"]))
                self.events.gate_required(str(evt.get("tool", "")),
                                          str(evt.get("reason", "")),
                                          str(evt.get("args_preview", "") or ""),
                                          "; ".join(_notes))
            elif etype == "gate_decision":
                self.events.gate_decision(str(evt.get("decision", "")))
            # llm_start/llm_end: enrichment only (local providers emit none) - ignored.
        except Exception:
            pass                                     # an observer must not fail a run

    def _emit(self, name: str, *args) -> None:
        """Optional event: a lane whose events object predates it stays quiet
        rather than raising into a command."""
        fn = getattr(self.events, name, None)
        if callable(fn):
            try:
                fn(*args)
            except Exception:
                pass

    def on_console_event(self, type_name: str, message: str, style: str) -> None:
        """Wired via UI.add_console_sink; the Router/Context/Memory narration."""
        try:
            self.events.event_note(str(type_name), str(message), str(style))
        except Exception:
            pass

    # ── the gate responder ──────────────────────────────────────────────────────────
    DECISIONS = {"once": "allow_once", "always": "allow_always", "cancel": "cancel"}

    def answer_gate(self, word: str) -> None:
        """Resolve the waiting gate through the same contract the web UI uses.

        Runs on its own short thread: the agent lane is the thread BLOCKED inside
        `_ask_user_about_gate`'s event.wait, and the UI thread must not sleep in
        the retry loop.
        """
        decision = self.DECISIONS.get(word, "cancel")

        def _resolve():
            session_id = getattr(self.agent, "current_session_id", None)
            for _ in range(40):
                try:
                    if self._get_web().resolve_gate(session_id, decision):
                        return
                except Exception:
                    pass
                time.sleep(0.05)

        threading.Thread(target=_resolve, daemon=True, name="vaf-tui-gate").start()

    # ── commands that touch the agent ───────────────────────────────────────────────
    def clear_conversation(self) -> None:
        """`clear`: reset the agent's history to the system message.

        On the lane, because `init_chat()` rebuilds the prompt manager and
        re-reads the project context. The app has already emptied the
        transcript; this confirms when the agent side actually landed.
        """
        def _run():
            self.agent.init_chat()
            self.events.system_note("conversation cleared")
        self._submit(_run)

    def undo_last_change(self) -> None:
        """`undo`: roll back the last snapshot. On the lane - constructing a
        Snapshot touches disk and shells out to git."""
        def _run():
            from vaf.core.snapshot import Snapshot
            if Snapshot().undo():
                self.events.system_note("rolled back to the last snapshot")
            else:
                self.events.event_note("Undo", "no snapshot available", "warning")
        self._submit(_run)

    def restore_context(self) -> None:
        """`restore`: bring the archived context back after a compression."""
        def _run():
            if self.agent.restore_context():
                self.events.system_note("full context restored from the archive")
                self._refresh_context()
            else:
                self.events.event_note("Restore", "no archive to restore", "warning")
        self._submit(_run)

    def show_context(self) -> None:
        """`context`: the tracked-state read-out, not just the token bar."""
        def _run():
            status = self.agent.get_context_status()
            self._emit("context_status", dict(status or {}))
        self._submit(_run)

    def load_session(self, session_id: str) -> None:
        """`session <id>`: the COMPLETE swap via load_session_context - never
        the classic hand-rolled replay, which drops tool_calls and images."""
        def _run():
            try:
                session = self.session_mgr.load(session_id)
            except Exception as exc:
                self.events.event_note("Session", f"cannot load {session_id}: {exc}",
                                       "error")
                return
            self._switch_to(session)
        self._submit(_run)

    def _switch_to(self, session) -> None:
        """Make `session` the live one - the runtime counterpart of the boot
        sequence. Lane-side only (every caller is inside a _submit)."""
        previous = self.session
        self.session = session
        if previous is not None and str(previous.id) != str(session.id):
            if self._discard_if_untouched(previous):
                self.events.system_note(
                    "the empty session you left was discarded")
        # Boot set this and the runtime switch never did: every
        # session-scoped IPC read (the tasks line, the queue drain) kept
        # answering for the PREVIOUS session after a switch.
        try:
            from vaf.core.subagent_ipc import set_current_session_id
            set_current_session_id(str(session.id))
        except Exception:
            pass
        self.agent.load_session_context(session.id)
        self._rebind_local_admin()
        self._emit("session_switched", session.id,
                   len(getattr(session, "messages", []) or []))
        # The transcript must FOLLOW the swap: without this the old
        # conversation stays on screen above the new session's turns.
        # fresh=True clears first - unlike the boot replay, which mounts
        # under the start banner.
        self._emit("transcript_replay", self._transcript_entries(session),
                   True)
        self._refresh_context()

    def new_session(self) -> None:
        """`session new` / the panel's `n`: create, persist, switch to it.

        The scope stamp mirrors the boot create: the local admin owns what
        this single-user lane creates, and the web's scope-filtered list
        would otherwise show the session only by the legacy no-scope rule.
        """
        def _run():
            try:
                from vaf.core.config import get_local_admin_scope_id
                try:
                    scope = str(get_local_admin_scope_id())
                except Exception:
                    scope = None
                session = self.session_mgr.new(user_scope_id=scope)
                self.session_mgr.save(session)
            except Exception as exc:
                self.events.event_note("Session",
                                       f"cannot create a session: {exc}", "error")
                return
            self._switch_to(session)
        self._submit(_run)

    def rename_session(self, session_id: str, new_name: str) -> None:
        """Rename on disk via the engine primitive; if it is the LIVE session,
        repair the in-memory copy too, so the exit save cannot write the old
        name back over the rename - the repair every interactive lane carries."""
        def _run():
            name = " ".join(str(new_name).split())
            if not name:
                self.events.event_note("Session", "a name cannot be empty",
                                       "warning")
                return
            if not self.session_mgr.rename(str(session_id), name):
                self.events.event_note(
                    "Session", f"cannot rename {str(session_id)[:12]}: not found",
                    "error")
                return
            if str(self.session.id) == str(session_id):
                self.session.name = name
            self.events.system_note(f"session renamed to {name}")
            self._emit("chrome_changed")
            self._emit("session_list", self.list_sessions())
        self._submit(_run)

    def delete_session(self, session_id: str) -> None:
        """Delete a session file. The LIVE session is refused honestly - the
        agent's history, the IPC scope and the transcript all point at it;
        switching away first keeps every one of them coherent."""
        def _run():
            if str(self.session.id) == str(session_id):
                self.events.event_note(
                    "Session", "cannot delete the session you are in - "
                               "switch to another one first", "warning")
                return
            try:
                ok = bool(self.session_mgr.delete(str(session_id)))
            except Exception as exc:
                self.events.event_note("Session", f"delete failed: {exc}",
                                       "error")
                return
            if ok:
                self.events.system_note(f"session {str(session_id)[:12]} deleted")
            else:
                self.events.event_note("Session",
                                       f"{str(session_id)[:12]} not found",
                                       "warning")
            self._emit("session_list", self.list_sessions())
        self._submit(_run)

    @staticmethod
    def _message_field(message, name: str, default=None):
        """One field of a stored message, whichever shape it arrives in.

        `Session.from_dict` rebuilds every entry as a `Message` dataclass, so
        the live lane always hands over objects. Tests and older callers pass
        plain dicts. Reading only one of the two shapes is how the transcript
        replay came to raise `'Message' object has no attribute 'get'` on
        every session switch while its test stayed green on dict fixtures.
        """
        if isinstance(message, dict):
            return message.get(name, default)
        return getattr(message, name, default)

    @classmethod
    def _transcript_entries(cls, session) -> list:
        """(role, text, HH:MM) rows for a transcript replay - user/assistant
        only, empty bodies dropped. The persisted timestamp is local ISO; its
        HH:MM is shown so a replayed head does not claim the message happened
        just now."""
        entries = []
        for m in getattr(session, "messages", []) or []:
            role = cls._message_field(m, "role")
            text = (cls._message_field(m, "content") or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            ts = str(cls._message_field(m, "timestamp") or "")
            when = ts[11:16] if len(ts) >= 16 else ""
            entries.append((role, text, when))
        return entries

    def request_transcript_replay(self) -> None:
        """Boot: a resumed session's conversation appears under the start
        banner instead of an empty screen. A session without messages stays a
        no-op - a fresh start keeps its clean banner."""
        def _run():
            entries = self._transcript_entries(self.session)
            if entries:
                self._emit("transcript_replay", entries, False)
        self._submit(_run)

    def _farewell(self, path) -> str:
        """What the classic lane printed on the way out, minus its blocking
        question: the id and the two ways back in.

        Not a `print` from here - the screen may still belong to the app when
        this runs, so `run_tui` shows it after the teardown.
        """
        session_id = str(getattr(self.session, "id", ""))
        count = len(getattr(self.session, "messages", []) or [])
        if not count:
            return ""
        lines = [f"Session saved ({count} messages)",
                 f"  id:   {session_id}"]
        if path:
            lines.append(f"  file: {path}")   # only when the save already landed
        lines += ["", "  To come back to it:",
                  f"    vaf run --session {session_id}",
                  f"    vaf session load {session_id}"]
        return "\n".join(lines)

    def _discard_if_untouched(self, session) -> bool:
        """Drop an abandoned session instead of leaving a husk behind.

        The question is `vaf.core.session.is_untouched`, the framework's one
        definition of "nobody put anything in here", which the browser's delete
        dialog and the husk cleanup ask too. This lane used to carry its own
        copy - no message with role "user" - and that copy discarded a chat
        holding an automation's proactive reply, or an attachment added but
        never sent, simply because the person had not answered yet.
        """
        from vaf.core.session import is_untouched

        if session is None or not is_untouched(session):
            return False
        try:
            self.session_mgr.delete(str(session.id))
            return True
        except Exception:
            return False

    def _rebind_local_admin(self) -> None:
        """Put the machine owner back after a session load overwrote the identity."""
        _bind_owner(self.agent)

    def apply_provider_change(self, provider: str = "", model: str = "",
                              *, new_key: str = "") -> None:
        """Move the RUNNING agent onto a different provider or API model.

        Uses the engine's own `reload_all_api_backends`, which is the whole
        job and already does the parts a hand-rolled version gets wrong: it
        re-reads the config from disk, rebuilds the backend under a lock, and
        RE-ATTACHES the event sink to the new backend. The classic lanes threw
        the agent away instead and silently lost the sink, the web
        registration and the real session id.

        Never `init_chat()` here: that resets the history to the system message
        and would wipe the conversation behind the transcript. The classic
        lanes may call it only because they discarded the object first.

        THE KEY GATE, and it lives here rather than in the overlay because this
        is where every route converges: the model overlay, the settings row and
        the `/model` command. Without a key the switch is REFUSED and nothing is
        written - storing the provider would have poisoned the next start, which
        boots straight into a provider that cannot build a backend, while the
        only message the user got was "restart VAF to apply".

        `new_key` is verified with a real request before the provider moves,
        exactly as `vaf settings` does, and only when a key was actually typed:
        an empty answer means "keep the stored one", and re-testing a key that
        already works would spend a request on every switch. A key that fails to
        verify is still STORED - the request can fail on the network as easily
        as on the key - but the provider stays where it was.

        The key value never reaches an event, a note or the transcript.
        """
        def _run():
            from vaf.core.agent import reload_all_api_backends
            from vaf.core.config import Config
            target = provider or str(Config.get("provider", "local") or "local")
            if target != "local":
                if new_key:
                    Config.set_api_key(target, new_key)
                if not Config.get_api_key(target):
                    self.events.event_note(
                        "Provider", f"no API key for {target} - not switched",
                        "warning")
                    return
                if new_key and not self._api_key_verifies(target):
                    self.events.event_note(
                        "Provider", f"{target}: the key was saved but did not "
                                    f"verify - provider unchanged", "warning")
                    return
            if provider:
                Config.set("provider", provider)
            if model and provider:
                Config.set(f"api_model_{provider}", model)
            # force=True: a key or model may have moved while the provider
            # name stayed the same, and the no-op guard would swallow that.
            changed = reload_all_api_backends(force=True)
            label = model or provider or "configuration"
            if changed:
                self.events.system_note(f"switched to {label}")
            else:
                self.events.event_note(
                    "Provider", f"{label} stored; the running agent kept its "
                                f"backend (restart VAF to apply)", "warning")
            self._emit("chrome_changed")
            self._refresh_context()

        if self._busy:
            self.events.event_note("Provider", "not while a turn is running",
                                   "warning")
            return
        self._submit(_run)

    def list_local_models(self):
        """`models/*.gguf` plus the active file name, for the settings submenu.

        The active marker uses the same normalization the classic menu used:
        the config may hold a bare name, a `repo/file.gguf` ref or a name
        without the extension.
        """
        import os
        from vaf.core.config import Config
        try:
            from vaf.core.backend import is_selectable_model
            files = sorted(f for f in os.listdir(getattr(self.agent, "models_dir", ""))
                           if is_selectable_model(f))
        except Exception:
            files = []
        current = str(Config.get("model") or "")
        current_name = current.split("/")[-1]
        if current_name and not current_name.endswith(".gguf"):
            current_name += ".gguf"
        return files, current_name

    def apply_local_model(self, filename: str) -> None:
        """Move the RUNNING local agent onto a different GGUF, live.

        Same shape as apply_provider_change: the engine primitive
        (`reload_local_model`) is the whole job - it re-resolves the file,
        swaps the ONE llama server, recomputes the parser identity and keeps
        the conversation. The swap blocks while the new weights load, which is
        exactly why it runs on the lane and refuses while a turn is running.

        With a non-local provider only the config moves (the classic contract):
        the file becomes active the next time the local provider serves.
        """
        def _run():
            from vaf.core.config import Config
            Config.set("model", filename)
            if getattr(self.agent, "provider", "local") != "local":
                self.events.system_note(
                    f"model saved: {filename} - applies when the provider is local")
                self._emit("chrome_changed")
                return
            self.events.event_note(
                "Model", f"switching local model to {filename} - the new "
                         f"weights have to load, this can take a while", "info")
            ok = False
            try:
                ok = bool(self.agent.reload_local_model())
            except Exception as exc:
                self.events.event_note("Model", f"local model switch failed: {exc}",
                                       "error")
            if ok:
                self.events.system_note(f"switched to {filename}")
            else:
                self.events.event_note(
                    "Model", f"{filename} is stored, but the running agent could "
                             f"not switch - the server did not come up with it",
                    "warning")
            self._emit("chrome_changed")
            self._refresh_context()

        if self._busy:
            self.events.event_note("Model", "not while a turn is running",
                                   "warning")
            return
        self._submit(_run)

    @staticmethod
    def _api_key_verifies(provider: str) -> bool:
        """One real request against the provider, on the lane thread.

        Its own method so a test can drive the refusal without going near the
        network - and so the reason it must not run on the UI thread has one
        place to be written down: `test_connection` performs an actual chat
        completion, so on the UI thread it freezes the whole app for as long as
        the provider takes to answer.
        """
        try:
            from vaf.core.api_backend import APIBackendManager
            return bool(APIBackendManager.test_connection(provider))
        except Exception:
            return False

    def listen_voice(self) -> None:
        """`l`/`listen`: capture one utterance and send it as a turn.

        Its own daemon thread, not the agent lane: the classic contract is
        "listening works any time", and the capture blocks for up to
        timeout+utterance - parked on the serialized lane it would wait behind
        a running turn, and parked on the UI thread it would freeze the app.
        The captured text is then SUBMITTED to the lane like any typed
        message, so a turn that is already running still goes first.

        Presentation is the caller's: `on_state` feeds the overlay through the
        event adapter (data, not painting - the reason the framework grew the
        callback), and `voice_done` closes it. Cancel is cooperative:
        `cancel_listen()` trips an Event that `should_stop` reads once per
        chunk. One capture at a time; a second press is told so.
        """
        if self._listen_stop is not None:
            self.events.event_note("Voice", "already listening", "warning")
            return
        stop = threading.Event()
        self._listen_stop = stop

        def _capture():
            try:
                from vaf.core.speech import get_speech_manager
                sm = get_speech_manager()
                if not sm.is_stt_enabled():
                    self._emit("voice_done", None,
                               "Speech input is disabled - Settings › Voice")
                    return
                sm.stop()          # never record the agent's own voice
                text = sm.listen(
                    timeout=5,
                    on_state=lambda phase, energy=0.0, threshold=0.0:
                        self._emit("voice_level", phase, energy, threshold),
                    should_stop=stop.is_set,
                )
                if stop.is_set():
                    self._emit("voice_done", None, "cancelled")
                    return
                # The transcript goes back to the APP, which routes it through
                # the same send path a typed message takes - so the turn gets
                # its "You" bubble and its history entry, and the
                # review-before-send preference has one place to act. A direct
                # _run_turn from here streamed an answer into a transcript
                # with no visible question.
                self._emit("voice_done", text or None,
                           "" if text else "no speech detected")
            except Exception as exc:
                self._emit("voice_done", None, f"speech error: {exc}")
            finally:
                self._listen_stop = None

        threading.Thread(target=_capture, daemon=True,
                         name="vaf-tui-listen").start()

    def cancel_listen(self) -> None:
        stop = self._listen_stop
        if stop is not None:
            stop.set()

    def stop_speech(self, *, announce: bool = True) -> None:
        """`halt`/`stop`: silence the agent WHILE it is speaking.

        Its own short thread, not the lane: the lane is exactly the thread that
        is busy running the turn whose speech the user wants stopped.

        `announce=False` is the classic loop's UNCONDITIONAL barge-in: every
        submitted input silences running speech before it is even parsed, and
        says nothing about it - only the explicit `halt` command narrates.
        """
        def _run():
            try:
                from vaf.core.speech import get_speech_manager
                get_speech_manager().stop()
            except Exception:
                pass
            if announce:
                self.events.system_note("speech stopped")

        threading.Thread(target=_run, daemon=True, name="vaf-tui-halt").start()

    def repair_stack(self) -> None:
        """`repair`: check the Docker services and put a broken one back.

        Its own thread, not the lane: starting a container engine can take
        minutes, and the lane is what every chat turn waits on. Each step is
        narrated as it finishes, because a run this long with a single line at
        the end is indistinguishable from a hang.
        """
        if self._repair_running:
            self.events.event_note("Repair", "a repair is already running", "warning")
            return
        self._repair_running = True

        def _run():
            try:
                from vaf.cli.cmd.repair import format_status, format_step
                from vaf.core.service_health import repair_service_stack

                self.events.system_note("checking the Docker services...")
                result = repair_service_stack(
                    progress=lambda step: self.events.system_note(format_step(step)))
                for line in format_status(result.get("status_after") or {}):
                    self.events.system_note(line)
                degraded = result.get("degraded") or []
                if result.get("ok") and not degraded:
                    self.events.event_note("Repair", "the service stack is healthy", "success")
                elif result.get("ok"):
                    self.events.event_note(
                        "Repair", "the core stack is up, still to look at: "
                        + ", ".join(degraded), "warning")
                else:
                    self.events.event_note(
                        "Repair", "some services still need attention", "warning")
            except Exception as exc:
                self.events.event_note("Repair", f"failed: {exc}", "error")
            finally:
                self._repair_running = False

        threading.Thread(target=_run, daemon=True, name="vaf-tui-repair").start()

    # ── chrome data ─────────────────────────────────────────────────────────────────
    def _refresh_context(self) -> None:
        try:
            used, total = self.agent.get_token_usage()
            self.events.context(int(used or 0), int(total or 0))
        except Exception:
            pass

    def refresh_context(self) -> None:
        """Enqueue a context-usage read on the lane (token counting may hit the
        tokenizer or the llama server; never on the UI thread)."""
        if not self._busy:
            self._submit(self._refresh_context)

    def tasks_snapshot(self) -> list:
        """(marker, label, id8, elapsed, progress) rows for the TasksLine - session-scoped
        accessors only (Rule 4.4), unlike the classic toolbar's global paused read.

        `progress` is "done/total" or "" - never a percentage, and never "0/0". A child
        that reports no counts, and one that has not planned yet, both render as nothing:
        an empty column is honest, a 0% bar that never moves is not.
        """
        entries = []
        try:
            from datetime import datetime

            from vaf.core.subagent_ipc import (get_current_session_id, get_ipc,
                                               session_context)
            ipc = get_ipc()
            # SCOPED, not a permanent stamp: this runs on whatever thread
            # polls (the app's tasks thread live, the caller's thread in
            # tests), and a stamp that outlives the read leaks the session
            # onto a foreign thread - it killed the env fallback for every
            # later reader on that thread. session_context restores even the
            # "never told" state, which a save-and-restore from out here
            # cannot (told-None and never-told answer differently).
            with session_context(str(self.session.id)):
                return self._tasks_snapshot_rows(ipc, datetime)
        except Exception:
            pass
        return entries

    def _tasks_snapshot_rows(self, ipc, datetime) -> list:
        entries = []
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            for task in ipc.get_active_tasks_for_current_session():
                elapsed = self._elapsed(datetime.fromisoformat(task.created_at))
                agent_type = task.agent_type
                done, total = task.progress_done, task.progress_total
                progress = f"{done}/{total}" if total else ""
                if agent_type.startswith("workflow:"):
                    entries.append(("[>>]", agent_type.split(":", 1)[1],
                                    task.task_id[:8], elapsed, progress))
                else:
                    entries.append(("[>]", agent_type, task.task_id[:8], elapsed, progress))
            for wf in ipc.get_paused_workflows_for_session(get_current_session_id()):
                elapsed = self._elapsed(datetime.fromisoformat(wf.created_at))
                entries.append(("[||]", wf.workflow_name,
                                wf.waiting_for_task_id[:8], elapsed, ""))
        except Exception:
            pass
        return entries

    @staticmethod
    def _elapsed(start) -> str:
        from datetime import datetime
        secs = int((datetime.now() - start).total_seconds())
        return f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60}s"

    def list_sessions(self) -> list:
        """The engine's ONE surface list (`list_ui`): channel and thinking
        sessions stay in their dashboards, exactly as the web sidebar shows
        it - the two lists rendering different sets was the divergence."""
        try:
            from vaf.core.config import get_local_admin_scope_id
            try:
                scope = str(get_local_admin_scope_id())
            except Exception:
                scope = None
            return self.session_mgr.list_ui(limit=20, user_scope_id=scope)
        except Exception:
            return []

    def request_session_list(self) -> None:
        """Read the session list on the lane and hand it back as an event.

        Listing touches the filesystem for every session file; doing that on
        the UI thread is the kind of jank that only shows up once a user has a
        few hundred sessions.
        """
        def _run():
            self._emit("session_list", self.list_sessions())
        self._submit(_run)

    def describe_session(self) -> None:
        """`session current`: the facts of THIS session as one note - above
        all the full id, which is what `vaf run --session <id>` needs and
        which the panel can only show truncated."""
        def _run():
            s = self.session
            parts = [f"id {getattr(s, 'id', '?')}"]
            name = getattr(s, "name", "") or ""
            if name:
                parts.append(f"name {name}")
            msgs = getattr(s, "messages", None)
            if msgs is not None:
                parts.append(f"{len(msgs)} messages")
            for label, attr in (("created", "created_at"), ("updated", "updated_at")):
                value = str(getattr(s, attr, "") or "")[:16]
                if value:
                    parts.append(f"{label} {value}")
            self.events.system_note(" · ".join(parts))
        self._submit(_run)

    def export_session(self, path: str) -> None:
        """`/export <file>`: the conversation as markdown (or json by
        extension), written where the user said.

        On the lane, not the UI thread: it reads `self.session`, which a
        running turn mutates - exactly the reason `load_session` queues too.
        Failure is an Export note, never a crash-log path: a typo'd directory
        is an answer, not an incident. The classic contract otherwise:
        the path verbatim (expanduser only), markdown by default, success
        says "Exported to: <path>".
        """
        def _run():
            from pathlib import Path
            try:
                fmt = "json" if str(path).lower().endswith(".json") else "markdown"
                content = self.session_mgr.export(self.session, format=fmt)
                target = Path(str(path)).expanduser()
                target.write_text(content, encoding="utf-8")
                self.events.system_note(f"Exported to: {target}")
            except Exception as exc:
                self.events.event_note("Export", f"failed: {exc}", "error")
        self._submit(_run)

    # ── misc ────────────────────────────────────────────────────────────────────────
    def _barge_in_stop(self) -> None:
        """Typing interrupts speech - the classic loop's barge-in, verbatim
        (the `get_speech_manager().stop()` block on every user input)."""
        try:
            from vaf.core.speech import get_speech_manager
            get_speech_manager().stop()
        except Exception:
            pass

    def note_streaming_started(self) -> None:
        if not self._streaming:
            self._streaming = True
            self.events.presence("talking", "")

    def shutdown(self) -> None:
        """Ordered teardown after the app released the screen.

        Order matters: (1) a turn blocked at the gate is waiting inside the
        agent's `_ask_user_about_gate` for up to 300 s - cancel it through the
        same resolve contract, or the finalizer below would wait its full grace
        period; (2) the lane gets its stop sentinel; (3) finalization (session
        save, sink off, agent shutdown) runs AFTER the lane is idle, never
        concurrently with a turn's own session save inside
        `_process_agent_message`. When a turn is still running the finalizer
        moves to a daemon thread with a short grace period, so quitting
        mid-turn never freezes the closed terminal.

        `_stopping` is set FIRST, so a direct shutdown() with no app behind it is
        covered too. On the normal path the app already set it from on_unmount,
        while its event loop was still alive - which is the only moment that can
        stop a queue tick, because this method runs after that loop is gone.
        """
        self._stopping.set()
        try:
            session_id = getattr(self.agent, "current_session_id", None)
            self._get_web().resolve_gate(session_id, "cancel")
        except Exception:
            pass
        # Composed BEFORE the finalizer, which may run on a daemon thread when
        # a turn is still in flight - the caller prints this as soon as
        # shutdown() returns, so it cannot wait for the save to finish.
        self.farewell = self._farewell(None)
        self._queue.put(None)

        def _finalize():
            # SCOPED, because this runs on its own daemon thread only when a
            # turn is still in flight - otherwise INLINE on the caller's
            # thread, and a permanent stamp there told the caller's context a
            # session it never declared (in tests: the main thread, killing
            # the env fallback for everything after). The shutdown path counts
            # active sessions, so it must know which one it is - for exactly
            # this block, and not a moment longer.
            from vaf.core.subagent_ipc import session_context
            with session_context(str(self.session.id)):
                _finalize_body()

        def _finalize_body():
            deadline = time.monotonic() + 5.0
            while self._busy and time.monotonic() < deadline:
                time.sleep(0.1)
            try:
                if self._discard_if_untouched(self.session):
                    pass                    # nothing was said: leave no husk
                else:
                    # The on-disk NAME wins at exit: every rename path writes
                    # the file (web included), and this full-object save must
                    # not resurrect the name this process booted with.
                    try:
                        disk = self.session_mgr.load(str(self.session.id),
                                                     restore_state=False,
                                                     repoint=False)
                        if disk.name and disk.name != self.session.name:
                            self.session.name = disk.name
                    except Exception:
                        pass
                    path = self.session_mgr.save(self.session)
                    self.farewell = self._farewell(path)
            except Exception:
                pass
            try:
                self.agent.set_event_sink(None)
            except Exception:
                pass
            try:
                self.agent.shutdown()
            except Exception:
                pass

        if self._busy:
            threading.Thread(target=_finalize, daemon=True,
                             name="vaf-tui-finalize").start()
        else:
            _finalize()


# ── production boot (Phase A: plain terminal, before the app owns the screen) ───────

def boot_bridge(events, theme_key: str, session_id: Optional[str], verbose: bool) -> AgentBridge:
    """Build agent + session exactly the way the classic modern lane does,
    in the same ORDER (source of truth: the boot section of `_run_modern` in
    run.py, from SessionManager creation through the model warmup), then wire
    the sinks.

    Runs before `app.run()` on purpose: model loading writes C-level fd-2 noise
    (llama.cpp) that would corrupt an app-mode screen - boot order is the
    mitigation, not suppression.

    Two things the classic lane does here are NOT optional and are started
    below, because dropping them is invisible until it hurts: the heartbeat
    (the only signal the tray has that a CLI session is alive - without it the
    tray unloads the local model out from under the session) and the git
    preflight (git-dependent tools would otherwise fail deep inside a turn,
    and an install prompt only works while the terminal is still plain).

    Deliberately NOT started here, each a named boundary: the web server +
    frontend (`vaf run --web` remains that lane), the classic result-notifier
    thread (the drain timer replaces it - results mount into the transcript
    instead of breaking a prompt), and the classic lane's speech preloads - TTS
    engine warmup, the STT microphone check, and the langid preload - which
    land with the voice round; until then the first spoken reply pays the
    engine spin-up lazily.

    The app lane DOES consume this process's TaskQueue now (see queue_tick), and
    there is exactly one producer in that process: a fired timer. The web UI's
    session commands are deliberately not handled - all four of their producers
    live in the web server, and an explicit --web routes to the modern lane, so
    no such task can reach here. That branch lands when a producer for it does.

    Raises SystemExit when the git preflight fails, exactly like the classic
    lane - the caller has not taken the screen yet, so the message is visible.
    """
    import threading

    from vaf.cli.cmd.run import (
        _check_and_install_git,
        _heartbeat_loop,
        _make_cli_agent,
        _quiet_cli_http_logs,
        _warmup_model,
    )
    from vaf.cli.tui import TUI
    from vaf.core.session import SessionManager
    from vaf.core.subagent_ipc import cleanup_other_sessions, set_current_session_id

    _quiet_cli_http_logs()
    boot_tui = TUI(theme_key)

    # The tray's liveness signal. Daemon so it never holds the process open.
    threading.Thread(target=_heartbeat_loop, daemon=True,
                     name="vaf-tui-heartbeat").start()

    if not _check_and_install_git(boot_tui):
        boot_tui.error("Git is required. Install it and run `vaf run` again.")
        raise SystemExit(1)

    # The door comes FIRST: everything below this line reads or writes the
    # user's session records (the stack start, the session claim, the empty-chat
    # cleanup), and a door behind those has already let the visitor in.
    from vaf.cli.gate import require_admin_password
    if not require_admin_password():
        boot_tui.info("Authentication failed.")
        raise SystemExit(1)

    # At-rest migration + retention, here too. This is the DEFAULT terminal lane
    # (tui_mode "app"), and it was the one lane that never ran it while the
    # modern and classic lanes both did - so a terminal-only user kept plaintext
    # chats indefinitely and the tolerant read never tightened. The same "one
    # lane only" repair this repo has shipped before, found by walking the start
    # paths per platform rather than by anyone hitting it.
    try:
        from vaf.core.at_rest_migration import run_once
        run_once()
    except Exception as _mig_err:
        boot_tui.info(f"At-rest migration skipped: {_mig_err}")
    # Embedding-model reconcile, same every-lane rule as run_once above.
    try:
        from vaf.memory.reembed import ensure_embedding_model_current
        ensure_embedding_model_current()
    except Exception as _reembed_err:
        boot_tui.info(f"Embedding-model reconcile skipped: {_reembed_err}")

    # The service stack (memory DB, sandbox, speech): the tray starts it and
    # STOPS it on quit, so a terminal-only start used to run against a dead
    # stack - memory_search then reported an empty memory that was in truth an
    # unreachable one. Same engine primitive as the tray, on a background
    # thread: the model load below must never wait on a compose up. No compose
    # file (a pip install has none) or no Docker -> honest quiet skip, and the
    # memory tool names the dead DB when asked. AFTER the git gate on purpose:
    # a boot that is about to abort must not leave containers starting.
    from vaf.core.service_stack import ensure_service_stack, find_stack_root
    if find_stack_root() is not None:
        boot_tui.info("Starting Docker services in the background (memory, sandbox, speech)...")
        threading.Thread(target=ensure_service_stack, daemon=True,
                         name="vaf-tui-services").start()

    session_mgr = SessionManager()
    session = None
    if session_id:
        try:
            session = session_mgr.load(session_id)
        except FileNotFoundError:
            boot_tui.warning(f"Session {session_id} not found - starting a new one.")
    if session is None:
        from vaf.core.config import get_local_admin_scope_id
        try:
            scope = str(get_local_admin_scope_id())
        except Exception:
            scope = None
        session = session_mgr.new(user_scope_id=scope)
        session_mgr.save(session)

    set_current_session_id(session.id)
    cleanup_other_sessions()
    session_mgr.cleanup_empty(exclude_session_id=session.id)

    # One-time repair: sessions from before scope stamping have no owner, and
    # the list's legacy rule shows them to EVERY user. They can only be the
    # machine owner's (multi-user arrived WITH scoping), so claim them.
    try:
        from vaf.core.config import get_local_admin_scope_id
        claimed = session_mgr.claim_unscoped(str(get_local_admin_scope_id()))
        if claimed:
            boot_tui.info(f"Claimed {claimed} legacy session(s) for the machine owner.")
    except Exception:
        pass

    agent = _make_cli_agent(verbose=verbose)

    try:
        web = AgentBridge._default_web_getter()
        if web is not None:
            web.register_agent(agent)
    except Exception:
        pass
    try:
        session_mgr.state_registry = agent.state_registry
    except Exception:
        pass

    # WebSocket session-id sync, verbatim from _run_modern's boot (the block
    # that swaps the agent's temporary random session id for the real one).
    try:
        if hasattr(agent, "_session_id") and agent._session_id != session.id:
            agent._unregister_session()
            agent._session_id = session.id
            agent._register_session()
    except Exception:
        pass

    # Backend init in the classic order (_run_modern's provider branch:
    # local downloads + loads + init_chat; API loads + init_chat; warmup
    # only when no API backend answered).
    if agent.provider == "local":
        agent.ensure_model_exists()
        with boot_tui.spinner("Loading model..."):
            agent.load_model(skip_download_check=True)
            agent.init_chat()
    else:
        agent.load_model(skip_download_check=True)
        agent.init_chat()
    if not agent.api_backend:
        _warmup_model(boot_tui)
        try:
            agent.get_token_usage()
        except Exception:
            pass

    # Speech preloads, in the boot phase ON PURPOSE: the terminal is still
    # plain here, so Piper's download progress, ALSA's fd-2 chatter and an
    # honest "pyaudio is not installed" all land where they are readable -
    # lazily inside the app they would surface mid-turn, or shred the
    # alternate screen. Ported from the classic boot; failures warn and never
    # block the chat.
    if agent.config.get("speech_tts_enabled", False):
        try:
            boot_tui.event("Speech", "Preloading TTS resources...", style="dim")
            from vaf.core.speech import get_speech_manager
            sm = get_speech_manager()
            sm._check_piper()
            sm._ensure_voice_model(agent.config.get("speech_language", "en-US")[:2])
            boot_tui.event("Speech", "TTS resources ready", style="success")
        except Exception as exc:
            boot_tui.warning(f"TTS preload failed: {exc}")
    if agent.config.get("speech_stt_enabled", False) or agent.config.get("stt_enabled", False):
        try:
            boot_tui.event("Speech", "Checking STT microphone...", style="dim")
            from vaf.core.speech import get_speech_manager
            sm = get_speech_manager()
            # ensure_stt_capture, not a bare stt_mic read: with the docker
            # engine the constructor skips mic init, and the bare read called
            # every docker-stack machine "no microphone detected".
            if sm.ensure_stt_capture():
                boot_tui.event("Speech", "STT microphone ready", style="success")
            else:
                import importlib.util as _ilu
                if _ilu.find_spec("pyaudio") is None:
                    boot_tui.warning(
                        'STT enabled but pyaudio is not installed - mic capture '
                        'needs the optional speech extra: pip install pyaudio '
                        '(or pip install "vaf[speech]")')
                else:
                    boot_tui.warning("STT enabled but no microphone detected")
        except Exception as exc:
            boot_tui.warning(f"STT check failed: {exc}")
    try:
        from vaf.vendor import langid
        boot_tui.event("System", "Preloading language detection...", style="dim")
        langid.classify("test")     # first call ~1.6s, every later one <1ms
    except ImportError:
        pass

    # The COMPLETE session swap - never the hand-rolled replay loops that the
    # classic lane still carries (those drop tool_calls, images and identity).
    agent.load_session_context(session.id)

    # Re-bind the machine owner AFTER the swap: load_session_context overwrites
    # the identity from the session's own metadata.
    _bind_owner(agent)

    bridge = AgentBridge(agent, session, session_mgr, events)
    agent.set_event_sink(bridge.on_sink_event)
    return bridge
