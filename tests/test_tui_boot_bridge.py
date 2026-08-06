# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""boot_bridge wiring: the production-only boot path, executed under stubs.

This was the round's largest blind spot: every seam boot_bridge touches
(session registration, IPC session id, agent construction, backend init,
session-context swap, admin re-bind, sink wiring) only ever ran on the owner's
machine. These tests drive the REAL function with the heavy seams stubbed, so
a renamed function, a changed signature, or a dropped wiring step goes red
here instead of exploding on the first `vaf run` after an update.
"""
from types import SimpleNamespace
from uuid import UUID

import pytest

SCOPE = "12345678-1234-5678-1234-567812345678"


class BootAgent:
    """Only what boot_bridge actually touches - an unexpected touch raises."""

    provider = "veyllo"
    api_backend = object()          # truthy: skips local download/warmup branch
    state_registry = "REGISTRY-SENTINEL"

    def __init__(self):
        self._session_id = "tmp-random-id"
        self.config = {}              # speech preloads: both gates off
        self.calls = []
        self.sink = None
        self.current_session_id = None

    def _unregister_session(self):
        self.calls.append("unregister")

    def _register_session(self):
        self.calls.append("register")

    def load_model(self, skip_download_check=False):
        self.calls.append("load_model")

    def init_chat(self):
        self.calls.append("init_chat")

    def load_session_context(self, session_id):
        self.calls.append(("load_ctx", session_id))

    def set_event_sink(self, sink):
        self.sink = sink


class BootSession:
    def __init__(self, sid):
        self.id = sid
        self.messages = []


class StubMgr:
    instances: list = []

    def __init__(self):
        StubMgr.instances.append(self)
        self.saved = []
        self.cleaned = []
        self.known = {}

    def new(self, name=None, model="", project_path="", user_scope_id=None):
        self.new_scope = user_scope_id
        return BootSession("new-sess-1")

    def load(self, session_id, restore_state=True):
        if session_id in self.known:
            return self.known[session_id]
        raise FileNotFoundError(session_id)

    def save(self, session, **kwargs):
        self.saved.append(session.id)

    def cleanup_empty(self, exclude_session_id=None):
        self.cleaned.append(exclude_session_id)
        return 0

    def claim_unscoped(self, user_scope_id):
        self.claimed_scope = user_scope_id
        return 0


@pytest.fixture()
def boot_env(monkeypatch):
    import vaf.cli.cmd.run as run_mod
    import vaf.core.config as config_mod
    import vaf.core.session as session_mod
    import vaf.core.subagent_ipc as ipc_mod

    agent = BootAgent()
    web = SimpleNamespace(registered=[],
                          register_agent=lambda a: web.registered.append(a))
    ipc_calls = []

    StubMgr.instances = []
    monkeypatch.setattr(session_mod, "SessionManager", StubMgr)
    monkeypatch.setattr(run_mod, "_make_cli_agent", lambda verbose=False: agent)
    monkeypatch.setattr(run_mod, "_quiet_cli_http_logs", lambda: None)
    monkeypatch.setattr(run_mod, "_warmup_model",
                        lambda tui: ipc_calls.append("warmup"))
    monkeypatch.setattr(run_mod, "get_web_interface", lambda: web)
    monkeypatch.setattr(ipc_mod, "set_current_session_id",
                        lambda sid: ipc_calls.append(("set_sid", sid)))
    monkeypatch.setattr(ipc_mod, "cleanup_other_sessions",
                        lambda: ipc_calls.append("cleanup_others"))
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: SCOPE)
    monkeypatch.setattr(config_mod, "get_local_admin_username", lambda: "admin-test")
    # The service-stack start is stubbed HARD: a boot test that reached the
    # real primitive would run `docker compose up` against the machine.
    import vaf.core.service_stack as stack_mod
    stack_calls = []
    monkeypatch.setattr(stack_mod, "find_stack_root",
                        lambda: "/repo")
    monkeypatch.setattr(stack_mod, "ensure_service_stack",
                        lambda log=None: stack_calls.append("ensure"))
    # The langid warmup is deliberately unconditional in boot (first call
    # ~1.6s); the fake keeps the boot tests from paying it for real.
    import sys as _sys

    import vaf.vendor as _vendor
    langid_calls = []
    fake_langid = SimpleNamespace(classify=lambda t: langid_calls.append(t))
    monkeypatch.setitem(_sys.modules, "vaf.vendor.langid", fake_langid)
    monkeypatch.setattr(_vendor, "langid", fake_langid, raising=False)
    return SimpleNamespace(agent=agent, web=web, ipc=ipc_calls,
                           langid=langid_calls, stack=stack_calls)


def _events():
    return SimpleNamespace(**{name: (lambda *a, **k: None) for name in (
        "presence", "context", "event_note", "system_note")})


def test_boot_wires_every_seam_in_the_classic_order(boot_env):
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    bridge = boot_bridge(_events(), "vaf", None, False)
    agent, mgr = boot_env.agent, StubMgr.instances[0]

    # Session lane: new session with the admin scope, saved, registered in IPC,
    # other sessions cleaned, empties swept around it.
    assert mgr.new_scope == SCOPE
    assert mgr.saved == ["new-sess-1"]
    assert ("set_sid", "new-sess-1") in boot_env.ipc
    assert "cleanup_others" in boot_env.ipc
    assert mgr.cleaned == ["new-sess-1"]

    # Web + state registry handshake.
    assert boot_env.web.registered == [agent]
    assert mgr.state_registry == "REGISTRY-SENTINEL"

    # WebSocket session-id swap ran (temporary id differed).
    assert agent.calls.count("unregister") == 1
    assert agent.calls.count("register") == 1
    assert agent._session_id == "new-sess-1"

    # Backend init for an API provider: load + init, NO local-only steps
    # (BootAgent defines neither ensure_model_exists nor get_token_usage -
    # calling them would raise) and no warmup (api_backend truthy).
    assert "load_model" in agent.calls and "init_chat" in agent.calls
    assert "warmup" not in boot_env.ipc

    # The COMPLETE session swap, then the owner re-bind AFTER it.
    assert ("load_ctx", "new-sess-1") in agent.calls
    assert agent.calls.index(("load_ctx", "new-sess-1")) > agent.calls.index("init_chat")
    # VERBATIM, not uuid.UUID(...). is_admin_identity compares this value against
    # the same config entry as plain text, so parsing it on this side only would
    # demote the machine owner wherever the entry is stored in another spelling.
    # Memory search normalises and fails closed on its own, so nothing needs the
    # object form. Asserting the type as well: `UUID(SCOPE) == SCOPE` is False,
    # but a future coercion elsewhere could still round-trip past a value check.
    assert agent._current_user_scope_id == SCOPE
    assert not isinstance(agent._current_user_scope_id, UUID)
    assert agent._current_username == "admin-test"

    # The sink is the bridge's dispatcher.
    assert agent.sink == bridge.on_sink_event
    assert bridge.agent is agent
    bridge.shutdown()


def test_boot_resumes_an_existing_session_without_creating_one(boot_env):
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    existing = BootSession("old-sess-7")
    # Sessions must exist before the manager is constructed inside boot_bridge:
    # seed via a subclass-level hook on the stub.
    orig_init = StubMgr.__init__

    def _seeded(self):
        orig_init(self)
        self.known["old-sess-7"] = existing

    StubMgr.__init__ = _seeded
    try:
        bridge = boot_bridge(_events(), "vaf", "old-sess-7", False)
    finally:
        StubMgr.__init__ = orig_init

    mgr = StubMgr.instances[-1]
    assert bridge.session is existing
    assert mgr.saved == []                       # no initial save on resume
    assert ("set_sid", "old-sess-7") in boot_env.ipc
    assert ("load_ctx", "old-sess-7") in boot_env.agent.calls
    bridge.shutdown()


def test_boot_falls_back_to_a_new_session_when_the_id_is_unknown(boot_env):
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    bridge = boot_bridge(_events(), "vaf", "no-such-session", False)
    assert bridge.session.id == "new-sess-1"
    assert ("set_sid", "new-sess-1") in boot_env.ipc
    bridge.shutdown()


def test_boot_preloads_speech_only_when_enabled(boot_env, monkeypatch):
    """The classic boot's third leg, ported: Piper + mic check run in the
    plain-terminal phase - and ONLY when their switches are on, because the
    first SpeechManager construction can open the microphone. The langid
    warmup is unconditional (cheap fake here; real first call ~1.6s, and the
    lazy alternative surfaces mid-chat)."""
    import sys as _sys

    from vaf.cli.tui_app.agent_bridge import boot_bridge

    touched = []
    manager = SimpleNamespace(
        _check_piper=lambda: touched.append("piper"),
        _ensure_voice_model=lambda lang: touched.append(("voice", lang)),
        # ensure_stt_capture, not a bare stt_mic read: the docker engine skips
        # mic init in the constructor, so boot must BUILD the capture stack to
        # judge it - a bare read called every docker-stack machine mic-less.
        ensure_stt_capture=lambda: touched.append("stt") or True,
    )
    monkeypatch.setitem(_sys.modules, "vaf.core.speech",
                        SimpleNamespace(get_speech_manager=lambda: manager))

    # Both switches off: the audio stack is never touched.
    bridge = boot_bridge(_events(), "vaf", None, False)
    assert touched == []
    assert boot_env.langid, "the langid warmup fell out of boot"
    bridge.shutdown()

    # Switches on: both preloads run, with the configured language.
    boot_env.agent.config = {"speech_tts_enabled": True,
                             "speech_stt_enabled": True,
                             "speech_language": "de-DE"}
    bridge = boot_bridge(_events(), "vaf", None, False)
    assert "piper" in touched and ("voice", "de") in touched
    assert "stt" in touched, "boot judged the mic without building the capture stack"
    bridge.shutdown()


def test_boot_starts_the_service_stack_in_the_background(boot_env):
    """The tray starts the stack and STOPS it on quit - a terminal-only start
    used to run against a dead memory DB. Boot spawns the same primitive."""
    import time as _time

    from vaf.cli.tui_app.agent_bridge import boot_bridge

    boot_bridge(_events(), "vaf", None, False)
    deadline = _time.monotonic() + 3.0
    while _time.monotonic() < deadline and not boot_env.stack:
        _time.sleep(0.02)
    assert boot_env.stack == ["ensure"], (
        "boot never started the service stack (or started it twice)")


def test_boot_skips_the_stack_without_a_compose_file(boot_env, monkeypatch):
    """A pip install ships no compose file - the honest branch is silence."""
    import time as _time

    import vaf.core.service_stack as stack_mod
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    monkeypatch.setattr(stack_mod, "find_stack_root", lambda: None)
    boot_bridge(_events(), "vaf", None, False)
    _time.sleep(0.2)
    assert boot_env.stack == []


def test_boot_claims_legacy_unscoped_sessions_for_the_owner(boot_env):
    """Pre-scoping sessions have no owner and the list's legacy rule shows
    them to every user - boot stamps them with the machine owner's scope."""
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    boot_bridge(_events(), "vaf", None, False)
    assert StubMgr.instances[0].claimed_scope == SCOPE
