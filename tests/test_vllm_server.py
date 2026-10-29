# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The managed vLLM server: launch arguments and environment, the fit check, the KV-cache
fallback, the install steps, the model download and the stop (vaf/core/vllm_server.py).

Nothing here installs vLLM, downloads a model or touches a GPU. The launch is a fake Popen;
the stop tests use real stand-in processes (a Python sleeper whose command line names a fake
environment), because what they prove is that the whole process group goes.

MUTATIONS, each turns the named test red:
- remove `--enable-auto-tool-choice`, add `--trust-remote-code`, drop VLLM_NO_USAGE_STATS,
  or launch with start_new_session=False -> test_start_argv_and_env;
- stop with `os.kill(pid, SIGTERM)` instead of the process-group stop ->
  test_stop_kills_group_and_removes_pid;
- drop the command-line check in `_looks_like_vllm_server`, or go back to a substring test on
  the environment's path -> test_stop_ignores_foreign_pid;
- pass inherited VLLM_ variables on, or drop a pinned switch -> test_child_env_drops_inherited_vllm_switches;
- go back to "one *.safetensors is enough" -> test_local_snapshot_needs_every_shard;
- drop the code-file exclusion -> test_no_code_file_of_the_repo_is_fetched;
- remove a pid file that names another pid, or spawn without the start lock check ->
  test_a_failed_start_keeps_another_servers_pid_file / test_a_server_started_meanwhile_is_not_spawned_twice;
- retry with auto after a bind failure -> test_a_taken_port_is_no_kv_failure;
- drop the fallback to the own child in `stop` -> test_stop_reaches_its_own_child_without_a_pid_file;
- drop the fp8 step (KV cache always 16-bit), or raise OVERHEAD_BYTES so the 4B no longer fits
  at fp8 -> test_fit_measured_case;
- drop the clamp to max_position_embeddings -> test_fit_clamps_to_max_position;
- drop the retry with auto, or the memo -> test_a_dying_fp8_start_retries_once_with_auto;
- let pip run in a shell, or keep a half environment after a failed pip ->
  test_install_steps / test_a_failed_install_leaves_nothing.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vaf.core import local_engine as le
from vaf.core import vllm_server as vs

SHAPE_4B = vs.ModelShape(weights_bytes=int(2.67e9), layers=36, kv_heads=8, head_dim=128,
                         max_position=40960)
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


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


@pytest.fixture
def home(monkeypatch, tmp_path):
    """The environment and the pid file inside tmp_path."""
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(vs, "base_dir", lambda: tmp_path)
    monkeypatch.setattr(vs, "pid_file", lambda: tmp_path / ".vaf" / "vllm.pid")
    monkeypatch.setattr(vs, "log_path", lambda: tmp_path / "logs" / "vllm_server.log")
    monkeypatch.setattr(vs, "_kv_fp8_failed", False)
    monkeypatch.setattr(vs, "_child", None)
    return tmp_path


class _Proc:
    _next = 900001

    def __init__(self, dies=False):
        self.pid = _Proc._next
        _Proc._next += 1
        self.dies = dies

    def poll(self):
        return 1 if self.dies else None

    def wait(self, timeout=None):
        return 0


def _ready_to_launch(monkeypatch, home, *, free_mb=8500, total_mb=10240, shape=SHAPE_4B):
    model_dir = home / "hf" / "snapshot"
    model_dir.mkdir(parents=True)
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: str(model_dir))
    monkeypatch.setattr(vs.ModelShape, "from_dir", classmethod(lambda cls, d: shape))
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (free_mb, total_mb))
    return model_dir


def test_start_argv_and_env(config, monkeypatch, home):
    model_dir = _ready_to_launch(monkeypatch, home)
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return _Proc()
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: 200 if spawned else None)
    monkeypatch.setenv("VAF_TEST_MARK", "kept")
    assert vs.start("Qwen/Qwen3-4B-AWQ", 8192, popen=popen) is True
    assert len(spawned) == 1
    argv, kw = spawned[0]
    assert argv[:3] == [str(vs.vllm_exe()), "serve", str(model_dir)]

    def value(flag):
        return argv[argv.index(flag) + 1]
    assert value("--host") == "127.0.0.1" and value("--port") == "8080"
    assert value("--served-model-name") == "Qwen/Qwen3-4B-AWQ"
    assert value("--max-model-len") == "32768", "the 32768 floor applies to vLLM too"
    assert 0.50 <= float(value("--gpu-memory-utilization")) <= 0.92
    assert value("--kv-cache-dtype") == "fp8"
    assert "--enable-auto-tool-choice" in argv and value("--tool-call-parser") == "hermes"
    assert value("--reasoning-parser") == "qwen3"
    assert "--trust-remote-code" not in argv
    assert all(isinstance(a, str) for a in argv), "an argument list, no shell"
    assert "shell" not in kw
    if os.name != "nt":
        assert kw.get("start_new_session") is True
    env = kw["env"]
    for name in ("VLLM_SKIP_MODEL_NAME_VALIDATION", "VLLM_NO_USAGE_STATS", "DO_NOT_TRACK",
                 "HF_HUB_OFFLINE"):
        assert env.get(name) == "1", name
    assert env.get("VAF_TEST_MARK") == "kept" and env.get("PATH") == os.environ.get("PATH"), \
        "the child gets VAF's environment"
    assert vs.pid_file().read_bytes().decode() == str(_Proc._next - 1)


