# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Output streams that survive a Windows console.

On Windows, `sys.stdout` uses the active code page - cp1252 on a default
install - whenever it is NOT a real console: a pipe, a redirect to a file, a CI
runner, a subprocess capturing output. Printing any character that code page
cannot encode then raises UnicodeEncodeError and kills the process.

The characters are not exotic and mostly not ours. A model reply, a filename, a
web page title, an exception message from a dependency: all can carry anything
in Unicode, and no lint over our own string literals can see them coming. So
this is fixed at the stream, once, rather than policed at every print.

`errors="backslashreplace"` rather than "replace": an unencodable character
becomes a visible escape instead of a question mark, so a log that goes through
a narrow code page still says WHICH character it could not render. Nothing is
lost silently.

WHY THIS IS A FUNCTION AND NOT JUST A BLOCK IN main.py: it already was a block
in `vaf/main.py`, and that protected exactly the one process started through the
CLI. Everything else - the runnable examples, the scripts under scripts/, any
tool that spawns a bare `python file.py` - had no protection at all, which is
how a documented example came to die on the first encrypted file it printed on
Windows while the CLI was fine.

`vaf/main.py` deliberately keeps its own inline copy: it runs before the
dependency bootstrap, so it cannot import from the package yet. The guard in
tests/test_windows_console_encoding.py holds the two together.
"""
from __future__ import annotations

import sys


def force_utf8_streams() -> None:
    """Make stdout/stderr encode any text. Safe to call more than once.

    Never raises: a stream that cannot be reconfigured (already-wrapped, a
    devnull handle installed for pythonw, an exotic replacement) is left as it
    was. Output that cannot be encoded is a smaller problem than a crash while
    trying to fix output.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
