# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How a tool logs, and where the log directory is allowed to be.

`BaseTool` is on the public surface: `from vaf import BaseTool`, listed as stable in
docs/EMBEDDING.md, and vaf/tools/README.md teaches strangers to subclass it. The contract
named every declarative field and no way to write a diagnostic line. So a tool author had
two options, both wrong: import `vaf.core.log_helper` - internal, no stability promise, and
an unrecognised domain string discards the lines with no error - or `logging.getLogger`,
which lands nowhere the log directory, the debug switch or the garbage collector look.

Our own tree answers the question three different ways: four files reach into log_helper,
fourteen use stdlib logging, and about sixty-eight log nothing at all. `self.log()` is the
one answer, and it fills in the two things a shared file needs to stay readable - which
tool, which session - while deliberately NOT filling in the caller's identity, which is not
ambient and would leak across users if it were cached on the instance.

Where the resulting file lands is a separate question, pinned in
tests/test_log_dir_resolution.py.
"""
from datetime import datetime
from pathlib import Path

import pytest

from vaf.tools.base import BaseTool


class _Quiet(BaseTool):
    name = "quiet_probe"
    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs):
        return "OK"


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    return tmp_path


def _lines(log_dir) -> list:
    path = log_dir / f"tools_{datetime.now().strftime('%Y-%m-%d')}.log"
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── the method, used the way a stranger would ────────────────────────────────

def test_a_tool_with_no_agent_behind_it_can_log(log_dir):
    """vaf/tools/README.md's own unit-test example instantiates a tool bare and calls run().
    That is the shape this has to work in: no agent, no session, no dispatcher."""
    _Quiet().log("hello from a tool")

    lines = _lines(log_dir)
    assert len(lines) == 1
    assert "[quiet_probe]" in lines[0], "the tool name is what makes a shared file readable"
    assert "hello from a tool" in lines[0]


def test_the_session_id_is_filled_in_when_there_is_one(log_dir, monkeypatch):
    """It is reachable through a contextvar that the bounded runner copies into the tool
    thread, so a tool never has to be handed it - and the line can be correlated with
    tool_use_*.log and the timeline."""
    import vaf.core.subagent_ipc as ipc

    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "green123456")
    _Quiet().log("with a session")
    assert "session=green123456" in _lines(log_dir)[0]


def test_the_field_is_present_even_with_no_session(log_dir, monkeypatch):
    """Fixed field count, so somebody can write a parser for this later without the
    substring-and-regex guesswork tool_use_*.log needs."""
    import vaf.core.subagent_ipc as ipc

    monkeypatch.setattr(ipc, "get_current_session_id", lambda: None)
    _Quiet().log("no session")
    assert "session=- " in _lines(log_dir)[0]


def test_the_callers_identity_is_deliberately_absent(log_dir):
    """A tool instance is shared by every user of one agent, so anything identity-shaped
    cached on `self` would be a cross-user leak. Identity arrives as arguments to run(), and
    a tool that wants it in its line declares identity_kwargs and writes it itself."""
    src = Path(__file__).resolve().parent.parent / "vaf" / "tools" / "base.py"
    body = src.read_text(encoding="utf-8").split("def log(self, message)", 1)[1].split("def query_llm", 1)[0]
    for leak in ("user_scope_id", "_current_user", "username"):
        assert f"self.{leak}" not in body


# ── it must never take a tool call down ──────────────────────────────────────

@pytest.mark.parametrize("value", [None, object(), 12345, b"bytes", ["a", "b"]])
def test_a_message_that_is_not_a_string_does_not_raise(log_dir, value):
    _Quiet().log(value)          # must not raise
    assert len(_lines(log_dir)) == 1


def test_a_multiline_message_stays_one_line(log_dir):
    """Every reader of these files assumes one entry per line, including the Logs window."""
    _Quiet().log("first\nsecond\r\nthird")
    assert len(_lines(log_dir)) == 1


def test_a_huge_message_is_capped(log_dir):
    """Domain logs have no rotation and the collector only reaches them after its window, so
    an uncapped message is an uncapped file. In-tree callers cap by hand; a public method
    cannot rely on a stranger doing that."""
    _Quiet().log("X" * 50_000)

    line = _lines(log_dir)[0]
    assert len(line) < BaseTool.LOG_MESSAGE_CHARS + 500
    assert "chars]" in line, "the truncation must be visible, not silent"


def test_an_object_that_is_not_a_tool_at_all_still_logs(log_dir):
    """A BaseTool subclass always inherits `name`, so the getattr fallback is not about
    them - it is about a duck-typed caller that borrowed the method, which the dispatch
    baselines show is a real shape in this codebase. Written this way after the first
    version tried to delete `name` off a subclass and only proved that inheritance works."""
    class _NotATool:
        LOG_MESSAGE_CHARS = BaseTool.LOG_MESSAGE_CHARS

    BaseTool.log(_NotATool(), "still fine")
    assert len(_lines(log_dir)) == 1
    assert "[?]" in _lines(log_dir)[0]


def test_a_broken_writer_does_not_raise(log_dir, monkeypatch):
    import vaf.core.log_helper as lh

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(lh, "append_domain_log", _boom)
    _Quiet().log("this will not land")     # must not raise


def test_it_is_silent_when_debug_logging_is_off(log_dir, monkeypatch):
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: False if key == "debug_logs_enabled" else default))
    _Quiet().log("nothing to see")
    assert _lines(log_dir) == []


def test_the_domain_is_one_the_writer_accepts(log_dir):
    """An unrecognised domain is a silent no-op in append_domain_log. That is a survivable
    failure mode for in-tree code and an awful one behind a public method, so the domain is
    fixed here rather than offered as a parameter - and it has to actually be allowed."""
    from vaf.core.log_helper import ALLOWED_DOMAINS

    assert "tools" in ALLOWED_DOMAINS


# ── the facade and the slim base ─────────────────────────────────────────────

def test_the_method_is_reachable_from_the_public_facade():
    import vaf

    assert callable(vaf.BaseTool.log)


def test_base_stays_importable_on_the_slim_base():
    """vaf/tools/base.py imports only stdlib at module level, which is what lets the facade
    expose BaseTool without pulling the engine in. The log_helper import is function-local
    for that reason; this says so, so nobody hoists it into the header."""
    src = Path(__file__).resolve().parent.parent / "vaf" / "tools" / "base.py"
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.startswith(("import ", "from ")):
            assert "vaf" not in line, f"top-level import pulls VAF into the slim base: {line}"
