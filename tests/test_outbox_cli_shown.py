# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf outbox list` prints every address whole, with the cards' marks in plain ASCII.

The listing used to cut the recipient, the Cc and the Bcc at 40 characters, so a long Bcc
list hid its tail on the one surface a headless install decides drafts on. And the cards'
mark brackets cannot reach a Windows console: measured, with ``PYTHONIOENCODING=cp1252`` both
``print("⟨")`` and Rich raise ``UnicodeEncodeError``.

MUTATIONS, each turns the named test red:
- restore the ``[:40]`` cut, or drop `ascii_marks` in `_to_cell` -> test_the_to_cell_is_whole_and_ascii;
- drop ``overflow="fold"`` on the To column -> test_the_to_column_folds.
"""
from rich.table import Table

import vaf.cli.cmd.outbox as cmd
from vaf.core.outbound_hold import shown_recipient_fields

LONG = "a.very.long.mailbox.name.for.the.test@" + "sub." * 6 + "example.org"


def _row():
    row = {"kind": "mail", "id": 3, "ref": "mail:3", "channel": "mail", "tool": "send_mail",
           "recipient": LONG, "recipient_name": "", "cc": "Ivan " + "X" * 50 + " <ivan@exаmple.com>",
           "bcc": ", ".join(f"b{i}@example.org" for i in range(8)) + ", evil@attacker.test",
           "attachments": [], "subject": "x", "preview": "Hallo", "created_ts": 1.0,
           "state": "held", "error": ""}
    row.update(shown_recipient_fields(row))
    return row


def test_the_to_cell_is_whole_and_ascii():
    assert len(LONG) > 60
    from rich.text import Text
    cell = Text.from_markup(cmd._to_cell(_row())).plain
    assert LONG in cell
    # The name is cut with an ellipsis, which a cp1252 console cannot print either.
    assert "ivan@ex<U+0430>mple.com [xn--exmple-4nf.com] (Ivan XXX" in cell
    assert "X<U+2026>)" in cell
    assert cell.endswith("evil@attacker.test")
    cell.encode("cp1252")
    assert cell.isascii()


def test_the_to_column_folds(monkeypatch):
    from typer.testing import CliRunner

    import vaf.core.outbound_hold as oh
    printed = []
    monkeypatch.setattr(cmd, "_identity", lambda: ("alice", "scope-1"))
    monkeypatch.setattr(oh, "pending", lambda *a, **k: [_row()])
    monkeypatch.setattr(cmd.UI.console, "print", lambda *a, **k: printed.extend(a))
    out = CliRunner().invoke(cmd.app, ["list"])
    assert out.exit_code == 0, out.output
    table = next(p for p in printed if isinstance(p, Table))
    column = next(c for c in table.columns if c.header == "To")
    assert column.overflow == "fold"
    assert LONG in "".join(column._cells)


def test_the_preview_and_the_files_print_on_a_cp1252_console(monkeypatch):
    """MUTATION: drop `_console_text` on the preview, or cut the file names again.

    Measured: with a cp1252 console one Cyrillic preview made `list_pending` raise
    UnicodeEncodeError, so nothing was listed; and the file names were cut at 60."""
    import io

    from rich.console import Console
    from typer.testing import CliRunner

    import vaf.core.outbound_hold as oh
    row = _row()
    longest = "Quartalsbericht_" + "x" * 70 + ".pdf"
    row.update(preview="\u041f\u0440\u0438\u0432\u0435\u0442, Anna \u202e",
               attachments=[longest, "plan.pdf", 'a, "b".pdf'])
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    console = Console(file=out, width=100, force_terminal=False, legacy_windows=False)
    monkeypatch.setattr(cmd.UI, "console", console)
    monkeypatch.setattr(cmd, "_identity", lambda: ("alice", "scope-1"))
    monkeypatch.setattr(oh, "pending", lambda *a, **k: [row])
    result = CliRunner().invoke(cmd.app, ["list"])
    assert result.exit_code == 0, (result.output, result.exception)
    out.flush()
    printed = out.buffer.getvalue().decode("cp1252")
    assert "<U+041F><U+0440>" in printed and "<U+202E>" in printed
    # Every file name whole in the cell, and the column folds rather than cutting it.
    tables = []
    monkeypatch.setattr(console, "print", lambda *a, **k: tables.extend(a))
    assert CliRunner().invoke(cmd.app, ["list"]).exit_code == 0
    from rich.table import Table
    column = next(c for c in next(t for t in tables if isinstance(t, Table)).columns
                  if c.header == "Preview")
    assert column.overflow == "fold"
    cell = "".join(column._cells)
    # Each name quoted, a quote inside one marked: a comma in a name is not a second file.
    assert f'"{longest}", "plan.pdf", "a, <U+0022>b<U+0022>.pdf"' in cell, cell
    # An umlaut stays an umlaut where the console can print it.
    monkeypatch.setattr(cmd.UI, "console", Console(file=io.StringIO(), width=100))
    assert cmd._console_text("Gr\u00fc\u00dfe \u202e") == "Gr\u00fc\u00dfe \u27e8U+202E\u27e9"
