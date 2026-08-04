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

    The classic loop did this for every unexpected turn error (the same
    `get_dated_log_path("crash", "log")` file). In app mode a printed traceback
    is worse than useless - it lands under the alternate screen - so the file
    IS the artifact, and only its path goes into the transcript.
    """
    import traceback
    try:
        from vaf.core.log_helper import get_dated_log_path
        path = get_dated_log_path("crash", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%dT%H:%M:%S')} (vaf run, app lane) ---\n")
            fh.write(traceback.format_exc())
        return path
    except Exception:
        return None


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
        self.farewell = ""            # printed by run_tui after the screen is free
        self.history: list = []       # (HH:MM, user text) for the history screen

    @staticmethod
    def _default_web_getter():
        from vaf.cli.cmd.run import get_web_interface
        return get_web_interface()

    # ── lane plumbing ────────────────────────────────────────────────────────────────
    def _lane_loop(self) -> None:
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

    def _run_turn(self, text: str) -> None:
        self._tools_ran = False
        self._streaming = False
        self.events.presence("thinking", "")
        self._barge_in_stop()

        # Pre-turn drain, exactly where the classic loop does it: results that
        # arrived while the user typed are summarized BEFORE the new turn.
        self._drain_once()

        expanded = _inline_attachments(
            text, on_error=lambda msg: self.events.event_note("Error", msg, "error"))
        # Caller-side contract, exactly like the classic loop: the USER message
        # is added here; _process_agent_message adds the assistant half.
        self.session.add_message("user", expanded)

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
                self.events.gate_required(str(evt.get("tool", "")),
                                          str(evt.get("reason", "")))
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
            previous = self.session
            self.session = session
            if previous is not None and str(previous.id) != str(session.id):
                if self._discard_if_untouched(previous):
                    self.events.system_note(
                        "the empty session you left was discarded")
            self.agent.load_session_context(session.id)
            self._rebind_local_admin()
            self._emit("session_switched", session.id,
                       len(getattr(session, "messages", []) or []))
            self._refresh_context()
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

    @staticmethod
    def session_is_untouched(session) -> bool:
        """True when the user never actually said anything in this session.

        The criterion is the one `SessionManager.cleanup_empty` already uses -
        no message with role "user" - deliberately NOT a second definition of
        "empty". A session carrying only the system prompt is a session that
        was opened and abandoned, and keeping it just grows the list.
        """
        for message in getattr(session, "messages", []) or []:
            role = getattr(message, "role", None)
            if role is None and isinstance(message, dict):
                role = message.get("role")
            if role is None and isinstance(message, (tuple, list)) and message:
                role = message[0]
            if role == "user":
                return False
        return True

    def _discard_if_untouched(self, session) -> bool:
        """Drop an abandoned session instead of leaving a husk behind."""
        if session is None or not self.session_is_untouched(session):
            return False
        try:
            self.session_mgr.delete(str(session.id))
            return True
        except Exception:
            return False

    def _rebind_local_admin(self) -> None:
        """Put the machine owner back after a session load overwrote the identity."""
        _bind_owner(self.agent)

    def apply_provider_change(self, provider: str = "", model: str = "") -> None:
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
        """
        def _run():
            from vaf.core.agent import reload_all_api_backends
            from vaf.core.config import Config
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

    def stop_speech(self) -> None:
        """`halt`/`stop`: silence the agent WHILE it is speaking.

        Its own short thread, not the lane: the lane is exactly the thread that
        is busy running the turn whose speech the user wants stopped.
        """
        def _run():
            try:
                from vaf.core.speech import get_speech_manager
                get_speech_manager().stop()
            except Exception:
                pass
            self.events.system_note("speech stopped")

        threading.Thread(target=_run, daemon=True, name="vaf-tui-halt").start()

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

            from vaf.core.subagent_ipc import get_current_session_id, get_ipc
            ipc = get_ipc()
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
        try:
            return self.session_mgr.list(limit=20)
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
        """
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
            deadline = time.monotonic() + 5.0
            while self._busy and time.monotonic() < deadline:
                time.sleep(0.1)
            try:
                if self._discard_if_untouched(self.session):
                    pass                    # nothing was said: leave no husk
                else:
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
    instead of breaking a prompt), the web-input TaskQueue watcher (coexistence
    is the next round's decision), and the classic lane's speech preloads - TTS
    engine warmup, the STT microphone check, and the langid preload - which
    land with the voice round; until then the first spoken reply pays the
    engine spin-up lazily.

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

    # The COMPLETE session swap - never the hand-rolled replay loops that the
    # classic lane still carries (those drop tool_calls, images and identity).
    agent.load_session_context(session.id)

    # Re-bind the machine owner AFTER the swap: load_session_context overwrites
    # the identity from the session's own metadata.
    _bind_owner(agent)

    bridge = AgentBridge(agent, session, session_mgr, events)
    agent.set_event_sink(bridge.on_sink_event)
    return bridge
