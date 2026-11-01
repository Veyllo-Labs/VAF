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
- drop the retry with auto, or the memory of the attempt that came up ->
  test_a_dying_fp8_start_retries_once_with_auto;
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
- start() checking only `is_installed()` -> test_start_refuses_an_environment_of_the_earlier_recipe;
- drop `--require-hashes` or `--only-binary=:all:` from the pip step -> test_install_steps;
- drop the lock check before the uninstall -> test_a_missing_lock_refuses_before_anything_changes;
- run `vllm download-kernels` again -> test_install_steps;
- drop `--extra-index-url` from the ROCm pip step, write the CUDA tag on ROCm, or make the
  ROCm environment with sys.executable -> test_rocm_install_steps;
- let recipe_current accept either build's tag -> test_a_cuda_environment_is_rebuilt_on_rocm;
- drop `vllm._rocm_C` from the ROCm probe -> test_the_rocm_probe_maps_its_causes;
- map the ROCm probe's exit 3 to no_cuda -> test_a_failed_rocm_probe_names_its_cause;
- read the GPU memory from nvidia-smi on ROCm -> test_rocm_fit_reads_amd_memory;
- keep the no-GPU answer of backend() for the process -> test_the_backend_follows_the_gpu;
- install or start without with_rocm_library ->
  test_a_rocm_off_the_wheels_runpath_reaches_probe_and_server;
- plan fp8 for 8.6 without FlashInfer, or fp8 with TRITON_ATTN below 8.9 ->
  test_attention_plan_without_flashinfer_build;
- drop the VLLM_USE_FLASHINFER_SAMPLER line from child_env ->
  test_child_env_turns_flashinfer_sampler_off;
- make flashinfer_buildable always False -> test_buildable_flashinfer_keeps_the_old_command;
- drop the `+ 4` of the per-token-head scale -> test_int8_per_token_head_fit_counts_scales;
- remove the shrink of the context -> test_fit_shrinks_context_to_fit; let it go below
  MIN_CONTEXT -> test_fit_refuses_below_min_context;
- name a smaller context in the refusal, or a smaller model for the small default ->
  test_too_big_reason_names_other_programs;
- send the test request without top_p/top_k, or leave the server running after a passing
  first start -> test_first_start_sends_a_sampled_request_and_stops;
- restore "FlashInfer builds the kernels it needs at the first start" ->
  test_no_tracked_file_says_flashinfer_builds_at_the_first_start.
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

REAL_BUILDABLE = vs.flashinfer_buildable
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


@pytest.fixture(autouse=True)
def cuda_build(monkeypatch):
    """The NVIDIA build unless a test says otherwise, on a GPU of compute capability 8.6
    where FlashInfer can build its kernels (fp8 then auto, vLLM's own backend): the build and
    the plan follow this machine's GPU and toolkit, and the tests must not. Every test starts
    without a remembered start."""
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    monkeypatch.setattr(vs, "compute_capability", lambda: (8, 6))
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: True)
    monkeypatch.setattr(vs, "_start_failure", None)
    monkeypatch.setattr(vs, "_came_up", None)
    monkeypatch.setattr(vs, "_logged_failure", None)
    monkeypatch.setattr(vs, "_stopped_while_loading", set())


@pytest.fixture
def home(monkeypatch, tmp_path):
    """The environment and the pid file inside tmp_path."""
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(vs, "base_dir", lambda: tmp_path)
    monkeypatch.setattr(vs, "pid_file", lambda: tmp_path / ".vaf" / "vllm.pid")
    monkeypatch.setattr(vs, "log_path", lambda: tmp_path / "logs" / "vllm_server.log")
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
    assert vs._came_up[1] == 1, "the attempt that came up is remembered"
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


