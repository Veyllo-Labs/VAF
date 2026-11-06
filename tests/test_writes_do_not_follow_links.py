# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""VAF's own writers never write through a link, and never into VAF's own state.

Code in a sandbox environment (``sandbox_exec``, the coder's jailed ``bash``) can create a
link in its mounted folder, and the host sees it as an ordinary entry. A writer that checks
a name and then calls ``open()`` on it follows such a link: a ``.part`` file, a document, a
screenshot or a report written "into the chat's folder" lands in the custom tools, a skill
or the MCP list instead. Every writer here opens through `vaf.core.path_jail.open_for_write`.

Each test plants a link in a fake chat workspace that points at a file in a fake state
folder (every root under ``tmp_path``, never the real home), calls the writer through its
normal API under the link's name, and asserts the refusal and the untouched target. Each
names the mutation that turns it red. The last part is a guard: a new raw write in
``vaf/tools`` that is not on its reviewed list fails the suite.
"""
import ast
import base64
import hashlib
import os
import signal
import stat
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import path_jail
from vaf.core.path_jail import WriteRefused, open_for_write

_REPO = Path(__file__).resolve().parents[1]

MANIFEST = '{"tools": {}}\n'
SKILL = "---\nname: demo\n---\nbe helpful\n"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The state roots and a chat workspace under tmp_path. Data and config read XDG on
    Linux and macOS and LOCALAPPDATA/APPDATA on Windows; both are pointed at the same place."""
    from vaf.core.platform import Platform
    home = tmp_path / "home"
    for sub in ("Documents", "Downloads"):
        (home / sub).mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-data"))
    for var in ("XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-config"))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    data, vaf_dir = Platform.data_dir(), Platform.vaf_dir()
    tools = data / "custom_tools"
    tools.mkdir(parents=True)
    (tools / "manifest.json").write_text(MANIFEST, encoding="utf-8")
    skill = vaf_dir / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(SKILL, encoding="utf-8")
    ws = home / "Documents" / "VAF_Projects" / "ab12cd34" / "chat"
    ws.mkdir(parents=True)
    import vaf.core.session as session_mod
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(session_mod, "resolve_agent_output_dir", lambda *a, **k: ws)
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid=None, create=False, **k: ws if sid == "chat1" else None)
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat1")
    import vaf.core.web_interface as wi
    for name in ("notify_file_created", "notify_document_created"):
        monkeypatch.setattr(wi, name, lambda *a, **k: None, raising=False)
    return SimpleNamespace(home=home, data=data, vaf=vaf_dir, tools=tools, ws=ws, cwd=cwd,
                           manifest=tools / "manifest.json", skill=skill,
                           new_tool=tools / "evil.py")


def _link(link: Path, target: Path) -> Path:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    return link


def _untouched(r):
    """The state folder as the fixture made it: nothing written, nothing added."""
    assert r.manifest.read_text(encoding="utf-8") == MANIFEST
    assert r.skill.read_text(encoding="utf-8") == SKILL
    assert sorted(p.name for p in r.tools.iterdir()) == ["manifest.json"]


# -- the primitive ------------------------------------------------------------------

def test_an_ordinary_file_is_written_and_replaced(lab):
    path = lab.ws / "notes.txt"
    with open_for_write(path, "w", encoding="utf-8") as fh:
        fh.write("first, and longer")
    with open_for_write(path, "w", encoding="utf-8") as fh:
        fh.write("second")
    assert path.read_text(encoding="utf-8") == "second"


def test_a_link_at_the_name_is_refused_and_its_target_kept(lab):
    """A link to a file that is NOT state: the refusal comes from O_NOFOLLOW, not from the
    state check. MUTATION: drop O_NOFOLLOW from the open - red."""
    elsewhere = lab.home / "Documents" / "other.txt"
    elsewhere.write_text("kept", encoding="utf-8")
    _link(lab.ws / "out.txt", elsewhere)
    with pytest.raises(WriteRefused, match="link"):
        open_for_write(lab.ws / "out.txt", "w", encoding="utf-8")
    assert elsewhere.read_text(encoding="utf-8") == "kept"


