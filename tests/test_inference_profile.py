# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The api inference profile: an installation that runs no model of its own.

What it has to hold, measured where it is decided (vaf/core/inference_profile.py): the one
download entry, the one server start (also past the voice lane's provider bypass), the
one local backend entry of an agent, the one list of services the stack, the status and
the repair read, and the speech clients' local fallback. Every test here fails when its
guard is taken out.
"""
import subprocess
import types
from pathlib import Path

import pytest

from vaf.core import inference_profile as ip
from vaf.core import service_health as sh
from vaf.core import service_stack as ss


@pytest.fixture(autouse=True)
def _no_real_server_stop(monkeypatch):
    """apply() stops the llama server its pid file names: never a real one from here."""
    from vaf.core import inference_profile
    monkeypatch.setattr(inference_profile, "_stop_local_server", lambda: "")


@pytest.fixture
def config(monkeypatch):
    """Config values for this test only, the rest from the real defaults."""
    from vaf.core.config import Config
    values = {}
    real = Config.get

    def fake(key, default=None):
        return values[key] if key in values else real(key, default)

    monkeypatch.setattr(Config, "get", staticmethod(fake))
    return values


@pytest.mark.parametrize("value, expected", [
    ("api", "api"), (" API ", "api"), ("local", "local"), ("", "local"),
    (None, "local"), ("lokal", "local"), ("cloud", "local"),
])
def test_anything_but_the_literal_api_keeps_the_local_models(config, value, expected):
    """A mistyped value must not take the models away from an installation that runs on
    them."""
    config["inference_profile"] = value
    assert ip.current() == expected


def test_no_model_is_downloaded_under_the_api_profile(config, monkeypatch, tmp_path):
    """The one download entry every lane passes. MUTATION: drop its guard - red."""
    import huggingface_hub
    pulled = []
    monkeypatch.setattr(huggingface_hub, "hf_hub_download",
                        lambda *a, **k: pulled.append(k) or pytest.fail("downloaded"))
    config["inference_profile"] = "api"
    from vaf.core.backend import ensure_model_available
    assert ensure_model_available("auto", tmp_path) == ""
    assert pulled == [] and not any(tmp_path.iterdir())


@pytest.mark.parametrize("provider, skip_gate, expected", [
    ("local", False, False),     # a local chat provider on an api installation
    ("openai", True, False),     # the voice lane asking past the provider gate
    ("openai", False, True),     # an API provider: nothing to start, as before
])
def test_the_local_server_never_starts_under_the_api_profile(config, provider, skip_gate,
                                                              expected):
    """MUTATION: drop the profile check in start_server - the voice lane starts the server
    on an api installation - red."""
    from vaf.core.backend import ServerManager
    config.update({"inference_profile": "api", "provider": provider,
                   "auto_start_local_server": True})
    calls = []
    fake = types.SimpleNamespace(ensure_server_exists=lambda: calls.append(1) or False)
    assert ServerManager._start_server_locked(fake, "model.gguf",
                                              skip_provider_gate=skip_gate) is expected
    assert calls == []


def test_an_agent_loads_no_local_model_under_the_api_profile(config):
    """MUTATION: drop the guard in load_model - red."""
    from vaf.core.agent import Agent
    config["inference_profile"] = "api"
    calls = []
    fake = types.SimpleNamespace(provider="local",
                                 ensure_model_exists=lambda: calls.append(1))
    Agent.load_model(fake)
    assert calls == []


def test_the_speech_containers_are_the_only_local_inference_services():
    """The registry says which services run a model; pinned, because a new one that does
    must be left out of the api profile on purpose, not by accident."""
    assert {s.service_key for s in ss.SERVICES if s.local_inference} == {"stt", "tts"}


def test_the_api_profile_needs_no_speech_container():
    """MUTATION: return every service from needed_services - red."""
    api = {"inference_profile": "api"}
    assert "stt" not in ss.needed_core_services(api)
    assert "tts" not in ss.needed_optional_services(api)
    assert {"postgres", "redis", "gotenberg"} <= set(ss.needed_core_services(api))
    assert "vaf-browser" in ss.needed_optional_services(api), "the browser is an agent tool"
    assert ss.needed_services({"inference_profile": "local"}) == ss.SERVICES


class _Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, ""


def test_the_stack_start_leaves_the_speech_containers_out_under_the_api_profile(config,
                                                                              monkeypatch):
    """MUTATION: start CORE_SERVICES / OPTIONAL_SERVICES again - red."""
    from vaf.core import environment_image
    monkeypatch.setattr(ss, "ensure_service_stack", ss._real_ensure_service_stack)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(ss, "_ensure_macos_brew_path", lambda: None)
    monkeypatch.setattr(ss, "is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(ss, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(ss, "_maybe_rebuild_stale_browser_image", lambda *a, **k: None)
    monkeypatch.setattr(environment_image, "start_background_build", lambda: None)
    # The start's side work touches the keyring, the home folder and the checkout's .env;
    # none of it is under test here.
    monkeypatch.setattr(ss, "_write_compose_env_file", lambda *a, **k: None)
    monkeypatch.setattr(ss, "_retire_repo_env_file", lambda *a, **k: None)
    monkeypatch.setattr(ss, "_warn_about_default_db_password", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_remove_legacy_sandbox", lambda *a, **k: None)
    ups = []
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda cmd, **kw: (ups.append(list(cmd)) if "up" in cmd else None)
                        or _Result())
    config["inference_profile"] = "api"
    assert ss.ensure_service_stack() is True
    started = {arg for cmd in ups for arg in cmd}
    assert {"postgres", "redis", "gotenberg", "vaf-browser"} <= started
    assert not {"stt", "tts"} & started


@pytest.fixture
def _no_real_environments(monkeypatch):
    from vaf.core import environments
    fake = types.SimpleNamespace(
        summary=lambda: {"environments": 0, "running": 0, "image_built": True})
    monkeypatch.setattr(environments, "get_environment_manager", lambda: fake)
    monkeypatch.setattr(sh, "start_in_progress", lambda: False)


def test_a_container_the_profile_leaves_out_is_off_not_broken(config, _no_real_environments):
    """A missing stt was an error, `vaf repair --check` exited 1 and the dialog turned red.
    MUTATION: derive its row as a required service again - red."""
    config["inference_profile"] = "api"
    status = sh.collect_service_status(
        daemon_probe=lambda: {"ok": True, "reason": "ok", "detail": ""},
        inspect_probe=lambda names: [],
        port_reader=lambda spec: spec.default_port or None,
        service_probe=lambda spec, port: None,
    )
    rows = {s["service_key"]: s for s in status["services"]}
    assert rows["stt"]["state"] == "off" and rows["stt"]["required"] is False
    assert rows["tts"]["state"] == "off"
    assert rows["postgres"]["state"] == "error", "a needed service missing is still an error"


def test_repair_does_not_bring_back_what_the_profile_leaves_out(monkeypatch):
    """MUTATION: count an off service among the ones to start - red."""
    monkeypatch.setattr(sh, "diagnose_docker_daemon",
                        lambda: {"ok": True, "reason": "ok", "detail": ""})
    monkeypatch.setattr(sh, "find_stack_root", lambda: "/repo")
    monkeypatch.setattr(sh, "_run_docker",
                        lambda args, timeout=None: subprocess.CompletedProcess(args, 0, "", ""))
    ups = []
    monkeypatch.setattr(sh, "ensure_service_stack", lambda log=None: ups.append(1) or True)
    off = {"name": "vaf-stt", "service_key": "stt", "required": False, "needed": False,
           "exists": False, "running": False, "health": "none", "host_ports": [],
           "configured_port": None, "port_mismatch": False, "probe": None,
           "probe_ok": None, "state": "off", "reason": "Not used."}
    status = {"docker": {"available": True, "reason": "ok", "detail": ""},
              "stack_root": "/repo", "services": [off], "checked_at": "now"}
    result = sh.repair_service_stack(status_probe=lambda: status)
    assert ups == []
    assert result["ok"] is True and result["degraded"] == []


def test_speech_to_text_never_falls_back_to_a_container_under_the_api_profile(config,
                                                                            monkeypatch):
    """The cloud lane's answer, or nothing - not a request to a container that does not
    run. MUTATION: drop the guard in transcribe - red."""
    from vaf.core import speech_api, speech_client
    config.update({"inference_profile": "api", "speech_stt_engine": "local"})
    monkeypatch.setattr(speech_api, "select_stt_backend", lambda: (None, None))
    posted = []
    monkeypatch.setattr(speech_client, "_post_stt", lambda *a, **k: posted.append(a))
    assert speech_client.transcribe(b"RIFF0000WAVE" + b"\0" * 64) == (None, None)
    assert posted == []
    assert speech_api.resolve_stt_engine() == "docker", "never faster-whisper here"


def test_text_to_speech_never_falls_back_to_a_container_under_the_api_profile(config,
                                                                            monkeypatch):
    """MUTATION: drop the guard in speech_client.synthesize or in synthesize_audio - red."""
    from vaf.core import speech, speech_api, speech_client
    config.update({"inference_profile": "api", "speech_tts_engine": "docker"})
    monkeypatch.setattr(speech_api, "select_tts_backend", lambda: (None, None))
    monkeypatch.setattr(speech_api, "synthesize", lambda *a, **k: None)
    reached = []
    monkeypatch.setattr(speech_client, "synthesize_docker",
                        lambda *a, **k: reached.append(a) or b"RIFF")
    assert speech_client.synthesize("Hallo", "de") is None
    manager = speech.SpeechManager.__new__(speech.SpeechManager)
    manager._clean_markdown = lambda text: text
    assert manager.synthesize_audio("Hallo", "de", force_engine="docker") is None
    assert reached == []


def test_the_messengers_speak_through_a_chatterbox_server_under_the_api_profile(config,
                                                                              monkeypatch):
    """A TTS server the admin runs elsewhere stays usable without local models; the voice
    replies in the messengers (speech_client.synthesize) refused it with the container.
    MUTATION: refuse every engine again, or send chatterbox to the container's URL - red."""
    from vaf.core import speech_api, speech_client
    config.update({"inference_profile": "api", "speech_tts_engine": " Chatterbox ",
                   "speech_tts_chatterbox_url": "http://tts.example:4123/",
                   "speech_tts_docker_url": "http://localhost:5002"})
    monkeypatch.setattr(speech_api, "select_tts_backend", lambda: (None, None))
    reached = []
    monkeypatch.setattr(speech_client, "synthesize_docker",
                        lambda text, lang, want_format="wav", base_url=None:
                        reached.append(base_url) or b"OggS")
    assert speech_client.synthesize("Hallo", "de", want_format="ogg",
                                    docker_url="http://localhost:5002") == b"OggS"
    assert reached == ["http://tts.example:4123"]


def test_a_local_lane_on_an_api_installation_is_named_not_refused(config):
    """Nothing is refused when it is written; `vaf doctor` names it. MUTATION: drop the
    finding - red."""
    from vaf.core.security_misconfig import collect_security_findings
    cfg = {"inference_profile": "api", "provider": "local", "voice_agent_provider": "local",
           "failover_level": "off", "speech_stt_enabled": True, "speech_stt_provider": ""}
    sentences = ip.conflicts(cfg)
    assert any("chat provider" in s for s in sentences)
    assert any("voice_agent_provider" in s for s in sentences)
    assert any("Speech-to-text" in s for s in sentences)
    assert not any("failover" in s for s in sentences)
    codes = [f["code"] for f in collect_security_findings(cfg)]
    assert codes.count("inference_profile_conflict") == len(sentences)
    assert ip.conflicts({**cfg, "inference_profile": "local"}) == []


def test_apply_is_the_one_switch_and_refuses_what_it_does_not_know(config, monkeypatch):
    """MUTATION: let apply prune under local, or skip the container step - red."""
    from vaf.core.config import Config
    written = {}
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v: written.__setitem__(k, v)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: {}))
    calls = []
    monkeypatch.setattr(ss, "retire_local_inference_services",
                        lambda prune=False, log=None: calls.append(("retire", prune))
                        or {"stopped": True, "removed": ["img"], "errors": []})
    monkeypatch.setattr(ss, "ensure_service_stack", lambda log=None: calls.append("up") or True)
    out = ip.apply("api", prune=True)
    assert written == {"inference_profile": "api"} and calls == [("retire", True)]
    assert out["stopped"] is True and out["removed"] == ["img"]
    assert ip.apply("local")["started"] is True and calls[-1] == "up"
    calls.clear()
    ip.apply("api", stack=False)
    assert calls == []
    for bad in (("cloud", False), ("local", True)):
        with pytest.raises(ValueError):
            ip.apply(bad[0], prune=bad[1])