def _fake_installing_run(home, calls, pip_code=0, probe_code=0, envs=None, records=None,
                         version=None):
    version = version or vs.PIN

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
            site = vs.venv_dir() / "lib" / "python3.13" / "site-packages" / f"vllm-{version}.dist-info"
            site.mkdir(parents=True)
            (site / "METADATA").write_text(f"Name: vllm\nVersion: {version}\n", encoding="utf-8")
            vs.vllm_exe().write_text("#!/bin/sh\n", encoding="utf-8")
        code = pip_code if "pip" in argv else (probe_code if "-c" in argv else 0)
        return subprocess.CompletedProcess(argv, code)
    return run


def test_install_steps(config, monkeypatch, home):
    _supported(monkeypatch)
    calls, records = [], []
    assert vs.install(run=_fake_installing_run(home, calls, records=records)) == ""
    assert calls[0] == [sys.executable, "-m", "venv", str(vs.venv_dir())]
    # The hash-locked tree from PyPI only: no extra index, no unhashed file, no source build.
    assert calls[1] == [str(vs.venv_python()), "-m", "pip", "install", "--require-hashes",
                        "--no-deps", "--only-binary=:all:", "-r", str(vs.LOCK_PATH)]
    assert vs.LOCK_PATH == Path(vs.__file__).with_name("vllm_requirements.lock")
    assert calls[2][:2] == [str(vs.venv_python()), "-c"]
    assert "torch.cuda.is_available" in calls[2][2]
    assert "import vllm._C_stable_libtorch" in calls[2][2]
    # No `vllm download-kernels`: it would pip-install unhashed FlashInfer kernels from
    # FlashInfer's own index, past the lock.
    assert len(calls) == 3 and not any("download-kernels" in a for argv in calls for a in argv)
    assert records == [False, False, False], "the record was written before the probe"
    assert vs.recipe_path() == vs.venv_dir() / "vaf_recipe"
    assert vs.recipe_path().read_bytes() == b"vllmenv-1-18dfb7"
    assert vs.installed_version() == vs.PIN and vs.is_installed() and vs.is_current()
    calls.clear()
    assert vs.install(run=_fake_installing_run(home, calls)) == "" and calls == [], \
        "an installed pin installs nothing"


def test_a_missing_lock_refuses_before_anything_changes(config, monkeypatch, home, tmp_path):
    _supported(monkeypatch)
    monkeypatch.setattr(vs, "LOCK_PATH", tmp_path / "gone.lock")
    vs.venv_dir().mkdir(parents=True)
    (vs.venv_dir() / "keep").write_bytes(b"x")
    calls = []
    reason = vs.install(run=_fake_installing_run(home, calls))
    assert "hash-locked" in reason and "gone.lock" in reason
    assert calls == [] and (vs.venv_dir() / "keep").exists(), "the old environment was touched"


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
        assert "update the NVIDIA driver and try again" in reason, "no next step"
    else:
        assert "CUDA libraries" in reason and "cannot start" in reason
        assert "Run `vaf engine vllm` again" in reason and "llama.cpp keeps running" in reason
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
    assert len(envs) == 3
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
    monkeypatch.setattr(vs, "launch_plan", lambda base=None: vs.KvPlan(("auto",), ("",)))
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
    assert vs.start_failure() is None, "a taken port is not the model's failure"
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
    assert len(seen) == 3 and all(kw.get("stderr") is subprocess.STDOUT for kw in seen)
    assert all(getattr(kw.get("stdout"), "name", "") == str(log) for kw in seen)
    assert log.exists()
    vs.uninstall()
    seen.clear()
    assert vs.install(run=run) == ""
    assert seen == [{}, {}, {}], "without a file the steps print to the terminal"


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


# ── the ROCm build (AMD) ─────────────────────────────────────────────────────────

ROCM_VERSION = f"{vs.PIN}+{vs.ROCM_VARIANT}"


@pytest.fixture
def rocm(monkeypatch):
    """This machine's build is ROCm, with a Python 3.12 of its own."""
    monkeypatch.setattr(vs, "backend", lambda: vs.ROCM)
    monkeypatch.setattr(vs, "rocm_python", lambda: "/usr/bin/python3.12")


