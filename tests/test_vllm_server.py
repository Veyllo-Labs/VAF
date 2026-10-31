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
  test_install_steps / test_a_failed_install_leaves_nothing;
- re-add `--extra-index-url`, drop the extension import from the probe, or write the recipe
  record before the probe -> test_install_steps;
- go back to the early return `installed_version() == PIN and vllm_exe().exists()` ->
  test_an_environment_of_the_earlier_recipe_is_rebuilt;
- map probe exit 4 to no_cuda, or write the record on a failed probe ->
  test_a_failed_probe_names_its_cause;
- `env = dict(os.environ)` in install, or child_env filtering os.environ directly ->
  test_install_and_server_get_the_gpu_back;
- start() checking only `is_installed()` -> test_start_refuses_an_environment_of_the_earlier_recipe.
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
    monkeypatch.setattr(vs, "is_current", lambda: True)
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


def _fake_installing_run(home, calls, pip_code=0, probe_code=0, envs=None, records=None):
    def run(argv, env=None, **kw):
        assert isinstance(argv, list) and "shell" not in kw
        calls.append(argv)
        if envs is not None:
            envs.append(env)
        if records is not None:
            records.append(vs.recipe_path().exists())
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
    calls, records = [], []
    assert vs.install(run=_fake_installing_run(home, calls, records=records)) == ""
    assert calls[0] == [sys.executable, "-m", "venv", str(vs.venv_dir())]
    # The release's own command: PyPI only, its CUDA 13.0 build and the torch it requires.
    assert calls[1] == [str(vs.venv_python()), "-m", "pip", "install", "vllm==0.32.0"]
    assert calls[2] == [str(vs.vllm_exe()), "download-kernels"]
    assert calls[3][:2] == [str(vs.venv_python()), "-c"]
    assert "torch.cuda.is_available" in calls[3][2]
    assert "import vllm._C_stable_libtorch" in calls[3][2]
    assert records == [False, False, False, False], "the record was written before the probe"
    assert vs.recipe_path() == vs.venv_dir() / "vaf_recipe"
    assert vs.recipe_path().read_bytes() == b"vllmenv-1-18dfb7"
    assert vs.installed_version() == vs.PIN and vs.is_installed() and vs.is_current()
    calls.clear()
    assert vs.install(run=_fake_installing_run(home, calls)) == "" and calls == [], \
        "an installed pin installs nothing"


def test_a_failed_install_leaves_nothing(config, monkeypatch, home):
    _supported(monkeypatch)
    calls = []
    reason = vs.install(run=_fake_installing_run(home, calls, pip_code=1))
    assert "Installing vLLM" in reason and "exit 1" in reason
    assert not vs.venv_dir().exists()


def test_the_probe_script_maps_its_causes(tmp_path):
    """The probe really exits 3 without a CUDA GPU and 4 when the extension does not load
    (stand-in torch and vllm packages on its path)."""
    import textwrap
    for cuda, ext_ok, expected in [(False, True, 3), (True, False, 4), (True, True, 0)]:
        pkgs = tmp_path / f"p{int(cuda)}{int(ext_ok)}"
        (pkgs / "torch").mkdir(parents=True)
        (pkgs / "torch" / "__init__.py").write_text(textwrap.dedent(f"""
            class cuda:
                @staticmethod
                def is_available():
                    return {cuda}
        """), encoding="utf-8")
        (pkgs / "vllm").mkdir()
        (pkgs / "vllm" / "__init__.py").write_text("", encoding="utf-8")
        if ext_ok:
            (pkgs / "vllm" / "_C_stable_libtorch.py").write_text("", encoding="utf-8")
        else:
            (pkgs / "vllm" / "_C_stable_libtorch.py").write_text(
                "raise ImportError('libcudart.so.13: cannot open shared object file')",
                encoding="utf-8")
        out = subprocess.run([sys.executable, "-c", vs._PROBE], capture_output=True, text=True,
                             timeout=60, env={**os.environ, "PYTHONPATH": str(pkgs)})
        assert out.returncode == expected, (cuda, ext_ok, out.stderr)
        if expected == 4:
            assert "libcudart.so.13" in out.stderr


@pytest.mark.parametrize("probe_code, cause", [(3, "no_cuda"), (4, "libs"), (1, "libs"),
                                               (None, "libs")])