def test_a_model_that_cannot_fit_is_refused_before_a_launch(config, monkeypatch, home):
    _ready_to_launch(monkeypatch, home, free_mb=3000, total_mb=6144)
    errors = []
    monkeypatch.setattr("vaf.cli.ui.UI.error", staticmethod(lambda t, *a, **k: errors.append(t)))

    def popen(*a, **kw):
        raise AssertionError("launched a model that does not fit")
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is False
    assert errors and "MiB" in errors[-1]


def test_a_dying_fp8_start_retries_once_with_auto(config, monkeypatch, home):
    """fp8 KV is not verified on every attention backend: a start that dies with it is retried
    once with the model's own dtype, and the next start skips the doomed attempt."""
    _ready_to_launch(monkeypatch, home, free_mb=20000, total_mb=24576)
    spawned, groups = [], []
    procs = [_Proc(dies=True), _Proc(), _Proc()]

    def popen(argv, **kw):
        spawned.append(argv[argv.index("--kv-cache-dtype") + 1])
        return procs[len(spawned) - 1]
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "terminate_process_group", staticmethod(
        lambda pgid, grace=1.0: groups.append(pgid)))
    monkeypatch.setattr(vs, "health_status",
                        lambda port=8080, timeout=1.0: 200 if len(spawned) >= 2 else None)
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is True
    assert spawned == ["fp8", "auto"]
    assert groups == [procs[0].pid], "what the dead API server left in its group is stopped"
    assert vs._kv_fp8_failed is True
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is True
    assert spawned == ["fp8", "auto", "auto"]


def test_fit_measured_case():
    """The measured card: RTX 3080, 10240 MiB, 8500 MiB free, Qwen3-4B-AWQ at 32768."""
    result = vs.fit(SHAPE_4B, 32768, 8500, 10240)
    assert result.ok, result.reason
    assert result.kv_dtype == "fp8" and result.max_model_len == 32768
    assert 0.50 <= result.utilization <= 0.92
    assert abs(result.utilization - (8500 - 512) / 10240) < 1e-9
    fp16 = vs.fit(SHAPE_4B, 32768, 8500, 10240, kv_dtype="auto")
    assert not fp16.ok
    for number in (str(fp16.need_mb), str(fp16.budget_mb), "10240", "8500", "32768"):
        assert number in fp16.reason, (number, fp16.reason)
    assert fp16.need_mb > fp16.budget_mb > result.need_mb


def test_fit_clamps_to_max_position():
    result = vs.fit(SHAPE_4B, 131072, 20000, 24576)
    assert result.max_model_len == 40960
    assert "131072" in result.note and "40960" in result.note
    assert vs.fit(SHAPE_4B, 32768, 20000, 24576).note == ""


def test_fit_refuses_a_share_above_the_free_memory():
    result = vs.fit(SHAPE_4B, 32768, 4000, 10240, utilization=0.9)
    assert not result.ok and "4000" in result.reason
    low = vs.fit(SHAPE_4B, 32768, 3000, 10240)
    assert low.utilization == 0.50 and not low.ok


def test_model_shape_from_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "num_hidden_layers": 36, "num_attention_heads": 32, "num_key_value_heads": 8,
        "head_dim": 128, "hidden_size": 2560, "max_position_embeddings": 40960}), encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"\0" * 1000)
    assert vs.ModelShape.from_dir(str(tmp_path)) == vs.ModelShape(1000, 36, 8, 128, 40960)
    (tmp_path / "config.json").write_text(json.dumps({"text_config": {
        "num_hidden_layers": 4, "num_attention_heads": 8, "hidden_size": 512}}), encoding="utf-8")
    assert vs.ModelShape.from_dir(str(tmp_path)) == vs.ModelShape(1000, 4, 8, 64, 0)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert vs.ModelShape.from_dir(str(tmp_path)) is None


