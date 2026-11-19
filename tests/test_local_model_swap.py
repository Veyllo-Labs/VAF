# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Model-aware single-server lifecycle (vaf/core/backend.py).

The dedicated voice-model lane swaps the ONE llama server between the voice
GGUF and the main GGUF. These tests pin the safety contract:
- `_loaded_model_matches` compares basenames and treats an unreadable id as
  MATCH (legacy reuse behavior preserved),
- `ensure_local_model` is a no-op when the server already holds the model,
  never touches ServerManager then, and requests a restart (with the
  provider gate skipped) when the model differs.
"""
import types

import pytest

import vaf.core.backend as backend


class _Resp:
    def __init__(self, status=200, model_id=""):
        self.status_code = status
        self._id = model_id

    def json(self):
        return {"data": [{"id": self._id}]} if self._id else {"data": [{}]}


def _fake_models_endpoint(monkeypatch, model_id, status=200):
    def _get(url, timeout=None):
        assert "/v1/models" in url
        return _Resp(status, model_id)
    monkeypatch.setattr(backend.requests, "get", _get)


def test_loaded_model_matches_by_basename_case_insensitive(monkeypatch):
    _fake_models_endpoint(monkeypatch, "/x/models/Gemma-Test.GGUF")
    assert backend._loaded_model_matches("/other/dir/gemma-test.gguf") is True
    assert backend._loaded_model_matches("/other/dir/qwen-main.gguf") is False


def test_unreadable_model_id_counts_as_match(monkeypatch):
    # Legacy behavior: a healthy server whose id we cannot read is reused.
    def _boom(url, timeout=None):
        raise ConnectionError("down")
    monkeypatch.setattr(backend.requests, "get", _boom)
    assert backend._loaded_model_matches("/x/whatever.gguf") is True

    _fake_models_endpoint(monkeypatch, "", status=200)  # no id in the payload
    assert backend._loaded_model_matches("/x/whatever.gguf") is True


def test_ensure_local_model_noop_when_already_loaded(monkeypatch, tmp_path):
    model = tmp_path / "voice.gguf"
    model.write_bytes(b"gguf")
    _fake_models_endpoint(monkeypatch, str(model))

    class _Explode:
        def __init__(self, *a, **kw):
            raise AssertionError("ServerManager must not be touched on a match")
    monkeypatch.setattr(backend, "ServerManager", _Explode)
    assert backend.ensure_local_model(str(model)) is True


def test_ensure_local_model_swaps_on_mismatch(monkeypatch, tmp_path):
    model = tmp_path / "voice.gguf"
    model.write_bytes(b"gguf")
    _fake_models_endpoint(monkeypatch, "/x/models/qwen-main.gguf")
    seen = {}

    class _Mgr:
        def __init__(self, skip_cleanup=False):
            seen["skip_cleanup"] = skip_cleanup

        def start_server(self, model_path, skip_provider_gate=False, **kw):
            seen["model_path"] = model_path
            seen["skip_provider_gate"] = skip_provider_gate
            return True

    monkeypatch.setattr(backend, "ServerManager", _Mgr)
    assert backend.ensure_local_model(str(model), reason="test",
                                      skip_provider_gate=True) is True
    assert seen == {"skip_cleanup": True, "model_path": str(model),
                    "skip_provider_gate": True}


def test_ensure_local_model_missing_file_fails_closed(tmp_path):
    assert backend.ensure_local_model(str(tmp_path / "missing.gguf")) is False
    assert backend.ensure_local_model("") is False


def test_voice_model_ref_default_is_gemma(monkeypatch):
    from vaf.core.config import Config
    from vaf.core import voice_model as vm
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: ""))
    assert vm.voice_model_ref() == vm.DEFAULT_VOICE_MODEL
    assert "gemma-4-E4B" in vm.DEFAULT_VOICE_MODEL
    # The ref must be resolvable by the shared download entry point.
    repo, filename = backend._resolve_model_ref(vm.DEFAULT_VOICE_MODEL)
    assert repo == "bartowski/google_gemma-4-E4B-it-GGUF"
    assert filename == "google_gemma-4-E4B-it-Q4_K_M.gguf"


# ---------------------------------------------------------------------------
# Local vision (vision_provider=local, mmproj launch)
# ---------------------------------------------------------------------------

def _cfg_map(monkeypatch, mapping):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None: mapping.get(k, d)))


def test_mmproj_off_when_vision_is_not_local(monkeypatch, tmp_path):
    """A cloud vision provider means the local server stays blind - that
    provider sees the images itself.

    This test used to assert the same for an EMPTY setting, which was the
    defect: empty means "follow the main agent if it can see"
    (vision_infer.select_vision_backend), and every other reader honoured
    that while the server start read the raw key. A local vision model
    therefore launched without its projector and answered "image input is
    not supported" with nothing in the settings looking wrong.
    """
    import vaf.core.vision_infer as vi

    model = tmp_path / "qwen3.5-4b-test.gguf"
    model.write_bytes(b"g")

    monkeypatch.setattr(vi, "select_vision_backend", lambda: ("veyllo", "veyllo-chat"))
    assert backend.resolve_mmproj_for(str(model)) == ""

    # And when nothing can see at all.
    monkeypatch.setattr(vi, "select_vision_backend", lambda: (None, None))
    assert backend.resolve_mmproj_for(str(model)) == ""


def test_mmproj_found_on_disk_no_download(monkeypatch, tmp_path):
    _cfg_map(monkeypatch, {"vision_provider": "local"})
    model = tmp_path / "qwen3.5-4b-test.gguf"
    model.write_bytes(b"g")
    # Auto path uses a PER-MODEL local name: Qwen and Gemma repos both ship
    # "mmproj-F16.gguf", a shared name would pair the wrong projector.
    mmproj = tmp_path / "mmproj-qwen3.5-4b-test.gguf"
    mmproj.write_bytes(b"m")
    assert backend.resolve_mmproj_for(str(model)) == str(mmproj)


def test_mmproj_for_dedicated_voice_model_uses_voice_ref_repo(monkeypatch, tmp_path):
    """Vision during a live call: the voice GGUF's repo (from the voice ref)
    resolves its own projector even though it is not in _KNOWN_MODEL_REPOS."""
    _cfg_map(monkeypatch, {"vision_provider": "local",
                           "voice_agent_model": "owner/gemma-repo/gemma-voice.gguf"})
    model = tmp_path / "gemma-voice.gguf"
    model.write_bytes(b"g")
    mmproj = tmp_path / "mmproj-gemma-voice.gguf"
    mmproj.write_bytes(b"m")
    assert backend.resolve_mmproj_for(str(model)) == str(mmproj)


def test_mmproj_unknown_model_fails_open_empty(monkeypatch, tmp_path):
    # No known repo, nothing on disk: vision quietly off, text server unharmed.
    _cfg_map(monkeypatch, {"vision_provider": "local"})
    model = tmp_path / "totally-custom.gguf"
    model.write_bytes(b"g")
    assert backend.resolve_mmproj_for(str(model)) == ""


def test_mmproj_explicit_ref_wins(monkeypatch, tmp_path):
    _cfg_map(monkeypatch, {"vision_provider": "local",
                           "vision_local_mmproj": "owner/repo/mmproj-custom.gguf"})
    model = tmp_path / "whatever.gguf"
    model.write_bytes(b"g")
    mmproj = tmp_path / "mmproj-custom.gguf"
    mmproj.write_bytes(b"m")
    assert backend.resolve_mmproj_for(str(model)) == str(mmproj)


def test_vision_local_mmproj_is_admin_only():
    from vaf.core.config import Config
    assert Config.is_global_config_key("vision_local_mmproj")


def test_stop_server_kills_swap_owned_server_too(monkeypatch, tmp_path):
    """Tray Quit regression: after a model swap the live server belongs to a
    throwaway manager; THIS manager's self.process is a stale handle. The old
    elif skipped the pid file and the real server survived Quit - both the
    own handle AND the pid-file process must be stopped on force_external."""
    mgr = backend.ServerManager(skip_cleanup=True)
    mgr.pid_file = str(tmp_path / "server.pid")
    (tmp_path / "server.pid").write_text("222")

    class _StaleProc:
        pid = 111
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0
    mgr.process = _StaleProc()

    killed = []
    monkeypatch.setattr(mgr, "_is_process_running", lambda pid: pid == 222)
    monkeypatch.setattr(mgr, "_looks_like_llama_server", lambda pid: pid == 222)
    # Force the Unix kill branch: on Windows stop_server uses taskkill via
    # subprocess and the os.kill recorder below never fires (CI failure on
    # the windows-latest runner).
    monkeypatch.setattr(mgr, "system", "Linux")
    monkeypatch.setattr(backend.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    mgr.stop_server(force_external=True)
    assert (222, 9) in killed          # the swap-owned live server dies
    assert not (tmp_path / "server.pid").exists()


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_a_reaped_child_whose_pid_was_reused_is_not_killed(monkeypatch, tmp_path, system):
    """The own child is reaped by its wait, and its pid may already belong to another
    process; a pid file can name any process after a crash or a reboot. Only a pid verified
    as a llama server is killed, on the taskkill branch too. The pid file here names a real
    llama server (333), so the recorders are shown to see the kill they guard against.
    MUTATION: gate the kill on _is_process_running again (red: 111 killed)."""
    import subprocess

    mgr = backend.ServerManager(skip_cleanup=True)
    mgr.pid_file = str(tmp_path / "server.pid")

    class _ReapedProc:
        pid = 111
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    killed, ran = [], []
    monkeypatch.setattr(mgr, "system", system)
    monkeypatch.setattr(mgr, "_job_handle", None, raising=False)
    monkeypatch.setattr(mgr, "_is_process_running", lambda pid: True)
    monkeypatch.setattr(mgr, "_looks_like_llama_server", lambda pid: pid == 333)
    monkeypatch.setattr(backend.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(backend.subprocess, "run", lambda cmd, **k: ran.append(list(cmd)))

    def targets():
        return killed + [int(cmd[-1]) for cmd in ran if cmd[:1] == ["taskkill"]]

    mgr.process = _ReapedProc()
    mgr.stop_server(force_external=True, include_vllm=False)
    assert 111 not in targets(), "a reused pid of the reaped child was killed"

    (tmp_path / "server.pid").write_text("333")
    mgr.process = _ReapedProc()
    mgr.stop_server(force_external=True, include_vllm=False)
    assert targets() == [333], targets()
    assert not (tmp_path / "server.pid").exists()


_IDENTITY_CASES = [
    # (process name, command line, is a llama server)
    ("llama-server", ["/home/user/.vaf/bin/llama-cpp/llama-server", "-m", "model.gguf"], True),
    ("llama-server", [], True),                                          # command line unreadable
    ("python", ["llama-server", "-c", "pass"], True),                    # argv[0] is the program
    ("llama-server.exe", ["C:\\Users\\Alice\\vaf\\bin\\llama-cpp\\llama-server.exe", "-m", "m.gguf"], True),
    ("LLAMA-SERVER.EXE", ["C:\\VAF\\BIN\\LLAMA-SERVER.EXE"], True),
    ("python3", ["python3", "-c", "import time; time.sleep(60)", "llama-server"], False),
    ("vim", ["vim", "/home/user/.vaf/bin/llama-cpp/llama-server"], False),
    ("tail", ["tail", "-f", "/home/user/.vaf/logs/llama-server.log"], False),
    ("notepad.exe", ["C:\\Windows\\notepad.exe", "C:\\logs\\llama-server.exe"], False),
    ("code.exe", ["C:\\Program Files\\Code\\code.exe", "llama-server.exe"], False),
    ("llama-server-wrapper", ["/usr/local/bin/llama-server-wrapper"], False),
    ("bash", ["bash", "-c", "exec llama-server -m m.gguf"], False),
]


@pytest.mark.parametrize("name,argv,expected", _IDENTITY_CASES,
                         ids=[f"{c[0]}-{i}" for i, c in enumerate(_IDENTITY_CASES)])
def test_only_the_llama_server_program_passes_the_identity_check(monkeypatch, name, argv, expected):
    """A stale server.pid survives a reboot and can name any process; every kill by pid
    (stop_server, the orphan cleanup, the handover, the install) asks this check. It took any
    process whose name or command line contained the word, so an editor or a `tail` of the log
    passed. Linux and Windows shapes (the backslash paths) are compared on any host.
    MUTATIONS: the substring match back (red: the argument-only cases); drop argv[0] (red: the
    python stand-in); drop the lower() (red: the upper-case Windows name)."""
    psutil = pytest.importorskip("psutil")

    class _Proc:
        def __init__(self, pid):
            self.pid = pid
        def name(self):
            return name
        def cmdline(self):
            return list(argv)

    monkeypatch.setattr(psutil, "Process", _Proc)
    mgr = backend.ServerManager.__new__(backend.ServerManager)
    assert mgr._looks_like_llama_server(4242) is expected


def test_a_zombie_llama_server_no_longer_counts(monkeypatch):
    """A killed child of another process stays a zombie until it is reaped; it keeps its name
    but its command line cannot be read. It must read as gone, or the wait after a stop never
    ends. MUTATION: decide on the name alone (red)."""
    psutil = pytest.importorskip("psutil")

    class _Zombie:
        def __init__(self, pid):
            self.pid = pid
        def name(self):
            return "llama-server"
        def cmdline(self):
            raise psutil.ZombieProcess(self.pid)

    monkeypatch.setattr(psutil, "Process", _Zombie)
    mgr = backend.ServerManager.__new__(backend.ServerManager)
    assert mgr._looks_like_llama_server(4242) is False