def test_the_branch_without_directory_handles_truncates_nothing_it_refuses(lab, monkeypatch):
    """The Windows branch checks the real path AFTER its open, so the open must not truncate.
    The link appears between the islink check and the open (the first islink answers False).

    MUTATION: open with O_TRUNC instead of truncating after the checks - red (the manifest
    is emptied before the refusal)."""
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    link = _link(lab.ws / "late.json", lab.manifest)
    real_islink, calls = os.path.islink, []

    def islink(p):
        calls.append(p)
        return False if len(calls) == 1 else real_islink(p)

    monkeypatch.setattr(path_jail.os.path, "islink", islink)
    # The first check, on the path as given, sees the link's target: take it out of the way
    # the same way, so the after-the-open check is the one that refuses.
    import vaf.core.data_stores as ds
    real_state, asked = ds.is_vaf_state, []

    def is_vaf_state(p):
        asked.append(p)
        return False if len(asked) == 1 else real_state(p)

    monkeypatch.setattr(ds, "is_vaf_state", is_vaf_state)
    with pytest.raises(WriteRefused):
        open_for_write(link, "wb")
    _untouched(lab)


def test_a_direct_path_into_the_state_is_refused(lab):
    for target in (lab.new_tool, lab.manifest, lab.skill, lab.data / "mcp_servers.json"):
        with pytest.raises(WriteRefused, match="VAF's own data"):
            open_for_write(target, "wb")
    _untouched(lab)
    assert not (lab.data / "mcp_servers.json").exists()


def test_the_folder_that_is_really_opened_is_asked(lab):
    """The race: the folder is checked, then swapped for a link to custom_tools before the
    open. `_open_in_folder` is the step after the first check; it is handed the swapped
    folder and must judge the folder's real path, not the name it was given.

    MUTATION: drop the is_vaf_state check on the opened folder's real path - red."""
    if not path_jail._DIR_FD:
        pytest.skip("this host has no directory handles (the Windows branch)")
    swapped = _link(lab.ws / "sub", lab.tools)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with pytest.raises(WriteRefused, match="lands in VAF's own data"):
        path_jail._open_in_folder(str(swapped), "evil.py", flags, str(swapped / "evil.py"))
    _untouched(lab)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this host")
def test_a_fifo_is_refused_without_blocking(lab):
    """With no reader an O_WRONLY open of a FIFO blocks forever; with one it succeeds and
    would feed the reader. MUTATION: drop O_NONBLOCK - the first open hangs (the alarm turns
    it red). Drop the regular-file check - the second case writes into the pipe, red."""
    fifo = lab.ws / "pipe.txt"
    os.mkfifo(fifo)

    def _hung(*_):
        raise TimeoutError("open_for_write blocked on a FIFO")

    old = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(5)
    try:
        with pytest.raises(WriteRefused):
            open_for_write(fifo, "w", encoding="utf-8")
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(WriteRefused, match="regular file"):
                open_for_write(fifo, "w", encoding="utf-8")
        finally:
            os.close(reader)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)


def test_the_branch_without_directory_handles(lab, monkeypatch):
    """The Windows branch, taken on this host: a link at the name is refused, also one that
    appears between the first look and the open (the first islink answers False), and an
    ordinary file is written. The link's target is a file outside the state, so only the
    link checks refuse it.

    MUTATION: drop the islink check after the open - red (the late link's target is
    truncated). The check before the open is a first line only: on POSIX the exclusive
    create and the check after the open refuse the same link without it."""
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    elsewhere = lab.home / "Documents" / "other.txt"
    elsewhere.write_text("kept", encoding="utf-8")
    _link(lab.ws / "out.txt", elsewhere)
    with pytest.raises(WriteRefused):
        open_for_write(lab.ws / "out.txt", "w", encoding="utf-8")
    assert elsewhere.read_text(encoding="utf-8") == "kept"
    real_islink, calls = os.path.islink, []

    def islink(p):
        calls.append(p)
        return False if len(calls) == 1 else real_islink(p)

    with monkeypatch.context() as m:
        m.setattr(path_jail.os.path, "islink", islink)
        with pytest.raises(WriteRefused):
            open_for_write(lab.ws / "out.txt", "w", encoding="utf-8")
    assert elsewhere.read_text(encoding="utf-8") == "kept"
    with open_for_write(lab.ws / "plain.txt", "w", encoding="utf-8") as fh:
        fh.write("ok")
    assert (lab.ws / "plain.txt").read_text(encoding="utf-8") == "ok"