# ── stop ──────────────────────────────────────────────────────────────────────────

_LEADER = r"""
import subprocess, sys, time
# The middle process starts the grandchild and exits at once: the grandchild is nobody's
# child any more, but it stays in this session's process group (an EngineCore that
# outlives its parent holds the GPU the same way).
middle = ("import subprocess, sys; "
          "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
          "open(sys.argv[1], 'w').write(str(p.pid))")
subprocess.run([sys.executable, "-c", middle, sys.argv[2]])
time.sleep(120)
"""


def _alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


@POSIX_ONLY
def test_stop_kills_group_and_removes_pid(home):
    pytest.importorskip("psutil")
    fake_exe = vs.vllm_exe()
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_text(_LEADER, encoding="utf-8")
    grandchild_file = home / "grandchild.pid"
    # The command line of the real server: <venv>/bin/vllm serve ... (run by its interpreter).
    leader = subprocess.Popen([sys.executable, str(fake_exe), "serve", str(grandchild_file)],
                              start_new_session=True)
    grandchild = None
    try:
        deadline = time.monotonic() + 15
        while not grandchild_file.exists() or not grandchild_file.read_text().strip():
            assert time.monotonic() < deadline, "the stand-in never started its grandchild"
            time.sleep(0.05)
        grandchild = int(grandchild_file.read_text().strip())
        assert _alive(grandchild)
        vs.pid_file().parent.mkdir(parents=True, exist_ok=True)
        vs.pid_file().write_text(str(leader.pid))
        assert vs.running_pid() == leader.pid
        assert vs.stop(wait=2.0) == ""
        assert leader.wait(5) is not None
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), "the group member outside the parent's tree survived"
        assert not vs.pid_file().exists()
    finally:
        for pid in (leader.pid, grandchild):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        leader.wait(5)


@POSIX_ONLY
def test_stop_ignores_foreign_pid(home):
    pytest.importorskip("psutil")
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        vs.pid_file().parent.mkdir(parents=True, exist_ok=True)
        vs.pid_file().write_text(str(stranger.pid))
        assert vs.running_pid() is None
        assert vs.stop(wait=0.5) == ""
        time.sleep(0.3)
        assert stranger.poll() is None, "a process that is no managed vLLM was signalled"
        assert not vs.pid_file().exists(), "the stale record is removed"
    finally:
        stranger.kill()
        stranger.wait(5)


# ── install / model ──────────────────────────────────────────────────────────────


def _supported(monkeypatch):
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.SUPPORTED, "ok"))


def _fake_installing_run(home, calls, pip_code=0, probe_code=0):
    def run(argv, env=None, **kw):
        assert isinstance(argv, list) and "shell" not in kw
        calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            vs.venv_python().parent.mkdir(parents=True, exist_ok=True)
        elif "pip" in argv and pip_code == 0:
            site = vs.venv_dir() / "lib" / "python3.13" / "site-packages" / f"vllm-{vs.PIN}.dist-info"
            site.mkdir(parents=True)
            (site / "METADATA").write_text(f"Name: vllm\nVersion: {vs.PIN}\n", encoding="utf-8")
            vs.vllm_exe().write_text("#!/bin/sh\n", encoding="utf-8")
        code = pip_code if "pip" in argv else (probe_code if "-c" in argv else 0)
        return subprocess.CompletedProcess(argv, code)
    return run


def test_install_steps(config, monkeypatch, home):
    _supported(monkeypatch)
    calls = []
    assert vs.install(run=_fake_installing_run(home, calls)) == ""
    assert calls[0] == [sys.executable, "-m", "venv", str(vs.venv_dir())]
    assert calls[1] == [str(vs.venv_python()), "-m", "pip", "install", f"vllm=={vs.PIN}",
                        "--extra-index-url", "https://download.pytorch.org/whl/cu129"]
    assert calls[2] == [str(vs.vllm_exe()), "download-kernels"]
    assert calls[3][:2] == [str(vs.venv_python()), "-c"] and "torch.cuda.is_available" in calls[3][2]
    assert vs.installed_version() == vs.PIN and vs.is_installed()
    calls.clear()
    assert vs.install(run=_fake_installing_run(home, calls)) == "" and calls == [], \
        "an installed pin installs nothing"


def test_a_failed_install_leaves_nothing(config, monkeypatch, home):
    _supported(monkeypatch)
    calls = []
    reason = vs.install(run=_fake_installing_run(home, calls, pip_code=1))
    assert "Installing vLLM" in reason and "exit 1" in reason
    assert not vs.venv_dir().exists()


