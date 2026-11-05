# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Node.js where the installers put it (vaf/core/node_runtime.py).

Measured on a fresh server install with no system Node: the systemd user unit does not read
the shell file the installer wrote the portable Node into, the service logged "npm not found
in PATH", and every page of the web UI answered 502.
"""
import json
import os
import re
import shutil
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


def test_finding_it_leaves_this_process_path_alone(tmp_path, monkeypatch):
    """Everything this process starts afterwards would see the portable Node, the linter too,
    which stays on PATH alone on purpose (_PATH_ONLY): after the web UI had started, it found
    the portable npx. MUTATION: put the folder on os.environ["PATH"] in find() - red."""
    folder, _ = _portable(tmp_path, monkeypatch)
    before = os.environ["PATH"]
    assert node_runtime.find("npm")
    assert os.environ["PATH"] == before
    assert shutil.which("npm") is None, "a PATH-only lookup still does not see it"


def test_the_child_that_runs_it_gets_the_folder_first(tmp_path, monkeypatch):
    """npm starts node by name, so its child needs the folder; the caller's own environment
    is copied, not changed. MUTATION: return the base without the folder - red."""
    folder, _ = _portable(tmp_path, monkeypatch)
    env = node_runtime.child_env(node_runtime.find("npm"), {"PATH": "/usr/bin", "KEEP": "1"})
    assert env["PATH"].split(os.pathsep) == [str(folder), "/usr/bin"]
    assert env["KEEP"] == "1"
    assert os.environ["PATH"] == str(tmp_path / "empty")


def test_prepending_keeps_the_rest_of_path_byte_for_byte(tmp_path, monkeypatch):
    """An empty PATH entry means the working directory on POSIX: dropping it, or adding one to
    an empty PATH, changes what the child finds. MUTATION: rebuild PATH from its non-empty
    entries - red."""
    folder, _ = _portable(tmp_path, monkeypatch)
    tool = node_runtime.find("npm")
    original = os.pathsep.join(["/usr/bin", "", "/bin"])
    assert node_runtime.child_env(tool, {"PATH": original})["PATH"] == str(folder) + os.pathsep + original
    assert node_runtime.child_env(tool, {"PATH": ""})["PATH"] == str(folder)
    assert node_runtime.child_env(tool, {})["PATH"] == str(folder)


def test_a_tool_found_on_path_leaves_the_child_path_as_it_was(tmp_path):
    """Moving a PATH folder to the front would reorder what the child finds (a venv's python
    behind the system's). MUTATION: always prepend - red."""
    system = str(tmp_path / "system")
    base = {"PATH": os.pathsep.join([str(tmp_path / "venv"), system])}
    assert node_runtime.child_env(os.path.join(system, _TOOL), base)["PATH"] == base["PATH"]


# Every child that runs a found Node tool gets its environment from child_env: the file and
# how many such spawns it has. One without it runs npm whose `env node` finds nothing.
_SPAWNS = {
    "vaf/core/frontend_manager.py": 2,   # npm ci/install, and the build and start
    "vaf/api/whatsapp_bridge.py": 2,     # npm ci/install, and the bridge's node
    "vaf/cli/cmd/update.py": 1,          # npm ci/install
}


def test_every_child_of_a_found_tool_gets_its_folder():
    """MUTATION: spawn the bridge's node without child_env - red."""
    for rel, count in _SPAWNS.items():
        assert (ROOT / rel).read_text(encoding="utf-8").count("node_runtime.child_env(") == count, rel
    finders = {path.relative_to(ROOT).as_posix() for path in (ROOT / "vaf").rglob("*.py")
               if "node_runtime.find(" in path.read_text(encoding="utf-8")}
    # The QR login hands the node it found to the bridge's spawn helper.
    assert finders - set(_SPAWNS) == {"vaf/api/whatsapp_routes.py"}


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


# ── lock_drift: the tree on disk against its package-lock.json ───────────────

def _npm_tree(tmp_path, lock: dict, on_disk: dict, *, manifest=None, newline="\n") -> Path:
    """A project with ``lock`` as the packages table of its lockfile and ``on_disk`` as the
    package.json versions under node_modules, keyed the way the lock keys them."""
    project = tmp_path / "project"
    (project / "node_modules").mkdir(parents=True)
    text = json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "x"}, **lock}}, indent=2)
    (project / "package-lock.json").write_bytes(text.replace("\n", newline).encode("utf-8"))
    if manifest is not None:
        (project / "node_modules" / ".package-lock.json").write_text(
            json.dumps({"packages": manifest}), encoding="utf-8")
    for key, version in on_disk.items():
        pkg = project / key
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "package.json").write_text(json.dumps({"name": key.rsplit("/", 1)[-1], "version": version}),
                                          encoding="utf-8")
    return project