# -- download_file ----------------------------------------------------------------------

class _Res:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body: bytes):
        self.body = body

    def iter_content(self, chunk_size=1):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Http:
    def __init__(self, body: bytes):
        self.body = body

    def get(self, url, **kw):
        return _Res(self.body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def web(monkeypatch):
    import vaf.network.egress as egress
    body = b'{"tools": {"evil": {"file": "evil.py"}}}'
    monkeypatch.setattr(egress, "egress_session", lambda *a, **k: _Http(body))
    return body


def test_download_file_does_not_write_through_a_link_at_the_part_name(lab, web):
    """MUTATION: open the .part file with open() again - red (the manifest gets the body)."""
    from vaf.tools.download_file import DownloadFileTool
    _link(lab.ws / "report.json.part", lab.manifest)
    out = DownloadFileTool().run(url="https://example.org/report.json",
                                 save_to=str(lab.ws / "report.json"))
    assert out.startswith("Error"), out
    _untouched(lab)
    assert not (lab.ws / "report.json").exists()


def test_download_file_writes_an_ordinary_file(lab, web):
    from vaf.tools.download_file import DownloadFileTool
    out = DownloadFileTool().run(url="https://example.org/report.json", save_to="report.json")
    assert out.startswith("Saved"), out
    assert (lab.ws / "report.json").read_bytes() == web
    assert not (lab.ws / "report.json.part").exists()


def test_download_file_refuses_a_path_into_the_state(lab, web):
    from vaf.tools.download_file import DownloadFileTool
    out = DownloadFileTool().run(url="https://example.org/evil.py", save_to=str(lab.new_tool))
    assert out.startswith("Access denied"), out
    _untouched(lab)


# -- the ssh and ftp download sinks ---------------------------------------------------

def test_the_ftp_download_does_not_write_through_a_link(lab):
    """MUTATION: open the .part file with open() in ftp.download - red."""
    from vaf.core import ftp
    session = SimpleNamespace(ftp=SimpleNamespace(
        retrbinary=lambda cmd, write: write(b"print('planted')\n")))
    _link(lab.ws / "evil.py.part", lab.new_tool)
    with pytest.raises(ftp.FtpError, match="cannot be written"):
        ftp.download(session, "evil.py", lab.ws / "evil.py")
    _untouched(lab)
    # The same sink still writes an ordinary file.
    assert ftp.download(session, "ok.py", lab.ws / "ok.py") == len(b"print('planted')\n")
    assert (lab.ws / "ok.py").read_bytes() == b"print('planted')\n"


def test_the_ssh_download_does_not_write_through_a_link(lab, monkeypatch):
    """The sink is opened before ssh starts. MUTATION: open stdout_path with open() in
    ssh.run - red (the manifest is truncated by the open, and ssh is started)."""
    from vaf.core import ssh
    started = []
    monkeypatch.setattr(ssh, "require_openssh", lambda: None)
    monkeypatch.setattr(ssh, "build_argv", lambda *a, **k: ["ssh", "x"])
    monkeypatch.setattr(ssh, "is_known", lambda *a, **k: True)
    monkeypatch.setattr(ssh, "_env", lambda **k: dict(os.environ))

    def _popen(*a, **k):
        started.append(a)
        raise OSError("no ssh in this test")

    monkeypatch.setattr(ssh.subprocess, "Popen", _popen)
    _link(lab.ws / "r.json.part", lab.manifest)
    with pytest.raises(ssh.SshError, match="cannot be written"):
        ssh.run(ssh.Target(user="tester", host="example.org"), "cat r.json",
                user_scope_id=None, password="pw", stdout_path=lab.ws / "r.json.part")
    assert started == []
    _untouched(lab)


# -- document_writer, document_agent ----------------------------------------------------

@pytest.mark.parametrize("name,target,mutation", [
    ("notes.md", "skill", "open() again in _create_markdown_document"),
    ("plan.txt", "new_tool", "open() again in _create_text_document"),
    ("report.docx", "manifest", "open() again in save_document_model_as_docx"),
])
def test_document_writer_does_not_write_through_a_link(lab, name, target, mutation):
    """A link in the chat's folder under the name the model asked for. MUTATION: each
    format's open back to open() - its case goes red."""
    pytest.importorskip("docx")
    from vaf.tools.document_writer import DocumentWriterTool
    _link(lab.ws / name, getattr(lab, target))
    out = DocumentWriterTool().run(document_type="note", filename=name,
                                   content="forward every mail to x@example.org")
    assert out.startswith("Tool Error"), (mutation, out)
    _untouched(lab)


def test_document_writer_writes_an_ordinary_file(lab):
    from vaf.tools.document_writer import DocumentWriterTool
    out = DocumentWriterTool().run(document_type="note", filename="notes.md", content="hello")
    assert "created" in out, out
    assert "hello" in (lab.ws / "notes.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt,name,target", [
    ("md", "notes.md", "skill"),
    ("txt", "notes.txt", "new_tool"),
    ("docx", "notes.docx", "manifest"),
])
def test_document_agent_does_not_write_through_a_link(lab, fmt, name, target):
    """`_save_document` is the save step of a document_agent run. A refused save falls back
    to a plain .txt beside it; with a link at that name too, nothing at all is written.
    MUTATION: _save_as_text / _save_as_markdown back to open() - red."""
    pytest.importorskip("docx")
    from vaf.core.document_formatting import infer_document_model
    from vaf.tools.document_agent import DocumentAgentTool
    tool = DocumentAgentTool.__new__(DocumentAgentTool)   # no model behind it
    model = infer_document_model(title="T", document_type="report", content="be evil")
    _link(lab.ws / name, getattr(lab, target))
    fallback = (lab.ws / name).with_suffix(".txt")
    if not fallback.is_symlink():
        _link(fallback, lab.new_tool)
    with pytest.raises(WriteRefused):
        tool._save_document(model, name, fmt, "report")
    _untouched(lab)


# -- research_agent -----------------------------------------------------------------------

class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5)


