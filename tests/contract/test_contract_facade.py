# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: the `import vaf` surface (docs/EMBEDDING.md, "What is and isn't stable").

Vendored by embedders and run against a pip-installed vaf: every assertion
here is a promise a stranger's code may rely on. Removing, renaming or
retyping one of these names is a breaking change.
"""
import re

import pytest

import vaf


EXPORTED = [
    "Agent",
    "BOOKKEEPING_KINDS",
    "BaseTool",
    "CoreAgent",
    "PathEscape",
    "RemoteRefused",
    "RemoteRoom",
    "Room",
    "RoomError",
    "SOUL_CONTINUITY_ADDENDUM",
    "StoreError",
    "ToolCaller",
    "ToolRequest",
    "TurnOutcome",
    "UnsafeName",
    "UploadVerdict",
    "VoiceTurnEngine",
    "__version__",
    "account_allows_tool",
    "build_capability_addendum",
    "contained_path",
    "derive_peer_id",
    "describe_room_entry",
    "extract_pdf_markdown",
    "fold_room_owners",
    "fold_room_tasks",
    "fold_room_votes",
    "inspect_upload",
    "install_thread_excepthook",
    "invited_rooms",
    "joined_rooms",
    "markers",
    "participant_key",
    "record_threat",
    "room_invitation",
    "safe_entry_name",
    "set_account_allowlist_resolver",
    "set_account_directory_resolver",
    "set_confirmation_bypass_resolver",
    "unread_counts",
    "user_jail",
]


def test_the_facade_exports_exactly_the_documented_names():
    """Spelled out rather than derived: a new export must be added HERE (and
    get its own contract tests) before it ships, and a dropped one fails."""
    assert sorted(vaf.__all__) == EXPORTED
    assert dir(vaf) == sorted(vaf.__all__)


def test_every_exported_name_resolves_lazily():
    """The facade serves its names via PEP 562; a name in __all__ that
    __getattr__ does not serve would be a broken promise."""
    for name in EXPORTED:
        assert getattr(vaf, name) is not None, f"vaf.{name} did not resolve"


def test_an_unknown_name_raises_attribute_error():
    with pytest.raises(AttributeError):
        vaf.definitely_not_part_of_the_contract  # noqa: B018


def test_version_is_a_pep440_string():
    # The VALUE changes every release; the type and format are the contract.
    assert isinstance(vaf.__version__, str)
    assert re.match(
        r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$", vaf.__version__
    ), f"__version__ {vaf.__version__!r} is not PEP 440"


def test_core_agent_is_the_engine_class_itself():
    """Documented: vaf.CoreAgent (a.k.a. vaf.core.agent.Agent) - identity,
    not a wrapper, so the two can never drift."""
    from vaf.core.agent import Agent as EngineAgent  # the documented alias target

    assert vaf.CoreAgent is EngineAgent


def test_the_resolver_setter_is_the_engine_function_itself():
    """Same rule as CoreAgent: the facade re-exports the engine's function,
    it does not wrap it."""
    import vaf.core.tool_dispatch as td  # re-export source named in EMBEDDING.md

    assert vaf.set_account_allowlist_resolver is td.set_account_allowlist_resolver
    assert vaf.set_confirmation_bypass_resolver is td.set_confirmation_bypass_resolver


def test_pdf_extraction_is_the_engine_function_itself():
    import vaf.core.pdf_extract as pe  # re-export source named in EMBEDDING.md

    assert vaf.extract_pdf_markdown is pe.extract_pdf_markdown


def test_the_tool_contract_names_are_classes_with_their_documented_members():
    assert isinstance(vaf.BaseTool.identity_kwargs, tuple)
    assert callable(vaf.BaseTool.log)
    assert callable(vaf.ToolCaller.execute)
    for method in ("deny", "ask", "allow"):
        assert callable(getattr(vaf.ToolRequest, method)), f"ToolRequest lost {method}()"
    assert callable(vaf.user_jail)


# ── rooms ──────────────────────────────────────────────────────────────────

def test_a_room_can_be_opened_read_and_closed_through_the_facade_alone(tmp_path):
    """The contract an embedder actually depends on: a whole room, start to finish,
    without importing anything private.

    `base` keeps this off the real store. Everything else is the shipped path.
    """
    room = vaf.Room.create(kind="round", owner_scope="tenant-a", base=tmp_path,
                           topic="Contract")
    key = vaf.participant_key("agent", "tenant-a")
    me = room.join(display="App", scope_id="tenant-a",
                   peer_id=vaf.derive_peer_id(key, room.room_id))
    room.say(me, "hello")

    lines = [vaf.describe_room_entry(e) for e in room.transcript()]
    assert lines[-1] == "hello"

    room.close(me, reason="done")
    with pytest.raises(vaf.RoomError):
        room.say(me, "and again")


def test_the_lookup_and_the_handle_derivation_work_together(tmp_path):
    """MUTATION: drop derive_peer_id from the facade.

    Without it `participant_key` and `joined_rooms` cannot be used together at all: a
    room joined with a minted handle is invisible to the lookup, so two exported names
    would be useless without a third that was not exported. Found by writing the
    example rather than by counting call sites.
    """
    import vaf.core.a2a.store as store_mod

    original = store_mod.rooms_root
    store_mod.rooms_root = lambda base=None: tmp_path if base is None else __import__(
        "pathlib").Path(base)
    try:
        room = vaf.Room.create(kind="round", owner_scope="tenant-b", base=tmp_path)
        key = vaf.participant_key("agent", "tenant-b")
        room.join(display="App", scope_id="tenant-b",
                  peer_id=vaf.derive_peer_id(key, room.room_id))

        found = [r.room_id for r, _identity in vaf.joined_rooms(key)]
        assert room.room_id in found
    finally:
        store_mod.rooms_root = original


def test_the_room_errors_are_distinguishable(tmp_path):
    """An embedder branches on these: the room said no, the room is not there, or the
    id was never a valid one."""
    assert issubclass(vaf.RoomError, Exception)
    with pytest.raises(vaf.StoreError):
        vaf.Room.open("no-such-room-here", base=tmp_path)
    with pytest.raises(vaf.UnsafeName):
        vaf.Room.open("../escape", base=tmp_path)


def test_an_invitation_carries_the_briefing(tmp_path):
    room = vaf.Room.create(kind="chain", owner_scope="tenant-a", base=tmp_path)
    key = vaf.participant_key("agent", "tenant-a")
    me = room.join(display="App", scope_id="tenant-a",
                   peer_id=vaf.derive_peer_id(key, room.room_id))

    row = vaf.room_invitation(room, me, display="Guest")
    assert row["ticket"] and row["role"] == "worker"
    assert row["ticket"] in row["briefing"]
