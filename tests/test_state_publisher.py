# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The gate in front of every live agent view.

MEASURED BEFORE BUILDING (2026-08-04). Eight emit methods on the web interface
repeated the same six-line transport fork; five of them had byte-identical bodies
differing in one string literal. Three publishers carried a verbatim copy of the
same "environment first, IPC context second" session lookup, a fourth used an
env-only variant no comment marked as different, and six publishers ran on four
different throttles. `force` meant two different things in two adjacent files.

THE TWO RULES THIS FILE EXISTS TO PIN, because both are silent when broken:

1. `publish` REFUSES a falsy session id before touching any bookkeeping. Below the
   emitters, a falsy session falls through to a broadcast that reaches every
   connected client - one user's source file, research topic or filesystem
   listing. Four of five publishers guarded against that by hand; the guard is
   load-bearing, not defensive.
2. `force` bypasses the CLOCK and never the HASH. They answer different questions:
   the clock asks "is it too soon", which a caller may override; the hash asks
   "does the receiver already have this", which a caller cannot know better.

AND THE ONE THE PUBLISHER MUST NOT DO: it adds no key. On the sub-agent bridge the
payload is validated against a typed model with `extra="allow"`, so an unknown key
rides through but a DECLARED key at the wrong type is a validation error - and the
POST has no `raise_for_status`, so the failure is swallowed and every event of that
run disappears. That is why there is no `progress` and no `agent_type` argument.
"""
import json

import pytest

from vaf.core.progress import StatePublisher, resolve_ui_session_id

SESSION = "green123456"


class _Interface:
    """Duck-typed web interface: records what the publisher handed the transport."""

    def __init__(self, boom: bool = False):
        self.sent = []
        self.boom = boom

    def emit_agent_state(self, msg_type, state, session_id=None):
        if self.boom:
            raise RuntimeError("transport down")
        self.sent.append((msg_type, state, session_id))


@pytest.fixture
def interface(monkeypatch):
    """Intercept the late import inside publish() at its source module."""
    iface = _Interface()
    import vaf.core.web_interface as wi

    monkeypatch.setattr(wi, "get_web_interface", lambda: iface)
    return iface


# ── the session guard ────────────────────────────────────────────────────────

@pytest.mark.parametrize("empty", ["", None])
def test_no_session_sends_nothing(interface, empty) -> None:
    pub = StatePublisher("coder_state")
    assert pub.publish({"a": 1}, session_id=empty) is False
    assert interface.sent == []


def test_a_room_anchored_child_passes_the_session_guard(interface, monkeypatch) -> None:
    """MUTATION: refuse every sessionless frame, room anchor or not.

    A room-ordered child may run with NO session at all (the runner's room
    frame binds no chat) - measured live: the whole editor/code stream of such
    a coder died at this guard while its status events flowed, and the room
    window stayed dark. VAF_ROOM_ID is the child's address then: the bridge
    stamps it and the endpoint delivers to the room's tenant, so the frame is
    addressed, not broadcast."""
    monkeypatch.setenv("VAF_ROOM_ID", "room-orderer")
    pub = StatePublisher("coder_state")
    assert pub.publish({"a": 1}, session_id="") is True
    assert len(interface.sent) == 1

    monkeypatch.delenv("VAF_ROOM_ID")
    assert pub.publish({"a": 2}, session_id="") is False
    assert len(interface.sent) == 1


def test_a_refused_frame_leaves_the_bookkeeping_untouched(interface) -> None:
    """Otherwise the first frame after a session appears lands inside a window
    opened by a send that never happened."""
    pub = StatePublisher("coder_state", min_interval=60.0, dedupe=True)
    assert pub.publish({"a": 1}, session_id="") is False
    assert pub.publish({"a": 1}, session_id=SESSION) is True
    assert len(interface.sent) == 1


# ── the clock and the hash answer different questions ────────────────────────

def test_the_clock_holds_a_second_frame_back(interface) -> None:
    pub = StatePublisher("coder_state", min_interval=60.0)
    assert pub.publish({"n": 1}, session_id=SESSION) is True
    assert pub.publish({"n": 2}, session_id=SESSION) is False
    assert len(interface.sent) == 1


def test_force_bypasses_the_clock(interface) -> None:
    pub = StatePublisher("coder_state", min_interval=60.0)
    pub.publish({"n": 1}, session_id=SESSION)
    assert pub.publish({"n": 2}, session_id=SESSION, force=True) is True
    assert len(interface.sent) == 2


def test_force_never_bypasses_the_duplicate_check(interface) -> None:
    """The caller can know it is not too soon; it cannot know the receiver is
    missing a frame it already has."""
    pub = StatePublisher("research_state", min_interval=0.4, dedupe=True)
    pub.publish({"n": 1}, session_id=SESSION)
    assert pub.publish({"n": 1}, session_id=SESSION, force=True) is False
    assert len(interface.sent) == 1


def test_dedupe_is_off_by_default(interface) -> None:
    """A publisher that silently drops frames by default is the wrong default."""
    pub = StatePublisher("librarian_state")
    assert pub.publish({"n": 1}, session_id=SESSION) is True
    assert pub.publish({"n": 1}, session_id=SESSION) is True
    assert len(interface.sent) == 2


def test_a_changed_payload_passes_the_duplicate_check(interface) -> None:
    pub = StatePublisher("coder_state", dedupe=True)
    pub.publish({"n": 1}, session_id=SESSION)
    assert pub.publish({"n": 2}, session_id=SESSION) is True
    assert len(interface.sent) == 2


# ── what it must not do ──────────────────────────────────────────────────────

def test_the_payload_reaches_the_transport_untouched(interface) -> None:
    """No injected key, no rename, no normalisation. The frontend rebuilds these
    field by field, and on the bridge a declared key at the wrong type erases the
    whole run's stream."""
    state = {"fileTree": [], "taskProgress": "Task 1/3", "loop": 2}
    pub = StatePublisher("coder_state", dedupe=True)
    pub.publish(state, session_id=SESSION)
    msg_type, sent, sid = interface.sent[0]
    assert msg_type == "coder_state"
    assert sent == state
    assert sid == SESSION


