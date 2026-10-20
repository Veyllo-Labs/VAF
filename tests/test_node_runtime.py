# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Node.js where the installers put it (vaf/core/node_runtime.py).

Measured on a fresh server install with no system Node: the systemd user unit does not read
the shell file the installer wrote the portable Node into, the service logged "npm not found
in PATH", and every page of the web UI answered 502.
"""
import os
import re
from pathlib import Path

import pytest

from vaf.core import node_runtime

ROOT = Path(__file__).resolve().parents[1]
_TOOL = "npm.cmd" if os.name == "nt" else "npm"


def _portable(tmp_path, monkeypatch):
    folder = tmp_path / "portable"
    folder.mkdir()
    tool = folder / _TOOL
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(node_runtime, "portable_dirs", lambda: [folder])
    return folder, tool


def test_a_portable_node_is_found_without_it_on_path(tmp_path, monkeypatch):
    """MUTATION: answer from PATH alone - None, red."""
    folder, tool = _portable(tmp_path, monkeypatch)
    found = node_runtime.find("npm")
    assert found and Path(found).resolve() == tool.resolve()
    # npm starts node by name: the folder is on this process's PATH for every child.
    assert os.environ["PATH"].split(os.pathsep)[0] == str(folder)


def test_path_wins_over_the_portable_node(tmp_path, monkeypatch):
    _portable(tmp_path, monkeypatch)
    system = tmp_path / "system"
    system.mkdir()
    tool = system / _TOOL
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(system))
    assert Path(node_runtime.find("npm")).resolve() == tool.resolve()


def test_no_node_anywhere_is_none(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(node_runtime, "portable_dirs", lambda: [tmp_path / "missing"])
    assert node_runtime.find("node") is None


def test_the_folders_are_the_ones_the_installers_write():
    """The installers and this module name the same folders; one moved without the other is
    a web UI that does not start. MUTATION: change either path - red."""
    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    module = (ROOT / "vaf" / "core" / "node_runtime.py").read_text(encoding="utf-8")
    assert 'NODE_DIR="$HOME/.vaf/node"' in install_sh
    assert 'Platform.vaf_dir() / "node" / "bin"' in module
    assert 'Join-Path $env:LOCALAPPDATA "Veyllo\\node"' in install_ps1
    assert '"LOCALAPPDATA", "")) / "Veyllo" / "node"' in module


_BARE_LOOKUP = re.compile(r"""shutil\.which\(\s*["'](?:node|npm|npx)["']""")
# Files that look Node up on PATH alone on purpose, with the reason.
_PATH_ONLY = {
    "vaf/tools/linter.py": "npx --yes eslint fetches and runs a package with the whole "
                           "environment; the portable Node would hand that to servers",
}


def test_the_path_only_list_has_no_dead_entries():
    for rel in _PATH_ONLY:
        assert _BARE_LOOKUP.search((ROOT / rel).read_text(encoding="utf-8")), rel


def test_no_lookup_of_node_skips_the_portable_install():
    """Five lookups in four files looked Node up on PATH alone (the web UI, the WhatsApp
    bridge and its QR login, `vaf update`); one knew the portable install, on Windows only.
    The linter stays on PATH by decision (_PATH_ONLY says why).
    MUTATION: put `shutil.which("npm")` back into the frontend manager - red."""
    hits = []
    for path in (ROOT / "vaf").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if path.name == "node_runtime.py" or rel in _PATH_ONLY:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BARE_LOOKUP.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{no}")
    assert hits == [], "use vaf.core.node_runtime.find: " + ", ".join(hits)


@pytest.mark.skipif(os.name == "nt", reason="the POSIX folder of install.sh")
def test_the_posix_folder_is_under_the_vaf_directory(monkeypatch, tmp_path):
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    assert node_runtime.portable_dirs() == [tmp_path / "node" / "bin"]


def test_on_windows_the_newest_portable_node_is_tried_first(tmp_path, monkeypatch):
    """Version order, not the order of the names: "v22.9.0" sorts after "v22.11.0" as text.
    MUTATION: sort the folder names - red."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "is_windows", staticmethod(lambda: True))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "Veyllo" / "node"
    for name in ("node-v20.18.0-win-x64", "node-v22.9.0-win-x64", "node-v22.11.0-win-x64", "notes"):
        (root / name).mkdir(parents=True)
    assert [p.name for p in node_runtime.portable_dirs()] == [
        "node-v22.11.0-win-x64", "node-v22.9.0-win-x64", "node-v20.18.0-win-x64", "notes"]
