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
    def _run(cmd, *a, **kw):
        # simulate `docker cp <container>:<src> <dest>` by creating the dest file
        Path(cmd[-1]).write_bytes(b"\x89PNG-fake")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return _run


def test_relative_path_exports_into_workspace(export_env, monkeypatch):
    dest, notified = export_env
    monkeypatch.setattr("vaf.core.containers.docker", _fake_cp_success(dest))
    notes = PythonSandboxTool()._export_artifacts(
        ["chart.png"], "/tmp/vaf_abc", container="vaf-env-ab12cd34ef56-scratch", session_id="chat1")
    assert any("Exported to chat workspace" in n for n in notes), notes
    assert (dest / "chart.png").exists()
    assert notified and notified[0][0] == "chat1"


def test_paths_outside_scratch_are_refused(export_env, monkeypatch):
    dest, _ = export_env
    called = []
    monkeypatch.setattr("vaf.core.containers.docker",
                        lambda *a, **k: called.append(1))
    notes = PythonSandboxTool()._export_artifacts(
        ["/etc/passwd", "/root/x.png"], "/tmp/vaf_abc", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    assert all("export skipped" in n for n in notes), notes
    assert not called, "docker cp must never run for non-scratch paths"


def test_only_files_inside_this_runs_workdir_are_exported(export_env, monkeypatch):
    """The persistent sandbox is shared, so another user's run sits next to this one
    under /tmp. Any /tmp or /workspace path used to be accepted, which let one run copy
    another run's files out. MUTATION: accept any /tmp or /workspace path again - red."""
    dest, _ = export_env
    copied = []

    def _run(cmd, *a, **kw):
        copied.append(cmd[-2])
        Path(cmd[-1]).write_bytes(b"data")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("vaf.core.containers.docker", _run)
    notes = PythonSandboxTool()._export_artifacts(
        ["/tmp/vaf_otheruser_1234/secret.txt", "/workspace/testrun_x/a.txt",
         "../vaf_otheruser_1234/secret.txt", "/tmp/vaf_abc/../vaf_x/b.txt"],
        "/tmp/vaf_abc", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    assert all("export skipped" in n for n in notes), notes
    assert copied == [], "docker cp must never run for a path outside this run's dir"

    notes = PythonSandboxTool()._export_artifacts(
        ["/tmp/vaf_abc/out/chart.png", "sub/../plot.png"], "/tmp/vaf_abc",
        container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    assert [c.split(":", 1)[1] for c in copied] == ["/tmp/vaf_abc/out/chart.png",
                                                   "/tmp/vaf_abc/plot.png"]
    assert all("Exported" in n for n in notes), notes


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_link_or_directory_from_docker_cp_is_removed_not_delivered(export_env, monkeypatch,
                                                                     tmp_path, kind):
    """docker cp copies a symbolic link AS a link: one the code planted would land in
    the chat workspace pointing at a host file. MUTATION: drop _refuse_copied_non_file -
    the link resolves to a real file, isfile() is True and it is reported as exported."""
    dest, notified = export_env
    host_secret = tmp_path / "host_secret"
    host_secret.write_text("private")

    def _run(cmd, *a, **kw):
        target = Path(cmd[-1])
        if kind == "symlink":
            target.symlink_to(host_secret)
        else:
            target.mkdir()
            (target / "inner.txt").write_text("x")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("vaf.core.containers.docker", _run)
    notes = PythonSandboxTool()._export_artifacts(
        ["thing"], "/tmp/vaf_abc", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    assert notes and "export refused" in notes[0], notes
    assert not (dest / "thing").exists() and not (dest / "thing").is_symlink()
    assert notified == []
    assert host_secret.read_text() == "private"


def test_an_export_never_lands_on_a_folder_the_person_has(export_env, monkeypatch):
    """docker cp copies INTO an existing folder, and the non-file check then removed that
    folder. MUTATION: drop the pre-check - red: the person's folder was deleted."""
    dest, _ = export_env
    mine = dest / "report"
    mine.mkdir()
    (mine / "notes.txt").write_text("keep me")
    calls = []

    def _cp(cmd, *a, **kw):
        calls.append(cmd)
        Path(cmd[-1], "report").mkdir(exist_ok=True)       # what docker cp into a folder does
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("vaf.core.containers.docker", _cp)
    notes = PythonSandboxTool()._export_artifacts(
        ["report"], "/tmp/vaf_abc", container="vaf-env-ab12cd34ef56-scratch", session_id="chat1")
    assert (mine / "notes.txt").read_text() == "keep me" and calls == []
    assert any("already exists" in n for n in notes), notes


def test_cp_failure_yields_note_not_crash(export_env, monkeypatch):
    monkeypatch.setattr("vaf.core.containers.docker",
                        lambda cmd, *a, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="no such file"))
    notes = PythonSandboxTool()._export_artifacts(
        ["missing.png"], "/tmp/vaf_abc", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    assert notes and "export failed" in notes[0]


def test_export_caps_at_five_files(export_env, monkeypatch):
    dest, _ = export_env
    monkeypatch.setattr("vaf.core.containers.docker", _fake_cp_success(dest))
    notes = PythonSandboxTool()._export_artifacts(
        [f"f{i}.png" for i in range(9)], "/tmp/w", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    assert len([n for n in notes if "Exported" in n]) == 5


def test_basename_is_sanitized(export_env, monkeypatch):
    dest, _ = export_env
    monkeypatch.setattr("vaf.core.containers.docker", _fake_cp_success(dest))
    PythonSandboxTool()._export_artifacts(
        ["evil name$(rm).png"], "/tmp/w", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
    names = [p.name for p in dest.iterdir()]
    assert names and all(re.fullmatch(r"[A-Za-z0-9._-]+", n) for n in names), names


def test_export_never_raises(monkeypatch):
    monkeypatch.setattr(session_mod, "resolve_agent_output_dir",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    notes = PythonSandboxTool()._export_artifacts(
        ["x.png"], "/tmp/w", container="vaf-env-ab12cd34ef56-scratch", session_id="c")
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