def _environment(version, tag):
    """An installed environment: vllm's METADATA, the exe and a recipe record."""
    site = vs.venv_dir() / "lib" / "python3.12" / "site-packages" / f"vllm-{version}.dist-info"
    site.mkdir(parents=True)
    (site / "METADATA").write_text(f"Name: vllm\nVersion: {version}\n", encoding="utf-8")
    vs.vllm_exe().parent.mkdir(parents=True, exist_ok=True)
    vs.vllm_exe().write_text("#!/bin/sh\n", encoding="utf-8")
    vs.recipe_path().write_bytes(tag.encode("utf-8"))


def test_rocm_install_steps(config, monkeypatch, home, rocm):
    """On AMD: an environment of Python 3.12, the ROCm lock with vLLM's ROCm index as an extra
    index (hashes required, no deps, wheels only), the ROCm probe, then its own recipe tag.
    MUTATION: drop --extra-index-url; write the CUDA tag; make the venv with sys.executable
    - red."""
    _supported(monkeypatch)
    calls, records = [], []
    run = _fake_installing_run(home, calls, records=records, version=ROCM_VERSION)
    assert vs.install(run=run) == ""
    assert calls[0] == ["/usr/bin/python3.12", "-m", "venv", str(vs.venv_dir())]
    assert calls[1] == [str(vs.venv_python()), "-m", "pip", "install", "--require-hashes",
                        "--no-deps", "--only-binary=:all:", "--extra-index-url",
                        "https://wheels.vllm.ai/rocm/0.32.0/rocm723", "-r", str(vs.LOCK_PATH_ROCM)]
    assert vs.LOCK_PATH_ROCM == Path(vs.__file__).with_name("vllm_requirements_rocm.lock")
    assert calls[2][:2] == [str(vs.venv_python()), "-c"] and calls[2][2] == vs._PROBE_ROCM
    assert len(calls) == 3 and records == [False, False, False]
    assert vs.recipe_path().read_bytes() == b"vllmrocm-1-a69536"
    assert vs.installed_version() == "0.32.0+rocm723" and vs.is_current()
    calls.clear()
    assert vs.install(run=run) == "" and calls == [], "an installed ROCm build installs nothing"


def test_rocm_install_says_its_size_and_the_venv_package(config, monkeypatch, home, rocm):
    _supported(monkeypatch)
    said = []
    run = _fake_installing_run(home, [], version=ROCM_VERSION)
    assert vs.install(log=said.append, run=run) == ""
    assert "Installing vLLM 0.32.0 for ROCm 7.2.3 (at least 3.3 GB to download)..." in said

    def no_venv(argv, env=None, **kw):
        return subprocess.CompletedProcess(argv, 1)
    vs.uninstall()
    reason = vs.install(run=no_venv)
    assert "Creating the environment failed (exit 1)" in reason and "python3.12-venv" in reason
    assert not vs.venv_dir().exists()


@pytest.mark.parametrize("build, other_version, other_tag", [
    (vs.ROCM, vs.PIN, "vllmenv-1-18dfb7"),
    (vs.CUDA, ROCM_VERSION, "vllmrocm-1-a69536"),
])
def test_a_cuda_environment_is_rebuilt_on_rocm(config, monkeypatch, home, build, other_version,
                                               other_tag):
    """The other build's environment, complete with its own record, is not current on this
    machine (a GPU that changed vendor): the install removes it and runs every step.
    MUTATION: recipe_current accepting either tag - red."""
    monkeypatch.setattr(vs, "backend", lambda: build)
    monkeypatch.setattr(vs, "rocm_python", lambda: "/usr/bin/python3.12")
    _supported(monkeypatch)
    _environment(other_version, other_tag)
    assert vs.is_installed() and not vs.is_current() and le.needs_install()
    # Even at this build's version, the other build's record does not pass.
    vs.uninstall()
    _environment(vs.expected_version(build), other_tag)
    assert not vs.recipe_current() and not vs.is_current()
    calls = []
    run = _fake_installing_run(home, calls, version=vs.expected_version(build))
    assert vs.install(run=run) == ""
    assert len(calls) == 3 and vs.is_current()
    assert vs.recipe_path().read_bytes().decode() == vs.recipe_format(build)