@pytest.mark.parametrize("state, profile, expected", [
    ({"llm": None, "use_server": True, "api_backend": None, "provider": "local"}, "api",
     "without local models"),                                  # loaded before the switch
    ({"llm": None, "use_server": True, "api_backend": None, "provider": "local"}, "local", ""),
    ({"llm": None, "use_server": False, "api_backend": None, "provider": "openai",
      "_api_init_error": "AuthenticationError: bad key"}, "api", "openai is not set up"),
    ({"llm": None, "use_server": False, "api_backend": None, "provider": "local"}, "api",
     "without local models"),
    ({"llm": None, "use_server": False, "api_backend": object(), "provider": "openai"}, "api", ""),
])
def test_a_turn_says_which_backend_is_missing(config, state, profile, expected):
    """A failed API provider was reported as a missing local model, and a local model
    loaded before a switch to api kept answering. MUTATION: drop either branch - red."""
    from vaf.core.agent import Agent
    config["inference_profile"] = profile
    reason = Agent._no_backend_reason(types.SimpleNamespace(**state))
    assert (expected in reason) if expected else reason == ""
    if expected == "openai is not set up":
        assert "bad key" in reason and "local" not in reason


def test_the_failover_chain_has_no_local_link_under_the_api_profile(config):
    """MUTATION: drop the profile check on the local link - red."""
    from vaf.core.api_backend import APIBackendManager
    manager = APIBackendManager.__new__(APIBackendManager)
    manager.provider_name, manager.caller_config = "openai", None
    manager.config = {"failover_level": "basic"}
    config["inference_profile"] = "api"
    assert [m for m, _ in manager._build_failover_chain("gpt")] == [manager]
    config["inference_profile"] = "local"
    assert len(manager._build_failover_chain("gpt")) == 2


