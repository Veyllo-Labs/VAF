# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coding loop books a call the file tools refused as a failure, never as a read or a write.

Measured before the change: the coder's real `write_file`/`edit_file`/`read_file` ran through
the real `_coder_funnel`, and their refusals (`Access denied: .env`, `Access denied: .git`,
`Access denied: outside your own data`, ...) passed none of the loop's four hand-written error
lists. A refused write of an existing file was answered `Created <path>` to the model and listed
as created in the summary, the task list and a download chip; a refused read had its file
published to the live editor by the mirror, which opens the path with the process's own rights.
The other way round, a successful write whose name contains "locked" counted as an error.

The loop now asks one helper, `_loop_call_failed`: the classifier every lane shares
(`tool_result_is_error`, `content_carrying` from the tool's `result_is_deliverable`) plus
`abort_kind`. The duplicate-write shortcut asks the file screen and the caller's jail before it
opens a file. No LLM and no network: the tools run for real in a scratch project under a
scratch home.

MUTATIONS, each turns the named test red:
- the body of `_loop_call_failed` replaced by the old hand list (prefixes plus the "locked" /
  "permission denied" substrings) -> test_refusals_through_the_funnel_are_failures,
  test_successes_stay_successes (the `blocked_locked.html` write);
- `content_carrying=` dropped, or write_file/edit_file no longer judged content-carrying
  -> test_a_write_whose_path_has_failure_words_is_a_write;
- the read_file branch dropped (a read judged by the shared classifier's wide openers)
  -> test_a_read_of_any_text_is_a_read;
- the guard's write branch opened with `if True:` (or not on the helper's answer), or its read
  branch without `not _gfailed` -> test_the_guard_books_only_what_the_helper_calls_done;
- `_project_read_path` returning the path unchanged, or not called for read_file
  -> test_a_relative_read_is_the_projects_file;
- `_live_view_target` without its `is_safe_path` or its `jail_allows` question, or the
  mirror opening its own spelling -> test_the_live_view_opens_only_what_the_read_screen_allows;
- `_preview_old_lines` without its `is_safe_path` or its `jail_allows` question, or the
  preview opening the path itself -> test_the_write_preview_never_opens_a_refused_file;
- the duplicate-write shortcut without the dispatch refusal in front of it
  -> test_the_duplicate_write_shortcut_asks_the_dispatch_first;
- ReadFileTool answering a read the OS refused with the bare exception text again, or
  "Tool Error:" dropped from `_READ_FAILURE_OPENERS`
  -> test_every_failed_read_opens_with_an_opener;
- the call site's `if fn_name == "read_file" and fn_args.get("path"):` turned off
  -> test_a_relative_read_is_the_projects_file;
- `_live_view_target` without its size cap or its binary-suffix check
  -> test_the_live_view_shows_only_small_text_files;
- the `abort_kind` check dropped -> test_funnel_and_abort_answers_are_failures;
- the `is_error_result = (` list restored in `run()`, the guard's write check back to
  `.startswith("error")`, `and not is_error_result` dropped from the mirror, or the refusal
  cause line dropped from the stop message -> test_the_loop_books_through_the_one_classifier;
- the old guard comment restored -> test_the_guard_comment_no_longer_claims_refusals_stay_out;
- `_identical_rewrite` without its `is_safe_path` question
  -> test_identical_rewrite_never_answers_for_a_refused_path;
- without its `jail_allows` question
  -> test_identical_rewrite_never_opens_a_file_outside_the_jail;
- `scope`/`role` given defaults, or the call site without them
  -> test_identical_rewrite_takes_the_caller_by_keyword.
"""
import ast
import builtins
import inspect
import os
import re
from pathlib import Path

import pytest

import vaf.tools.filesystem as fs

A = "ab12cd34-0000-4000-8000-00000000000a"
B = "cd34ef56-0000-4000-8000-00000000000b"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A scratch home (HOME, USERPROFILE, XDG) with a project of account A and a file of
    account B, and the coder's three file tools behind the real funnel."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for var in ("VAF_USER_SCOPE_ID", "VAF_USER_ROLE", "VAF_IN_SUBAGENT_TERMINAL",
                "VAF_SESSION_ID", "VAF_PARENT_CWD"):
        monkeypatch.delenv(var, raising=False)
    fs._shared_room_roots_cache.clear()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    from vaf.core.tool_dispatch import set_account_allowlist_resolver
    set_account_allowlist_resolver(None)

    from vaf.core.session import get_user_projects_root
    proj = get_user_projects_root(A) / "proj"
    proj.mkdir(parents=True)
    (proj / ".env").write_text("OLD=1\n", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".vaf").mkdir()
    (proj / ".vaf" / "codex.md").write_text("notes\n", encoding="utf-8")
    (proj / "index.html").write_text("<p>old</p>\n", encoding="utf-8")
    b_proj = get_user_projects_root(B) / "proj"
    b_proj.mkdir(parents=True)
    (b_proj / "secret.txt").write_text("B-SECRET\n", encoding="utf-8")

    from vaf.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
    tools = {"write_file": WriteFileTool(), "edit_file": EditFileTool(),
             "read_file": ReadFileTool()}

    def run(name, args, *, scope=None, role=None):
        from vaf.tools.coder import _coder_funnel
        out = _coder_funnel(tools, scope=scope, role=role, session_id=None).execute(name, args)
        return str(out)

    class World:
        pass
    w = World()
    w.home, w.proj, w.b_proj, w.tools, w.run = home, proj, b_proj, tools, run
    yield w
    set_account_allowlist_resolver(None)
    fs._shared_room_roots_cache.clear()


def _failed(world, name, result):
    from vaf.tools.coder import _loop_call_failed
    return _loop_call_failed(world.tools, name, result)


# ── AC1-4: the one classifier, on the tools' real answers ─────────────────────────────

def test_refusals_through_the_funnel_are_failures(world):
    p = world.proj
    cases = [
        ("write_file", {"path": str(p / ".env"), "content": "NEW=1"}, None, None),
        ("edit_file", {"path": str(p / ".env"), "edits": [{"search": "OLD=1", "replace": "NEW=1"}]},
         None, None),
        ("write_file", {"path": str(p / ".git" / "hooks" / "pre-commit"), "content": "x"},
         None, None),
        ("write_file", {"path": str(p / "node_modules" / "x.js"), "content": "x"}, None, None),
        ("read_file", {"path": str(p / ".env")}, None, None),
        ("read_file", {"path": str(p / ".vaf" / "codex.md")}, None, None),
        ("write_file", {"path": str(p / ".vaf" / "codex.md"), "content": "x"}, None, None),
        # A non-admin reading another account's file: the jail's own refusal.
        ("read_file", {"path": str(world.b_proj / "secret.txt")}, A, "user"),
        ("write_file", {"path": str(world.b_proj / "secret.txt"), "content": "x"}, A, "user"),
    ]
    for name, args, scope, role in cases:
        out = world.run(name, args, scope=scope, role=role)
        assert out.startswith("Access denied:"), (name, args, out)
        assert _failed(world, name, out), (name, args, out)
    # Nothing was changed by the refused calls.
    assert (p / ".env").read_text(encoding="utf-8") == "OLD=1\n"
    assert (world.b_proj / "secret.txt").read_text(encoding="utf-8") == "B-SECRET\n"
    assert not (p / "node_modules").exists()
    from vaf.tools.filesystem import REMOTE_PATH_REFUSAL
    assert _failed(world, "write_file", REMOTE_PATH_REFUSAL)
    assert _failed(world, "read_file", REMOTE_PATH_REFUSAL)


def test_successes_stay_successes(world):
    p = world.proj
    out = world.run("write_file", {"path": str(p / "blocked_locked.html"), "content": "<p>x</p>"})
    assert (p / "blocked_locked.html").exists(), out
    assert not _failed(world, "write_file", out), out

    out = world.run("edit_file", {"path": str(p / "index.html"),
                                  "edits": [{"search": "old", "replace": "new"}]})
    assert "new" in (p / "index.html").read_text(encoding="utf-8"), out
    assert not _failed(world, "edit_file", out), out

    (p / "notes.txt").write_text(
        "Release notes\n"
        "❌ marks a step that failed tool execution last time\n"
        "Traceback (most recent call last):\n  File \"x.py\", line 1\n",
        encoding="utf-8")
    out = world.run("read_file", {"path": str(p / "notes.txt")})
    assert "Traceback (most recent call last)" in out, out
    assert not _failed(world, "read_file", out), out

    # What the loop puts in place of a booked write stays a success for the guard tracking.
    assert not _failed(world, "write_file", f"✓ Created {p / 'locked_door.html'} (8 bytes)")


def test_funnel_and_abort_answers_are_failures(world):
    from vaf.core.bounded_run import STOPPED_PREFIX, TIMEOUT_PREFIX
    from vaf.core.tool_dispatch import set_account_allowlist_resolver
    p = world.proj

    out = world.run("write_file", {"path": str(p / "a.txt")})
    assert out.startswith("Tool Error:"), out
    assert _failed(world, "write_file", out)

    out = world.run("edit_file", {"path": str(p / "index.html"),
                                  "edits": [{"search": "not there", "replace": "x"}]})
    assert out.startswith("EDIT FAILED"), out
    assert _failed(world, "edit_file", out)

    out = world.run("read_file", {"path": str(p / "missing.txt")})
    assert out.startswith("Error:"), out
    assert _failed(world, "read_file", out)

    set_account_allowlist_resolver(lambda scope: {"read_file"} if scope == A else None)
    out = world.run("write_file", {"path": str(p / "b.txt"), "content": "x"}, scope=A,
                    role="user")
    assert out.startswith("Security Error:"), out
    assert _failed(world, "write_file", out)
    assert not (p / "b.txt").exists()

    for prefix in (TIMEOUT_PREFIX, STOPPED_PREFIX):
        for name in ("write_file", "read_file"):
            assert _failed(world, name, f"{prefix} {name} did not finish"), (prefix, name)


# ── AC5-9, AC11: the loop asks the helper and nothing else ────────────────────────────

def _run_source():
    from vaf.tools.coder import CodingAgentTool
    return inspect.getsource(CodingAgentTool.run)


def test_the_loop_books_through_the_one_classifier():
    src = _run_source()
    assigned = re.findall(r"\bis_error_result\s*=(?!=)", src)
    assert len(assigned) == 1, assigned
    assert "is_error_result = _loop_call_failed(self.local_tools, fn_name, result_str)" in src

    start = src.index("is_error_result = _loop_call_failed(")
    block = src[start:src.index("# Track created files", start)]
    for old in ('startswith("❌")', 'startswith("Error:")', '"locked" in'):
        assert old not in block, old
    assert 'if fn_name == "read_file" and not is_error_result:' in block

    track = src[src.index("# Track created files", start):]
    head = track[:track.index("if is_error_result:") + len("if is_error_result:")]
    assert 'if fn_name in ("write_file", "edit_file") and "path" in fn_args:' in head
    assert len(head) < 300, "the write tracking no longer opens with the error branch"

    stop = track[:track.index("elif os.path.exists(path):")]
    assert ("- The file tools refuse this path (the error says why: VAF's own data, a protected "
            "folder or file name, or outside this account's folders)") in stop
    assert "Write a different path if the error says Access denied" in stop

    g0 = src.index("loop.guard_seq += 1")
    guard = src[g0:src.index("_result_char_limit", g0)]
    assert "_loop_call_failed(" in guard
    assert '.startswith("error")' not in guard and '.startswith("Error")' not in guard
    assert "if _gpath and not _gfailed:" in guard

    # The bash echo check keeps its own question, with its reason written down.
    echo = src[src.index("is_error = ("):src.index("if is_error and fn_name == \"bash\":")]
    assert '"locked" in result_str.lower()' in echo
    above = src[:src.index("is_error = (")][-400:]
    assert "Deliberately not _loop_call_failed" in above


def test_the_guard_comment_no_longer_claims_refusals_stay_out():
    src = _run_source()
    assert "so a refused write does not count as a write" not in src
    assert "a call the tool itself refused or failed does" in src


# ── AC10: the duplicate-write shortcut asks the screens first ─────────────────────────

def test_identical_rewrite_never_answers_for_a_refused_path(world):
    from vaf.tools.coder import _identical_rewrite
    p = world.proj
    assert _identical_rewrite(".env", "OLD=1", str(p), scope=None, role=None) is None
    assert _identical_rewrite(str(p / ".vaf" / "codex.md"), "notes", str(p),
                              scope=None, role=None) is None
    # The same question for an unprotected file still answers.
    assert _identical_rewrite("index.html", "<p>old</p>", str(p),
                              scope=None, role=None) == str(p / "index.html")


def test_identical_rewrite_never_opens_a_file_outside_the_jail(world, monkeypatch):
    from vaf.tools.coder import _identical_rewrite
    target = str(world.b_proj / "secret.txt")
    opened = []
    real_open = builtins.open

    def recording_open(file, *a, **k):
        if os.path.abspath(str(file)) == target:
            opened.append(file)
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", recording_open)
    assert _identical_rewrite(target, "B-SECRET", str(world.proj), scope=A, role="user") is None
    assert opened == [], opened
    # Without an account (a direct consumer) the jail does not apply, so the file is read.
    assert _identical_rewrite(target, "B-SECRET", str(world.proj),
                              scope=None, role=None) == target
    assert opened, "the unjailed caller should have read the file"


def test_identical_rewrite_takes_the_caller_by_keyword():
    from vaf.tools.coder import _identical_rewrite
    params = inspect.signature(_identical_rewrite).parameters
    for key in ("scope", "role"):
        assert params[key].kind is inspect.Parameter.KEYWORD_ONLY, key
        assert params[key].default is inspect.Parameter.empty, key
    src = _run_source()
    assert re.search(r"_identical_rewrite\(\s*path, content, base_dir,\s*scope=caller_scope, "
                     r"role=caller_role\)", src)


# ── round two: the success shapes, the guard's write branch, the loop's own file opens ──

def test_a_write_whose_path_has_failure_words_is_a_write(world):
    p = world.proj
    for name in ("failed_tool_calls.py", "tool_execution_failed.py"):
        out = world.run("write_file", {"path": str(p / name), "content": "x = 1\n"})
        assert (p / name).exists(), out
        assert not _failed(world, "write_file", out), out
    (p / "tool_failed.py").write_text("x = 1\n", encoding="utf-8")
    out = world.run("edit_file", {"path": str(p / "tool_failed.py"),
                                  "edits": [{"search": "x = 1", "replace": "x = 2"}]})
    assert "x = 2" in (p / "tool_failed.py").read_text(encoding="utf-8"), out
    assert not _failed(world, "edit_file", out), out
    for text in (f"⚠️ File written but size verification failed: {p / 'tools' / 'a.py'}",
                 f"✓ Created {p / 'failed_tool_execution.py'} (6 bytes)"):
        assert not _failed(world, "write_file", text), text
    # The write and edit failures still open the answer, so they stay failures.
    for text in ("❌ Permission denied: Cannot write to /p/x", "EDIT FAILED: edit 0's search",
                 "Error: edit_file needs a 'path'.", "Access denied: .env"):
        assert _failed(world, "write_file", text) and _failed(world, "edit_file", text), text


def test_a_read_of_any_text_is_a_read(world):
    p = world.proj
    openers = ["Failed attempts are logged below.", "Exception handling in this module",
               "Cannot be empty: the name field", "error log of the last build",
               "Refused: requests without a token", "[WARN] deprecated option",
               "Tests failed: 3 of 10", "Blocked: waiting for review",
               "Invalid input is rejected", "Build failed: see below", "❌ broken"]
    for i, first in enumerate(openers):
        f = p / f"doc{i}.md"
        f.write_text(first + "\nmore text\n", encoding="utf-8")
        out = world.run("read_file", {"path": str(f)})
        assert out.startswith(first), out
        assert not _failed(world, "read_file", out), first
    # What the read tool and the funnel answer for a failed read stays a failure.
    for text in ("Error reading PDF: broken xref", "Error: Unknown tool 'read_file'",
                 "Invalid path", "Security Error: not enabled"):
        assert _failed(world, "read_file", text), text


def _run_tree():
    """CodingAgentTool.run as a syntax tree, parsed from its module (its source holds string
    literals at column 0, so it cannot be dedented and parsed on its own)."""
    import vaf.tools.coder as coder
    module = ast.parse(Path(inspect.getsourcefile(coder)).read_text(encoding="utf-8"))
    cls = next(n for n in module.body
               if isinstance(n, ast.ClassDef) and n.name == "CodingAgentTool")
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "run")


def _assigns(node, attr_owner, attr):
    return any(isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Attribute) and t.attr == attr and isinstance(t.value, ast.Name)
        and t.value.id == attr_owner for t in n.targets) for n in ast.walk(node))


def test_the_guard_books_only_what_the_helper_calls_done():
    tree = _run_tree()
    names = {n.targets[0].id for n in ast.walk(tree)
             if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call)
             and getattr(n.value.func, "id", "") == "_loop_call_failed"}
    guard_names = names - {"is_error_result"}
    assert guard_names, "the guard tracking no longer asks _loop_call_failed"

    def is_not_failed(test):
        return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name) and test.operand.id in guard_names)

    # The write branch: the If that sets current_state.last_write_seq opens on "not failed".
    write_ifs = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                 and any(_assigns(b, "current_state", "last_write_seq") for b in n.body)
                 and not any(isinstance(b, ast.If) and _assigns(b, "current_state", "last_write_seq")
                             for b in n.body)]
    assert write_ifs, "no branch sets last_write_seq"
    assert all(is_not_failed(n.test) for n in write_ifs), [ast.unparse(n.test) for n in write_ifs]

    # The read branch: the If that adds to files_known under fn_name == "read_file".
    read_branch = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                   and ast.unparse(n.test) == "fn_name == 'read_file'"
                   and "files_known.add" in ast.unparse(n)]
    assert len(read_branch) == 1
    inner = read_branch[0].body[0]
    assert isinstance(inner, ast.If) and isinstance(inner.test, ast.BoolOp)
    assert any(is_not_failed(v) for v in inner.test.values), ast.unparse(inner.test)


@pytest.fixture
def linked(world, tmp_path, monkeypatch):
    """A harmless notes.txt in the process's working folder, and notes.txt in the project as a
    link to a protected file with a synthetic secret."""
    secret = world.home / ".vaf" / "config.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"api_key_x": "SYNTHETIC-SECRET"}\n', encoding="utf-8")
    try:
        os.symlink(secret, world.proj / "notes.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "notes.txt").write_text("harmless\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    return world


def test_a_relative_read_is_the_projects_file(linked):
    from vaf.tools.coder import _live_view_target, _project_read_path
    w = linked
    path = _project_read_path("notes.txt", str(w.proj))
    assert os.path.normpath(path) == os.path.normpath(str(w.proj / "notes.txt"))
    out = w.run("read_file", {"path": path})
    assert out.startswith("Access denied:"), out
    assert _failed(w, "read_file", out)
    assert _live_view_target(path, scope=None, role=None) is None
    # Absolute and home paths are handed on as given.
    assert _project_read_path(str(w.proj / "index.html"), "/elsewhere") == str(w.proj / "index.html")
    assert _project_read_path("~/x.txt", str(w.proj)) == "~/x.txt"
    src = _run_source()
    assert 'fn_args["path"] = _project_read_path(str(fn_args["path"]), base_dir)' in src
    # The join runs for every read_file call that has a path, and only for those.
    site = [n for n in ast.walk(_run_tree()) if isinstance(n, ast.If)
            and "_project_read_path(" in ast.unparse(n.body[0])
            and not any(isinstance(b, ast.If) for b in n.body)]
    assert len(site) == 1
    assert ast.unparse(site[0].test) == "fn_name == 'read_file' and fn_args.get('path')"
    # Before the tool runs, so the tool, the mirror and files_known judge the same file.
    assert src.index("_project_read_path(str(fn_args") < src.index("# THE FUNNEL, like every")


def test_the_live_view_opens_only_what_the_read_screen_allows(linked):
    from vaf.tools.coder import _live_view_target
    w = linked
    p = w.proj
    for path, scope, role in ((str(p / ".env"), None, None),
                              (str(p / ".vaf" / "codex.md"), None, None),
                              (str(p / "notes.txt"), None, None),
                              (str(w.b_proj / "secret.txt"), A, "user")):
        assert _live_view_target(path, scope=scope, role=role) is None, path
    assert _live_view_target(str(p / "index.html"), scope=A, role="user") == str(p / "index.html")
    assert _live_view_target(str(w.b_proj / "secret.txt"), scope=None, role=None) == str(
        w.b_proj / "secret.txt")
    src = _run_source()
    mirror = src[src.index('if fn_name == "read_file" and not is_error_result:'):
                 src.index("# Track created files")]
    assert "_live_view_target(" in mirror
    assert "os.path.join(base_dir" not in mirror


def test_the_write_preview_never_opens_a_refused_file(world, monkeypatch):
    from vaf.tools.coder import _preview_old_lines
    p = world.proj
    refused = {str(p / ".env"), str(world.b_proj / "secret.txt"), str(p / ".vaf" / "codex.md")}
    opened = []
    real_open = builtins.open

    def recording_open(file, *a, **k):
        if os.path.abspath(str(file)) in refused:
            opened.append(file)
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", recording_open)
    assert _preview_old_lines(str(p / ".env"), scope=None, role=None) is None
    assert _preview_old_lines(str(p / ".vaf" / "codex.md"), scope=None, role=None) is None
    assert _preview_old_lines(str(world.b_proj / "secret.txt"), scope=A, role="user") is None
    assert opened == [], opened
    assert _preview_old_lines(str(p / "index.html"), scope=A, role="user") == ["<p>old</p>\n"]
    assert _preview_old_lines(str(p / "new.html"), scope=None, role=None) is None
    src = _run_source()
    start = src.index("# Generate Diff or Content Preview")
    block = src[start:src.index("tui.set_code_preview(fname, preview_content", start)]
    assert "_preview_old_lines(path, scope=caller_scope" in block
    assert "open(" not in block


def test_the_duplicate_write_shortcut_asks_the_dispatch_first(world):
    from vaf.core.coder_tools import resolve_coder_tools
    from vaf.tools.coder import _coder_dispatch_refusal
    refusal = _coder_dispatch_refusal("write_file", world.tools["write_file"],
                                      coder_allowed=resolve_coder_tools(),
                                      caller_allowed={"read_file"}, scope=A, role="user",
                                      session_id=None)
    assert refusal and refusal.startswith("Security Error:"), refusal

    tree = _run_tree()
    fallback = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "'path' in extracted_args and 'content' in extracted_args"]
    assert len(fallback) == 1
    block = fallback[0]
    calls = [n for n in ast.walk(block) if isinstance(n, ast.Call)]
    ask = [c for c in calls if getattr(c.func, "id", "") == "_coder_dispatch_refusal"]
    rewrite = [c for c in calls if getattr(c.func, "id", "") == "_identical_rewrite"]
    assert len(ask) == 1 and len(rewrite) == 1
    assert ask[0].lineno < rewrite[0].lineno
    # Asked by the name the model used, like every dispatch check (an alias is its own name).
    assert getattr(ask[0].args[0], "id", "") == "_requested_fn_name"
    # The rewrite runs only when the dispatch refused nothing.
    cond = [n for n in ast.walk(block) if isinstance(n, ast.IfExp)
            and any(c is rewrite[0] for c in ast.walk(n.orelse))]
    assert cond and "is not None" in ast.unparse(cond[0].test), "rewrite asked regardless"


def test_every_failed_read_opens_with_an_opener(world, monkeypatch):
    p = world.proj
    target = str(p / "index.html")
    real_open = builtins.open

    def refusing_open(file, *a, **k):
        if os.path.abspath(str(file)) == target:
            raise PermissionError(13, "Permission denied", target)
        return real_open(file, *a, **k)

    # A read the operating system refuses after every screen passed (a mode-000 file in the
    # account's own project): the tool's answer must open like its other failures.
    monkeypatch.setattr(builtins, "open", refusing_open)
    out = world.run("read_file", {"path": target})
    monkeypatch.setattr(builtins, "open", real_open)
    assert "Permission denied" in out, out
    assert _failed(world, "read_file", out), out

    # The funnel's own answer for a call without its required argument.
    out = world.run("read_file", {})
    assert out.startswith("Tool Error:"), out
    assert _failed(world, "read_file", out), out


def test_the_live_view_shows_only_small_text_files(world):
    from vaf.tools.coder import _LIVE_VIEW_MAX_BYTES, _live_view_target
    p = world.proj
    big = p / "big.txt"
    big.write_bytes(b"a" * (_LIVE_VIEW_MAX_BYTES + 1))
    assert _live_view_target(str(big), scope=None, role=None) is None
    fits = p / "fits.txt"
    fits.write_bytes(b"a" * _LIVE_VIEW_MAX_BYTES)
    assert _live_view_target(str(fits), scope=None, role=None) == str(fits)
    picture = p / "logo.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert _live_view_target(str(picture), scope=None, role=None) is None