@pytest.mark.parametrize("build, other_version, other_tag", [
    (vs.ROCM, vs.PIN, "vllmenv-1-18dfb7"),
    (vs.CUDA, ROCM_VERSION, "vllmrocm-1-a69536"),
])
def test_start_refuses_the_other_backends_environment(config, monkeypatch, home, build,
                                                      other_version, other_tag):
    from vaf.cli.ui import UI
    monkeypatch.setattr(vs, "backend", lambda: build)
    _environment(other_version, other_tag)
    said = []
    monkeypatch.setattr(UI, "error", staticmethod(lambda text, *a, **k: said.append(text)))
    spawned = []

    def popen(*a, **k):
        spawned.append(a)
        raise RuntimeError("launched")
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is False
    assert spawned == [] and said and "`vaf engine vllm`" in said[-1]


def test_the_rocm_probe_maps_its_causes(tmp_path):
    """The ROCm probe really exits 3 when torch sees no GPU, 4 when vLLM's ROCm extension does
    not load, 0 when both do (stand-in packages). MUTATION: probe importing only
    _C_stable_libtorch - red."""
    import textwrap
    for gpu, rocm_ok, expected in [(False, True, 3), (True, False, 4), (True, True, 0)]:
        pkgs = tmp_path / f"p{int(gpu)}{int(rocm_ok)}"
        (pkgs / "torch").mkdir(parents=True)
        (pkgs / "torch" / "__init__.py").write_text(textwrap.dedent(f"""
            class cuda:
                @staticmethod
                def is_available():
                    return {gpu}
        """), encoding="utf-8")
        (pkgs / "vllm").mkdir()
        (pkgs / "vllm" / "__init__.py").write_text("", encoding="utf-8")
        (pkgs / "vllm" / "_C_stable_libtorch.py").write_text("", encoding="utf-8")
        (pkgs / "vllm" / "_rocm_C.py").write_text(
            "" if rocm_ok else "raise ImportError('libamdhip64.so.7: cannot open shared object file')",
            encoding="utf-8")
        out = subprocess.run([sys.executable, "-c", vs._PROBE_ROCM], capture_output=True,
                             text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(pkgs)})
        assert out.returncode == expected, (gpu, rocm_ok, out.stderr)
        if expected == 4:
            assert "libamdhip64.so.7" in out.stderr


