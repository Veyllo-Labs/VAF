# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from typing import TYPE_CHECKING

from .version import __version__

if TYPE_CHECKING:
    # Static type-checkers only (mypy / Pyright / VS Code): resolve the lazy public
    # API to the real classes so `from vaf import Agent` autocompletes and type-checks.
    # No runtime import here — `import vaf` stays cheap (the real loading is in
    # __getattr__ below). Paired with the vaf/py.typed marker (PEP 561).
    from .core.pdf_extract import extract_pdf_markdown
    from .core.tool_dispatch import ToolCaller, ToolRequest, set_account_allowlist_resolver, set_confirmation_bypass_resolver
    from .framework import Agent, CoreAgent
    from .tools.base import BaseTool
    from .tools.filesystem import user_jail

__all__ = ["__version__", "Agent", "BaseTool", "CoreAgent", "RemoteRefused",
           "RemoteRoom", "Room", "RoomError",
           "StoreError", "ToolCaller", "ToolRequest", "TurnOutcome", "UnsafeName",
           "VoiceTurnEngine",
           "derive_peer_id", "describe_room_entry", "extract_pdf_markdown",
           "fold_room_tasks", "fold_room_votes",
           "joined_rooms", "markers",
           "participant_key", "room_invitation", "set_account_allowlist_resolver",
           "set_confirmation_bypass_resolver", "unread_counts", "user_jail"]


