# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The code audit works on one decided folder: every git call, file read and state write of a
run is checked against the folder it decided on at the start (vaf.core.code_audit), through
`path_jail.open_for_read` and `path_jail.open_folder(create=True)`.

Measured before the change: a process of the account that renamed its project folder aside
and put a link to another account's repository in its place, during 1000 audits, got the
other repository's text to the model through a file the audit read in 11 runs and the audit's
state written into the other repository in 12. The swaps here are deterministic, made inside a
wrapped engine function at the moment that matters, in two scratch repositories: A (the
caller's own, tagged A_OWN) and B (another account's, tagged B_SECRET). Each window covers one
operation and closes again at the next git call where the test says so: a swap left in place
is refused by the next pinned git call as well, which would hide a read that went by path.
"""
import ast
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from test_git_and_notes_folder_pinned import (_git, _repo, _swap, _symlink_or_skip,  # noqa: F401
                                              world)
from vaf.core import code_audit as ca
from vaf.core import path_jail

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_ROOT = Path(__file__).resolve().parent.parent
_HANDLES = pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
_PROC = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"),
                           reason="counts open handles in /proc/self/fd (Linux)")


def _dress(repo: Path, tag: str) -> Path:
    """A change to review: a committed and then edited app.py, an untracked module, a tracked
    guideline file (in the base, so it is read only as a guideline) and the audit's own
    config, each carrying the repository's tag."""
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(f"guideline {tag}_GUIDE\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("-c", "core.hooksPath=", "commit", "-qm", "app", cwd=repo)
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("-c", "core.hooksPath=", "commit", "-qam", "two", cwd=repo)
    (repo / "app.py").write_text(f"def f():\n    return '{tag}_CODE'\n", encoding="utf-8")
    (repo / "new_mod.py").write_text(f"X = '{tag}_NEW'\n", encoding="utf-8")
    (repo / ".vaf").mkdir(exist_ok=True)
    (repo / ".vaf" / "code-audit.json").write_text(json.dumps({"path_instructions": [
        {"path": "*.py", "instructions": f"{tag}_CFG"}]}), encoding="utf-8")
    return repo


@pytest.fixture
def pair(world):
    _dress(world.a, "A_OWN")
    _dress(world.b, "B_SECRET")
    yield world
    _restore(world.a)


def _restore(folder: Path) -> None:
    aside = folder.with_name(folder.name + "-aside")
    if os.path.lexists(aside):
        if os.path.islink(folder):
            os.unlink(folder)
        elif folder.exists():
            shutil.rmtree(folder)
        os.rename(aside, folder)


class _Stub:
    """The model, local: records everything it is sent; reviews with `findings`, confirms
    every finding it is asked to verify. No provider is called."""

    def __init__(self, findings=()):
        self.findings, self.seen = list(findings), []

    def __call__(self, messages, max_tokens):
        text = "\n".join(m.get("content", "") for m in messages)
        self.seen.append(text)
        if "verify code review findings" in messages[0]["content"]:
            n = text.count("--- finding f")
            return json.dumps([{"id": f"f{i}", "verdict": "CONFIRMED", "reason": "seen"}
                               for i in range(n)])
        return json.dumps({"summary": "s", "files": {}, "effort": 1,
                           "findings": self.findings})

    @property
    def text(self) -> str:
        return "\n".join(self.seen)


def _audit(folder, stub, **kw):
    kw.setdefault("checks", False)
    kw.setdefault("verify_steps", 0)
    return ca.code_audit(str(folder), ask=stub, **kw)


def _swap_once(monkeypatch, folder: Path, target: Path, rel: str):
    """Swap ``folder`` for a link to ``target`` right after the audit resolved ``rel`` (its
    `_inside` answer), and put it back before the next git call or the next name it resolves:
    a window that covers the read of that one file and nothing else."""
    real_inside, real_git = ca._inside, ca._git
    state = {"swapped": False, "done": False}

    def _inside(top, name):
        if state["swapped"]:
            state["swapped"] = False
            _restore(folder)
        out = real_inside(top, name)
        if name == rel and not state["done"]:
            state["done"] = state["swapped"] = True
            _swap(folder, target)
        return out

    def _git_restoring(top, *args, **kw):
        if state["swapped"]:
            state["swapped"] = False
            _restore(folder)
        return real_git(top, *args, **kw)

    monkeypatch.setattr(ca, "_inside", _inside)
    monkeypatch.setattr(ca, "_git", _git_restoring)
    return state


def _no_state(repo: Path) -> bool:
    return not os.path.lexists(repo / ".git" / "vaf-audit")


# ── 1. and 2. the primitives ────────────────────────────────────────────────

@pytest.mark.parametrize("handles", [True, False], ids=["handles", "no-handles"])
def test_open_for_read_reads_a_file_and_refuses_a_link_and_a_moved_folder(world, monkeypatch,
                                                                         handles):
    """MUTATION: drop O_NOFOLLOW in open_for_read - red (the link is followed); call
    open_folder without expect_folder there - red (the moved folder is read)."""
    if handles and not path_jail._DIR_FD:
        pytest.skip("no folder handles here")
    monkeypatch.setattr(path_jail, "_DIR_FD", handles and path_jail._DIR_FD)
    a = world.a
    with path_jail.open_for_read(str(a / "f.txt"), expect_folder=str(a)) as f:
        assert f.read() == b"A-OWN\n"
    _symlink_or_skip(a / "link.txt", world.b / "f.txt")
    with pytest.raises(path_jail.PathEscape):
        path_jail.open_for_read(str(a / "link.txt"))
    linked = world.tmp / "linked"
    _symlink_or_skip(linked, world.b)
    with pytest.raises(path_jail.FolderMoved):
        path_jail.open_for_read(str(linked / "f.txt"), expect_folder=str(linked))
    with pytest.raises(FileNotFoundError):
        path_jail.open_for_read(str(a / "missing.txt"), expect_folder=str(a))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_open_for_read_refuses_a_pipe_without_blocking(world):
    """MUTATION: drop O_NONBLOCK - red (the open waits for a writer that never comes)."""
    fifo = world.a / "pipe"
    os.mkfifo(fifo)
    out = []

    def _read():
        try:
            path_jail.open_for_read(str(fifo), expect_folder=str(world.a))
            out.append("read")
        except path_jail.PathEscape:
            out.append("refused")

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(5)
    blocked = t.is_alive()
    if blocked:                         # unblock the reader before failing
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(5)
    assert not blocked and out == ["refused"]


def test_the_read_primitive_replaced_the_notes_readers_own_copy():
    from vaf.core.persistence import PersistenceManager
    import vaf
    assert not hasattr(PersistenceManager, "_made_in")
    assert "open_for_read" in path_jail.__all__
    assert not hasattr(vaf, "open_for_read") and "open_for_read" not in vaf.__all__


@_HANDLES
def test_open_folder_create_makes_the_folder_relative_to_the_handle(world, monkeypatch):
    """The parent is swapped for a link after its handle was opened: the folder is made in
    the folder the handle holds, and a link at the name is refused, not followed.
    MUTATION: os.mkdir without dir_fd (by the name) - red."""
    parent = world.tmp / "p"
    parent.mkdir()
    other = world.tmp / "q"
    other.mkdir()
    pfd = os.open(str(parent), os.O_RDONLY)
    try:
        aside = world.tmp / "p-aside"
        os.rename(parent, aside)
        _symlink_or_skip(parent, other)
        monkeypatch.chdir(other)          # a mkdir by the bare name would land here
        fd, real = path_jail.open_folder("n", dir_fd=pfd, follow=False, create=True,
                                         expect_folder=str(aside / "n"))
        os.close(fd)
        assert real == str(aside / "n") and (aside / "n").is_dir()
        assert not (other / "n").exists()
        _symlink_or_skip(aside / "m", other)
        with pytest.raises(path_jail.PathEscape):
            path_jail.open_folder("m", dir_fd=pfd, follow=False, create=True)
    finally:
        os.close(pfd)
    with pytest.raises(ValueError):
        path_jail.open_folder(str(parent), create=True)


@_HANDLES
def test_open_for_write_with_a_held_folder_writes_only_there(world):
    """The folder is replaced at its name after its handle was taken: the file is written in
    the held folder, the folder now at the name is not touched, and the handle stays open.
    MUTATION: _open_in_folder ignores dir_fd (opens the folder by path) - red."""
    held = world.tmp / "held"
    held.mkdir()
    real = str(held)
    hfd = os.open(real, os.O_RDONLY)
    try:
        aside = world.tmp / "held-aside"
        os.rename(held, aside)
        held.mkdir()
        (held / "x.tmp").write_text("INFLIGHT", encoding="utf-8")
        with pytest.raises(path_jail.WriteRefused):        # the held folder moved away
            path_jail.open_for_write(str(held / "x.tmp"), "w", expect_folder=real, dir_fd=hfd)
        with path_jail.open_for_write(str(held / "x.tmp"), "w", expect_folder=str(aside),
                                      dir_fd=hfd) as f:
            f.write("mine")
        os.fstat(hfd)                                      # still open: the caller's handle
    finally:
        os.close(hfd)
    assert (aside / "x.tmp").read_text(encoding="utf-8") == "mine"
    assert (held / "x.tmp").read_text(encoding="utf-8") == "INFLIGHT"


# ── 3. and 4. a read whose folder was swapped ───────────────────────────────

@pytest.mark.parametrize("rel, mark", [
    ("app.py", "B_SECRET_CODE"),            # a changed file (the numbered code)
    ("new_mod.py", "B_SECRET_NEW"),         # an untracked file
    ("AGENTS.md", "B_SECRET_GUIDE"),        # a guideline file
    (ca.CONFIG_FILE, "B_SECRET_CFG"),       # the config's path instructions
])
@pytest.mark.parametrize("handles", [True, False], ids=["handles", "no-handles"])
def test_a_read_through_a_swapped_folder_ends_the_run(pair, monkeypatch, rel, mark, handles):
    """MUTATIONS, each red for its row: _worktree_text reads `_inside(...)` by path (app.py,
    new_mod.py); the guideline read by path (AGENTS.md); the config read by path (the config);
    without folder handles, drop the realpath comparison in open_for_read (every row)."""
    if handles and not path_jail._DIR_FD:
        pytest.skip("no folder handles here")
    monkeypatch.setattr(path_jail, "_DIR_FD", handles and path_jail._DIR_FD)
    state = _swap_once(monkeypatch, pair.a, pair.b, rel)
    stub = _Stub()
    report = _audit(pair.a, stub, expect_folder=str(pair.a))
    assert state["done"], "the swap never happened"
    assert "B_SECRET" not in stub.text and mark not in stub.text
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    assert report.findings == [] and report.files_reviewed == []
    _restore(pair.a)
    assert _no_state(pair.b) and _no_state(pair.a)


def test_without_a_swap_every_kind_of_text_is_read(pair):
    """The control of the test above: the same run, no swap, reads all four."""
    stub = _Stub()
    report = _audit(pair.a, stub, expect_folder=str(pair.a))
    assert report.status == "complete", report.status_reason
    for mark in ("A_OWN_CODE", "A_OWN_NEW", "A_OWN_GUIDE", "A_OWN_CFG"):
        assert mark in stub.text, mark
    assert "B_SECRET" not in stub.text


# ── 5. and 6. git after a swap, and a folder already swapped ────────────────

def test_a_swap_after_the_top_was_decided_makes_the_next_git_call_refuse(pair, monkeypatch):
    """The folder is a link to B for the length of `_default_base` (its git calls) and back
    afterwards. MUTATION: `_git` without expect_folder=top - red (git answers from B, and
    the run fails for another reason: B's commit is no commit of A)."""
    real = ca._default_base

    def _swapped(top):
        _swap(pair.a, pair.b)
        try:
            return real(top)
        finally:
            _restore(pair.a)

    monkeypatch.setattr(ca, "_default_base", _swapped)
    stub = _Stub()
    report = _audit(pair.a, stub)
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    assert "B_SECRET" not in stub.text and stub.seen == []
    assert _no_state(pair.b)


def test_a_folder_that_is_already_a_link_is_not_audited(pair):
    """MUTATION: _run decides without expect_folder (`_decide(root, None)`) - red (B is
    audited, its text reaches the model and its state is written)."""
    _swap(pair.a, pair.b)
    stub = _Stub()
    report = _audit(pair.a, stub, expect_folder=str(pair.a))
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    assert "B_SECRET" not in stub.text
    assert _no_state(pair.b)


# ── 7. and 8. the state, and a folder replaced at its name ──────────────────

@pytest.mark.parametrize("handles", [True, False], ids=["handles", "no-handles"])
def test_the_state_is_not_written_through_a_swapped_folder(pair, monkeypatch, handles):
    """MUTATION: _save_state back to mkdir(parents=True) + write_text by path - red (B gets
    .git/vaf-audit)."""
    if handles and not path_jail._DIR_FD:
        pytest.skip("no folder handles here")
    monkeypatch.setattr(path_jail, "_DIR_FD", handles and path_jail._DIR_FD)
    real = ca._save_state

    def _swapped(top, name, data):
        if not os.path.islink(pair.a):
            _swap(pair.a, pair.b)
        return real(top, name, data)

    monkeypatch.setattr(ca, "_save_state", _swapped)
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    assert _no_state(pair.b)
    _restore(pair.a)
    assert _no_state(pair.a)


def test_nothing_is_made_at_the_name_of_a_folder_renamed_away(pair, monkeypatch):
    """MUTATION: the same by-path _save_state - red (an A/.git/vaf-audit tree appears at the
    name A no longer has)."""
    aside = pair.a.with_name(pair.a.name + "-aside")
    real = ca._save_state

    def _renamed(top, name, data):
        if pair.a.exists():
            os.rename(pair.a, aside)
        return real(top, name, data)

    monkeypatch.setattr(ca, "_save_state", _renamed)
    _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert not os.path.lexists(pair.a)
    os.rename(aside, pair.a)
    assert _no_state(pair.a)


def test_a_linked_state_folder_is_neither_read_nor_written(pair, monkeypatch):
    """A's `.git/vaf-audit` becomes a link into B's state, whose dismissed.json names A's
    finding, after the run's last git call (the runner refuses a `.git` that holds a link, so
    a link already there fails the whole run): the finding is still reported and B's state is
    unchanged. MUTATIONS: _load_state opens without expect_folder - red (the finding is
    hidden); _save_state opens `vaf-audit` by its path, following links - red (B gets
    last.json)."""
    finding = {"file": "app.py", "start_line": 2, "end_line": 2, "type": "issue",
               "severity": "major", "category": "correctness", "effort": "low",
               "title": "Returns a string", "explanation": "f() returned an int before.",
               "suggestion": "return 1", "evidence": "return 'A_OWN_CODE'"}
    first = _audit(pair.a, _Stub([finding]), remember=False)
    [f] = first.findings
    b_state = pair.b / ".git" / "vaf-audit"
    b_state.mkdir()
    (b_state / "dismissed.json").write_text(json.dumps({f.id: {"reason": "no"}}),
                                            encoding="utf-8")
    before = sorted(os.listdir(b_state))
    link = pair.a / ".git" / "vaf-audit"
    real = ca._load_state

    def _planting(top, name):
        if not os.path.lexists(link):
            _symlink_or_skip(link, b_state)
        return real(top, name)

    monkeypatch.setattr(ca, "_load_state", _planting)
    again = _audit(pair.a, _Stub([finding]), expect_folder=str(pair.a))
    assert again.status == "complete", again.status_reason
    assert [x.id for x in again.findings] == [f.id] and again.dismissed == 0
    assert sorted(os.listdir(b_state)) == before
    assert json.loads((b_state / "dismissed.json").read_text(encoding="utf-8")) == {
        f.id: {"reason": "no"}}
    # With the link in place before the run, the runner refuses the repository as a whole.
    monkeypatch.setattr(ca, "_load_state", real)
    refused = _audit(pair.a, _Stub([finding]), expect_folder=str(pair.a))
    assert refused.status == "failed" and refused.findings == []
    assert sorted(os.listdir(b_state)) == before


def test_a_folder_replaced_at_its_name_before_the_end_fails_the_run(pair, monkeypatch):
    """Another repository is moved to A's name between the review and the end of the run; it
    has audit state of its own, so nothing on the way to the save notices a missing file.
    MUTATION: `_state_handle` without the identity comparison - red (the new folder's state
    is overwritten; the check on the way out still fails the run)."""
    aside = pair.a.with_name(pair.a.name + "-aside")
    real = ca._dedupe
    kept = {"text": "OTHER's own last audit"}

    def _replaced(found):
        os.rename(pair.a, aside)
        _repo(pair.a, "OTHER", "OTHER_ONLY")
        (pair.a / ".git" / "vaf-audit").mkdir()
        (pair.a / ".git" / "vaf-audit" / "last.json").write_text(json.dumps(kept),
                                                                 encoding="utf-8")
        return real(found)

    monkeypatch.setattr(ca, "_dedupe", _replaced)
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    assert json.loads((pair.a / ".git" / "vaf-audit" / "last.json").read_text(
        encoding="utf-8")) == kept
    assert _no_state(aside)
    shutil.rmtree(pair.a)
    os.rename(aside, pair.a)


def _replace_with_repo(folder: Path, kept: dict) -> Path:
    """Move ``folder`` aside and put another repository at its name, with audit state of
    its own (``kept`` as its last.json). Returns the aside path."""
    aside = folder.with_name(folder.name + "-aside")
    os.rename(folder, aside)
    _repo(folder, "OTHER", "OTHER_ONLY")
    (folder / ".git" / "vaf-audit").mkdir()
    (folder / ".git" / "vaf-audit" / "last.json").write_text(json.dumps(kept), encoding="utf-8")
    return aside


def _put_back(folder: Path, aside: Path) -> None:
    shutil.rmtree(folder)
    os.rename(aside, folder)


@_HANDLES
def test_a_folder_replaced_after_the_end_check_gets_no_state(pair, monkeypatch):
    """Another repository is moved to A's name at `_save_state`'s entry, after the end check:
    its state is not overwritten and the run fails. MUTATION: `_state_handle` without the
    identity comparison (.git opened by its real path alone) - red."""
    kept = {"text": "OTHER's own last audit"}
    real = ca._save_state
    moved = {}

    def _replaced(top, name, data):
        if not moved:
            moved["aside"] = _replace_with_repo(pair.a, kept)
        return real(top, name, data)

    monkeypatch.setattr(ca, "_save_state", _replaced)
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    state = pair.a / ".git" / "vaf-audit"
    assert sorted(os.listdir(state)) == ["last.json"]
    assert json.loads((state / "last.json").read_text(encoding="utf-8")) == kept
    assert _no_state(moved["aside"])
    _put_back(pair.a, moved["aside"])


@_HANDLES
def test_the_state_file_is_written_only_in_the_held_folder(pair, monkeypatch):
    """Another repository is moved to A's name after the state folder's handle was taken and
    before the file is opened (the same real path): nothing of it is opened, so its own
    in-flight temporary file keeps its text. MUTATION: open the temporary file by its path
    (no dir_fd) - red (the other repository's last.json.tmp is truncated)."""
    kept = {"text": "OTHER's own last audit"}
    real = ca._state_handle
    moved = {}

    def _then_replaced(top, d, folder):
        fd = real(top, d, folder)
        if not moved:
            moved["aside"] = _replace_with_repo(pair.a, kept)
            (pair.a / ".git" / "vaf-audit" / "last.json.tmp").write_text("INFLIGHT",
                                                                         encoding="utf-8")
        return fd

    monkeypatch.setattr(ca, "_state_handle", _then_replaced)
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed"
    state = pair.a / ".git" / "vaf-audit"
    assert sorted(os.listdir(state)) == ["last.json", "last.json.tmp"]
    assert json.loads((state / "last.json").read_text(encoding="utf-8")) == kept
    assert (state / "last.json.tmp").read_text(encoding="utf-8") == "INFLIGHT"
    _put_back(pair.a, moved["aside"])


def test_a_dismissal_is_not_written_into_a_folder_moved_to_the_name(pair, monkeypatch):
    """MUTATION: `_state_handle` without the identity comparison - red (the other
    repository gets dismissed.json and the call says True)."""
    kept = {"text": "OTHER's own last audit"}
    real = ca._save_state
    moved = {}

    def _replaced(top, name, data):
        if not moved:
            moved["aside"] = _replace_with_repo(pair.a, kept)
        return real(top, name, data)

    monkeypatch.setattr(ca, "_save_state", _replaced)
    assert ca.dismiss_finding(str(pair.a), "abc", "no") is False
    assert not (pair.a / ".git" / "vaf-audit" / "dismissed.json").exists()
    _put_back(pair.a, moved["aside"])


def test_an_empty_listing_from_a_replaced_folder_is_not_a_clean_result(pair, monkeypatch):
    """The folder is replaced while the listing comes back empty: the run must not end as
    "nothing to review", complete. MUTATION: drop the check on the common exit path - red."""
    real = ca._collect
    moved = {}

    def _replaced(*args, **kwargs):
        real(*args, **kwargs)
        moved["aside"] = _replace_with_repo(pair.a, {"text": "x"})
        return [], [], None

    monkeypatch.setattr(ca, "_collect", _replaced)
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED
    _put_back(pair.a, moved["aside"])


@pytest.mark.parametrize("link", ["evil", "timed out after", "Git is not installed."])
def test_a_refused_git_call_is_never_read_as_an_empty_answer(pair, monkeypatch, link):
    """A link planted inside `.git` during the run makes the runner refuse every later call;
    `check-ignore` read as "not ignored" sent a git-ignored guideline file to the model. The
    refusal quotes the link's name, so a name like the timeout message must not turn it into a
    timeout. MUTATIONS: only the moved-folder words count as a refusal in `_git` - red
    ("evil"); a timeout or a missing git told by the words in stderr again - red (the two
    other names)."""
    (pair.a / ".gitignore").write_text("sub/AGENTS.md\n", encoding="utf-8")
    (pair.a / "sub" / "AGENTS.md").write_text("PRIVATE_NOTES of the person\n", encoding="utf-8")
    (pair.a / "sub" / "x.txt").write_text("x2\n", encoding="utf-8")
    real = ca._guidelines

    def _planting(top, files):
        _symlink_or_skip(pair.a / ".git" / link, pair.b / ".git")
        return real(top, files)

    monkeypatch.setattr(ca, "_guidelines", _planting)
    stub = _Stub()
    report = _audit(pair.a, stub, expect_folder=str(pair.a))
    assert "PRIVATE_NOTES" not in stub.text
    assert report.status == "failed" and report.status_reason.startswith("git refused")
    assert "is a link" in report.status_reason
    os.unlink(pair.a / ".git" / link)


def _runner_answering(monkeypatch, when, kind, stderr):
    """`run_project_git` fails (its own `ProjectGitFailed`, ``kind``, ``stderr``) for the call
    one of whose arguments ends with ``when``; every other call runs."""
    import vaf.core.git_runner as gr
    real = gr.run_project_git

    def _run(args, *a, **kw):
        if any(str(a).endswith(when) for a in args):
            return gr._failed(["git", *args], kind, stderr)
        return real(args, *a, **kw)

    monkeypatch.setattr(gr, "run_project_git", _run)


def test_a_private_view_that_cannot_start_is_reported_as_it_is(pair, monkeypatch):
    """bubblewrap failing on this host is no moved folder. MUTATION: count "could not start
    in a private view" as a moved folder again - red (the reason says the folder changed)."""
    _runner_answering(monkeypatch, "merge-base", "refused", "could not start in a private view of "
                      f"{pair.a} (exit 1). Try again")
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed"
    assert report.status_reason.startswith("git refused the folder under review (could not "
                                           "start in a private view")


def test_a_refusal_while_the_base_is_resolved_is_not_a_missing_commit(pair, monkeypatch):
    """The runner refuses the base's rev-parse (the folder was away for that call and back
    by the next check). MUTATION: ignore resolve_commit's errors - red ("names no commit")."""
    _runner_answering(monkeypatch, "^{commit}", "refused",
                      f"{pair.a}: its folder is /elsewhere, not "
                      f"{pair.a} (a link on the way)")
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "failed" and report.status_reason == ca.FOLDER_CHANGED


def test_a_search_that_timed_out_still_finds_nothing(pair, monkeypatch):
    """A timeout is no refusal: the reference search (git grep) finds nothing and the run goes
    on. MUTATION: drop "timeout" from _NOT_REFUSALS - red."""
    (pair.a / "app.py").write_text("def total_of(xs):\n    return 'A_OWN_CODE'\n",
                                   encoding="utf-8")
    _runner_answering(monkeypatch, "grep", "timeout", "Command '['git', 'grep']' timed out "
                      "after 20 seconds")
    report = _audit(pair.a, _Stub(), expect_folder=str(pair.a))
    assert report.status == "complete", report.status_reason


# ── 9. the callers pass the folder they judged ──────────────────────────────

def test_every_audit_call_of_the_tools_passes_the_judged_folder():
    """MUTATION: remove expect_folder= at one call site - red."""
    calls = []
    for rel in ("vaf/tools/coder.py", "vaf/tools/code_audit.py"):
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "code_audit":
                calls.append((rel, node.lineno, {k.arg for k in node.keywords}))
    assert len(calls) == 2, calls
    for rel, line, keywords in calls:
        assert "expect_folder" in keywords, f"{rel}:{line}"


def test_the_tool_audits_the_folder_it_resolved(pair, monkeypatch):
    """The tool is handed a link to the project: the engine gets the resolved folder as both
    `root` and `expect_folder`. MUTATION: pass expect_folder=None in the tool - red."""
    import vaf.core.code_audit as engine
    from vaf.tools.code_audit import CodeAuditTool
    seen = {}

    def _record(root, **kw):
        seen.update(root=root, expect=kw.get("expect_folder"))
        return ca.AuditReport(root=root, scope="changes", status="failed", status_reason="x")

    monkeypatch.setattr(engine, "code_audit", _record)
    link = pair.tmp / "link-to-a"
    _symlink_or_skip(link, pair.a)
    out = CodeAuditTool().run(project_path=str(link))
    assert seen == {"root": str(pair.a), "expect": str(pair.a)}, out


# ── 10. what stays ──────────────────────────────────────────────────────────

def test_an_inside_link_is_read_an_outside_link_skipped_and_a_subfolder_audits_its_repo(pair):
    """MUTATION: refuse every link in the read helper (open the name as given) - red (the
    CLAUDE.md that links to AGENTS.md is no guideline any more)."""
    _symlink_or_skip(pair.a / "CLAUDE.md", "AGENTS.md")
    _symlink_or_skip(pair.a / "GEMINI.md", pair.b / "AGENTS.md")
    _symlink_or_skip(pair.a / "leak.py", pair.b / "app.py")
    stub = _Stub()
    report = _audit(pair.a / "sub", stub)
    assert report.status == "complete", report.status_reason
    assert os.path.normcase(report.root) == os.path.normcase(str(pair.a))
    assert "--- AGENTS.md ---\nguideline A_OWN_GUIDE" in stub.text
    assert "--- CLAUDE.md ---\nguideline A_OWN_GUIDE" in stub.text
    assert "B_SECRET" not in stub.text
    assert ("leak.py", "points outside the repository") in report.files_skipped
    assert (pair.a / ".git" / "vaf-audit" / "last.json").is_file()


def test_the_last_report_and_a_dismissal_use_the_same_checked_state(pair):
    _audit(pair.a, _Stub())
    assert ca.last_report(str(pair.a))
    assert ca.dismiss_finding(str(pair.a), "abc", "not a bug")
    data = ca._load_state(str(pair.a), "dismissed.json")
    assert data["abc"]["reason"] == "not a bug"
    _swap(pair.a, pair.b)
    assert ca.last_report(str(pair.a)) is None or "B_SECRET" not in ca.last_report(str(pair.a))


# ── 12. no handle is left open ──────────────────────────────────────────────

@_PROC
def test_no_handle_is_left_open_after_audits_kept_and_refused(pair, monkeypatch):
    """MUTATION: drop one os.close in _save_state - red."""
    real = ca._default_base
    flip = {"n": 0}

    def _maybe_swapped(top):
        flip["n"] += 1
        if flip["n"] % 2:
            return real(top)
        _swap(pair.a, pair.b)
        try:
            return real(top)
        finally:
            _restore(pair.a)

    _audit(pair.a, _Stub())                     # warm-up: imports, lazy handles
    before = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(ca, "_default_base", _maybe_swapped)
    statuses = [_audit(pair.a, _Stub(), expect_folder=str(pair.a)).status for _ in range(20)]
    after = len(os.listdir("/proc/self/fd"))
    assert statuses.count("failed") == 10 and statuses.count("complete") == 10, statuses
    assert after == before