@pytest.mark.parametrize("probe_code, cause", [(3, "no_rocm"), (4, "libs"), (1, "libs")])
def test_a_failed_rocm_probe_names_its_cause(config, monkeypatch, home, rocm, probe_code, cause):
    """MUTATION: map exit 3 to no_cuda - red."""
    _supported(monkeypatch)
    outcome = {}
    run = _fake_installing_run(home, [], probe_code=probe_code, version=ROCM_VERSION)
    reason = vs.install(run=run, outcome=outcome)
    assert outcome == {"probe": cause}
    assert "CUDA libraries" not in reason and "NVIDIA" not in reason
    if cause == "no_rocm":
        for word in ("ROCm 7.2 or newer", "render and video groups", "HIP_VISIBLE_DEVICES",
                     "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
            assert word in reason, word
    else:
        assert "ROCm libraries" in reason and "Run `vaf engine vllm` again" in reason
    assert not vs.recipe_path().exists() and not vs.is_current()


def test_rocm_fit_reads_amd_memory(monkeypatch, tmp_path):
    """On ROCm the fit reads the amdgpu driver's figures; nvidia-smi is never asked.
    MUTATION: _gpu_memory without the dispatch - red."""
    from vaf.core import gpu_detection as gd
    monkeypatch.setattr(vs, "backend", lambda: vs.ROCM)

    def no_nvidia(*a, **k):
        raise AssertionError("nvidia-smi was asked on ROCm")
    monkeypatch.setattr(gd, "nvidia_free_vram_mb", no_nvidia)
    monkeypatch.setattr(gd, "detect_nvidia_gpu", no_nvidia)
    monkeypatch.setattr(vs.ModelShape, "from_dir", classmethod(lambda cls, d: SHAPE_4B))
    monkeypatch.setattr(gd, "amd_vram_mb", lambda: (20000, 24560))
    result = vs.plan_fit(str(tmp_path), 32768)
    assert result.ok and result.total_mb == 24560 and result.free_mb == 20000
    monkeypatch.setattr(gd, "amd_vram_mb", lambda: (0, 0))
    result = vs.plan_fit(str(tmp_path), 32768)
    assert (result.ok, result.code) == (False, "no_gpu_memory")
    assert result.reason == "The amdgpu driver does not report the GPU's memory (/sys/class/drm)."


def test_the_backend_follows_the_gpu(monkeypatch):
    """NVIDIA first (the primary GPU's order), AMD then; kept once a GPU was found, not kept
    without one. MUTATION: memoize the no-GPU answer - red."""
    from vaf.core import gpu_detection as gd
    monkeypatch.undo()       # lifts the autouse cuda pin for this test
    monkeypatch.setattr(vs, "_backend", None)
    found = {"nvidia": None, "amd": None}
    monkeypatch.setattr(gd, "detect_nvidia_gpu", lambda: found["nvidia"])
    monkeypatch.setattr(gd, "detect_amd_gpu", lambda: found["amd"])
    assert vs.backend() == vs.CUDA and vs._backend is None
    found["amd"] = object()
    assert vs.backend() == vs.ROCM and vs._backend == vs.ROCM
    found["amd"] = None
    assert vs.backend() == vs.ROCM, "kept for the process once found"
    monkeypatch.setattr(vs, "_backend", None)
    found["nvidia"], found["amd"] = object(), object()
    assert vs.backend() == vs.CUDA
    assert vs.download_gb_at_least(vs.ROCM) == 3.3 and vs.download_gb_at_least(vs.CUDA) == 2.8
    assert vs.ROCM_INDEX.endswith(f"/{vs.PIN}/{vs.ROCM_VARIANT}")


def test_a_rocm_off_the_wheels_runpath_reaches_probe_and_server(config, monkeypatch, home,
                                                                 rocm, tmp_path):
    """A ROCm under $ROCM_PATH (or another /opt/rocm-<version>) is not on the wheels' RUNPATH
    (/opt/rocm/lib in torch): every install step, the probe and the server get its lib
    directory first on LD_LIBRARY_PATH, on the ROCm build only. MUTATION: install without
    with_rocm_library - red."""
    custom = tmp_path / "rocm-7.2.1"
    (custom / "lib").mkdir(parents=True)
    (custom / "lib" / "libamdhip64.so.7").write_bytes(b"")
    monkeypatch.setenv("ROCM_PATH", str(custom))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/local/lib")
    _supported(monkeypatch)
    envs = []
    run = _fake_installing_run(home, [], envs=envs, version=ROCM_VERSION)
    assert vs.install(run=run) == ""
    lib = str(custom / "lib")
    assert len(envs) == 3
    for env in envs:
        assert env["LD_LIBRARY_PATH"] == os.pathsep.join([lib, "/usr/local/lib"]), env.get("LD_LIBRARY_PATH")
    assert vs.child_env()["LD_LIBRARY_PATH"].split(os.pathsep)[0] == lib
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    assert vs.child_env()["LD_LIBRARY_PATH"] == "/usr/local/lib", "the CUDA build is untouched"


# ── the attention plan, the int8 fit, the first start ───────────────────────────────


@pytest.mark.parametrize("cap, first, fallback", [
    ((7, 5), ("int8_per_token_head", "TRITON_ATTN"), ("auto", "TRITON_ATTN")),
    ((8, 6), ("int8_per_token_head", "TRITON_ATTN"), ("auto", "FLASH_ATTN")),
    ((8, 9), ("fp8", "TRITON_ATTN"), ("auto", "FLASH_ATTN")),
    ((9, 0), ("fp8", "FLASH_ATTN"), ("auto", "FLASH_ATTN")),
    ((12, 0), ("fp8", "TRITON_ATTN"), ("auto", "FLASH_ATTN")),
])
def test_attention_plan_without_flashinfer_build(cap, first, fallback):
    """Without a CUDA toolkit FlashInfer cannot build its kernels, and vLLM still picked it
    (measured on an RTX 3080: fp8 KV fell to FlashInfer, which died on "Could not find
    nvcc"). The plan names a backend that needs no build, per compute capability; Triton's
    fp8 needs 8.9 (measured refusal on 8.6), and the FlashInfer sampler is off."""
    plan = vs.attention_plan(cap, buildable=False)
    assert list(zip(plan.kv_dtypes, plan.backends)) == [first, fallback]
    assert plan.sampler_off is True
    if cap < (8, 9):
        assert not any(d.startswith("fp8") and be == vs.TRITON_ATTN
                       for d, be in zip(plan.kv_dtypes, plan.backends))


def test_the_plan_reaches_the_command_and_the_fallback(config, monkeypatch, home):
    """8.6 without FlashInfer: the launch carries int8_per_token_head with TRITON_ATTN, the
    fallback auto with FLASH_ATTN, and the server's environment turns the sampler off."""
    _ready_to_launch(monkeypatch, home, free_mb=20000, total_mb=24576)
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: False)
    spawned = []
    procs = [_Proc(dies=True), _Proc()]

    def popen(argv, **kw):
        spawned.append((argv, kw["env"]))
        return procs[len(spawned) - 1]
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "terminate_process_group", staticmethod(lambda pgid, grace=1.0: None))
    monkeypatch.setattr(vs, "health_status",
                        lambda port=8080, timeout=1.0: 200 if len(spawned) >= 2 else None)
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is True

    def value(argv, flag):
        return argv[argv.index(flag) + 1]
    assert [(value(a, "--kv-cache-dtype"), value(a, "--attention-backend")) for a, _ in spawned] \
        == [("int8_per_token_head", "TRITON_ATTN"), ("auto", "FLASH_ATTN")]
    assert all(env.get("VLLM_USE_FLASHINFER_SAMPLER") == "0" for _, env in spawned)


