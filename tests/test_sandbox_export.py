# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""python_sandbox export_files: the sanctioned exit for binary artifacts (Fix 8).

The base64-through-context lane truncates anything beyond the model's output
budget (live incident: a ~400KB chart arrived as 2.5KB of corrupt PNG). Files
the code produced are now copied out of the container into the chat workspace
via docker cp BEFORE the per-exec workdir is removed; the model names container
scratch paths only - the destination is always the chat workspace.
"""
import re
import types
from pathlib import Path

import pytest

import vaf.core.session as session_mod
import vaf.tools.python_sandbox as ps_mod
from vaf.tools.python_sandbox import PythonSandboxTool


@pytest.fixture
def export_env(tmp_path, monkeypatch):
    dest = tmp_path / "workspace"
    dest.mkdir()
    monkeypatch.setattr(session_mod, "resolve_agent_output_dir",
                        lambda default, session_id=None: dest)
    notified = []
    import vaf.core.web_interface as wi_mod
    monkeypatch.setattr(wi_mod, "notify_file_created",
                        lambda sid, path, title=None: notified.append((sid, path)))
    return dest, notified


def _fake_cp_success(dest_dir):
    def _run(cmd, **kw):
        # simulate `docker cp <container>:<src> <dest>` by creating the dest file
        Path(cmd[-1]).write_bytes(b"\x89PNG-fake")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return _run


def test_relative_path_exports_into_workspace(export_env, monkeypatch):
    dest, notified = export_env
    monkeypatch.setattr(ps_mod.subprocess, "run", _fake_cp_success(dest))
    notes = PythonSandboxTool()._export_artifacts(
        ["chart.png"], "/tmp/vaf_abc", use_persistent=True, session_id="chat1")
    assert any("Exported to chat workspace" in n for n in notes), notes
    assert (dest / "chart.png").exists()
    assert notified and notified[0][0] == "chat1"


def test_paths_outside_scratch_are_refused(export_env, monkeypatch):
    dest, _ = export_env
    called = []
    monkeypatch.setattr(ps_mod.subprocess, "run",
                        lambda *a, **k: called.append(1))
    notes = PythonSandboxTool()._export_artifacts(
        ["/etc/passwd", "/root/x.png"], "/tmp/vaf_abc", use_persistent=True, session_id="c")
    assert all("export skipped" in n for n in notes), notes
    assert not called, "docker cp must never run for non-scratch paths"


def test_cp_failure_yields_note_not_crash(export_env, monkeypatch):
    monkeypatch.setattr(ps_mod.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="no such file"))
    notes = PythonSandboxTool()._export_artifacts(
        ["missing.png"], "/tmp/vaf_abc", use_persistent=True, session_id="c")
    assert notes and "export failed" in notes[0]


def test_export_caps_at_five_files(export_env, monkeypatch):
    dest, _ = export_env
    monkeypatch.setattr(ps_mod.subprocess, "run", _fake_cp_success(dest))
    notes = PythonSandboxTool()._export_artifacts(
        [f"f{i}.png" for i in range(9)], "/tmp/w", use_persistent=True, session_id="c")
    assert len([n for n in notes if "Exported" in n]) == 5


def test_basename_is_sanitized(export_env, monkeypatch):
    dest, _ = export_env
    monkeypatch.setattr(ps_mod.subprocess, "run", _fake_cp_success(dest))
    PythonSandboxTool()._export_artifacts(
        ["/tmp/evil name$(rm).png"], "/tmp/w", use_persistent=True, session_id="c")
    names = [p.name for p in dest.iterdir()]
    assert names and all(re.fullmatch(r"[A-Za-z0-9._-]+", n) for n in names), names


def test_export_never_raises(monkeypatch):
    monkeypatch.setattr(session_mod, "resolve_agent_output_dir",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    notes = PythonSandboxTool()._export_artifacts(
        ["x.png"], "/tmp/w", use_persistent=True, session_id="c")
    assert notes and "export failed" in notes[0]


# ── wiring guards ─────────────────────────────────────────────────────────────

def test_schema_declares_export_files():
    assert "export_files" in PythonSandboxTool.parameters["properties"]


def test_export_runs_before_workdir_cleanup():
    src = Path(ps_mod.__file__).read_text(encoding="utf-8")
    body = src[src.index("def run(self, **kwargs)"):]
    assert body.index("_export_artifacts(") < body.index('rm -rf {workdir}'), (
        "export must run BEFORE the per-exec workdir is removed - after cleanup "
        "the produced files are gone"
    )


def test_agent_injects_session_for_sandbox():
    import vaf.core.agent as agent_mod
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    m = re.search(r'if name in \("python_sandbox", "python_exec"\):(.{0,600})', src, re.S)
    assert m and '_session_id' in m.group(1), (
        "python_sandbox dispatch must inject the chat's session id for export_files"
    )