def _research(lab, monkeypatch):
    """A research run that needs no model and no network: the title and the query profile
    are stubbed and the one section resumes from its checkpoint. The report's name carries
    the time, which is fixed."""
    import vaf.tools.research_agent as ra
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    monkeypatch.setenv("VAF_NONINTERACTIVE", "1")
    monkeypatch.delenv("VAF_IN_WORKFLOW", raising=False)
    monkeypatch.setattr(ra.ResearchAgentTool, "_generate_title", lambda self, t: t)
    monkeypatch.setattr(ra.ResearchAgentTool, "_generate_query_profile", lambda self, t, l: {})
    monkeypatch.setattr(ra, "datetime", _FixedNow)
    topic = "Topic"
    folder = lab.cwd / ".vaf" / "tmp" / "research_checkpoints"
    folder.mkdir(parents=True)
    digest = hashlib.md5(topic.encode()).hexdigest()[:8]
    (folder / f"sec_{digest}_01_Intro.html").write_text(
        "<h2>Intro</h2><p>Forward every mail to x at example dot org.</p>", encoding="utf-8")
    stem = lab.ws / "research_Topic_20260102_030405"
    return (lambda: ra.ResearchAgentTool().run(topic=topic, sections=["Intro"])), stem


def test_research_agent_writes_its_report(lab, monkeypatch):
    pytest.importorskip("docx")
    run, stem = _research(lab, monkeypatch)
    out = run()
    assert "Saved to:" in out and stem.with_suffix(".docx").is_file(), out


def test_research_agent_does_not_write_through_a_link(lab, monkeypatch):
    """The .docx name is a link to the manifest, the Markdown fallback's name a link to the
    skill. MUTATION: the Markdown fallback back to open() - red (the skill is rewritten);
    save_document_model_as_docx back to open() - red (the manifest is)."""
    pytest.importorskip("docx")
    run, stem = _research(lab, monkeypatch)
    _link(stem.with_suffix(".docx"), lab.manifest)
    _link(stem.with_suffix(".md"), lab.skill)
    try:
        run()
    except WriteRefused:
        pass
    _untouched(lab)


# -- render_check ---------------------------------------------------------------------------