def test_child_env_turns_flashinfer_sampler_off(monkeypatch):
    """FlashInfer's top-p/top-k sampler JIT-compiles on the first sampled request (measured:
    the server died in its warmup with "Could not find nvcc"), on every compute capability:
    off whenever FlashInfer cannot build, and VAF's own VLLM_ value of it never passes."""
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: False)
    for cap in ((8, 6), (9, 0), (12, 0)):
        monkeypatch.setattr(vs, "compute_capability", lambda cap=cap: cap)
        env = vs.child_env({"PATH": "/usr/bin", "VLLM_USE_FLASHINFER_SAMPLER": "1"})
        assert env["VLLM_USE_FLASHINFER_SAMPLER"] == "0", cap
    monkeypatch.setattr(vs, "backend", lambda: vs.ROCM)
    assert "VLLM_USE_FLASHINFER_SAMPLER" not in vs.child_env({"PATH": "/usr/bin"}), \
        "the ROCm launch stays as it was"


def _tool(directory: Path, name: str) -> None:
    """A file shutil.which finds as ``name``; never started (FlashInfer's check only looks)."""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / (name + (".exe" if os.name == "nt" else ""))
    exe.write_bytes(b"")
    exe.chmod(0o755)


def test_buildable_flashinfer_keeps_the_old_command(config, monkeypatch, home, tmp_path):
    """Where FlashInfer can build (nvcc and ninja on the server's PATH, or flashinfer_cubin
    in the environment) nothing changes: fp8 first, vLLM's own backend, no sampler switch."""
    monkeypatch.setattr(vs, "flashinfer_buildable", REAL_BUILDABLE)
    cuda = tmp_path / "cuda" / "bin"
    tools = tmp_path / "tools"
    _tool(cuda, "nvcc")
    _tool(tools, "ninja")
    path = os.pathsep.join([str(cuda), str(tools)])
    assert vs.flashinfer_buildable({"PATH": path}) is True
    assert vs.flashinfer_buildable({"PATH": str(tools)}) is False, "no nvcc"
    assert vs.flashinfer_buildable({"PATH": str(cuda)}) is False, "no ninja"
    assert vs.flashinfer_buildable({"PATH": str(tools),
                                    "CUDA_HOME": str(tmp_path / "cuda")}) is True
    site = vs.venv_dir() / "lib" / "python3.13" / "site-packages" / "flashinfer_cubin"
    site.mkdir(parents=True)
    assert vs.flashinfer_buildable({"PATH": ""}) is True, "the prebuilt kernels need no JIT"
    import shutil as _shutil
    _shutil.rmtree(site)
    monkeypatch.setenv("PATH", path)
    _ready_to_launch(monkeypatch, home)
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw["env"]))
        return _Proc()
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: 200 if spawned else None)
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen) is True
    argv, env = spawned[0]
    assert argv[argv.index("--kv-cache-dtype") + 1] == "fp8"
    assert "--attention-backend" not in argv
    assert "VLLM_USE_FLASHINFER_SAMPLER" not in env


