# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one list of interactive commands, for every terminal lane.

WHY THIS EXISTS, measured before it was written: the same list lived in FIVE
places and had already drifted in two directions.

* `vaf/cli/cmd/run.py` recognised 18 words as bare commands.
* Its own dispatch chain also handled `restart`/`reload`/`r` - which were NOT
  in that set, so typing `restart` alone sent the word to the model.
* `vaf/cli/tui.py`'s completer offered `model`, `history` and `export`, which
  no dispatcher handled, so accepting the completion produced "Unknown command".
* The app lane's routes, its palette and its help screen were three more.

Drift in a command list is not cosmetic: a word the completer offers and the
dispatcher ignores costs a turn and lands in the session, and a word the
dispatcher handles but the catch set omits is silently sent to the model.

DELIBERATELY IMPORT-LIGHT: no textual, no prompt_toolkit, no agent. Both the
classic lane and the app lane import this at module level, and the app lane's
lazy-import guard (`tests/test_tui_lazy_import.py`) fails if textual is pulled
onto the base graph.
"""
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Optional, Tuple

# Where a command may run. The app lane has three destinations, not two,
# because its agent lane is a single worker that serializes turns:
#   "ui"    - the UI thread; no IO, no agent call.
#   "agent" - queued on the agent lane, behind a running turn.
#   "now"   - its own short thread, because it must act WHILE a turn runs
#             (stopping speech is the case: the lane itself is what is busy).
LANES = ("ui", "agent", "now")


@dataclass(frozen=True)
class Command:
    word: str                                   # canonical form, no slash
    help: str
    aliases: Tuple[str, ...] = ()
    bare: bool = True                           # recognised as a lone word
    args: str = ""                              # "" | "<name>" | "<id>"
    lane: str = "ui"
    confirm: str = ""                           # non-empty: ask this first
    palette: bool = True                        # offer in the command palette

    @property
    def words(self) -> Tuple[str, ...]:
        return (self.word,) + self.aliases

    @property
    def label(self) -> str:
        return f"/{self.word} {self.args}".strip()


COMMANDS: Tuple[Command, ...] = (
    Command("help", "All keys and commands", aliases=("?",)),
    Command("settings", "Settings", aliases=("s",)),
    Command("model", "Provider and model", aliases=("c",)),
    Command("theme", "Switch theme", aliases=("t",), args="<name>"),
    Command("history", "This session's messages", aliases=("h",)),
    Command("sessions", "Sessions panel"),
    Command("session", "Load, create or rename a session",
            args="<id>|new|rename <name>", lane="agent", palette=False),
    Command("tools", "Show all loaded tools"),
    Command("context", "Context usage and tracked state", lane="agent"),
    Command("clear", "Reset the conversation", lane="agent",
            confirm="Clear the conversation? The context is discarded."),
    Command("undo", "Roll back the last code change", lane="agent",
            confirm="Undo the last snapshot? Files on disk are rewritten."),
    Command("restore", "Restore the full context from the archive", lane="agent"),
    Command("export", "Write this conversation to a file", args="<file>",
            lane="agent"),
    Command("room", "Show an agent room as a group chat", args="<id>",
            lane="agent"),
    Command("listen", "Voice input", aliases=("l",)),
    Command("halt", "Stop the agent speaking", aliases=("stop", "quiet", "stfu"),
            lane="now"),
    Command("restart", "Restart VAF in place", aliases=("reload", "r"),
            confirm="Restart VAF? The current session is saved first."),
    # "ui" lane on purpose: the handler returns at once and the repair runs on
    # its own thread. A repair can take minutes (starting a container engine),
    # and the agent lane would hold every chat turn behind it.
    Command("repair", "Check and repair the Docker services",
            confirm="Repair the Docker services? Stopped containers are started "
                    "and unhealthy ones restarted. No data is removed."),
    Command("exit", "Quit", aliases=("quit", "q", "bye")),
)

_BY_WORD = {w: c for c in COMMANDS for w in c.words}


def lookup(word: str) -> Optional[Command]:
    """The command for a word or alias, case-insensitively. None if unknown."""
    return _BY_WORD.get(str(word or "").strip().lower())


def bare_words() -> frozenset:
    """Every word that counts as a command when typed ALONE.

    This is the catch set: a lane that sends anything outside it to the model
    is correct, and a lane that sends anything inside it to the model has the
    bug this module was written to end.
    """
    return frozenset(w for c in COMMANDS if c.bare for w in c.words)


def suggest(word: str, limit: int = 1):
    """Closest known commands for an unknown word - so a typo says what it meant."""
    return get_close_matches(str(word or "").lower(), sorted(_BY_WORD), n=limit,
                             cutoff=0.6)


@dataclass(frozen=True)
class ParsedInput:
    """What one submitted line turned out to be."""
    command: Optional[Command] = None
    args: Tuple[str, ...] = field(default_factory=tuple)
    unknown_word: str = ""          # a /slash form nobody handles
    text: str = ""                  # the line, when it is a message

    @property
    def is_message(self) -> bool:
        return self.command is None and not self.unknown_word


def parse(line: str) -> ParsedInput:
    """Route one submitted line.

    Three rules, each with a reason:

    * A `/slash` form is ALWAYS a command attempt. Unknown ones never reach the
      model - they come back as `unknown_word` so the lane can say so.
    * A bare word routes only when it is in the catch set.
    * A bare word WITH arguments routes only for commands that take arguments
      (`theme dark`, `session <id>`); everything else is a message, which is
      what makes "clear the table for dinner" a sentence and not a command.

    Arguments keep their original case - session ids and theme names are not
    lowercase, and the old app-lane parser lowercased the whole line.
    """
    raw = (line or "").strip()
    if not raw:
        return ParsedInput(text=raw)

    slash = raw.startswith("/")
    body = raw[1:].strip() if slash else raw
    parts = body.split()
    if not parts:
        return ParsedInput(text=raw)

    word, args = parts[0].lower(), tuple(parts[1:])
    cmd = lookup(word)

    if slash:
        if cmd is None:
            return ParsedInput(unknown_word=word, text=raw)
        return ParsedInput(command=cmd, args=args, text=raw)

    if cmd is None or not cmd.bare:
        return ParsedInput(text=raw)
    if args and not cmd.args:
        return ParsedInput(text=raw)
    return ParsedInput(command=cmd, args=args, text=raw)
