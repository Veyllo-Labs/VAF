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


class _Console:
    def __init__(self, log):
        self._log = log

    def print(self, *args, **kwargs):
        self._log.append(" ".join(str(a) for a in args))


class _FakeTUI:
    """The terminal: records what the loop shows, answers the prompt from a script."""

    primary = "cyan"
    muted = "dim"
    accent = "magenta"

    def __init__(self, rec):
        self._rec = rec
        self.console = _Console(rec["printed"])

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

    def chat_step(self, text, *a, **k):
        self._rec["chat_steps"].append(text)
        return "ok"

    def load_session_context(self, sid, *a, **k):
        self._rec["loaded"].append(sid)

    def get_token_usage(self):
        return (0, 0)

    def init_chat(self, *a, **k):
        self._rec["init_chat"] += 1

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
    rec = {"warnings": [], "errors": [], "events": [], "printed": [], "other": [], "chat_steps": [],
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


def _queue(source, text, *, contact=True, sid=CONTACT_SID):
    meta = {"enqueue_session_id": sid, "user_scope_id": SCOPE}
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
    enqueue_system_command("NEW_SESSION")
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
    modern_lane.monkeypatch.setattr(_Console, "print", lambda self, *a, **k: _breaks(*a, **k))
    rec = modern_lane.run(console_breaks=True)
    assert any("standard input" in w for w in console["warnings"])
    assert rec["chat_steps"] == [], "typed quit was handed to the agent as chat text"
    assert typed == [], "the fallback never read the typed input"


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