def test_the_legacy_stt_toggle_counts_for_the_conflict_too():
    """MUTATION: read only speech_stt_enabled - red."""
    cfg = {"inference_profile": "api", "provider": "openai", "stt_enabled": True}
    assert any("Speech-to-text" in s for s in ip.conflicts(cfg))
    assert not any("failover" in s for s in ip.conflicts({**cfg, "failover_level": "balanced"}))
    assert any("basic" in s for s in ip.conflicts({**cfg, "failover_level": "basic"}))


def test_speaking_through_piper_downloads_nothing_under_the_api_profile(config, monkeypatch):
    """speak() has a Piper lane of its own, which installs the binary and fetches voices.
    MUTATION: drop the guard in front of _check_piper - red."""
    import threading

    from vaf.core import speech, speech_api
    config.update({"inference_profile": "api", "speech_tts_enabled": True,
                   "speech_tts_engine": "piper"})
    monkeypatch.setattr(speech_api, "select_tts_backend", lambda: (None, None))
    manager = speech.SpeechManager.__new__(speech.SpeechManager)
    manager._clean_markdown = lambda text: text
    manager._speech_generation = 0
    manager._tts_lock = threading.Lock()
    manager.on_speech_loading = manager.on_speech_start = manager.on_speech_end = None
    manager._check_piper = lambda: pytest.fail("Piper reached")
    manager._ensure_voice_model = lambda lang: pytest.fail("a voice download reached")

    class _Now:
        def __init__(self, target=None, daemon=None, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(speech.threading, "Thread", _Now)
    monkeypatch.setattr(speech.platform, "system", lambda: "Linux")
    manager.speak("Hallo", "de")


def test_a_refused_turn_is_stored_so_a_reload_keeps_it(tmp_path, monkeypatch):
    """A web turn on an installation without local models and without a provider is
    answered with the reason. Both the message and the reason used to live only in the
    browser: a reload lost them. MUTATION: drop the save in refuse_turn_without_backend,
    or answer the turn inline in the runner again - red."""
    import inspect

    from vaf.core import headless_runner as hr
    from vaf.core.session import SessionManager
    emitted = []
    monkeypatch.setattr(hr, "get_web_interface", lambda: types.SimpleNamespace(
        emit_agent_message=lambda role, text, session_id=None: emitted.append(text),
        emit_message_complete=lambda content=None, session_id=None: emitted.append("done")))
    mgr = SessionManager(storage_dir=str(tmp_path))
    task = types.SimpleNamespace(session_id="green123456", input_text="  hello  ",
                                 metadata={"username": "alice", "user_scope_id": "ab12cd34"})
    hr.refuse_turn_without_backend(task, ip.refusal("the local model"), mgr)
    hr.refuse_turn_without_backend(task, ip.refusal("the local model"), mgr)
    session = mgr.load("green123456")
    assert [(m.role, m.content) for m in session.messages] == [
        ("user", "hello"), ("assistant", ip.refusal("the local model"))] * 2
    # A chat stored without its owner belongs to nobody: the person who wrote could not
    # open it. MUTATION: create the session bare again - red.
    assert session.metadata.get("user_scope_id") == "ab12cd34"
    assert session.metadata.get("username") == "alice"
    assert emitted[1] == "done"
    runner = inspect.getsource(hr.run_headless_agent)
    assert "turn_refusal_reason(agent, task)" in runner and "refuse_turn_without_backend(" in runner


def test_a_switch_during_a_download_ends_it_and_downloads_no_fallback(config, monkeypatch,
                                                                     tmp_path):
    """The check at the start of a download cannot see a switch made while it runs, and a
    chat model is up to 17.9 GB. The progress hook now reads the profile, and the self-heal
    does not start a second download after it. MUTATION: drop the guard from the progress
    hook, or the profile check before the self-heal - red."""
    import huggingface_hub
    pulled = []

    def fake_download(*a, **k):
        pulled.append(k["filename"])
        config["inference_profile"] = "api"          # switched while this runs
        bar = k["tqdm_class"](total=100)
        bar.update(10)
        pytest.fail("the download went on after the switch")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    config["inference_profile"] = "local"
    from vaf.core import backend
    said = []
    monkeypatch.setattr(backend.UI, "event", lambda *a, **k: said.append(" ".join(map(str, a))))
    assert backend.ensure_model_available(
        "unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf", tmp_path) == ""
    assert len(pulled) == 1, "no fallback download after the switch"
    assert not any("falling back" in line for line in said), "no self-heal attempt either"


def test_every_model_download_lane_passes_the_profile_guard():
    """The automatic download, the web settings and the terminal settings each start their
    own hf_hub_download. MUTATION: drop the guard from one lane - red."""
    import inspect

    from vaf.cli.cmd import settings
    from vaf.core import backend, web_server
    for source in (inspect.getsource(backend.ensure_model_available),
                   inspect.getsource(settings.download_model_flow)):
        assert "make_state_tqdm(inference_profile.download_guard())" in source
    web = inspect.getsource(web_server)
    assert "_profile_guard = _inference_profile.download_guard()" in web
    assert web.count("_profile_guard()") == 1


def test_the_download_guard_reads_the_profile_at_most_every_interval(config, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ip.time, "monotonic", lambda: clock[0])
    config["inference_profile"] = "local"
    guard = ip.download_guard(interval=2.0)
    guard()
    config["inference_profile"] = "api"
    guard()                                   # within the interval: not read again
    clock[0] += 2.5
    with pytest.raises(InterruptedError, match="without local models"):
        guard()


def test_a_caller_waiting_on_the_download_lock_rechecks_the_profile(config, monkeypatch,
                                                                    tmp_path):
    """A second caller waits on the download lock while the first one downloads; when the
    profile was switched meanwhile it must not start its own download after the wait.
    MUTATION: drop the recheck after the lock - red."""
    import filelock
    import huggingface_hub

    class _Lock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            config["inference_profile"] = "api"      # switched while this caller waited
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(filelock, "FileLock", _Lock)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download",
                        lambda *a, **k: pytest.fail("downloaded after the wait"))
    config["inference_profile"] = "local"
    from vaf.core.backend import ensure_model_available
    assert ensure_model_available("unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf", tmp_path) == ""