def _drift(project, include_dev=False):
    return node_runtime.lock_drift(project, include_dev=include_dev)


def test_lock_drift_a_tree_that_matches_is_empty(tmp_path):
    lock = {"node_modules/a": {"version": "1.0.0"},
            "node_modules/@s/b": {"version": "2.0.0"},
            "node_modules/a/node_modules/c": {"version": "3.0.0"}}
    project = _npm_tree(tmp_path, lock, {k: v["version"] for k, v in lock.items()})
    assert _drift(project) == []


def test_lock_drift_names_a_transitive_package_that_is_behind(tmp_path):
    """MUTATION: compare one named package only (the Baileys check) - this one is missed, red."""
    lock = {"node_modules/@whiskeysockets/baileys": {"version": "6.7.24"},
            "node_modules/follow-redirects": {"version": "1.16.2"}}
    project = _npm_tree(tmp_path, lock, {"node_modules/@whiskeysockets/baileys": "6.7.24",
                                         "node_modules/follow-redirects": "1.16.0"})
    assert _drift(project) == [node_runtime.NpmDrift("node_modules/follow-redirects", "1.16.0", "1.16.2")]


def test_lock_drift_a_wrong_version_counts_even_for_an_optional_entry(tmp_path):
    """MUTATION: let the optional allowance cover a mismatch - a stale optional build passes, red."""
    lock = {"node_modules/@img/sharp-linux-x64": {"version": "0.34.5", "optional": True, "os": ["linux"]}}
    project = _npm_tree(tmp_path, lock, {"node_modules/@img/sharp-linux-x64": "0.34.4"})
    assert _drift(project) == [node_runtime.NpmDrift("node_modules/@img/sharp-linux-x64", "0.34.4", "0.34.5")]


@pytest.mark.parametrize("meta", [{}, {"peer": True}], ids=["plain", "peer-without-optional"])
def test_lock_drift_a_required_package_missing_from_disk_counts(tmp_path, meta):
    """MUTATION: skip `peer` entries - npm 7+ installs a peer that is not optional, red."""
    project = _npm_tree(tmp_path, {"node_modules/semver": {"version": "7.7.3", **meta}}, {})
    assert _drift(project) == [node_runtime.NpmDrift("node_modules/semver", "", "7.7.3")]


@pytest.mark.parametrize("meta", [
    {"optional": True}, {"devOptional": True}, {"os": ["win32"]}, {"cpu": ["arm64"]}, {"libc": ["musl"]},
    {"optional": True, "peer": True},
], ids=["optional", "devOptional", "os", "cpu", "libc", "optional-peer"])
def test_lock_drift_a_platform_gated_package_may_be_absent(tmp_path, meta):
    """MUTATION: drop one flag from the allowance - its case reports a build this platform
    never unpacks, red."""
    project = _npm_tree(tmp_path, {"node_modules/@img/sharp-win32-arm64": {"version": "0.34.5", **meta}}, {})
    assert _drift(project) == []


