# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf profile`, `vaf server provision --profile` and the installers.

The installers used to keep the service list a second time (`up -d postgres redis stt
gotenberg`, `up -d tts vaf-browser`); they now ask VAF, so the profile decides there too.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from vaf.cli.cmd import profile as profile_mod
from vaf.core import service_stack as ss

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(monkeypatch):
    from vaf.core.config import Config
    values = {}
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: values[key] if key in values else real(key, default)))
    monkeypatch.setattr(Config, "set", staticmethod(lambda key, value: values.__setitem__(key, value)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))
    return values


def _app():
    app = typer.Typer()
    app.command()(profile_mod.profile_cmd)
    return app


@pytest.mark.parametrize("profile, core, optional", [
    ("local", ["postgres", "redis", "stt", "gotenberg"], ["tts", "vaf-browser"]),
    ("api", ["postgres", "redis", "gotenberg"], ["vaf-browser"]),
])
def test_the_installers_get_the_service_list_from_vaf(config, profile, core, optional):
    config["inference_profile"] = profile
    runner = CliRunner()
    assert runner.invoke(_app(), ["--services", "core"]).output.split() == core
    assert runner.invoke(_app(), ["--services", "optional"]).output.split() == optional


def test_switching_writes_the_profile_and_retires_or_starts_the_speech_containers(config,
                                                                                monkeypatch):
    """MUTATION: drop the retire call, or start nothing on the way back - red."""
    calls = []
    monkeypatch.setattr(ss, "retire_local_inference_services",
                        lambda prune=False, log=None: calls.append(("retire", prune))
                        or {"stopped": True, "removed": [], "errors": []})
    monkeypatch.setattr(ss, "ensure_service_stack", lambda log=None: calls.append("up") or True)
    runner = CliRunner()
    assert runner.invoke(_app(), ["api"]).exit_code == 0
    assert config["inference_profile"] == "api" and calls == [("retire", False)]
    assert runner.invoke(_app(), ["api", "--prune"]).exit_code == 0
    assert calls[-1] == ("retire", True)
    assert runner.invoke(_app(), ["local"]).exit_code == 0
    assert config["inference_profile"] == "local" and calls[-1] == "up"
    calls.clear()
    assert runner.invoke(_app(), ["api", "--no-stack"]).exit_code == 0
    assert calls == [], "the installers write the setting before any container exists"


@pytest.mark.parametrize("args", [["bogus"], ["local", "--prune"], ["--services", "all"]])
def test_a_wrong_profile_or_combination_changes_nothing(config, args):
    """MUTATION: announce the container start before the request is checked - red on
    `local --prune`, which used to say it was starting and then refuse."""
    out = CliRunner().invoke(_app(), args)
    assert out.exit_code == 2 and "inference_profile" not in config
    assert "Starting" not in out.output


def test_a_stop_that_failed_is_an_incomplete_switch_not_a_success(config, monkeypatch):
    """The profile stays written (the choke points read it at once), but the exit code says
    the containers did not stop: the installers warn on it. MUTATION: drop the exit - red."""
    monkeypatch.setattr(ss, "retire_local_inference_services",
                        lambda prune=False, log=None: {
                            "stopped": False, "removed": [],
                            "errors": ["Could not stop stt, tts: Cannot connect to the Docker daemon"]})
    out = CliRunner().invoke(_app(), ["api"])
    assert out.exit_code == 1 and config["inference_profile"] == "api"
    assert "Cannot connect to the Docker daemon" in out.output and "not complete" in out.output
    # The retry repeats the request: without --prune it would leave the images behind.
    # MUTATION: drop the --prune from the retry - red.
    out = CliRunner().invoke(_app(), ["api", "--prune"])
    assert out.exit_code == 1 and "`vaf profile api --prune`" in out.output


def test_retiring_names_only_the_speech_services_and_their_own_volumes(monkeypatch, tmp_path):
    """The resolved compose config carries every service; the database volume must never
    be among the ones removed. MUTATION: take the volumes of every service - red."""
    (tmp_path / ss.COMPOSE_FILENAME).write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(ss, "find_stack_root", lambda: tmp_path)
    monkeypatch.setattr(ss, "resolve_docker_exe", lambda: "docker")
    resolved = {
        "services": {
            "postgres": {"image": "pgvector/pgvector:pg16",
                         "volumes": [{"type": "volume", "source": "vaf_pgdata"}]},
            "stt": {"image": "onerahmet/openai-whisper-asr-webservice:latest",
                    "volumes": [{"type": "volume", "source": "vaf_stt_models"}]},
            "tts": {"image": "vaf-tts-multilang:latest",
                    "volumes": [{"type": "volume", "source": "vaf_tts_models"},
                                {"type": "bind", "source": "/somewhere"}]},
        },
        "volumes": {"vaf_pgdata": {"name": "vaf_memory_pgdata"},
                    "vaf_stt_models": {"name": "vaf_stt_models"},
                    "vaf_tts_models": {"name": "vaf_tts_models"}},
    }
    seen = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        out = json.dumps(resolved) if "config" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    result = ss.retire_local_inference_services(prune=True)
    flat = " ".join(" ".join(c) for c in seen)
    assert "postgres" not in flat and "vaf_memory_pgdata" not in flat and "pgvector" not in flat
    assert set(result["removed"]) == {"onerahmet/openai-whisper-asr-webservice:latest",
                                      "vaf-tts-multilang:latest", "vaf_stt_models",
                                      "vaf_tts_models"}
    stop = next(c for c in seen if "stop" in c)
    assert stop[-2:] == ["stt", "tts"]


