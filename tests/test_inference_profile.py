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