def test_lock_drift_an_extraneous_nested_copy_counts(tmp_path):
    """A stale nested copy shadows a bumped hoisted one for the package that holds it.
    MUTATION: drop the walk of the tree on disk - it is missed, red."""
    lock = {"node_modules/a": {"version": "1.0.0"}, "node_modules/follow-redirects": {"version": "1.16.2"}}
    project = _npm_tree(tmp_path, lock, {"node_modules/a": "1.0.0", "node_modules/follow-redirects": "1.16.2",
                                         "node_modules/a/node_modules/follow-redirects": "1.16.0",
                                         "node_modules/@s/stray": "0.1.0"})
    (project / "node_modules" / ".bin").mkdir()
    (project / "node_modules" / ".cache" / "x").mkdir(parents=True)
    (project / "node_modules" / "not-a-package").mkdir()
    assert sorted(_drift(project)) == [
        node_runtime.NpmDrift("node_modules/@s/stray", "0.1.0", ""),
        node_runtime.NpmDrift("node_modules/a/node_modules/follow-redirects", "1.16.0", ""),
    ]


def test_lock_drift_dev_entries_count_only_when_asked(tmp_path):
    """MUTATION: ignore include_dev (either way) - one of the two asserts goes red."""
    lock = {"node_modules/typescript": {"version": "5.9.3", "dev": True}}
    project = _npm_tree(tmp_path, lock, {})
    assert _drift(project, include_dev=False) == []
    assert _drift(project, include_dev=True) == [node_runtime.NpmDrift("node_modules/typescript", "", "5.9.3")]


def test_lock_drift_skips_link_entries(tmp_path):
    """A workspace link's version lives at its target; the link has none to compare."""
    lock = {"node_modules/local": {"resolved": "packages/local", "link": True},
            "packages/local": {"version": "0.0.1"}}
    assert _drift(_npm_tree(tmp_path, lock, {})) == []


@pytest.mark.parametrize("lock_text", [None, "{not json", json.dumps({"lockfileVersion": 1, "dependencies": {}}),
                                       json.dumps(["packages"])],
                         ids=["missing", "invalid", "v1", "not-an-object"])
def test_lock_drift_cannot_tell_without_a_v2_lock(tmp_path, lock_text):
    project = tmp_path / "project"
    (project / "node_modules").mkdir(parents=True)
    if lock_text is not None:
        (project / "package-lock.json").write_text(lock_text, encoding="utf-8")
    assert _drift(project) is None


def test_lock_drift_reads_a_crlf_lock_the_same(tmp_path):
    """A CRLF checkout (Windows, autocrlf) gives the same answer as LF."""
    lock = {"node_modules/a": {"version": "1.0.0"}, "node_modules/b": {"version": "2.0.0"}}
    disk = {"node_modules/a": "1.0.0", "node_modules/b": "1.9.0"}
    lf = _drift(_npm_tree(tmp_path / "lf", lock, disk))
    crlf_project = _npm_tree(tmp_path / "crlf", lock, disk, newline="\r\n")
    assert b"\r\n" in (crlf_project / "package-lock.json").read_bytes()
    assert _drift(crlf_project) == lf == [node_runtime.NpmDrift("node_modules/b", "1.9.0", "2.0.0")]


def test_lock_drift_does_not_believe_npms_manifest(tmp_path):
    """`npm ci --dry-run` rewrites node_modules/.package-lock.json without unpacking anything,
    so the manifest names the locked version while the disk holds the old one.
    MUTATION: read versions from the manifest - the drift disappears, red."""
    lock = {"node_modules/follow-redirects": {"version": "1.16.2"}}
    project = _npm_tree(tmp_path, lock, {"node_modules/follow-redirects": "1.16.0"}, manifest=lock)
    assert _drift(project) == [node_runtime.NpmDrift("node_modules/follow-redirects", "1.16.0", "1.16.2")]


def test_lock_drift_keys_are_spelled_the_way_the_lock_spells_them(tmp_path):
    """The keys are entry names joined with "/", never str(Path), which carries the host
    separator on Windows (that spelling is refused statically by
    tests/test_windows_path_hygiene.py). A nested scoped package comes out exactly as the
    lock names it. MUTATION: treat an @scope folder as a package - its key is lost, red."""
    lock = {"node_modules/a": {"version": "1.0.0"}, "node_modules/a/node_modules/@s/b": {"version": "2.0.0"}}
    project = _npm_tree(tmp_path, lock, {k: v["version"] for k, v in lock.items()})
    keys = set(node_runtime._installed_packages(project))
    assert keys == set(lock)
    assert not any("\\" in k for k in keys)