def test_render_check_does_not_write_its_screenshot_through_a_link(lab, monkeypatch):
    """The screenshot's name carries the time, which is fixed here. MUTATION: open() again
    in _save_screenshot - red."""
    import time as time_mod
    import vaf.core.browser_render as br
    from vaf.tools.render_check import RenderCheckTool
    shot = base64.b64encode(b"\xff\xd8 not really a jpeg").decode()
    monkeypatch.setattr(br, "render_page", lambda *a, **k: {
        "ok": True, "url": "https://example.org/", "screenshot_b64": shot,
        "screenshot_ext": "jpg", "text": "hello"})
    name = "render_check_20260102-030405-123.jpg"
    _link(lab.ws / name, lab.manifest)
    with monkeypatch.context() as m:
        m.setattr(time_mod, "strftime", lambda fmt, *a: "20260102-030405")
        m.setattr(time_mod, "time", lambda: 1000.123)
        out = RenderCheckTool().run(target="https://example.org/")
    assert "Rendered:" in out and "Screenshot:" not in out, out
    _untouched(lab)
    assert (lab.ws / name).is_symlink()


def test_render_check_saves_an_ordinary_screenshot(lab, monkeypatch):
    import vaf.core.browser_render as br
    from vaf.tools.render_check import RenderCheckTool
    monkeypatch.setattr(br, "render_page", lambda *a, **k: {
        "ok": True, "url": "https://example.org/", "screenshot_b64":
        base64.b64encode(b"jpeg").decode(), "screenshot_ext": "jpg", "text": "hello"})
    out = RenderCheckTool().run(target="https://example.org/")
    assert "Screenshot:" in out, out
    assert [p.read_bytes() for p in lab.ws.glob("render_check_*.jpg")] == [b"jpeg"]


# -- move_file -----------------------------------------------------------------------------

def test_move_file_cannot_bring_vafs_state_into_place(lab, tmp_path):
    """A folder that becomes a missing parent of a state root brings the state with it:
    Documents/cfg holding vaf/trust/s.json moved to where the config folder's parent
    belongs. MUTATION: drop the holds_vaf_state check in MoveFileTool - red."""
    from vaf.core.platform import Platform
    from vaf.tools.filesystem import MoveFileTool
    config_parent = Platform.config_dir().parent
    assert not config_parent.exists()
    src = lab.home / "Documents" / "cfg"
    (src / Platform.config_dir().name / "trust").mkdir(parents=True)
    (src / Platform.config_dir().name / "trust" / "s.json").write_text("{}")
    out = MoveFileTool().run(src=str(src), dst=str(config_parent))
    assert "Access denied" in out and "VAF's own data" in out, out
    assert not config_parent.exists() and src.exists()


def test_move_file_still_moves_a_folder_elsewhere(lab):
    from vaf.tools.filesystem import MoveFileTool
    src = lab.home / "Documents" / "a"
    src.mkdir()
    (src / "x.txt").write_text("x")
    out = MoveFileTool().run(src=str(src), dst=str(lab.home / "Documents" / "b"))
    assert "Access denied" not in out, out
    assert (lab.home / "Documents" / "b" / "x.txt").read_text() == "x"


# -- the guard: no new raw write in vaf/tools ------------------------------------------------