def __getattr__(name):
    # Lazy public API (PEP 562). Keeps `import vaf` cheap: the ~9k-line core
    # engine and its dependency chain (incl. the latent Agent<->thinking_mode
    # cycle, which resolves fine at call time) are only loaded on first access
    # to `vaf.Agent` / `vaf.CoreAgent`.
    if name in ("Agent", "CoreAgent"):
        from .framework import Agent, CoreAgent
        return {"Agent": Agent, "CoreAgent": CoreAgent}[name]
    if name == "BaseTool":
        # What you subclass to add a tool, and where you declare identity_kwargs so the
        # dispatcher hands your tool the caller's identity. Pure stdlib underneath, so it
        # costs nothing on the slim base.
        from .tools.base import BaseTool
        return BaseTool
    if name == "user_jail":
        # Confine one tool run to the caller's own files. Declaring identity_kwargs tells
        # the dispatcher WHO is calling; this turns that answer into an actual boundary.
        # Enter it INSIDE your run(): a tool is also called directly, without any
        # dispatcher to have set it. See docs/EMBEDDING.md.
        from .tools.filesystem import user_jail
        return user_jail
    if name in ("VoiceTurnEngine", "TurnOutcome"):
        # The live-call turn pipeline as an object: audio bytes in, ONE decided
        # TurnOutcome back - noise gate, STT, speaker verification with the
        # anti-spoofing rules, the reflex policy, the first-layer reply, the
        # delegate DECISION. The embedder owns the transport and the TTS (the
        # outcome carries text + language), and injects its own STT via the
        # `transcribe` seam. Exported after the web handler became a thin
        # consumer of this exact object - that consumer is the proof the
        # surface suffices. Pure stdlib at module level (slim-base safe).
        from .core.voice_turn import VoiceTurnEngine, TurnOutcome
        return {"VoiceTurnEngine": VoiceTurnEngine, "TurnOutcome": TurnOutcome}[name]
    if name == "ToolRequest":
        # What an authorizer is handed: the caller's identity, which is trustworthy, and the
        # model's arguments, which are not. Exported so an application can type-annotate its
        # authorizer and, in tests, build one without an agent.
        from .core.tool_dispatch import ToolRequest
        return ToolRequest
    if name == "ToolCaller":
        # Run a tool the way the agent runs one - policy, confirmation gate, declared
        # identity, bounded execution, events - without an Agent, a session or a chat turn.
        # This is the same object the agent's own dispatch uses; there is not a second
        # implementation for embedders. Stdlib-only underneath, so the slim base is
        # unaffected. See docs/EMBEDDING.md.
        from .core.tool_dispatch import ToolCaller, ToolRequest
        return ToolCaller
    if name == "set_account_allowlist_resolver":
        # Which tools each ACCOUNT may use, answered by YOUR backend. One resolver per
        # process, consulted in the funnel after the hard policy block and BEFORE the
        # authorizer, so an account-level ban cannot be lifted by an allow(). The answer
        # also crosses into the coder child as data (VAF_ALLOWED_TOOLS). Stdlib-only
        # underneath, so the slim base is unaffected. See docs/EMBEDDING.md.
        from .core.tool_dispatch import set_account_allowlist_resolver
        return set_account_allowlist_resolver
    if name == "set_confirmation_bypass_resolver":
        # The allowlist resolver's sibling: whether an ACCOUNT holds the admin-granted
        # hands-off switch that skips the tool-confirmation dialog. Consulted UNDER the
        # authorization stages, so it can only remove the human question, never widen
        # who may call what; every use is announced as a gate_bypassed event. Unregistered
        # means: nobody has it. Stdlib-only underneath. See docs/EMBEDDING.md.
        from .core.tool_dispatch import set_confirmation_bypass_resolver
        return set_confirmation_bypass_resolver
    if name == "extract_pdf_markdown":
        # PDF -> Markdown with honest coverage facts (pages_read/total_pages/
        # truncated, absolute page markers, OCR reason). Exported because in-tree
        # consumers hand-rolled byte-identical truncations twice over private
        # imports - an embedder building a document lane has the same need and
        # had no supported way in. Module is stdlib at import time (pdfplumber/
        # PyPDF2 load on call, `vaf[pdf]` extra), so the slim base is unaffected.
        # See docs/EMBEDDING.md.
        from .core.pdf_extract import extract_pdf_markdown
        return extract_pdf_markdown
    if name in ("Room", "RoomError", "StoreError", "UnsafeName", "derive_peer_id",
                "describe_room_entry", "fold_room_tasks", "fold_room_votes",
                "joined_rooms", "participant_key", "room_invitation",
                "unread_counts"):
        # Rooms: several agents in one conversation, some of which may not be VAF and
        # may not be on this machine. Exported because SIX surfaces outside the room
        # package already reach into it for this same handful of names - the CLI, the
        # terminal app, the classic lane, the agent's own room tools, the web server
        # and the process wiring - and by the mission's own rule that list IS the
        # specification of what an embedder needs, not a guess at one.
        #
        # `derive_peer_id` is here for a reason found by writing the example rather
        # than by counting: without it `participant_key` and `joined_rooms` cannot work
        # together at all. A room joined with a minted handle is invisible to the lookup
        # that finds a participant's rooms, so two exported names would have been
        # useless without a third that was not exported - a gap, not a preference.
        #
        # `fold_room_tasks` is the task board computed from FRAMES rather than from a
        # store, and it is exported for the same measured reason: `RemoteRoom` is on
        # this facade, a peer reading a room over the wire has frames and no store, and
        # our own CLI needed exactly this function the moment the remote lane had to
        # answer "what work is open". Without it an embedder would fold the chain a
        # second time and hold a second opinion about what "working" means.
        #
        # `fold_room_votes` is here on the same measurement, taken twice: the remote
        # lane could open a vote and cast a ballot but never read the tally, because
        # the fold was a method on a store. A vote that also carries a deadline, a
        # reminder and an abstention makes a second opinion worse than useless - two
        # readers would disagree about who abstained, which is the one part of a
        # vote nobody may recompute differently.
        #
        # `describe_room_entry` and `room_invitation` are renamed on the way out. Inside
        # the package `describe` and `invitation` sit next to the things they describe
        # and invite into; on a facade shared with agents, tools and voice they would be
        # two of the vaguest names available.
        #
        # Stdlib at import time, so the slim base is unaffected. See docs/EMBEDDING.md.
        from .core.a2a.room import (Room, RoomError, derive_peer_id, describe,
                                    fold_tasks, fold_votes, joined_rooms,
                                    participant_key, unread_counts)
        from .core.a2a.store import StoreError, UnsafeName
        from .core.a2a.invite import invitation
        return {"Room": Room, "RoomError": RoomError, "StoreError": StoreError,
                "UnsafeName": UnsafeName,
                "derive_peer_id": derive_peer_id,
                "describe_room_entry": describe, "fold_room_tasks": fold_tasks,
                "fold_room_votes": fold_votes,
                "joined_rooms": joined_rooms,
                "participant_key": participant_key, "room_invitation": invitation,
                "unread_counts": unread_counts}[name]
    if name in ("RemoteRoom", "RemoteRefused"):
        # The room protocol spoken from the OTHER machine: connect with a ticket or
        # a seat, read the backlog, submit frames, keep the seat the welcome hands
        # over. The CLI's remote lane is its first consumer, and an embedder writing
        # their own peer needs exactly this class or has to reimplement the wire -
        # handshake, welcome check, ack mapping - from the protocol document.
        # Imports websockets only on first touch, so the slim base is unaffected.
        from .core.a2a.client import RemoteRefused, RemoteRoom
        return {"RemoteRoom": RemoteRoom, "RemoteRefused": RemoteRefused}[name]
    if name == "markers":
        # importlib, not `from . import`: the latter re-enters this
        # __getattr__ while the submodule is being set and recurses.
        import importlib

        return importlib.import_module(".markers", __name__)
    raise AttributeError(f"module 'vaf' has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