def test_int8_per_token_head_fit_counts_scales():
    """int8_per_token_head stores one byte per element plus a float32 scale per token, head
    and side: Qwen3-4B (36 layers, 8 KV heads, head dim 128) at 32768 tokens is 2376 MiB."""
    result = vs.fit(SHAPE_4B, 32768, 20000, 24576, kv_dtype=vs.INT8_KV)
    assert result.kv_mb == 2376, result.kv_mb
    assert vs.fit(SHAPE_4B, 32768, 20000, 24576, kv_dtype="fp8").kv_mb == 2304


def test_fit_shrinks_context_to_fit():
    """The clamped context (40960) does not fit, a smaller one above the floor does: the
    largest multiple of 1024 that fits is taken, with a note."""
    full = vs.fit(SHAPE_4B, 40960, 10000, 10240, kv_dtype=vs.INT8_KV, utilization=0.95)
    assert full.ok and full.max_model_len == 40960 and "reduced" not in full.note
    result = vs.fit(SHAPE_4B, 40960, 10000, 10240, kv_dtype=vs.INT8_KV, utilization=0.70)
    assert result.ok, result.reason
    assert vs.MIN_CONTEXT <= result.max_model_len < 40960
    assert result.max_model_len % vs.CONTEXT_STEP == 0
    assert str(result.max_model_len) in result.note and "reduced" in result.note
    assert result.need_mb <= result.budget_mb
    # One step more would not fit.
    over = vs.fit(SHAPE_4B, result.max_model_len + vs.CONTEXT_STEP, 10000, 10240,
                  kv_dtype=vs.INT8_KV, utilization=0.70)
    assert over.max_model_len == result.max_model_len


def test_fit_refuses_below_min_context():
    """The measured case: 6222 of 10240 MiB free (other programs held the rest). int8 at the
    32768 floor does not fit either: refused, never launched below the floor."""
    result = vs.fit(SHAPE_4B, 40960, 6222, 10240, kv_dtype=vs.INT8_KV)
    assert not result.ok and result.code == "too_big"
    assert result.max_model_len == vs.MIN_CONTEXT
    assert result.other_mb == 10240 - 6222