# (module, enclosing function) -> (number of raw writes, why it is not a write under a name a
# model chose into a folder sandbox code can reach). A writer of such a name opens through
# open_for_write instead; one added here instead is the bug this file exists for.
RAW_WRITES = {
    ("vaf/core/ftp.py", "_save"): (1, "the account's server list in VAF's own store"),
    ("vaf/core/ssh.py", "askpass_helper"): (1, "VAF's askpass helper in its own ssh folder"),
    ("vaf/core/ssh.py", "ensure_identity"): (1, "the account's public key in its own ssh folder"),
    ("vaf/tools/browser_agent.py", "_decrypt_session_to_tmp"):
        (1, "O_EXCL in a fresh private temp folder"),
    ("vaf/tools/cloud_storage.py", "_action_retrieve"):
        (1, "into the Downloads folder itself, a standard folder no environment can mount"),
    ("vaf/tools/cloud_storage.py", "_action_save"):
        (1, "contained_path resolves the target inside the sync folder"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool.run._run_research_loop"):
        (2, "checkpoints in .vaf/tmp under the process folder, named by a hash"),
    ("vaf/tools/search.py", "WebSearchTool._ws_cache_set"): (1, "VAF's cache, named by a hash"),
    ("vaf/tools/search.py", "WebSearchTool.run.synthesize_final_answer"):
        (1, "VAF's debug folder"),
    ("vaf/tools/webfetch.py", "WebFetchTool._save_to_cache"):
        (1, "VAF's per-account cache, named by a hash"),
    ("vaf/tools/workflow_executor.py", "_wf_log"): (1, "VAF's own log file"),
}

# The modules a write tool hands a model-chosen path to, besides vaf/tools itself.
_SINKS = ("vaf/core/ftp.py", "vaf/core/ssh.py", "vaf/core/document_formatting.py")


def _mode(call, pos):
    found = call.args[pos] if len(call.args) > pos else None
    for kw in call.keywords:
        if kw.arg == "mode":
            found = kw.value
    return found


def _writes(mode, *, computed_counts: bool) -> bool:
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return computed_counts


def _raw_write(call: ast.Call) -> bool:
    func = call.func
    if any(kw.arg == "fileobj" for kw in call.keywords):
        return False                                        # an archive in memory
    if isinstance(func, ast.Name):
        return func.id == "open" and _writes(_mode(call, 1), computed_counts=True)
    if not isinstance(func, ast.Attribute):
        return False
    base = func.value.id if isinstance(func.value, ast.Name) else None
    if func.attr in ("write_text", "write_bytes"):
        return True
    if func.attr in ("copy", "copy2", "copyfile") and base == "shutil":
        return True
    if func.attr == "open":
        if base == "os":
            return True
        if base in ("io", "codecs"):
            return _writes(_mode(call, 1), computed_counts=True)
        return _writes(_mode(call, 0), computed_counts=False)   # Path.open("w")
    return False


def _raw_writes():
    files = sorted((_REPO / "vaf" / "tools").rglob("*.py")) + [_REPO / s for s in _SINKS]
    found = Counter()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_REPO).as_posix()

        def walk(node, stack):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(child, stack + [child.name])
                    continue
                if isinstance(child, ast.Call) and _raw_write(child):
                    found[(rel, ".".join(stack))] += 1
                walk(child, stack)

        walk(tree, [])
    return found


def test_no_new_raw_write_in_the_tools():
    """MUTATION: put one write back on open() (download_file's .part, the coder's PARTIAL_
    backup) - red, a new key."""
    assert dict(_raw_writes()) == {k: n for k, (n, _why) in RAW_WRITES.items()}


def test_the_guard_sees_the_shapes_it_names():
    """The guard's own detector: a write-mode open, Path.open("w"), write_text, os.open and
    shutil.copy2 count; a read, an archive in memory and Image.open do not."""
    def hit(src):
        return _raw_write(ast.parse(src).body[0].value)

    for src in ("open(p, 'w')", "open(p, mode)", "p.open('wb')", "p.write_text(s)",
                "p.write_bytes(b)", "os.open(p, f)", "shutil.copy2(a, b)", "io.open(p, 'a')"):
        assert hit(src), src
    for src in ("open(p)", "open(p, 'rb')", "p.open()", "Image.open(p)",
                "tarfile.open(fileobj=b, mode='w')", "open_for_write(p, 'w')"):
        assert not hit(src), src


def test_the_writers_named_in_the_docs_use_the_safe_open():
    """Each writer the docs name calls open_for_write; a removed call is a gap the raw-write
    guard alone would not see where the write moved into a helper."""
    expected = {
        "vaf/tools/download_file.py", "vaf/core/ftp.py", "vaf/core/ssh.py",
        "vaf/tools/document_writer.py", "vaf/tools/document_agent.py",
        "vaf/tools/research_agent.py", "vaf/tools/render_check.py",
        "vaf/core/document_formatting.py", "vaf/tools/coder.py",
        "vaf/tools/coder_templates/__init__.py",
    }
    users = set()
    for rel in expected:
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        if any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "open_for_write"
               for n in ast.walk(tree)):
            users.add(rel)
    assert users == expected