@pytest.mark.parametrize("profile, provider, backend, source, expected", [
    ("api", "local", None, None, "without local models"),
    ("local", "openai", None, None, "The API provider openai is not set up (no key)"),
    ("api", "openai", None, None, "The API provider openai is not set up (no key)"),
    ("local", "local", None, None, ""),          # loaded below, on first use
    ("api", "openai", object(), None, ""),       # a backend: the turn runs
    ("api", "local", None, "telegram", "without local models"),   # closed without a reply
])
def test_a_turn_without_any_backend_is_refused_before_chat_step(config, profile, provider,
                                                                backend, source, expected):
    """chat_step only logs a missing backend, and the chat then read "[Error] No response
    was produced", for an API provider whose backend could not be built as well. MUTATION:
    keep refusing only the local provider - red."""
    from vaf.core import headless_runner as hr
    from vaf.core.agent import Agent
    config["inference_profile"] = profile
    agent = types.SimpleNamespace(provider=provider, api_backend=backend, llm=None,
                                  use_server=False, _api_init_error="no key")
    agent._no_backend_reason = lambda: Agent._no_backend_reason(agent)
    reason = hr.turn_refusal_reason(agent, types.SimpleNamespace(source=source))
    assert (expected in reason) if expected else reason == ""


@pytest.mark.parametrize("profile, says, never", [
    ("api", "no local engine to fall back to", "container"),
    ("local", "vaf repair", "start_vaf.sh"),
])
def test_the_terminal_mic_names_the_right_lane_when_nothing_was_transcribed(config, profile,
                                                                          says, never):
    """Under api only the provider was asked; the message used to send people to the
    Whisper container and start_vaf.sh. MUTATION: one message for both profiles - red."""
    from vaf.core.speech import stt_failure_message
    config["inference_profile"] = profile
    assert says in stt_failure_message() and never not in stt_failure_message()