def test_a_docker_cli_without_the_compose_plugin_falls_back_like_the_start(monkeypatch,
                                                                           tmp_path):
    """Homebrew docker + Colima has no compose plugin but a standalone docker-compose; the
    stack start falls back to it, the retire used to fail there for good (and `vaf profile
    api` would ask to run it again forever). MUTATION: drop the fallback - red."""
    (tmp_path / ss.COMPOSE_FILENAME).write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(ss, "find_stack_root", lambda: tmp_path)
    monkeypatch.setattr(ss, "resolve_docker_exe", lambda: "docker")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        if cmd[:2] == ["docker", "compose"]:
            return subprocess.CompletedProcess(cmd, 125, "", "unknown shorthand flag: 'f' in -f\n")
        out = json.dumps({"services": {}, "volumes": {}}) if "config" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    result = ss.retire_local_inference_services(prune=True)
    assert result["stopped"] is True and result["errors"] == []
    assert [c[0] for c in seen if "stop" in c or "config" in c or "rm" in c] == \
        ["docker", "docker-compose", "docker-compose", "docker-compose"]


def test_a_compose_config_that_fails_is_reported_not_taken_as_nothing_to_remove(
        monkeypatch, tmp_path):
    """A failed `compose config` prints nothing on stdout, which parsed as an empty config:
    the prune removed nothing and reported no error. MUTATION: drop the returncode check -
    red."""
    (tmp_path / ss.COMPOSE_FILENAME).write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(ss, "find_stack_root", lambda: tmp_path)
    monkeypatch.setattr(ss, "resolve_docker_exe", lambda: "docker")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        if "config" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "invalid interpolation format\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    result = ss.retire_local_inference_services(prune=True)
    assert result["stopped"] is True and result["removed"] == []
    assert result["errors"] == ["The compose file could not be read: invalid interpolation format"]
    assert not any("rm" in c for c in seen), "nothing is removed from a config that was not read"


@pytest.mark.parametrize("installer", ["install.sh", "install.ps1"])
def test_no_installer_keeps_its_own_list_of_services(installer):
    """A list written into an installer is the copy the next service forgets. MUTATION:
    put `up -d postgres redis stt gotenberg` back - red."""
    names = {s.service_key for s in ss.SERVICES}
    offenders = []
    for n, line in enumerate((ROOT / installer).read_text(encoding="utf-8").splitlines(), 1):
        if "up -d" not in line or line.strip().startswith("#"):
            continue
        words = set(re.findall(r"[A-Za-z][\w-]*", line))
        if words & names:
            offenders.append(f"{installer}:{n}: {line.strip()}")
    assert offenders == []


