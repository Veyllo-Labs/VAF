# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which files may be ingested into long-term memory.

`learn_document` is not a viewer. It chunks a file, summarises it and writes the result into
the RAG store under a scope, where it stays searchable. That is what made its access policy
the sharpest of the file tools: anything read here outlives the session, the chat and any
cleanup, and a credential pulled in this way keeps working outside VAF entirely. It is the
same reasoning that closed `~/.vaf` to the file tools in the first place.

It carried its OWN policy, `_is_path_allowed`, which said yes to anything under the home
directory, the working directory, the data dir or VAF's own directory. Measured against the
shared rule, that was strictly wider in exactly the places that matter:

    ~/.ssh/id_rsa          own policy: allowed      is_safe_path: refused
    ~/.vaf/config.json     own policy: allowed      is_safe_path: refused
    ~/.vaf/secrets/x       own policy: allowed      is_safe_path: refused
    ~/.env                 own policy: allowed      is_safe_path: refused
    ~/Documents/report.pdf own policy: allowed      is_safe_path: allowed

A second, weaker policy is worse than none, because it reads like a check. The fix was a
deletion: the private function is gone (with its now-dead imports), and the shared
`is_safe_path` decides - which also answers the per-user jail, so one question covers both.

What is pinned below is the OUTCOME, not the spelling: a refused file must never reach
`ingest_document_knowledge`. Asserting that the tool returns an error would pass even if the
content had already been written to memory before the return.
"""
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from vaf.tools.learn_document import LearnDocumentTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID
SECRET = "SECRET-MATERIAL-DO-NOT-INGEST"


@pytest.fixture
def ingested():
    """Records every document that reaches the memory writer."""
    seen = []

    async def _spy(*args, **kwargs):
        seen.append(kwargs or args)
        return {"chunks": 0}

    with patch("vaf.tools.learn_document.ingest_document_knowledge", _spy):
        yield seen


def _learn(path, ingested, **kwargs):
    # Force the SYNC lane: with separate terminals on (the shipped default) an
    # allowed path would SPAWN a real learn_agent child out of the test run -
    # a terminal window, a queue record and, with the DB up, a real ingest.
    # These tests probe the ACCESS decision and the ingest wiring, nothing else.
    from vaf.core.config import Config
    real_get = Config.get

    def _no_spawn(key, default=None):
        if key == "sub_agents_in_separate_terminals":
            return False
        return real_get(key, default)

    with patch.object(Config, "get", classmethod(lambda cls, k, d=None: _no_spawn(k, d))):
        result = LearnDocumentTool().run(path=str(path), _agent=MagicMock(), **kwargs)
    return result, ingested


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_rsa").write_text(SECRET)
    (h / ".env").write_text(SECRET)
    (h / "Documents").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: h))
    return h


# ── the deletion ─────────────────────────────────────────────────────────────

def test_the_private_policy_is_gone():
    """Not "is not called" - GONE. A dormant second policy is a standing invitation to be
    called again by the next edit, and it would still be the weaker one."""
    import vaf.tools.learn_document as mod

    assert not hasattr(mod, "_is_path_allowed"), (
        "the tool's own path policy is back; it allowed ~/.ssh and ~/.vaf, which the shared "
        "rule refuses"
    )


def test_the_shared_rule_decides_now():
    """The shared rule decides, and the boundary it answers against is now DECLARED rather
    than installed by hand inside run()."""
    import inspect

    import vaf.tools.learn_document as mod

    assert "is_safe_path" in inspect.getsource(mod.LearnDocumentTool.run)
    assert mod.LearnDocumentTool.file_access == "read"
    assert {"user_scope_id", "user_role"} <= set(mod.LearnDocumentTool.identity_kwargs)
    assert getattr(mod.LearnDocumentTool.run, "_vaf_jailed", False)


# ── the outcome: nothing blocked reaches memory ──────────────────────────────

@pytest.mark.parametrize("relative", [".ssh/id_rsa", ".env"])
def test_a_blocked_file_is_never_ingested(home, ingested, relative):
    """THE regression, asserted on the memory writer rather than on the return string: a
    refusal that happens after the write would look identical from the outside."""
    result, seen = _learn(home / relative, ingested)
    assert seen == [], "a blocked file was written into long-term memory"
    assert "[ERROR]" in result or "denied" in result.lower()


def test_vaf_own_directory_is_not_ingestible(tmp_path, monkeypatch, ingested):
    """`~/.vaf` holds the config with every API key and the JWT secret. The old policy listed
    it as an ALLOWED root."""
    h = tmp_path / "home"
    vaf_dir = h / ".vaf"
    vaf_dir.mkdir(parents=True)
    (vaf_dir / "config.json").write_text(SECRET)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: h))

    result, seen = _learn(vaf_dir / "config.json", ingested)
    assert seen == []
    assert "[ERROR]" in result or "denied" in result.lower()


def test_another_tenants_file_is_refused(tmp_path, monkeypatch, ingested):
    """The per-user jail, which this tool never applied even though it declares a scope."""
    h = tmp_path / "home"
    foreign = h / "Documents" / "VAF_Projects" / "ffffffff" / "notes.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text(SECRET)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: h))

    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[]):
        result, seen = _learn(foreign, ingested, user_scope_id=SCOPE)
    assert seen == []
    assert "denied" in result.lower() or "[ERROR]" in result


def test_the_refusal_does_not_reveal_whether_the_file_exists(home, ingested):
    """Existence is probed only after the path is allowed."""
    present, _ = _learn(home / ".ssh" / "id_rsa", ingested)
    absent, _ = _learn(home / ".ssh" / "no-such-key", ingested)
    assert "not found" not in present.lower()
    assert "not found" not in absent.lower()
    assert present.split(":")[0] == absent.split(":")[0]


# ── the control ──────────────────────────────────────────────────────────────

def test_an_ordinary_document_still_reaches_memory(home, ingested):
    """Without this, every assertion above would also hold for a tool that refuses
    everything."""
    doc = home / "Documents" / "notes.txt"
    doc.write_text("a perfectly ordinary note worth remembering")
    result, seen = _learn(doc, ingested)
    assert seen, f"an allowed document was not ingested: {result!r}"


def test_the_static_rules_apply_without_an_identity_too(home, ingested):
    """A direct consumer passes no scope, which means no jail - never no rules."""
    result, seen = _learn(home / ".ssh" / "id_rsa", ingested)
    assert seen == []


# ── why the guard is narrower here than in the viewer ────────────────────────

def test_the_jail_does_not_reach_the_ingestion_thread():
    """The measured fact the narrow `with` block rests on.

    In `document_viewer` the jail wraps the whole body, because `_open_in_viewer` re-asks
    through `LibrarianTool._read_file`, which calls `is_safe_path` itself - so holding the
    jail is what makes the second asker agree with the first. Here nothing re-asks, and the
    ingestion runs through `_run_async_in_new_loop`, which is a bare `threading.Thread`.
    Contextvars do not cross into a new thread.

    So widening the block to "cover" the ingestion would cover nothing while looking exactly
    as though it did. That is the failure mode this test exists to keep visible: if this ever
    goes red, contextvar propagation changed and the comment in `run()` needs revisiting -
    not silently trusting.
    """
    from vaf.tools.filesystem import _librarian_scope_ctx, user_jail
    from vaf.tools.learn_document import _run_async_in_new_loop

    async def _probe():
        return _librarian_scope_ctx.get(None)

    with user_jail(SCOPE, "user", mode="read"):
        here = _librarian_scope_ctx.get(None)
        in_ingestion = _run_async_in_new_loop(_probe())

    assert here, "the jail was not installed even in the calling thread"
    assert in_ingestion is None, (
        "the jail now reaches the ingestion thread; the reasoning for keeping the `with` block "
        "narrow in run() was measured against the opposite and must be re-read"
    )
