# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""User-isolation guard for the RAG snippet / context push to the web UI.

Incident (network mode): the admin, logged in over the LAN, saw another user's ("Max") RAG
snippets in the "RAG-Snippets" hover panel. Root cause: run_memory_search_sync retrieved the
snippets correctly under the owner's scope, then pushed them to the web UI via push_update() -
an UNCONDITIONAL GLOBAL BROADCAST to every connected websocket (web_interface.py broadcast()),
with no user/session tag. A background thinking/automation run under user B's scope therefore
painted B's snippets into user A's open tab. The same held for the real_context_payload X-ray,
which carries the full prompt (incl. the "## Memory context" RAG block) + history.

Fix: route both pushes to the OWNER's connections only via push_update_to_user(scope, ...),
which is fail-closed (drops the event when the scope is unknown). These text guards fail if a
future edit reintroduces a global push_update() for either payload.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAG_PY = (ROOT / "vaf" / "memory" / "rag.py").read_text(encoding="utf-8")
AGENT_PY = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")


def _push_call_before(src: str, marker: str) -> str:
    """Return the nearest get_web_interface().<call> that precedes `marker`."""
    idx = src.index(marker)
    calls = list(re.finditer(r"get_web_interface\(\)\.(\w+)\(", src[:idx]))
    assert calls, f"no web_interface push call found before {marker!r}"
    return calls[-1].group(1)


def test_rag_results_push_is_user_scoped_not_global():
    # the rag_results snippet payload must be pushed to the owner only, fail-closed
    assert '"type": "rag_results"' in RAG_PY
    assert _push_call_before(RAG_PY, '"type": "rag_results"') == "push_update_to_user"
    assert "push_update_to_user(user_scope_id, {" in RAG_PY
    # and gated on a known scope so a missing scope drops the event instead of broadcasting
    assert "if web_sources and user_scope_id:" in RAG_PY


def test_real_context_payload_push_is_user_scoped_not_global():
    # the X-ray (full prompt incl. memory context + history) must not be a global broadcast
    assert '"type": "real_context_payload"' in AGENT_PY
    assert _push_call_before(AGENT_PY, '"type": "real_context_payload"') == "push_update_to_user"
    assert "_current_user_scope_id" in AGENT_PY


def test_memory_learning_push_is_session_scoped_not_global():
    # metadata only, but a global broadcast still exposed another user's session id + "saved N
    # memories" activity to every connected client; route it per session instead.
    markers = list(re.finditer(r'"type": "memory_learning"', RAG_PY))
    assert markers, "memory_learning push not found"
    for m in markers:
        calls = list(re.finditer(r"get_web_interface\(\)\.(\w+)\(", RAG_PY[: m.start()]))
        assert calls and calls[-1].group(1) == "_push_session_update", (
            "memory_learning must be pushed via _push_session_update, not a global push_update"
        )