def test_a_settings_save_never_carries_the_profile(monkeypatch):
    """The profile changes through apply, which also stops or starts the containers; a page
    loaded before a switch sent the old value back with every save. MUTATION: drop the
    filter from either save lane - red."""
    import asyncio
    import inspect

    from vaf.api import config_routes
    from vaf.core import web_server
    from vaf.core.config import Config
    state = {"inference_profile": "api", "server_mode": False}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(state)))
    monkeypatch.setattr(Config, "save", classmethod(
        lambda cls, cfg: (state.clear(), state.update(cfg))[0]))
    user = {"role": "admin", "user_scope_id": "ab12cd34-0000-0000-0000-000000000000",
            "username": "Alice"}
    asyncio.run(config_routes.patch_config(
        {"inference_profile": "local", "tts_auto_speak": True},
        types.SimpleNamespace(state=types.SimpleNamespace(user=user)), user))
    assert state["inference_profile"] == "api" and state["tts_auto_speak"] is True
    assert ip.without_profile_key({"inference_profile": "local", "a": 1}) == {"a": 1}
    assert "_inference_profile.without_profile_key(new_config)" in inspect.getsource(web_server)


def test_no_model_resolved_leaves_the_configured_model_alone(monkeypatch):
    """Under api ensure_model_available returns "": the agent kept an empty model path and
    file name, which every later message about the model then named. MUTATION: assign the
    empty path again - red."""
    from vaf.core import backend
    from vaf.core.agent import Agent
    monkeypatch.setattr(backend, "ensure_model_available", lambda name, models_dir: "")
    agent = types.SimpleNamespace(config={"model": "auto"}, models_dir="/models",
                                  model_path="/models/qwen.gguf", filename="qwen.gguf")
    Agent.ensure_model_exists(agent)
    assert (agent.model_path, agent.filename) == ("/models/qwen.gguf", "qwen.gguf")