def test_the_constructor_offers_no_field_injection() -> None:
    """`progress` and `agent_type` are absent, not unused: the bridge's model
    declares `progress` as an int and the coder's reads "Task 1/3"."""
    import inspect

    params = set(inspect.signature(StatePublisher.__init__).parameters)
    assert "progress" not in params
    assert "agent_type" not in params


def test_a_failing_transport_propagates_rather_than_returning_false(interface) -> None:
    """Every call site already has a blanket swallow. A second one here would make
    False mean both "gated" and "failed", and the telemetry gated on the return
    value would start claiming emits that never happened."""
    interface.boom = True
    pub = StatePublisher("coder_state")
    with pytest.raises(RuntimeError):
        pub.publish({"n": 1}, session_id=SESSION)


def test_a_failing_send_still_consumes_its_window(interface) -> None:
    """The safe direction: a failing transport must not be retried at full loop
    speed with a payload carrying several large diffs."""
    interface.boom = True
    pub = StatePublisher("coder_state", min_interval=60.0)
    with pytest.raises(RuntimeError):
        pub.publish({"n": 1}, session_id=SESSION)
    interface.boom = False
    assert pub.publish({"n": 2}, session_id=SESSION) is False


def test_two_publishers_do_not_share_a_budget(interface) -> None:
    """Two concurrent in-process runs must not throttle each other."""
    a = StatePublisher("coder_state", dedupe=True)
    b = StatePublisher("coder_state", dedupe=True)
    assert a.publish({"n": 1}, session_id=SESSION) is True
    assert b.publish({"n": 1}, session_id=SESSION) is True
    assert len(interface.sent) == 2


def test_a_zero_hash_is_not_read_as_already_seen(interface, monkeypatch) -> None:
    """hash() can legitimately return 0, and with hash randomisation a 0 sentinel
    would be a dropped first frame nobody can reproduce."""
    monkeypatch.setattr("vaf.core.progress.hash", lambda _s: 0, raising=False)
    pub = StatePublisher("coder_state", dedupe=True)
    assert pub._last_hash is None
    assert pub.publish({"n": 1}, session_id=SESSION) is True


def test_an_unserialisable_value_does_not_break_the_duplicate_check(interface) -> None:
    """`default=str` is why: agents put paths and datetimes into these payloads."""
    from pathlib import Path

    pub = StatePublisher("coder_state", dedupe=True)
    assert pub.publish({"p": Path("/tmp/x")}, session_id=SESSION) is True
    assert pub.publish({"p": Path("/tmp/x")}, session_id=SESSION) is False


# ── the shared session lookup ────────────────────────────────────────────────

def test_the_context_wins_over_the_environment(monkeypatch) -> None:
    """Inverted deliberately, and this test used to pin the opposite.

    The environment is process-global and three tool-dispatching lanes are threads
    in one process, so "environment first" let a run that belongs to no web session
    address whichever session a chat turn had left behind. The environment is now
    the PROCESS BOUNDARY channel only: a child is spawned with it and declares it
    into its own context at bootstrap. In the parent, a context that was told wins.
    """
    monkeypatch.setenv("VAF_SESSION_ID", "from-env")
    import vaf.core.subagent_ipc as ipc

    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "from-ipc")
    assert resolve_ui_session_id() == "from-ipc"


def test_a_run_that_belongs_to_nobody_addresses_nobody(monkeypatch) -> None:
    """The automation lane's case: it declares None, and a stale environment value
    must not resurrect a foreign session behind that declaration."""
    monkeypatch.setenv("VAF_SESSION_ID", "someone-elses-session")
    import vaf.core.subagent_ipc as ipc

    monkeypatch.setattr(ipc, "get_current_session_id", lambda: None)
    assert resolve_ui_session_id() == ""


def test_a_raising_ipc_lookup_yields_no_session_rather_than_an_error(monkeypatch) -> None:
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    import vaf.core.subagent_ipc as ipc

    def _boom():
        raise RuntimeError("no ipc")

    monkeypatch.setattr(ipc, "get_current_session_id", _boom)
    assert resolve_ui_session_id() == ""


def test_progress_imports_without_the_server_extra() -> None:
    """web_interface imports fastapi at module scope and fastapi is an optional
    extra, so the hop to it must stay inside publish()."""
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "vaf" / "core" / "progress.py"
    ).read_text(encoding="utf-8")
    head = src.split("class StatePublisher", 1)[0]
    assert "from vaf.core.web_interface" not in head, (
        "web_interface is imported above the class; that makes vaf.core.progress "
        "unimportable on a slim install"
    )


def test_the_declared_view_types_are_json_safe() -> None:
    from vaf.core.progress import VAF_LIVE_VIEW_TYPES

    assert json.loads(json.dumps(sorted(VAF_LIVE_VIEW_TYPES)))