def test_a_failed_probe_names_its_cause(config, monkeypatch, home, probe_code, cause):
    _supported(monkeypatch)
    run = _fake_installing_run(home, [], probe_code=probe_code or 0)
    if probe_code is None:
        inner = run

        def run(argv, env=None, **kw):     # noqa: F811 - the probe cannot even run
            if "-c" in argv:
                raise OSError("no such file")
            return inner(argv, env=env, **kw)
    outcome = {}
    reason = vs.install(run=run, outcome=outcome)
    assert outcome == {"probe": cause}
    assert "12.9" not in reason
    if cause == "no_cuda":
        assert "13.0" in reason and "580" in reason and "CUDA_VISIBLE_DEVICES" in reason
    else:
        assert "CUDA libraries" in reason and "cannot start" in reason
    assert not vs.recipe_path().exists(), "a failed probe left a recipe record"
    assert not vs.is_current()


def test_an_environment_of_the_earlier_recipe_is_rebuilt(config, monkeypatch, home):
    """The pinned METADATA and the exe, but no recipe record: what the earlier cu129 recipe
    left behind. It is not current, so the install removes it and runs every step again."""
    _supported(monkeypatch)
    site = vs.venv_dir() / "lib" / "python3.13" / "site-packages" / f"vllm-{vs.PIN}.dist-info"
    site.mkdir(parents=True)
    (site / "METADATA").write_text(f"Name: vllm\nVersion: {vs.PIN}\n", encoding="utf-8")
    vs.vllm_exe().parent.mkdir(parents=True, exist_ok=True)
    vs.vllm_exe().write_text("#!/bin/sh\n", encoding="utf-8")
    stale = vs.venv_dir() / "lib" / "python3.13" / "site-packages" / "torch-2.13.0+cu129.dist-info"
    stale.mkdir(parents=True)
    assert vs.is_installed() and not vs.recipe_current() and not vs.is_current()
    assert le.needs_install()
    calls = []
    assert vs.install(run=_fake_installing_run(home, calls)) == ""
    assert calls[0] == [sys.executable, "-m", "venv", str(vs.venv_dir())]
    assert not stale.exists(), "the old environment was not removed first"
    assert vs.is_current() and not le.needs_install()


def test_install_and_server_get_the_gpu_back(config, monkeypatch, home):
    """Under the tray's hide every install step and the server run without the hidden
    CUDA_VISIBLE_DEVICES; a VLLM_ variable is still dropped from the server's env."""
    from vaf.core.gpu_detection import HIDE_RECORD
    _supported(monkeypatch)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32")
    monkeypatch.setenv(HIDE_RECORD, "CUDA_VISIBLE_DEVICES,PYTORCH_CUDA_ALLOC_CONF")
    monkeypatch.setenv("VLLM_PLUGINS", "evil")
    envs = []
    assert vs.install(run=_fake_installing_run(home, [], envs=envs)) == ""
    assert len(envs) == 4
    for env in envs:
        assert "CUDA_VISIBLE_DEVICES" not in env and HIDE_RECORD not in env
        assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    server = vs.child_env()
    assert "CUDA_VISIBLE_DEVICES" not in server and "PYTORCH_CUDA_ALLOC_CONF" not in server
    assert "VLLM_PLUGINS" not in server and server["VLLM_NO_USAGE_STATS"] == "1"


def test_start_refuses_an_environment_of_the_earlier_recipe(config, monkeypatch, home):
    from vaf.cli.ui import UI
    _ready_to_launch(monkeypatch, home)
    monkeypatch.setattr(vs, "is_current", lambda: False)
    said = []
    monkeypatch.setattr(UI, "error", staticmethod(lambda text, *a, **k: said.append(text)))
    spawned = []

    def popen(*a, **k):
        # Recorded, then refused, so a start that wrongly launches returns at once instead of
        # waiting for /health.
        spawned.append(a)
        raise RuntimeError("launched")
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: None)
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is False
    assert spawned == [], "an environment of the earlier recipe was launched"
    assert said and "earlier VAF" in said[-1] and "`vaf engine vllm`" in said[-1]


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