def test_a_messenger_turn_without_a_backend_gets_no_reply(tmp_path, monkeypatch):
    """Running such a turn sent the contact "[Error] No response was produced ... try
    closing the Document Editor" (the outbound sanitizer lets it pass), and the reason is the
    admin's setup, not theirs to read. The turn ends without a reply and is logged; nothing
    reaches the web or the session. MUTATION: drop the messenger branch - red."""
    from vaf.core import headless_runner as hr
    from vaf.core.session import SessionManager
    logged, emitted = [], []
    monkeypatch.setattr(hr, "append_domain_log_always", lambda domain, line: logged.append(line))
    monkeypatch.setattr(hr, "get_web_interface", lambda: types.SimpleNamespace(
        emit_agent_message=lambda *a, **k: emitted.append(a),
        emit_message_complete=lambda *a, **k: emitted.append(k)))
    mgr = SessionManager(storage_dir=str(tmp_path))
    task = types.SimpleNamespace(session_id="telegram_123", source="telegram",
                                 input_text="hello", metadata={})
    hr.refuse_turn_without_backend(task, ip.refusal("the local model"), mgr)
    assert emitted == []
    with pytest.raises(FileNotFoundError):
        mgr.load("telegram_123")
    assert logged and "without a reply" in logged[0]


@pytest.mark.parametrize("profile, says, never", [
    ("api", "text-to-speech provider", "Docker"),
    ("local", "Docker TTS", "provider"),
])
def test_speak_names_the_right_lane_when_no_voice_is_left(config, profile, says, never):
    """Under api the warning recommended Docker TTS, which does not run there. MUTATION:
    one message for both profiles - red."""
    from vaf.core.speech import tts_unavailable_message
    config["inference_profile"] = profile
    assert says in tts_unavailable_message() and never not in tts_unavailable_message()


class _LiveAgent:
    """A stand-in the live-agent registry can hold (it is a WeakSet)."""

    def __init__(self):
        self.llm = object()
        self._tokenizer_instance = object()


def test_a_switch_releases_the_in_process_models(config, monkeypatch):
    """The switch stops the llama server; an in-process model (the library lane, the
    installer's Python 3.12 venv on Linux among them) stayed in memory after a switch to api,
    refused but loaded, and so did the tokenizer cached for counting, a second Llama on the
    same GGUF. apply() releases both in this process. MUTATION: drop the release from apply,
    release under local too, or leave the tokenizer - red."""
    from vaf.core import agent as agent_mod
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v: config.__setitem__(k, v)))
    monkeypatch.setattr(ss, "retire_local_inference_services",
                        lambda prune=False, log=None: {"stopped": True, "removed": [], "errors": []})
    monkeypatch.setattr(ss, "ensure_service_stack", lambda log=None: True)
    a, b = _LiveAgent(), _LiveAgent()
    for each in (a, b):
        agent_mod._register_live_agent(each)
    config["inference_profile"] = "local"
    assert agent_mod.release_disallowed_local_backends() == 0 and a.llm is not None
    ip.apply("local")
    assert a.llm is not None and b.llm is not None, "the local profile keeps its models"
    assert a._tokenizer_instance is not None
    ip.apply("api")
    assert a.llm is None and b.llm is None
    assert a._tokenizer_instance is None and b._tokenizer_instance is None