def test_a_gpu_pytorch_cannot_see_is_reported(config, monkeypatch, home):
    _supported(monkeypatch)
    reason = vs.install(run=_fake_installing_run(home, [], probe_code=3))
    assert "driver" in reason


def test_install_refuses_an_unsupported_machine(config, monkeypatch, home):
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.LATER, "Windows here."))
    calls = []
    assert vs.install(run=_fake_installing_run(home, calls)) == "Windows here."
    assert calls == []


def test_uninstall_stays_inside_bin(monkeypatch, home, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "venv"
    elsewhere.mkdir(parents=True)
    monkeypatch.setattr(vs, "venv_dir", lambda: elsewhere)
    assert "not inside" in vs.uninstall()
    assert elsewhere.exists()


def test_ensure_model_reads_the_cache_first_and_never_fetches_code(config, monkeypatch, home):
    import huggingface_hub
    cached = home / "hf" / "snap"
    calls = []

    def snapshot_download(repo_id, **kw):
        calls.append(kw)
        if kw.get("local_files_only"):
            if cached.exists():
                return str(cached)
            raise FileNotFoundError("not cached")
        cached.mkdir(parents=True)
        (cached / "model.safetensors").write_bytes(b"x")
        return str(cached)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    assert vs.ensure_model("Qwen/Qwen3-4B-AWQ") == str(cached)
    fetch = [c for c in calls if not c.get("local_files_only")]
    assert len(fetch) == 1 and not any(p.endswith(".py") for p in fetch[0]["allow_patterns"])
    calls.clear()
    assert vs.ensure_model("Qwen/Qwen3-4B-AWQ") == str(cached)
    assert all(c.get("local_files_only") for c in calls), "a cached model reaches no network"
    assert vs.ensure_model("not a repo") == ""


def test_installed_version_reads_metadata_only(home):
    assert vs.installed_version() is None
    site = vs.venv_dir() / "Lib" / "site-packages" / "vllm-0.31.0.dist-info"
    site.mkdir(parents=True)
    (site / "METADATA").write_bytes(b"Metadata-Version: 2.1\r\nName: vllm\r\nVersion: 0.31.0\r\n")
    assert vs.installed_version() == "0.31.0"
    assert Path(vs.venv_dir()).is_dir()


# ── round 1: identity, environment, model completeness, races ──────────────────────

@POSIX_ONLY
def test_stop_ignores_a_process_that_only_names_the_environment(home):
    """A command line that mentions a path inside the environment is not the server: only the
    managed vllm executable followed by `serve` is."""
    pytest.importorskip("psutil")
    looker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               str(vs.venv_dir()), str(vs.vllm_exe()), "status"])
    try:
        time.sleep(0.2)
        vs.pid_file().parent.mkdir(parents=True, exist_ok=True)
        vs.pid_file().write_text(str(looker.pid))
        assert vs.running_pid() is None
        assert vs.stop(wait=0.5) == ""
        time.sleep(0.3)
        assert looker.poll() is None, "a process that only names the environment was signalled"
    finally:
        looker.kill()
        looker.wait(5)


def test_child_env_drops_inherited_vllm_switches():
    env = vs.child_env({"PATH": "/usr/bin", "CUDA_VISIBLE_DEVICES": "0",
                        "VLLM_SERVER_DEV_MODE": "1", "VLLM_ALLOW_RUNTIME_LORA_UPDATING": "True",
                        "VLLM_PLUGINS": "evil", "vllm_attention_backend": "X",
                        "VLLM_NO_USAGE_STATS": "0"})
    assert env["PATH"] == "/usr/bin" and env["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["VLLM_SERVER_DEV_MODE"] == "0" and env["VLLM_ALLOW_RUNTIME_LORA_UPDATING"] == "0"
    assert "VLLM_PLUGINS" not in env and "vllm_attention_backend" not in env
    assert env["VLLM_NO_USAGE_STATS"] == "1"
    assert {k for k in env if k.upper().startswith("VLLM_")} == {
        "VLLM_SERVER_DEV_MODE", "VLLM_ALLOW_RUNTIME_LORA_UPDATING",
        "VLLM_SKIP_MODEL_NAME_VALIDATION", "VLLM_NO_USAGE_STATS"}


def test_local_snapshot_needs_every_shard(monkeypatch, tmp_path):
    import huggingface_hub
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "model-00001-of-00002.safetensors").write_bytes(b"x")
    (snap / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
        "a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}),
        encoding="utf-8")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **kw: str(snap))
    assert vs.local_snapshot("Qwen/Qwen3-8B-AWQ") == "", "half a sharded model counted as there"
    (snap / "model-00002-of-00002.safetensors").write_bytes(b"x")
    assert vs.local_snapshot("Qwen/Qwen3-8B-AWQ") == str(snap)
    (snap / "model.safetensors.index.json").write_text("not json", encoding="utf-8")
    assert vs.local_snapshot("Qwen/Qwen3-8B-AWQ") == ""