@pytest.mark.parametrize("installer, env_args", [
    ("install.sh", '"${COMPOSE_ENV_ARGS[@]}"'),
    ("install.ps1", "@composeEnvArgs"),
])
def test_every_installer_compose_up_passes_vafs_env_file(installer, env_args):
    """VAF keeps the Redis password and the browser's stream credential in
    ~/.vaf/compose.env, which compose reads only with --env-file. An installer `up`
    without it recreated Redis with no password on every upgrade. MUTATION: drop the
    env args from one call - red."""
    offenders = []
    for n, line in enumerate((ROOT / installer).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip().lstrip("|& ").strip()
        if "up -d" not in stripped or stripped.startswith("#") or "compose" not in stripped:
            continue
        if re.match(r"(print_\w+|echo|Write-\w+)\b", stripped):
            continue
        if env_args not in stripped:
            offenders.append(f"{installer}:{n}: {line.strip()}")
    assert offenders == []


def test_provision_refuses_a_profile_it_does_not_know(config, monkeypatch):
    from vaf.cli.cmd import server
    monkeypatch.setattr(server.platform, "system", lambda: "Linux")
    out = CliRunner().invoke(server.app, ["provision", "--profile", "cloud", "--no-firewall"])
    assert out.exit_code == 2 and "server_mode" not in config


def test_a_model_is_not_downloaded_by_hand_under_the_api_profile(config, monkeypatch):
    """MUTATION: drop the guard in download_model_flow - red."""
    from vaf.cli.cmd import settings
    config["inference_profile"] = "api"
    monkeypatch.setattr(settings, "HfApi", lambda: pytest.fail("reached the model hub"))
    settings.download_model_flow("unsloth/Qwen3.5-4B-GGUF", "model.gguf")


@pytest.mark.parametrize("installer, write, fail, retire", [
    ("install.sh", 'profile "$INSTALL_PROFILE" --no-stack', "exit 1",
     'vaf.main profile api \\'),
    ("install.ps1", "profile $InferenceProfile --no-stack", "exit 1",
     "& python -m vaf.main profile api"),
])
def test_the_installers_stop_on_an_unwritten_profile_and_retire_the_speech_containers(
        installer, write, fail, retire):
    """A profile that could not be written left the services to start by the old one; and
    a switch to api with --no-stack left the speech containers of a local install running.
    MUTATION: warn and go on, or drop the retire call before the services start - red."""
    text = (ROOT / installer).read_text(encoding="utf-8")
    at = text.index(write)
    assert fail in text[at:at + 400], "an unwritten profile must stop the installer"
    assert retire in text, "switching to api must stop the speech containers through VAF"
    assert text.index(retire) < text.index("--services core"), "before the services start"


def test_provision_switches_through_the_one_implementation(config, monkeypatch):
    """On a running installation the speech containers follow the profile, and a switch that
    fails does not end the provisioning: the database passwords, the certificates and the
    firewall come after it. MUTATION: set the key directly again, or let the failure end
    the command - red."""
    from vaf.cli.cmd import server
    from vaf.core import db_credentials, inference_profile
    monkeypatch.setattr(server.platform, "system", lambda: "Linux")
    calls, reached = [], []

    def _apply(profile, prune=False, stack=True):
        calls.append(profile)
        raise RuntimeError("the lock file is not writable")

    def _next_step():
        # The step after the switch; stopped here, so no test ever touches a database.
        reached.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(inference_profile, "apply", _apply)
    monkeypatch.setattr(db_credentials, "shipped_password_in_use", _next_step)
    out = CliRunner().invoke(server.app, ["provision", "--profile", "api", "--no-firewall"])
    assert calls == ["api"] and reached == [True]
    said = " ".join(out.output.split())         # the console wraps long lines
    assert "could not be switched" in said and "the lock file is not writable" in said


@pytest.mark.parametrize("root, errors, exit_code", [
    (Path("/srv/vaf"), 1, 1),
    (None, 0, 0),
])
def test_a_stack_that_did_not_start_is_an_incomplete_switch_where_there_is_one(
        config, monkeypatch, root, errors, exit_code):
    """Back to local, a start that did not come up leaves the speech containers down until
    a repair: an error, and `vaf profile` exits 1. A pip install has no compose file and
    nothing to start: no error. MUTATION: drop the error, or the stack-root check - red."""
    monkeypatch.setattr(ss, "ensure_service_stack", lambda log=None: False)
    monkeypatch.setattr(ss, "find_stack_root", lambda: root)
    out = CliRunner().invoke(_app(), ["local"])
    assert out.exit_code == exit_code and config["inference_profile"] == "local"
    from vaf.core import inference_profile
    assert len(inference_profile.apply("local")["errors"]) == errors


def test_no_compose_file_means_nothing_to_retire_not_a_failure(config, monkeypatch):
    """A pip install has no stack and no speech containers: switching it to api is complete.
    It used to report a missing compose file, and `vaf profile api` asked to be run again
    forever. MUTATION: report the missing file as an error again - red."""
    monkeypatch.setattr(ss, "find_stack_root", lambda: None)
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: pytest.fail("ran docker"))
    assert ss.retire_local_inference_services(prune=True) == {
        "stopped": False, "removed": [], "errors": []}
    out = CliRunner().invoke(_app(), ["api"])
    assert out.exit_code == 0 and "stopped" not in out.output


def test_two_switches_never_interleave(config, monkeypatch, tmp_path):
    """The web switch, `vaf profile` and an installer can switch at the same time; two
    interleaved switches could leave the key on one profile and the containers on the other.
    The second waits and then runs whole. MUTATION: drop the lock in apply - red."""
    import threading
    import time as _time

    from vaf.core import inference_profile
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    events = []
    first_holds_the_lock = threading.Event()

    def slow_retire(prune=False, log=None):
        events.append("retire-start")
        first_holds_the_lock.set()
        _time.sleep(0.3)
        events.append("retire-end")
        return {"stopped": True, "removed": [], "errors": []}

    monkeypatch.setattr(ss, "retire_local_inference_services", slow_retire)
    monkeypatch.setattr(ss, "ensure_service_stack",
                        lambda log=None: events.append("start") or True)
    first = threading.Thread(target=inference_profile.apply, args=("api",))
    first.start()
    assert first_holds_the_lock.wait(5), "the first switch never reached its container work"
    second = threading.Thread(target=inference_profile.apply, args=("local",))
    second.start()
    first.join(5)
    second.join(5)
    assert events == ["retire-start", "retire-end", "start"]
    assert config["inference_profile"] == "local"
