# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf run --web` answers no messenger or mail task, and queued text is never a terminal command.

The modern lane hosts the web server in the terminal's process, and its prompt loop is the
only consumer of that process's task queue. A task a messenger bridge or the mail answering
lane queued used to run there as the owner's own turn: every tool, no Front Office set or
briefing, and the text went through the terminal's shortcuts and commands, so a contact's bare
`quit` ended the session and `settings` opened the menu on the operator's screen. These tests
drive the REAL `_run_modern` loop with a recording terminal and a recording agent; nothing
starts a bridge, a server or a model.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest

import vaf.cli.cmd.run as run_mod
from vaf.core.channel_ingress_policy import CHANNEL_TASK_SOURCES, MAIL_CHANNEL
from vaf.core.channels import CHAT_CHANNELS
from vaf.core.task_queue import TaskQueue, enqueue_system_command

ROOT = Path(__file__).resolve().parents[1]

CONTACT_SID = "whatsapp_alice_447700900123"
CHAT_LABEL = "Alice Example"
SCOPE = "ab12cd34-0000-4000-8000-000000000001"
WAKE = "__WAKE_WORD_TRIGGERED__"


class _Stop(Exception):
    """Raised by the recorded os.execl, as the real one never returns."""


class _FakeTUI:
    """The terminal: records what the loop shows, answers the prompt from a script."""

    primary = "cyan"
    muted = "dim"
    accent = "magenta"

    def __init__(self, rec):
        self._rec = rec
        # A real Rich console: a markup error or an escape sequence behaves as on a terminal.
        self.console = rec["console"]

    def warning(self, msg, *a, **k):
        self._rec["warnings"].append(str(msg))

    def error(self, msg, *a, **k):
        self._rec["errors"].append(str(msg))

    def event(self, title, msg="", *a, **k):
        self._rec["events"].append((str(title), str(msg), k.get("style")))

    def confirm(self, *a, **k):
        return False

    def spinner(self, *a, **k):
        return contextlib.nullcontext()

    def listen_overlay(self, *a, **k):
        self._rec["mic_opened"] += 1
        return None

    def input_box(self, *a, **k):
        self._rec["prompts"] += 1
        script = self._rec["script"]
        step = script.pop(0) if script else None
        return step() if callable(step) else step

    def __getattr__(self, name):
        def _record(*a, **k):
            self._rec["other"].append(name)
        return _record