def test_no_code_file_of_the_repo_is_fetched():
    """Filtered the way huggingface_hub filters a snapshot."""
    from huggingface_hub.utils import filter_repo_objects
    names = ["config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
             "tokenizer.model", "merges.txt", "chat_template.jinja", "tokenizer.py",
             "tokenization_qwen.py", "modeling_qwen.py", "tokenizer_fast.pyc", "kernels.so",
             "pytorch_model.bin"]
    kept = set(filter_repo_objects(names, allow_patterns=vs.MODEL_PATTERNS,
                                   ignore_patterns=vs.CODE_PATTERNS))
    assert kept == {"config.json", "model.safetensors", "tokenizer.json",
                    "tokenizer_config.json", "tokenizer.model", "merges.txt",
                    "chat_template.jinja"}


def test_the_download_passes_the_code_exclusion(config, monkeypatch, home):
    import huggingface_hub
    calls = []

    def snapshot_download(repo_id, **kw):
        calls.append(kw)
        if kw.get("local_files_only"):
            raise FileNotFoundError("not cached")
        d = home / "hf" / "s"
        d.mkdir(parents=True, exist_ok=True)
        (d / "model.safetensors").write_bytes(b"x")
        return str(d)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    assert vs.ensure_model("Qwen/Qwen3-4B-AWQ")
    assert calls and all(c.get("ignore_patterns") == vs.CODE_PATTERNS for c in calls)


def test_a_failed_start_keeps_another_servers_pid_file(config, monkeypatch, home):
    """Our launch died; meanwhile another process recorded its own server. Its record stays."""
    _ready_to_launch(monkeypatch, home, free_mb=20000, total_mb=24576)
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "terminate_process_group", staticmethod(lambda pgid, grace=1.0: None))
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: None)

    def popen(argv, **kw):
        proc = _Proc(dies=True)
        return proc

    real_wait = vs._wait_ready

    def wait_ready(proc, port):
        vs.pid_file().write_text("424242")       # another process's server, recorded now
        return real_wait(proc, port)
    monkeypatch.setattr(vs, "_wait_ready", wait_ready)
    monkeypatch.setattr(vs, "_kv_fp8_failed", True)       # one attempt only
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is False
    assert vs.pid_file().read_text() == "424242"


def test_a_server_started_meanwhile_is_not_spawned_twice(config, monkeypatch, home):
    _ready_to_launch(monkeypatch, home)
    vs.pid_file().parent.mkdir(parents=True, exist_ok=True)
    vs.pid_file().write_text("777")
    monkeypatch.setattr(vs, "running_pid", lambda: 777)
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: 200)
    spawned = []

    def popen(*a, **kw):
        # Recorded, not raised: start() turns a launch exception into a refusal.
        spawned.append(a)
        return _Proc()
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is False
    assert spawned == [], "a second server was spawned"
    assert vs.pid_file().read_text() == "777"


def test_a_taken_port_is_no_kv_failure(config, monkeypatch, home):
    _ready_to_launch(monkeypatch, home, free_mb=20000, total_mb=24576)
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "terminate_process_group", staticmethod(lambda pgid, grace=1.0: None))
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: None)
    spawned, errors = [], []
    monkeypatch.setattr("vaf.cli.ui.UI.error", staticmethod(lambda t, *a, **k: errors.append(t)))

    def popen(argv, stdout=None, **kw):
        spawned.append(argv[argv.index("--kv-cache-dtype") + 1])
        stdout.write("OSError: [Errno 98] Address already in use\n")
        stdout.flush()
        return _Proc(dies=True)
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is False
    assert spawned == ["fp8"], "a bind failure was retried as a KV-cache failure"
    assert vs._kv_fp8_failed is False
    assert errors and "port" in errors[-1]


@POSIX_ONLY
def test_stop_reaches_its_own_child_without_a_pid_file(home):
    pytest.importorskip("psutil")
    fake_exe = vs.vllm_exe()
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_text("import time; time.sleep(60)\n", encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(fake_exe), "serve"], start_new_session=True)
    try:
        vs._child = child
        assert not vs.pid_file().exists()
        assert vs.stop(wait=1.0) == ""
        assert child.wait(5) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(5)
        vs._child = None
