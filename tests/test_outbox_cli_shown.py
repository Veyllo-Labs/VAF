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
           "recipient": LONG, "recipient_name": "", "cc": "Ivan <ivan@exаmple.com>",
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
    assert "ivan@ex<U+0430>mple.com [xn--exmple-4nf.com] (Ivan)" in cell
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
