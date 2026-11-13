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
- `content_carrying=` dropped -> test_successes_stay_successes (the read of a file that
  mentions a traceback);
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
import builtins
import inspect
import os
import re

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
    assert re.search(r"_identical_rewrite\(path, content, base_dir,\s*scope=caller_scope, "
                     r"role=caller_role\)", src)
