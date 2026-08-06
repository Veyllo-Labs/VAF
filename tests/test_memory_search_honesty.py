# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""memory_search tells a dead database apart from an empty memory.

The search lane deliberately flattens every failure to "" (a chat turn must
not die because RAG is down - rag.py documents that as design). But the TOOL
lane answers the USER, and it translated that "" into "no stored information,
offer to remember things from now on" - so an agent sitting on a database
full of memories declared itself amnesic whenever the Docker stack slept.
The live incident: the memory container had been stopped for six hours and
the agent told the owner their long-term memory was a blank slate.

The tool now probes the DB (check_db_connection_sync) before trusting an
empty answer.
"""
from types import SimpleNamespace

import vaf.memory.database as db_mod
import vaf.memory.rag as rag_mod
from vaf.tools.context_tools import MemorySearchTool


def _search(monkeypatch, *, rag_result: str, db_up: bool) -> str:
    monkeypatch.setattr(rag_mod, "run_memory_search_sync",
                        lambda **kw: rag_result)
    monkeypatch.setattr(db_mod, "check_db_connection_sync",
                        lambda timeout_seconds=5.0: db_up)
    tool = MemorySearchTool()
    return tool.run(query="wer ist der Nutzer", k=5)


def test_a_dead_db_is_named_not_reported_as_amnesia(monkeypatch):
    out = _search(monkeypatch, rag_result="", db_up=False)
    assert "NOT reachable" in out and "service" in out
    assert "offer to remember" not in out, (
        "a dead database was reported as an empty memory again")


def test_a_reachable_db_with_no_hits_stays_the_old_honest_empty(monkeypatch):
    out = _search(monkeypatch, rag_result="", db_up=True)
    assert "No memories found" in out


def test_real_hits_pass_through_untouched(monkeypatch):
    probes = []
    monkeypatch.setattr(db_mod, "check_db_connection_sync",
                        lambda timeout_seconds=5.0: probes.append(True) or True)
    monkeypatch.setattr(rag_mod, "run_memory_search_sync",
                        lambda **kw: "[Memory] Der Nutzer heisst Alice.")
    out = MemorySearchTool().run(query="wer", k=3)
    assert "Alice" in out
    assert probes == [], "a successful search paid a needless health probe"