def test_the_api_switch_stops_the_llama_server(config, monkeypatch):
    """Only the desktop tray's watchdog unloaded the server: under `vaf server` (no display)
    and the macOS app it stayed loaded after a switch to api. A stop that failed leaves the
    switch incomplete. MUTATION: drop the stop from apply, stop under local or --no-stack
    too, or drop its error - red."""
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v: config.__setitem__(k, v)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: {}))
    monkeypatch.setattr(ss, "retire_local_inference_services",
                        lambda prune=False, log=None: {"stopped": True, "removed": [],
                                                       "errors": ["containers"]})
    monkeypatch.setattr(ss, "ensure_service_stack", lambda log=None: True)
    stops = []
    monkeypatch.setattr(ip, "_stop_local_server", lambda: stops.append(1) or "server")
    assert ip.apply("local")["errors"] == [] and stops == []
    assert ip.apply("api")["errors"] == ["server", "containers"] and stops == [1]
    ip.apply("api", stack=False)
    assert stops == [1], "the installers write the setting before anything runs"


def _sleeper(*marker):
    """A process that looks like a llama server to the identity check when marked."""
    import sys
    import time as _time

    from vaf.core.backend import ServerManager
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *marker])
    # The command line shows up only once the child has exec'd; before that it is ours.
    deadline = _time.monotonic() + 10
    while marker and not ServerManager(skip_cleanup=True)._looks_like_llama_server(proc.pid):
        assert _time.monotonic() < deadline, "the stand-in never showed its command line"
        _time.sleep(0.05)
    return proc


@pytest.fixture
def pid_home(monkeypatch, tmp_path):
    """A home whose ~/.vaf/server.pid the test writes (HOME, and USERPROFILE for Windows)."""
    pytest.importorskip("psutil")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".vaf").mkdir()
    return tmp_path / ".vaf" / "server.pid"


def test_stop_local_server_stops_the_server_its_pid_file_names(pid_home):
    """From any process, also a terminal running `vaf profile api`. MUTATION: drop the stop -
    red."""
    from vaf.core.backend import stop_local_server
    proc = _sleeper("llama-server")
    try:
        pid_home.write_text(str(proc.pid))
        assert stop_local_server() == ""
        assert proc.wait(5) is not None
        assert not pid_home.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stop_local_server_leaves_alone_what_is_no_llama_server(pid_home):
    """A pid file left behind (a crash, a reboot) can name any process by now. MUTATION: drop
    the identity check - red."""
    import time as _time

    from vaf.core.backend import stop_local_server
    proc = _sleeper()
    try:
        pid_home.write_text(str(proc.pid))
        assert stop_local_server() == ""
        _time.sleep(0.5)
        assert proc.poll() is None, "an unrelated process was killed"
    finally:
        proc.kill()
        proc.wait()


def test_stop_local_server_says_when_the_server_is_still_running(pid_home, monkeypatch):
    """MUTATION: report every stop as done - red."""
    from vaf.core import backend
    monkeypatch.setattr(backend.ServerManager, "stop_server",
                        lambda self, force_external=False: None)
    proc = _sleeper("llama-server")
    try:
        pid_home.write_text(str(proc.pid))
        assert str(proc.pid) in backend.stop_local_server(wait=0.3)
    finally:
        proc.kill()
        proc.wait()


def test_no_tokenizer_is_loaded_under_the_api_profile(config, monkeypatch, tmp_path):
    """_get_tokenizer loaded the whole GGUF in-process just to count tokens when the provider
    still read "local" by hand, and counted with one cached before a switch. MUTATION: drop
    its profile check, or check after the cache - red."""
    import sys

    from vaf.core.agent import Agent
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    loaded = []
    fake_llama = types.ModuleType("llama_cpp")
    fake_llama.Llama = lambda **k: loaded.append(k) or object()
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama)
    agent = types.SimpleNamespace(_tokenizer_instance=None, use_server=False, api_backend=None,
                                  provider="local", model_path=str(gguf))
    config["inference_profile"] = "api"
    assert Agent._get_tokenizer(agent) is None and loaded == []
    agent._tokenizer_instance = "cached before the switch"
    assert Agent._get_tokenizer(agent) is None
    config["inference_profile"] = "local"
    assert Agent._get_tokenizer(agent) == "cached before the switch"


