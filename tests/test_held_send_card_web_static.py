# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web draft card shows the recipients as the server shows them, with the marks in amber.

The rows carry `to_shown`, `cc_shown`, `bcc_shown` and `name_shown`
(`outbound_hold.shown_recipient_fields`, vaf/core/shown_text.py); the card renders those and
never the raw header strings, which show a Cyrillic letter as the Latin one it looks like.

MUTATIONS, each turns the named test red:
- render ``{row.cc}`` / ``{row.bcc}`` or the raw ``row.recipient`` in the subline again
  -> test_the_card_reads_the_shown_fields;
- rename a key on one side only -> test_the_four_keys_match_the_rows;
- drop the amber dark tone or the mark pattern -> test_the_marks_are_amber.
"""
import re
from pathlib import Path

from vaf.core.outbound_hold import shown_recipient_fields

ROOT = Path(__file__).resolve().parents[1]
CARD = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
KEYS = ("to_shown", "cc_shown", "bcc_shown", "name_shown")


def test_the_card_reads_the_shown_fields():
    for key in KEYS:
        assert f"row.{key}" in CARD, key
    assert "row.name_shown || row.to_shown || row.recipient || t('noRecipient')" in CARD
    assert "{row.name_shown ? <><Recipient text={row.to_shown || row.recipient} />" in CARD
    for raw in ("{row.cc}", "{row.bcc}", "recipients: row.cc", "recipients: row.bcc",
                "row.recipient} · ", "`${row.recipient} · `"):
        assert raw not in CARD, raw
    # Every value sits in its own bidi isolation.
    assert "return <bdi><Marked text={text} /></bdi>;" in CARD


def test_the_four_keys_match_the_rows():
    row = shown_recipient_fields({"channel": "mail", "recipient": "a@b.org"})
    assert set(row) == set(KEYS)
    declared = set(re.findall(r"^\s+(\w+_shown)\?: string;", CARD, re.M))
    assert declared == set(KEYS), declared


def test_the_marks_are_amber():
    assert r"const MARK = /(\u27e8U\+[0-9A-F]{4,6}\u27e9)/;" in CARD
    assert 'className="font-mono text-amber-700 dark:text-[#e0b866]"' in CARD
    # No new catalogue key: the marks and the punycode bracket are the signal.
    assert "t('lookalike" not in CARD and "t('confusable" not in CARD