def test_a_refused_start_leaves_the_running_servers_log_alone(config, monkeypatch, home):
    """Opening the log rotates it. A start refused because another process's server runs (or
    because the profile is api) must not move that server's log aside: its starter reads the
    log for a bind failure. MUTATION: open the log before the lock and the check - red."""
    _ready_to_launch(monkeypatch, home)
    log = vs.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("INFO: the other process's server is loading\n", encoding="utf-8")
    monkeypatch.setattr(vs, "running_pid", lambda: 777)
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=lambda *a, **k: _Proc()) is False
    assert log.read_text(encoding="utf-8") == "INFO: the other process's server is loading\n"
    assert not log.with_name("vllm_server.prev.log").exists()
    config["inference_profile"] = "api"
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=lambda *a, **k: _Proc()) is False
    assert log.read_text(encoding="utf-8") == "INFO: the other process's server is loading\n"
    assert not log.with_name("vllm_server.prev.log").exists()


# ── the switch from Settings and the TUI: pip output, the banner ──────────────────────


def test_install_output_goes_to_file(config, monkeypatch, home):
    """In the web server and the full-screen TUI pip has no terminal: every step writes into
    the file, and without one every step inherits the terminal as before. MUTATION: pass
    `stdout` only to the first step - red."""
    _supported(monkeypatch)
    calls, seen = [], []

    def run(argv, env=None, **kw):
        seen.append(kw)
        return _fake_installing_run(home, calls)(argv, env=env)
    steps = []
    log = home / "logs" / "vllm_install.log"
    assert vs.install(run=run, output=str(log), on_step=steps.append) == ""
    assert steps == list(vs.INSTALL_STEPS)
    assert len(seen) == 4 and all(kw.get("stderr") is subprocess.STDOUT for kw in seen)
    assert all(getattr(kw.get("stdout"), "name", "") == str(log) for kw in seen)
    assert log.exists()
    vs.uninstall()
    seen.clear()
    assert vs.install(run=run) == ""
    assert seen == [{}, {}, {}, {}], "without a file the steps print to the terminal"


def test_ensure_model_broadcasts_and_closes(config, monkeypatch, home):
    """A vLLM model download reaches the banner of every page: the progress stream runs
    while it downloads, and one closing frame without a model list ends it (the GGUF list
    on the page must not be emptied by it). MUTATION: drop the context manager from
    `ensure_model` - red."""
    import huggingface_hub

    from vaf.core import web_interface
    from vaf.core.model_download_state import MODEL_DOWNLOAD
    streams, pushed = [], []

    def start():
        streams.append("start")
        return lambda: streams.append("stop")
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", start)
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: type("W", (), {"push_update": staticmethod(pushed.append)})())

    def snapshot_download(repo_id, **kw):
        if kw.get("local_files_only"):
            raise FileNotFoundError("not cached")
        assert MODEL_DOWNLOAD.active and streams == ["start"], "the banner does not see it"
        d = home / "hf" / "b"
        d.mkdir(parents=True, exist_ok=True)
        (d / "model.safetensors").write_bytes(b"x")
        return str(d)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    assert vs.ensure_model("Qwen/Qwen3-4B-AWQ")
    assert streams == ["start", "stop"] and not MODEL_DOWNLOAD.active
    assert pushed == [{"type": "model_download_done", "success": True}]


@pytest.mark.parametrize("raised, cancelled", [("cancel", True), ("guard", False), ("network", False)])
def test_ensure_model_reports_only_its_own_cancel(config, monkeypatch, home, raised, cancelled):
    """The caller learns whether THIS download was cancelled, from its own exception: the
    profile guard's InterruptedError or a network error is a failure, not a cancel."""
    import huggingface_hub

    from vaf.core import web_interface
    from vaf.core.model_download_state import DownloadCancelled
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", lambda: (lambda: None))
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: type("W", (), {"push_update": staticmethod(lambda f: None)})())

    def snapshot_download(repo_id, **kw):
        if kw.get("local_files_only"):
            raise FileNotFoundError("not cached")
        raise {"cancel": DownloadCancelled("cancelled"),
               "guard": InterruptedError("profile switched to api"),
               "network": OSError("connection reset")}[raised]
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    outcome = {}
    assert vs.ensure_model("Qwen/Qwen3-4B-AWQ", outcome=outcome) == ""
    assert outcome.get("cancelled", False) is cancelled