def test_too_big_reason_names_other_programs():
    """The refusal names the free memory and what other programs hold, and the levers that
    help: closing them, or llama.cpp. Never a smaller context; a smaller model only for a
    model larger than the smallest default."""
    small = vs.fit(SHAPE_4B, 40960, 6222, 10240, kv_dtype=vs.INT8_KV, model=le.DEFAULT_MODEL_SMALL)
    assert "6222 MiB are free" in small.reason and "other programs hold 4018 MiB" in small.reason
    assert "Close programs that use the GPU" in small.reason and "llama.cpp" in small.reason
    assert "context" not in small.reason.split("),", 1)[1], small.reason
    assert "smaller model" not in small.reason and small.smaller_model is False
    large = vs.fit(SHAPE_4B, 40960, 6222, 10240, kv_dtype=vs.INT8_KV, model=le.DEFAULT_MODEL_LARGE)
    assert "smaller model" in large.reason and large.smaller_model is True
    data = le.fit_dict(large)
    assert data["other_mb"] == 4018 and data["smaller_model"] is True


class _Answer:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"choices": [{"message": {"content": "OK"}}]}

    def json(self):
        return self._body


def test_first_start_sends_a_sampled_request_and_stops(config, monkeypatch, home):
    """The first start of a switch runs the real start, one chat completion WITH top_p and
    top_k (the sampler path that killed a server on its first chat), and stops the server
    again. A server that dies on the request is a failure with the log's cause."""
    _ready_to_launch(monkeypatch, home)
    spawned, posted, stopped = [], [], []
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: 200 if spawned else None)
    monkeypatch.setattr(vs, "running_pid", lambda: 900 if spawned else None)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: stopped.append(1) or "")

    def popen(argv, **kw):
        spawned.append(argv)
        return _Proc()

    def post(url, json=None, timeout=None):
        posted.append((url, json))
        return _Answer()
    result = vs.first_start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen, post=post)
    assert result.ok, result
    url, body = posted[0]
    assert url.endswith("/v1/chat/completions") and body["model"] == "Qwen/Qwen3-4B-AWQ"
    assert body["top_p"] == 0.8 and body["top_k"] == 20 and body["max_tokens"] == 8
    assert stopped == [1], "the server is stopped after the first start"
    # The engine dies on the sampled request: the cause comes from the log, and it is
    # remembered for this signature.
    spawned.clear()
    stopped.clear()
    vs.log_path().parent.mkdir(parents=True, exist_ok=True)

    def dying_post(url, json=None, timeout=None):
        vs.log_path().write_bytes(b"RuntimeError: Could not find nvcc and default cuda_home\n")
        raise ConnectionError("closed")
    result = vs.first_start("Qwen/Qwen3-4B-AWQ", 32768, popen=popen, post=dying_post)
    assert not result.ok and result.cause == "kernels", result
    assert stopped == [1]
    assert vs.start_failure() is not None and vs.start_failure().cause == "kernels"


def test_start_log_causes():
    """A start that died is classified from its log; memory before kernels, because the
    "kernels are disabled" warning heads every log without a toolkit."""
    head = "FlashInfer kernels are disabled: flashinfer-cubin is not installed and nvcc\n"
    assert vs.classify_start_log(head + "ValueError: ... (2.32 GiB KV cache is needed, which is "
                                 "larger than the available KV cache memory") == "memory"
    assert vs.classify_start_log(head + "RuntimeError: Could not find nvcc") == "kernels"
    assert vs.classify_start_log(head + "Segmentation fault") == "other"


def test_no_tracked_file_says_flashinfer_builds_at_the_first_start():
    """Measured untrue without a CUDA toolkit: FlashInfer cannot build there, and the server
    died. No doc or source may promise it again."""
    root = Path(__file__).resolve().parent.parent
    for rel in ("docs/llm/VLLM_ENGINE.md", "vaf/core/local_engine.py", "vaf/core/vllm_server.py"):
        text = " ".join((root / rel).read_bytes().decode("utf-8").split())
        assert "FlashInfer builds the kernels it needs at the first start" not in text, rel
        assert "FlashInfer builds the kernels it needs at" not in text, rel