class _FakeAgent:
    provider = "deepseek"
    state_registry = None
    use_server = False
    api_backend = object()
    history: list = []
    LANGUAGE_NAMES_NATIVE: dict = {}

    def __init__(self, rec):
        self._rec = rec
        self.config = {}
        self._session_id = "cli-start"
        self.last_turn = types.SimpleNamespace(failed=False, failure="")
        # The claim and the history, as Agent.load_session_context keeps them: a load of the
        # chat the agent already claims returns early and keeps what it holds.
        self.current_session_id = None
        self.history = []

    def chat_step(self, text, *a, **k):
        self._rec["chat_steps"].append(text)
        self._rec["turn_history"].append(list(self.history))
        if self._rec.get("chat_raises"):
            raise self._rec.pop("chat_raises")
        return "ok"

    def load_session_context(self, sid, *a, force=False, **k):
        self._rec["loaded"].append(sid)
        if not force and self.current_session_id == sid:
            return
        self.current_session_id = sid
        self.history = [{"role": "system", "content": f"history of {sid}"}]

    def get_token_usage(self):
        return (0, 0)

    def init_chat(self, *a, **k):
        self._rec["init_chat"] += 1
        self.history = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class _NoThread:
    """No heartbeat, notifier or watcher may outlive the test."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture
def modern_lane(monkeypatch, tmp_path):
    screen = io.StringIO()
    from rich.console import Console
    rec = {"screen": screen,
           "console": Console(file=screen, force_terminal=False, color_system=None, width=200),
           "warnings": [], "errors": [], "events": [], "other": [], "chat_steps": [],
           "turn_history": [],
           "loaded": [], "prompts": 0, "script": [], "mic_opened": 0, "init_chat": 0,
           "menu": 0, "execl": [], "repair": 0, "server_starts": []}

    import vaf.core.session as session_mod
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(TaskQueue, "_instance", None)

    import vaf.cli.gate as gate_mod
    monkeypatch.setattr(gate_mod, "require_admin_password", lambda *a, **k: True)
    import vaf.core.at_rest_migration as at_rest
    monkeypatch.setattr(at_rest, "run_once", lambda *a, **k: None)
    import vaf.memory.reembed as reembed
    monkeypatch.setattr(reembed, "ensure_embedding_model_current", lambda *a, **k: None)
    monkeypatch.setattr(reembed, "read_status", lambda *a, **k: None)
    import vaf.cli.tui as tui_mod
    monkeypatch.setattr(tui_mod, "TUI", lambda *a, **k: _FakeTUI(rec))
    from vaf.cli.themes import ThemeManager
    monkeypatch.setattr(ThemeManager, "set_theme", staticmethod(lambda *a, **k: None))
    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "set_current_session_id", lambda *a, **k: None)
    monkeypatch.setattr(ipc_mod, "cleanup_other_sessions", lambda *a, **k: None)
    import vaf.vendor.langid as langid_mod
    monkeypatch.setattr(langid_mod, "classify", lambda *a, **k: ("en", 1.0))
    import vaf.core.speech as speech_mod
    monkeypatch.setattr(speech_mod, "get_speech_manager",
                        lambda: types.SimpleNamespace(stop=lambda: None))

    monkeypatch.setattr(run_mod, "threading",
                        types.SimpleNamespace(Thread=_NoThread, Event=threading.Event))
    monkeypatch.setattr(run_mod, "_check_and_install_git", lambda tui: True)
    monkeypatch.setattr(run_mod, "_check_subagent_results", lambda tui, agent: [])
    monkeypatch.setattr(run_mod, "get_web_interface", lambda: run_mod._NullWebInterface())
    monkeypatch.setattr(run_mod, "_make_cli_agent", lambda *a, **k: _FakeAgent(rec))

    import vaf.cli.cmd.settings as settings_mod

    def _menu(*a, **k):
        rec["menu"] += 1
        return False
    monkeypatch.setattr(settings_mod, "main_menu", _menu)
    # The settings branch resets sys.stdout to the raw stream; give it back afterwards.
    monkeypatch.setattr(sys, "stdout", sys.stdout)

    import vaf.core.service_health as health_mod

    def _repair(*a, **k):
        rec["repair"] += 1
        return {"ok": True}
    monkeypatch.setattr(health_mod, "repair_service_stack", _repair)

    def _execl(*a, **k):
        rec["execl"].append(a)
        raise _Stop("execl")
    monkeypatch.setattr(os, "execl", _execl)

    # A loop that broke its console falls back to input(); here that ends the run instead of
    # reading the test runner's stdin forever.
    def _no_stdin(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _no_stdin)

    # The web block, for the server gate: a recorded server start, never the real one, whose
    # startup starts the bridges and the product loops.
    fake_server = types.ModuleType("vaf.core.web_server")
    fake_server.start_background_server = lambda **k: rec["server_starts"].append(k)
    monkeypatch.setitem(sys.modules, "vaf.core.web_server", fake_server)
    from vaf.core.frontend_manager import FrontendManager
    monkeypatch.setattr(FrontendManager, "start_frontend", lambda self, *a, **k: None)
    import vaf.core.instance as instance_mod
    monkeypatch.setattr(instance_mod, "find_service", lambda: None)

    def run(web_enabled=False, console_breaks=False):
        run_mod._run_modern(None, False, "vaf", session_id=None, web_enabled=web_enabled)
        if not console_breaks:
            # A loop that broke its console runs on in the fallback and proves nothing.
            assert rec["errors"] == [], rec["errors"]
            assert not [w for w in rec["warnings"] if "standard input" in w], rec["warnings"]
        return rec

    lane = types.SimpleNamespace(run=run, rec=rec, tmp=tmp_path, monkeypatch=monkeypatch,
                                 instance=instance_mod)
    return lane


_OWNER = object()


def _owner_scope() -> str:
    from vaf.core.config import get_local_admin_scope_id
    return str(get_local_admin_scope_id())


def _queue(source, text, *, contact=True, sid=CONTACT_SID, scope=_OWNER, role=None):
    """A task shaped like its producer's. A browser chat carries the connection's identity
    (web_server's chat frame); the owner's browser has the local admin's scope."""
    if scope is _OWNER:
        scope = SCOPE if contact else _owner_scope()
    meta = {"enqueue_session_id": sid}
    if scope is not None:
        meta["user_scope_id"] = scope
    if role is not None:
        meta["role"] = role
    if contact:
        meta.update({"from_contact": True, "chat_label": CHAT_LABEL,
                     "whatsapp_chat_jid": "447700900123@s.whatsapp.net"})
    TaskQueue().add(session_id=sid, input_text=text, source=source, metadata=meta)


def _session_exists(sid) -> bool:
    from vaf.core.session import SessionManager
    try:
        SessionManager().load(sid)
        return True
    except Exception:
        return False


def _texts(tmp):
    return {
        "Hallo": None,
        "quit": None,
        "settings": None,
        "s": None,
        "restart": None,
        "repair": None,
        "/export": f"/export {(tmp / 'x.md').as_posix()}",
        "@file": f"@{(tmp / 'secret.txt').as_posix()} hi",
    }


_TEXT_KEYS = ["Hallo", "quit", "settings", "s", "restart", "repair", "/export", "@file"]

# (source, from_contact, the name the note must carry)
_DECLINED = [
    ("whatsapp", True, "WhatsApp"),
    ("telegram", True, "Telegram"),
    ("discord", True, "Discord"),
    ("email", True, "mail"),
    ("web", True, "a contact"),
]


def _text(lane, key):
    (lane.tmp / "secret.txt").write_text("SECRET-LINE-0001\n", encoding="utf-8")
    return _texts(lane.tmp)[key] or key


def _assert_declined(lane, rec, text, name, prompts_expected):
    assert rec["chat_steps"] == [], "a declined task ran a turn"
    assert rec["loaded"] == [], "a declined task loaded its session into the agent"
    assert not _session_exists(CONTACT_SID), "a declined task left a session file behind"
    assert rec["menu"] == 0 and rec["execl"] == [] and rec["repair"] == 0
    assert not (lane.tmp / "x.md").exists(), "a declined task wrote a file"
    assert rec["prompts"] == prompts_expected, "the loop did not come back to the prompt"
    assert CONTACT_SID not in TaskQueue()._session_inflight, "the declined task was not released"
    notes = [w for w in rec["warnings"] if w.startswith("Not run here:")]
    assert len(notes) == 1, rec["warnings"]
    assert rec["warnings"] == notes, rec["warnings"]
    note = notes[0]
    assert f"a task from {name}." in note
    assert "vaf start" in note
    # The sender's words never reach the terminal ("s" is one letter of the note itself).
    assert CHAT_LABEL not in note
    if len(text) > 1:
        assert text not in note


@pytest.mark.parametrize("key", _TEXT_KEYS)
@pytest.mark.parametrize("source,contact,name", _DECLINED)
def test_poll_before_the_prompt_declines_channel_and_contact_tasks(modern_lane, source, contact,
                                                                   name, key):
    text = _text(modern_lane, key)
    _queue(source, text, contact=contact)
    rec = modern_lane.run()
    _assert_declined(modern_lane, rec, text, name, prompts_expected=1)


@pytest.mark.parametrize("key", _TEXT_KEYS)
@pytest.mark.parametrize("source,contact,name", _DECLINED)
def test_poll_after_the_wake_declines_channel_and_contact_tasks(modern_lane, source, contact,
                                                                name, key):
    text = _text(modern_lane, key)

    def _arrives_while_waiting():
        _queue(source, text, contact=contact)
        return WAKE
    modern_lane.rec["script"] = [_arrives_while_waiting, None]
    rec = modern_lane.run()
    _assert_declined(modern_lane, rec, text, name, prompts_expected=2)
    assert rec["mic_opened"] == 0, "a declined wake opened the microphone"


@pytest.mark.parametrize("source,name", [("telegram", "Telegram"), ("email", "mail")])
@pytest.mark.parametrize("wake", [False, True])
def test_the_owners_own_channel_task_is_declined_too(modern_lane, wake, source, name):
    """No contact mark: the source alone decides, for the messengers and for mail."""
    if wake:
        def _arrives():
            _queue(source, "quit", contact=False)
            return WAKE
        modern_lane.rec["script"] = [_arrives, None]
    else:
        _queue(source, "quit", contact=False)
    rec = modern_lane.run()
    _assert_declined(modern_lane, rec, "quit", name, prompts_expected=2 if wake else 1)


def test_a_browser_chat_still_runs_and_a_session_command_still_runs_as_a_command(modern_lane):
    sid = "web-chat-0001"
    _queue("web", "Wie spaet ist es?", contact=False, sid=sid)
    enqueue_system_command("NEW_SESSION", user_scope_id=_owner_scope())
    rec = modern_lane.run()
    assert rec["chat_steps"] == ["Wie spaet ist es?"]
    assert sid in rec["loaded"]
    assert ("WebUI", "Started new session", "success") in rec["events"], \
        "the session command did not run as a command"
    assert not [w for w in rec["warnings"] if w.startswith("Not run here:")]


@pytest.mark.parametrize("key", ["quit", "settings", "s", "restart", "repair", "clear",
                                 "/export", "@file"])
@pytest.mark.parametrize("wake", [False, True])
def test_queued_browser_text_is_never_a_terminal_command(modern_lane, key, wake):
    text = _text(modern_lane, key) if key != "clear" else "clear"
    sid = "web-chat-0002"
    if wake:
        def _arrives():
            _queue("web", text, contact=False, sid=sid)
            return WAKE
        modern_lane.rec["script"] = [_arrives, None]
    else:
        _queue("web", text, contact=False, sid=sid)
    rec = modern_lane.run()
    assert rec["chat_steps"] == [text], "queued text was not handed to the agent unchanged"
    assert rec["menu"] == 0 and rec["execl"] == [] and rec["repair"] == 0
    assert rec["init_chat"] == 1, "queued text cleared the conversation"
    assert not (modern_lane.tmp / "x.md").exists(), "queued text wrote a file"
    assert rec["prompts"] == (2 if wake else 1), "queued text ended the loop"


def test_typed_settings_still_opens_the_menu(modern_lane):
    modern_lane.rec["script"] = ["settings", None]
    rec = modern_lane.run()
    assert rec["menu"] == 1 and rec["chat_steps"] == []


def test_typed_quit_still_ends_the_loop(modern_lane):
    modern_lane.rec["script"] = ["quit", "never read"]
    rec = modern_lane.run()
    assert rec["prompts"] == 1 and rec["chat_steps"] == []


def test_typed_file_reference_is_still_expanded(modern_lane):
    text = _text(modern_lane, "@file")
    modern_lane.rec["script"] = [text, None]
    rec = modern_lane.run()
    assert len(rec["chat_steps"]) == 1 and "SECRET-LINE-0001" in rec["chat_steps"][0]


def test_typed_quit_after_a_browser_chat_still_ends_the_loop(modern_lane):
    """The prompt after a queued task is typed input again, whatever the last task was."""
    _queue("web", "Hallo", contact=False, sid="web-chat-0003")
    modern_lane.rec["script"] = ["quit", "never read"]
    rec = modern_lane.run()
    assert rec["chat_steps"] == ["Hallo"] and rec["prompts"] == 1


def test_typed_quit_after_the_prompt_broke_on_a_queued_task_still_ends_the_loop(modern_lane):
    """The prompt fails after a queued task was taken; the fallback reads typed input."""
    _queue("web", "Hallo", contact=False, sid="web-chat-0004")
    console = modern_lane.rec

    def _breaks(*a, **k):
        if any("Web UI Input" in str(x) for x in a):
            raise RuntimeError("console gone")
    typed = ["quit"]

    def _typed(*a, **k):
        if typed:
            return typed.pop(0)
        raise EOFError
    modern_lane.monkeypatch.setattr("builtins.input", _typed)
    modern_lane.monkeypatch.setattr(console["console"], "print", _breaks)
    rec = modern_lane.run(console_breaks=True)
    assert any("standard input" in w for w in console["warnings"])
    assert rec["chat_steps"] == [], "typed quit was handed to the agent as chat text"
    assert typed == [], "the fallback never read the typed input"
    assert "web-chat-0004" not in TaskQueue()._session_inflight, "the lost task was not released"


def test_no_second_server_when_vaf_already_runs(modern_lane):
    from vaf.core.instance import Instance
    modern_lane.monkeypatch.setattr(modern_lane.instance, "find_service",
                                    lambda: Instance(pid=4242, mode="tray"))
    rec = modern_lane.run(web_enabled=True)
    assert rec["server_starts"] == []
    assert ("WebUI", "VAF is already running (pid 4242); it serves the web UI, messengers and "
            "mail. This terminal starts no second server.", "warning") in rec["events"]


def test_the_server_starts_when_no_vaf_runs(modern_lane):
    rec = modern_lane.run(web_enabled=True)
    assert len(rec["server_starts"]) == 1 and rec["server_starts"][0]["port"] == 8001


def test_channel_task_sources_is_the_one_composition():
    assert CHANNEL_TASK_SOURCES == CHAT_CHANNELS + (MAIL_CHANNEL,)
    pattern = re.compile(r"""\+\s*\(\s*["']email["']\s*,\s*\)""")
    offenders = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == [], f"compose CHANNEL_TASK_SOURCES instead: {offenders}"
    runner = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert runner.count("in CHANNEL_TASK_SOURCES") == 2
    run_src = (ROOT / "vaf" / "cli" / "cmd" / "run.py").read_text(encoding="utf-8")
    assert "in CHANNEL_TASK_SOURCES" in run_src


# Queued text that used to reach terminal actions before the chat branch: the microphone
# sentinel, a text-less frame (only files), Rich markup that does not parse, and an escape
# sequence. Each is the sender's text and nothing else.
_HOSTILE = [
    ("sentinel", WAKE),
    ("no text", None),
    ("markup", "[/]"),
    ("escape", "\x1bc\x1b]0;owned\x07Hallo"),
]


def _arrive(lane, wake, *args, **kwargs):
    if wake:
        def _arrives():
            _queue(*args, **kwargs)
            return WAKE
        lane.rec["script"] = [_arrives, None]
    else:
        _queue(*args, **kwargs)


@pytest.mark.parametrize("label,text", _HOSTILE, ids=[h[0] for h in _HOSTILE])
@pytest.mark.parametrize("wake", [False, True])
def test_queued_text_is_only_ever_chat_text(modern_lane, wake, label, text):
    sid = "web-chat-0005"
    _arrive(modern_lane, wake, "web", text, contact=False, sid=sid)
    rec = modern_lane.run()
    assert rec["chat_steps"] == ([text] if text else []), "the text was not one chat turn"
    assert rec["mic_opened"] == 0, "queued text opened the microphone"
    assert rec["prompts"] == (2 if wake else 1), "queued text ended the loop"
    assert "\x1b" not in rec["screen"].getvalue(), "an escape sequence reached the terminal"
    assert sid not in TaskQueue()._session_inflight


def test_the_echo_shows_markup_as_written_and_drops_control_characters():
    echo = run_mod._terminal_echo("[/] [bold]x[/bold] \x1b[2J\x07 \u202eabc\n\tok")
    from rich.console import Console
    out = io.StringIO()
    Console(file=out, force_terminal=False, color_system=None).print(echo)
    assert out.getvalue().startswith("[/] [bold]x[/bold] [2J abc\n"), out.getvalue()
    assert not {"\x1b", "\x07", "\u202e"} & set(echo)
    assert "\n\tok" in echo, "newline and tab are kept"



@pytest.mark.parametrize("mark", ["\u200e", "\u200f", "\u061c", "\u202a", "\u202e", "\u2066",
                                  "\u2069"])
def test_the_echo_drops_every_bidirectional_control(mark):
    """The marks (left-to-right, right-to-left, the Arabic letter mark) reorder a line as much
    as the embeddings, overrides and isolates do."""
    assert run_mod._terminal_echo(f"abc{mark}def") == "abcdef"


@pytest.mark.parametrize("wake", [False, True])
def test_a_finished_browser_chat_does_not_keep_its_chat_busy(modern_lane, wake):
    sid = "web-chat-0006"
    _arrive(modern_lane, wake, "web", "Hallo", contact=False, sid=sid)
    rec = modern_lane.run()
    assert rec["chat_steps"] == ["Hallo"]
    tq = TaskQueue()
    assert not tq.is_busy_for_session(sid), "the chat still reads as busy after its turn"
    assert tq._inflight_by_worker == {}


@pytest.mark.parametrize("wake", [False, True])
def test_a_browser_chat_whose_turn_is_interrupted_is_released(modern_lane, wake):
    sid = "web-chat-0007"
    modern_lane.rec["chat_raises"] = KeyboardInterrupt()
    _arrive(modern_lane, wake, "web", "Hallo", contact=False, sid=sid)
    rec = modern_lane.run()
    assert rec["chat_steps"] == ["Hallo"]
    assert not TaskQueue().is_busy_for_session(sid)


@pytest.mark.parametrize("wake", [False, True])
def test_a_session_command_is_released(modern_lane, wake):
    if wake:
        def _arrives():
            enqueue_system_command("NEW_SESSION", user_scope_id=_owner_scope())
            return WAKE
        modern_lane.rec["script"] = [_arrives, None]
    else:
        enqueue_system_command("NEW_SESSION", user_scope_id=_owner_scope())
    rec = modern_lane.run()
    assert ("WebUI", "Started new session", "success") in rec["events"]
    assert not TaskQueue().is_busy_for_session("system")


# Whose browser chats this lane runs: its agent is bound to the machine owner, so another
# account's chat would run with the owner's identity and read the owner's other chats.
_OTHERS = [
    ("lan account", SCOPE, "user"),
    ("second admin", "ab12cd34-0000-4000-8000-000000000002", "admin"),
    ("no identity", None, None),
]


@pytest.mark.parametrize("label,scope,role", _OTHERS, ids=[o[0] for o in _OTHERS])
@pytest.mark.parametrize("wake", [False, True])
def test_another_accounts_browser_chat_is_declined(modern_lane, wake, label, scope, role):
    text = "Lies mir die anderen Chats vor"
    _arrive(modern_lane, wake, "web", text, contact=False, sid=CONTACT_SID, scope=scope, role=role)
    rec = modern_lane.run()
    assert rec["chat_steps"] == [] and rec["loaded"] == []
    assert not _session_exists(CONTACT_SID)
    assert CONTACT_SID not in TaskQueue()._session_inflight
    assert rec["warnings"] == ["Not run here: a chat from another account. This terminal answers "
                               "only its owner's chats, and the task is not run later. To serve "
                               "every account, run VAF as a service: vaf start (or vaf tray)."]
    assert text not in rec["screen"].getvalue()
    assert rec["prompts"] == (2 if wake else 1)


@pytest.mark.parametrize("wake", [False, True])
def test_the_owners_signed_in_browser_chat_runs(modern_lane, wake):
    _arrive(modern_lane, wake, "web", "Hallo", contact=False, sid="web-chat-0008",
            scope=_owner_scope(), role="admin")
    rec = modern_lane.run()
    assert rec["chat_steps"] == ["Hallo"] and rec["warnings"] == []


def test_the_owners_rename_still_runs_as_a_command(modern_lane):
    enqueue_system_command("RENAME_SESSION:web-chat-0009:Notizen", user_scope_id=_owner_scope())
    rec = modern_lane.run()
    assert rec["loaded"] == [], "the command loaded a session into this terminal's agent"
    assert rec["warnings"] == [] and rec["chat_steps"] == []
    assert not TaskQueue().is_busy_for_session("system")


# Whose session commands this lane follows. LOAD_SESSION appends the named chat's messages to
# this terminal's agent, whose next turn then runs in that chat as the owner; NEW_SESSION and
# RENAME_SESSION move and rename this terminal's session. The web server stamps each with the
# connection's scope, and only the owner's are followed. RELOAD_CONFIG moves no session.
_BOB_SID = "web-chat-0010"
_DECLINED_COMMAND = ("Not run here: another account opened, started or renamed a chat in its "
                     "browser. This terminal follows only its owner's chats.")


def _seed_bobs_chat():
    from vaf.core.session import SessionManager
    mgr = SessionManager()
    s = mgr.new(name="Bobs Geheimnisse", user_scope_id=SCOPE)
    s.id = _BOB_SID
    s.add_message("user", "BOB-SECRET-0001")
    mgr.save(s)


def _command_arrives(lane, wake, command, scope):
    def _enqueue():
        enqueue_system_command(command, user_scope_id=scope)
    if wake:
        def _arrives():
            _enqueue()
            return WAKE
        lane.rec["script"] = [_arrives, None]
    else:
        _enqueue()


_SESSION_COMMANDS = [
    ("load", f"LOAD_SESSION:{_BOB_SID}"),
    ("new", "NEW_SESSION"),
    ("rename", f"RENAME_SESSION:{_BOB_SID}:Umbenannt"),
]


@pytest.mark.parametrize("label,command", _SESSION_COMMANDS, ids=[c[0] for c in _SESSION_COMMANDS])
@pytest.mark.parametrize("scope", [SCOPE, None], ids=["other account", "no scope"])
@pytest.mark.parametrize("wake", [False, True])
def test_another_accounts_session_command_is_declined(modern_lane, wake, scope, label, command):
    _seed_bobs_chat()
    _command_arrives(modern_lane, wake, command, scope)
    rec = modern_lane.run()
    assert rec["loaded"] == [], "the command was taken into this terminal's agent"
    assert rec["init_chat"] == 1, "the command reset this terminal's conversation"
    assert not [e for e in rec["events"] if e[0] == "WebUI"], rec["events"]
    assert rec["warnings"] == [_DECLINED_COMMAND]
    screen = rec["screen"].getvalue()
    assert _BOB_SID not in screen and "Bobs Geheimnisse" not in screen and "BOB-SECRET" not in screen
    assert not TaskQueue().is_busy_for_session("system"), "the declined command was not released"
    assert rec["prompts"] == (2 if wake else 1)


@pytest.mark.parametrize("wake", [False, True])
def test_the_owners_session_commands_still_run(modern_lane, wake):
    from vaf.core.session import SessionManager
    mgr = SessionManager()
    s = mgr.new(name="Notizen", user_scope_id=_owner_scope())
    s.add_message("user", "OWNER-LINE-0001")
    mgr.save(s)
    owner = _owner_scope()
    if wake:
        def _arrives():
            enqueue_system_command(f"LOAD_SESSION:{s.id}", user_scope_id=owner)
            return WAKE
        modern_lane.rec["script"] = [_arrives, None]
    else:
        enqueue_system_command(f"LOAD_SESSION:{s.id}", user_scope_id=owner)
    rec = modern_lane.run()
    assert ("WebUI", "Switched to session: Notizen", "success") in rec["events"]
    assert rec["warnings"] == []
    assert not TaskQueue().is_busy_for_session("system")


@pytest.mark.parametrize("scope", [None, SCOPE], ids=["no scope", "other account"])
def test_reload_config_runs_with_any_scope(modern_lane, scope):
    import vaf.core.agent as agent_mod
    reloads = []
    modern_lane.monkeypatch.setattr(agent_mod, "reload_all_api_backends",
                                    lambda **k: reloads.append(k) or False)
    TaskQueue().add(session_id="system", input_text="__CMD__:RELOAD_CONFIG", source="web",
                    metadata={"task_class": "background", "system_command": True,
                              **({"user_scope_id": scope} if scope else {})})
    rec = modern_lane.run()
    assert reloads == [{"force": True}], "RELOAD_CONFIG did not run"
    assert rec["warnings"] == []


def _ws_commands(monkeypatch, tmp_path, frames):
    """Drive the real WebSocket handler as a signed-in account (Bob) and return what it
    queued through enqueue_system_command."""
    import json
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    import vaf.auth.permissions as permissions
    import vaf.core.session as session_mod
    import vaf.core.task_queue as tq_mod
    import vaf.core.web_server as real_server

    sessions_dir = tmp_path / "ws-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions_dir)
    mgr = session_mod.SessionManager(storage_dir=str(sessions_dir))
    monkeypatch.setattr(real_server, "session_mgr", mgr)
    s = mgr.new(name="Bobs Chat", user_scope_id=SCOPE)
    s.id = _BOB_SID
    mgr.save(s)

    async def _unknown(scope):
        return ("unknown", "")
    monkeypatch.setattr(permissions, "account_standing_async", _unknown)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    queued = []
    monkeypatch.setattr(tq_mod, "enqueue_system_command",
                        lambda command, **k: queued.append((command, k.get("user_scope_id"))))
    token = jwt.encode({"sub": "2", "user_scope_id": SCOPE, "username": "bob", "role": "user"},
                       "s" * 32, algorithm="HS256")
    with TestClient(real_server.app, client=("127.0.0.1", 40001)).websocket_connect(
            f"/ws?token={token}") as ws:
        for frame in frames:
            ws.send_text(json.dumps(frame))
        import time
        deadline = time.monotonic() + 10
        while len(queued) < len(frames) and time.monotonic() < deadline:
            time.sleep(0.05)
    return queued


def test_the_web_server_stamps_the_connections_scope_on_session_commands(monkeypatch, tmp_path):
    queued = _ws_commands(monkeypatch, tmp_path, [
        {"type": "load_session", "id": _BOB_SID},
        {"type": "rename_session", "id": _BOB_SID, "newName": "Neu"},
        {"type": "new_session"},
    ])
    by_name = {c.split(":", 1)[0]: scope for c, scope in queued}
    assert by_name == {"LOAD_SESSION": SCOPE, "RENAME_SESSION": SCOPE, "NEW_SESSION": SCOPE}, queued


# A turn VAF starts by itself, the way its producers queue it (enqueue_wake_turn): the owner's
# scope, source "web" and the wake kind. The runner binds it as such; this loop cannot.
_WAKES = [
    ("timer", "a timer"),
    ("process", "a background command's report"),
    ("draft", "a draft's report"),
]


@pytest.mark.parametrize("kind,what", _WAKES, ids=[w[0] for w in _WAKES])
@pytest.mark.parametrize("wake", [False, True])
def test_a_wake_turn_is_declined(modern_lane, wake, kind, what):
    from vaf.core.task_queue import enqueue_wake_turn
    sid = "web-chat-0011"
    text = "Background command finished. End of its output:\nquit"

    def _enqueue():
        enqueue_wake_turn(kind=kind, session_id=sid, text=text, source="web",
                          user_scope_id=_owner_scope(), username="admin", role="admin")
    if wake:
        def _arrives():
            _enqueue()
            return WAKE
        modern_lane.rec["script"] = [_arrives, None]
    else:
        _enqueue()
    rec = modern_lane.run()
    assert rec["chat_steps"] == [] and rec["loaded"] == []
    assert not _session_exists(sid), "a declined wake left a session file behind"
    assert rec["warnings"] == [
        f"Not run here: {what}. This terminal does not run the turns VAF starts by itself "
        "(timers, background command and draft reports), and this one is not run later. "
        "To have them run, run VAF as a service: vaf start (or vaf tray)."]
    assert "End of its output" not in rec["screen"].getvalue()
    assert not TaskQueue().is_busy_for_session(sid)
    assert rec["prompts"] == (2 if wake else 1)


# A failure between the take and the turn: the session load or its fallback save raises. The
# task was taken, so the handler must release it, once.
@pytest.mark.parametrize("where", ["load_session_context", "fallback save"])
@pytest.mark.parametrize("wake", [False, True])
def test_a_task_whose_session_setup_fails_is_released_once(modern_lane, wake, where):
    sid = "web-chat-0012"
    if where == "load_session_context":
        def _boom(self, *a, **k):
            raise RuntimeError("session store gone")
        modern_lane.monkeypatch.setattr(_FakeAgent, "load_session_context", _boom)
    else:
        from vaf.core.session import SessionManager
        real_save = SessionManager.save

        def _save(self, session, *a, **k):
            if getattr(session, "id", None) == sid:
                raise RuntimeError("disk full")
            return real_save(self, session, *a, **k)
        modern_lane.monkeypatch.setattr(SessionManager, "save", _save)
    released = []
    real_done = TaskQueue.task_done

    def _done(self, task=None, worker_id=None):
        released.append(getattr(task, "session_id", None))
        return real_done(self, task=task, worker_id=worker_id)
    modern_lane.monkeypatch.setattr(TaskQueue, "task_done", _done)
    _arrive(modern_lane, wake, "web", "Hallo", contact=False, sid=sid, scope=_owner_scope())
    rec = modern_lane.run(console_breaks=True)
    assert rec["chat_steps"] == []
    assert released == [sid], f"released {released}, expected exactly once"
    tq = TaskQueue()
    assert not tq.is_busy_for_session(sid), "the taken task was left in flight"
    assert tq._inflight_by_worker == {}


def test_a_session_command_counts_as_nobodys_turn_in_the_queue(monkeypatch):
    """Scoped or not, a system command is housekeeping for the queue's fairness and for the
    revocation's stop: counted as the account's, revoking it would stop the shared command
    session and drop every account's queued commands."""
    from vaf.core.trust import _scope_key
    monkeypatch.setattr(TaskQueue, "_instance", None)
    enqueue_system_command(f"LOAD_SESSION:{_BOB_SID}", user_scope_id=SCOPE)
    tq = TaskQueue()
    assert tq.sessions_for_scope(SCOPE) == set()
    assert tq.is_busy_for_scope(_scope_key(SCOPE), _scope_key) is False
    tq.add(session_id=_BOB_SID, input_text="Hallo", source="web", metadata={"user_scope_id": SCOPE})
    assert tq.sessions_for_scope(SCOPE) == {_BOB_SID}
    assert tq.is_busy_for_scope(_scope_key(SCOPE), _scope_key) is True


@pytest.mark.parametrize("wake", [False, True])
def test_a_declined_task_whose_note_fails_is_released_once(modern_lane, wake):
    """The decline releases the task before its note; a note that raises reaches the handler,
    which must not release it a second time (that would free a claim someone else holds)."""
    sid = "web-chat-0013"
    released = []
    real_done = TaskQueue.task_done

    def _done(self, task=None, worker_id=None):
        released.append(getattr(task, "session_id", None))
        return real_done(self, task=task, worker_id=worker_id)
    modern_lane.monkeypatch.setattr(TaskQueue, "task_done", _done)

    def _warning(self, msg, *a, **k):
        self._rec["warnings"].append(str(msg))
        if str(msg).startswith("Not run here:"):
            raise RuntimeError("terminal gone")
    modern_lane.monkeypatch.setattr(_FakeTUI, "warning", _warning)
    _arrive(modern_lane, wake, "web", "Hallo", contact=False, sid=sid, scope=SCOPE)
    rec = modern_lane.run(console_breaks=True)
    assert rec["chat_steps"] == []
    assert released == [sid], f"released {released}, expected exactly once"



def _chats_holding(text):
    from vaf.core.session import SessionManager
    mgr = SessionManager()
    found = []
    for path in Path(mgr.storage_dir).glob("*.json"):
        try:
            s = mgr.load(path.stem)
        except Exception:
            continue
        if any(text in str(m.content) for m in s.messages):
            found.append(s.id)
    return found


_OWN_COMMANDS = [
    ("reload", "RELOAD_CONFIG", None),
    ("rename", "RENAME_SESSION:web-chat-0009:Notizen", _OWNER),
    ("new", "NEW_SESSION", _OWNER),
]


@pytest.mark.parametrize("label,command,scope", _OWN_COMMANDS, ids=[c[0] for c in _OWN_COMMANDS])
@pytest.mark.parametrize("wake", [False, True])
def test_a_command_leaves_this_terminal_on_its_own_chat(modern_lane, wake, label, command, scope):
    """A command is handled before anything is loaded for its task: its session is the shared
    command id, no chat. Loaded like a chat it left the terminal on a chat named `system`,
    created it, and the owner's next typed turn was saved there; any account's settings save
    queues RELOAD_CONFIG. MUTATION: move the session load back above the command branch - red."""
    typed = "OWNER-TYPED-LINE-0001"
    owner = _owner_scope() if scope is _OWNER else None
    if wake:
        def _arrives():
            enqueue_system_command(command, user_scope_id=owner)
            return WAKE
        modern_lane.rec["script"] = [_arrives, typed, None]
    else:
        enqueue_system_command(command, user_scope_id=owner)
        modern_lane.rec["script"] = [typed, None]
    import vaf.core.agent as agent_mod
    modern_lane.monkeypatch.setattr(agent_mod, "reload_all_api_backends", lambda **k: False)
    rec = modern_lane.run()
    assert rec["chat_steps"] == [typed]
    assert "system" not in rec["loaded"], "the command was loaded as a chat"
    assert not _session_exists("system"), "the command created a chat named system"
    holders = _chats_holding(typed)
    assert len(holders) == 1 and holders[0] != "system", holders
    assert not TaskQueue().is_busy_for_session("system")



@pytest.mark.parametrize("command", ["LOAD_SESSION", "NEW_SESSION"])
@pytest.mark.parametrize("wake", [False, True])
def test_a_chat_switch_leaves_the_agent_claiming_the_chat_it_holds(modern_lane, wake, command):
    """The owner chats in A (one tab), switches to B or starts a new chat (another tab), then
    chats in A again. The switch must leave the agent claiming the chat whose history it holds:
    a load of the chat the agent already claims returns early, so a claim left on A with B's
    history (or none) ran A's next turn on it. MUTATION: init_chat (plus a hand copy of the
    messages) instead of load_session_context in either branch - red."""
    from vaf.core.session import SessionManager
    owner = _owner_scope()
    mgr = SessionManager()
    chats = {}
    for name in ("A", "B"):
        s = mgr.new(name=f"Chat {name}", user_scope_id=owner)
        s.add_message("user", f"LINE-OF-{name}")
        mgr.save(s)
        chats[name] = s.id
    target = f"LOAD_SESSION:{chats['B']}" if command == "LOAD_SESSION" else "NEW_SESSION"
    steps = [
        lambda: _queue("web", "erste Frage", contact=False, sid=chats["A"], scope=owner),
        lambda: enqueue_system_command(target, user_scope_id=owner),
        lambda: _queue("web", "zweite Frage", contact=False, sid=chats["A"], scope=owner),
    ]
    if wake:
        def _arrives(step):
            def _do():
                step()
                return WAKE
            return _do
        modern_lane.rec["script"] = [_arrives(s) for s in steps] + [None]
    else:
        def _feed(tui, agent):
            if steps:
                steps.pop(0)()
            return []
        modern_lane.monkeypatch.setattr(run_mod, "_check_subagent_results", _feed)
    rec = modern_lane.run()
    assert rec["chat_steps"] == ["erste Frage", "zweite Frage"]
    expected = [{"role": "system", "content": f"history of {chats['A']}"}]
    assert rec["turn_history"][1] == expected, "A's second turn ran on another chat's history"
