# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The prompt history, shared by every terminal lane.

Both lanes read and write the SAME file (`~/.vaf/history`) through the SAME
class, prompt_toolkit's `FileHistory`. That is deliberate: a private store for
the full-screen lane would mean a second on-disk format, a second parser, and a
history that splits in two depending on how the user happened to start VAF.

prompt_toolkit is already loaded wherever this is called (the classic lane
builds its prompt with it, and the app lane imports `vaf.cli.tui` during boot),
so reusing it costs nothing and guarantees the formats cannot drift.

The format, for the record: a `# <timestamp>` line, then one `+<line>` per line
of the entry, append-only, UTF-8. `load_history_strings()` returns NEWEST
FIRST, which is the order an up-arrow walks.
"""
from pathlib import Path
from typing import List

MAX_ENTRIES = 500          # what an up-arrow could plausibly reach


def history_file() -> Path:
    return Path.home() / ".vaf" / "history"


def _store():
    from prompt_toolkit.history import FileHistory
    return FileHistory(str(history_file()))


def load_history(limit: int = MAX_ENTRIES) -> List[str]:
    """Past entries, newest first. Missing file yields an empty list and does
    NOT create one - reading must not leave a trace."""
    try:
        return list(_store().load_history_strings())[:limit]
    except Exception:
        return []


def append_history(text: str) -> None:
    """Record one submitted line. Small O_APPEND writes, so two lanes running
    at once interleave entries but never corrupt one another's."""
    text = (text or "").strip()
    if not text:
        return
    try:
        history_file().parent.mkdir(parents=True, exist_ok=True)
        _store().append_string(text)
    except Exception:
        pass


def _forget_listener(env=None, user_scope_id=None, transcript_scrubbed=False, **_):
    """A credential the machine owner handed over in a chat leaves the terminal's input history
    too (vaf/core/forget_secrets.py): the terminal lanes are the owner, and this file is theirs."""
    if transcript_scrubbed or not env:
        return
    from vaf.core.config import get_local_admin_scope_id
    if user_scope_id and str(user_scope_id) != str(get_local_admin_scope_id()):
        return
    from vaf.core.forget_secrets import scrub_file
    scrub_file(history_file(), env)


try:
    from vaf.core.forget_secrets import add_listener as _add_forget_listener
    _add_forget_listener(_forget_listener)
except Exception:
    pass
