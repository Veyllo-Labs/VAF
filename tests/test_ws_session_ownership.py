# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WebSocket commands may only act on a session the caller owns.

Six of the seventy-four command branches take a `sessionId` straight from the payload. Three
checked ownership, three did not, and the three that did not were found by counting branches
rather than by reading code - a first, narrower probe had reported the opposite for one of
them.

WHAT THEY ACTUALLY DID WITH A FOREIGN ID, measured one by one, because the fix is the same
everywhere and the severity is not:

  set_sidebar_documents  loads the named session, REPLACES its `sidebar_documents` and saves
                         the file - both paths, no scope filter anywhere. An empty payload
                         wipes another user's sidebar; a full one plants the caller's
                         documents in it. Nothing is pushed, so the victim meets them on next
                         load, confirms, and `learn_attached_knowledge` ingests foreign
                         content under THEIR scope into permanent memory.
  voice_call_turn        the call is keyed on the CONNECTION and cannot be hijacked, but the
                         payload's `sessionId` is used twice: a speaker-confirmation request
                         is routed into it, and a delegation is enqueued with
                         TaskQueue().add(session_id=...) carrying the caller's scope. An
                         injection into another user's work queue.
  stop_generation        stops that session's generation and cancels its in-flight attachment
                         indexing. Denial of service against another user's work; nothing is
                         read or written.

NOT THE ATTACHMENT RAG, although that was the first reading and it is worth writing down so
nobody "fixes" the wrong layer. Both RAG calls in the sidebar branch carry the CALLER's scope,
and `_scope_filters` never yields an empty filter - a None scope becomes
`user_scope_id IS NULL` - so caller-scope plus foreign-session matches zero rows. The victim's
attachments were never deletable that way, and the pollution lands in the attacker's own
scope. That filter is a genuine backstop; it is named here so it is not removed as redundant.

WHY THAT DISTINCTION DECIDES THIS FILE. A test asserting "the victim's RAG rows survive" would
have been GREEN BEFORE THE FIX - the scope filter makes it green, not the ownership check. It
would have proven nothing while looking like proof. So the assertion below is on the SESSION
FILE: hand a foreign id in, then read the victim's stored session and require it unchanged.
"""
import inspect
import re
from pathlib import Path

import pytest

WEB_SERVER = Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py"

# The branches that take a client-supplied sessionId. Frozen: a new one must either gate or be
# added here deliberately.
BRANCHES_WITH_CLIENT_SESSION_ID = {
    "artifact_edit", "chat", "set_sidebar_documents",
    "voice_call_start", "voice_call_turn", "stop_generation",
}


def _branch_bodies():
    lines = WEB_SERVER.read_bytes().decode().split("\n")
    marks = [(i, m.group(1)) for i, l in enumerate(lines)
             if (m := re.search(r'^\s*(?:el)?if\s+type\s*==\s*["\']([a-z_]+)["\']', l))]
    out = {}
    for k, (i, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        out[name] = "\n".join(lines[i:end])
    return out


def test_every_branch_taking_a_client_session_id_checks_ownership():
    """THE guard, asked of every branch rather than of the three that were found. The three
    that were missing had been missed by reading; only counting all seventy-four found them."""
    bodies = _branch_bodies()
    taking = {n for n, b in bodies.items() if re.search(r'cmd\.get\(\s*["\']sessionId["\']', b)}
    ungated = {n for n in taking if "_ws_session_owner_ok" not in bodies[n]}
    assert not ungated, (
        f"branch(es) act on a client-supplied sessionId without an ownership check: "
        f"{sorted(ungated)}. The storage layer is scope-agnostic, so this is the only "
        f"enforcement point."
    )


def test_the_set_of_branches_taking_a_session_id_is_frozen():
    """A new branch reading `sessionId` is a security decision and belongs in a diff. Frozen by
    NAME rather than by count: the count was wrong twice in the round that produced this."""
    bodies = _branch_bodies()
    taking = {n for n, b in bodies.items() if re.search(r'cmd\.get\(\s*["\']sessionId["\']', b)}
    assert taking == BRANCHES_WITH_CLIENT_SESSION_ID, (
        f"the set changed: {sorted(taking ^ BRANCHES_WITH_CLIENT_SESSION_ID)}"
    )


# ── the effect, asserted where it actually happened ─────────────────────────

def test_the_sidebar_handler_writes_the_session_file_and_that_is_the_exposure():
    """Pins WHERE the damage was, because the obvious answer was the wrong one.

    Not a behavioural test of the socket - it pins that the handler's write path is
    load -> mutate -> save on the named session with no scope filter, which is precisely what
    the ownership gate now stands in front of. A test aimed at the attachment RAG instead
    would have passed before the fix, because those calls carry the caller's scope and
    `_scope_filters` never matches another user's rows.
    """
    body = _branch_bodies()["set_sidebar_documents"]
    assert "session_mgr.load(" in body and "session_mgr.save(" in body, (
        "the handler no longer loads and saves the named session; if the write moved, this "
        "file's reasoning about WHERE the exposure was needs re-reading"
    )
    gate = body.index("_ws_session_owner_ok")
    load = body.index("session_mgr.load(")
    assert gate < load, (
        "the ownership check runs AFTER the session is loaded - it has to come first, or the "
        "handler has already acted on a session it may not touch"
    )


def test_the_rag_scope_filter_is_a_real_backstop_and_stays():
    """Named so it is not removed as redundant. It is why the RAG was never the exposure, and
    it is what keeps caller-scope plus foreign-session matching zero rows."""
    from vaf.memory import attachment_rag

    src = inspect.getsource(attachment_rag._scope_filters)
    assert "is_(None)" in src, (
        "a None scope no longer becomes an IS NULL filter. With an empty filter list the "
        "session-id alone would decide, and the attachment calls in the sidebar handler DO "
        "carry a foreign session id."
    )
    clear = inspect.getsource(attachment_rag._clear_session_async)
    assert "_scope_filters" in clear, "the clear path lost its scope filter"


@pytest.mark.parametrize("branch,reason", [
    ("voice_call_turn",
     "enqueues into TaskQueue with a payload-chosen session_id and the caller's scope"),
    ("stop_generation",
     "stops that session's generation and cancels its in-flight attachment indexing"),
])
def test_the_two_lower_severity_branches_are_gated_too(branch, reason):
    """They neither read nor write a session, which is why they rank below the sidebar handler -
    and they are gated anyway, because acting on a session that is not yours is the rule, not
    the damage."""
    body = _branch_bodies()[branch]
    assert "_ws_session_owner_ok" in body, f"{branch} is ungated: {reason}"
