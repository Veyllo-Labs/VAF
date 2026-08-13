# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Several agents in one conversation, from the public facade.

A room is a group chat between agents. Some of them may not be VAF and some may not be
on this machine; the transcript lives on the machine that hosts the room, encrypted the
way conversations are, and every participant reads it in the same order without
coordinating.

This walks the whole surface an embedder gets: opening a room, joining it, saying
something, reading it back in canonical order, finding the rooms a participant is in,
and minting an invitation for somebody else's agent. It also shows the two refusals
that matter - a peer cannot command in a round, and a closed room takes nothing more.

NEEDS NO PROVIDER, NO API KEY AND NO NETWORK. It never talks to a model. It runs against
a throwaway home directory, so your own installation is untouched.

    python examples/11_a2a_room.py
"""
import atexit
import logging
import os
import shutil
import tempfile
from pathlib import Path

# ── a throwaway HOME, set up BEFORE vaf is imported ─────────────────────────────
# A room is stored under the VAF directory and encrypted with the machine's key, and
# VAF resolves that directory at import time - so the sandbox has to exist first. Your
# own installation is never touched. The key backend is pinned to a FILE so the example
# runs the same on every platform; a real install uses the per-platform default.
SANDBOX = Path(tempfile.mkdtemp(prefix="vaf-room-example-"))
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
os.environ.update({
    "HOME": str(SANDBOX),
    "USERPROFILE": str(SANDBOX),                        # Windows
    "XDG_DATA_HOME": str(SANDBOX / ".local" / "share"),
    "XDG_CONFIG_HOME": str(SANDBOX / ".config"),
    "LOCALAPPDATA": str(SANDBOX / "AppData" / "Local"),  # Windows
    "VAF_LOG_DIR": str(SANDBOX / "logs"),
})
(SANDBOX / ".vaf").mkdir()
(SANDBOX / ".vaf" / "config.json").write_text(
    '{"secure_store_kek_backend": "file"}', encoding="utf-8")

# A first run logs "minted a new key". Correct on a fresh installation, and only in the
# way of reading the output.
logging.getLogger("vaf").setLevel(logging.ERROR)

import vaf  # noqa: E402

SCOPE = "tenant-a"


def main() -> None:
    print("Rooms: several agents in one conversation\n")

    # ── open one and join it ────────────────────────────────────────────────
    # "round" means peers: everybody equal, nobody may command. "chain" is the other
    # kind - one leader, workers who report back.
    room = vaf.Room.create(kind="round", owner_scope=SCOPE, topic="Deploy talk")

    # JOIN WITH THE DERIVED HANDLE, not a minted one. The handle a participant gets is
    # a function of who they are and which room it is, so it survives a restart with no
    # index to keep in sync and a re-join lands on the same seat. Join with a random one
    # and `joined_rooms` below will not find this room at all - the lookup asks for the
    # derived handle, because that is the only one it can compute without being told.
    key = vaf.participant_key("agent", SCOPE)
    me = room.join(display="MyApp", scope_id=SCOPE,
                   peer_id=vaf.derive_peer_id(key, room.room_id))
    print(f"opened {room.room_id} ({room.kind}) as {me.display} [{me.role}]")

    # ── somebody else's agent arrives ───────────────────────────────────────
    # In real use it presents a ticket; here it is joined directly to keep the example
    # to one process. A guest carries NO tenant scope, which is what keeps it out of
    # every gate that reads "no scope" as unrestricted.
    guest = room.join(display="Codex", scope_id=None)
    room.say(guest, "anyone looked at the logs?")
    room.say(me, "on it")

    # ── read it back ────────────────────────────────────────────────────────
    # Canonical order is (lamport, sender, seq) and never the wall clock, so every
    # reader computes the same sequence with nothing to agree on first.
    print("\ntranscript:")
    for entry in room.transcript():
        print(f"   {entry['label']:<12} {vaf.describe_room_entry(entry)}")

    # ── which rooms is this participant in ──────────────────────────────────
    # The lane separates the HUMAN from the AGENT. The same account owns both and they
    # are two different actors in a room: without the lane, "send my agent in" and "I am
    # in myself" collapse into one member.
    pending = vaf.unread_counts(key)
    print("\nrooms this participant is in:")
    for joined, identity in vaf.joined_rooms(key):
        print(f"   {joined.room_id} as {identity.role}, "
              f"{pending.get(joined.room_id, 0)} unread")

    # ── invite somebody, and hand over what they need ───────────────────────
    invitation = vaf.room_invitation(room, me, display="Fable")
    print(f"\ninvitation for Fable: ticket {invitation['ticket'][:12]}..., "
          f"joining as {invitation['role']}")
    print("   the briefing that travels with it is "
          f"{len(invitation['briefing'].splitlines())} lines, generated - including the "
          "paragraph\n   naming what that role may send, read off the same table that "
          "refuses.")

    # ── the two refusals worth knowing ──────────────────────────────────────
    print("\nwhat the room refuses:")
    try:
        room.ingest({"kind": "directive", "body": {"text": "do this"}}, identity=me)
    except vaf.RoomError as refusal:
        print(f"   a directive in a round: {refusal}")

    room.close(me, reason="finished")
    try:
        room.say(me, "one more thing")
    except vaf.RoomError as refusal:
        print(f"   anything after closing: {refusal}")

    print("\nA room hands out no tool, lifts no restriction, and carries no identity")
    print("into any tool funnel. What arrives is INPUT - treat it like model output.")
    print(f"\n(this ran against {SANDBOX}, removed on exit)")


if __name__ == "__main__":
    main()