def _library_lane(monkeypatch, tmp_path, *, on_probe=None, on_load=None):
    """An agent at the in-process library load (llama-cpp-python on Linux below Python 3.13),
    with the hooks a switch can land in: the :8080 probe right before the load, and the load."""
    import sys

    from vaf.core import agent as agent_mod
    from vaf.core import gpu_detection, user_notifications
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    constructed = []

    class _Llama:
        def __init__(self, **kwargs):
            if on_load:
                on_load()
            constructed.append(kwargs)

    fake_llama = types.ModuleType("llama_cpp")
    fake_llama.Llama = _Llama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama)
    monkeypatch.setattr(agent_mod, "sys", types.SimpleNamespace(
        version_info=types.SimpleNamespace(major=3, minor=12), stderr=sys.stderr,
        exit=sys.exit, executable=sys.executable))
    monkeypatch.setattr(agent_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detection, "get_primary_gpu", lambda: None)
    monkeypatch.setattr(agent_mod, "append_domain_log", lambda *a, **k: None)
    monkeypatch.setattr(user_notifications, "append_notification", lambda *a, **k: None)

    def _probe(*a, **k):
        if on_probe:
            on_probe()
        raise ConnectionError("nothing on :8080")

    monkeypatch.setattr(agent_mod.requests, "get", _probe)
    agent = types.SimpleNamespace(provider="local", config={"force_server": False},
                                  model_path=str(gguf), filename="m.gguf", verbose=True,
                                  llm=None)
    return agent, constructed


def test_a_switch_before_the_library_load_stops_it(config, monkeypatch, tmp_path):
    """The check at the top of load_model ran before every wait below it; a switch made
    meanwhile found no model to release, and the load went ahead. MUTATION: drop the check
    right before the load - red."""
    from vaf.core.agent import Agent
    config["inference_profile"] = "local"
    agent, constructed = _library_lane(
        monkeypatch, tmp_path, on_probe=lambda: config.__setitem__("inference_profile", "api"))
    Agent.load_model(agent, skip_download_check=True)
    assert constructed == [] and agent.llm is None


def test_a_switch_during_the_library_load_releases_it(config, monkeypatch, tmp_path):
    """The switch released nothing while the load ran, because the model is set on the agent
    only once it is loaded. MUTATION: drop the check after the load - red."""
    from vaf.core.agent import Agent
    config["inference_profile"] = "local"
    agent, constructed = _library_lane(
        monkeypatch, tmp_path, on_load=lambda: config.__setitem__("inference_profile", "api"))
    Agent.load_model(agent, skip_download_check=True)
    assert len(constructed) == 1 and agent.llm is None
    config["inference_profile"] = "local"
    agent2, _ = _library_lane(monkeypatch, tmp_path)
    Agent.load_model(agent2, skip_download_check=True)
    assert agent2.llm is not None, "without a switch the model stays loaded"


def test_the_download_progress_class_takes_the_hubs_name_argument():
    """huggingface_hub 1.1 to 1.9 pass `name=` to every progress class, and plain tqdm
    refuses it (TqdmKeyError): the download failed before its first byte. MUTATION: forward
    `name` again - red."""
    from vaf.core.model_download_state import make_state_tqdm
    bar = make_state_tqdm()(total=10, name="huggingface_hub.http_get", disable=False, unit="B")
    bar.update(1)
    bar.close()


def test_an_api_provider_without_a_backend_still_runs_on_a_loaded_local_server(config):
    """On the Full profile a turn whose API provider has no backend falls back to the local
    server that is up, as load_model does; only under api is that server refused."""
    from vaf.core import headless_runner as hr
    from vaf.core.agent import Agent
    agent = types.SimpleNamespace(provider="openai", api_backend=None, llm=None,
                                  use_server=True, _api_init_error="no key")
    agent._no_backend_reason = lambda: Agent._no_backend_reason(agent)
    config["inference_profile"] = "local"
    assert hr.turn_refusal_reason(agent, types.SimpleNamespace(source=None)) == ""
    config["inference_profile"] = "api"
    assert "without local models" in hr.turn_refusal_reason(agent, types.SimpleNamespace(source=None))


@pytest.mark.parametrize("role", ["admin", "user"])
def test_the_settings_get_the_profile_as_the_server_reads_it(role):
    """The settings compare the key with "api": a hand-written " API " showed them the full
    profile on a lean server, and the profile section reloaded the page over the difference.
    MUTATION: send the raw value again - red."""
    from vaf.core.config import Config
    assert Config.config_for_user({"inference_profile": " API "}, None, role)[
        "inference_profile"] == "api"
    assert Config.config_for_user({"inference_profile": "lokal"}, None, role)[
        "inference_profile"] == "local"
    assert "inference_profile" not in Config.config_for_user({}, None, role)
