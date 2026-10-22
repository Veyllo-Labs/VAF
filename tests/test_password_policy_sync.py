# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The password minimum has copies outside Python; they follow it.

The number lived in five places (the rule, the terminal's own length check, the wizard three
times, the Account tab) and in two catalogue sentences of seven languages with a literal 8.
Now the pages read one constant (web/lib/passwordPolicy.ts), the sentences take `{n}`, and
this test fails when the constant and `MIN_PASSWORD_LEN` differ.
"""
import json
import re
from pathlib import Path

import pytest

from vaf.auth.user_admin import MIN_PASSWORD_LEN

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "web" / "lib" / "passwordPolicy.ts"
PAGES = (ROOT / "web" / "app" / "login" / "page.tsx",
         ROOT / "web" / "components" / "settings" / "AccountSection.tsx")


def test_the_page_constant_is_the_rule():
    """MUTATION: set the TS constant to 8 - red."""
    found = re.findall(r"export const MIN_PASSWORD_LEN = (\d+);", POLICY.read_text(encoding="utf-8"))
    assert found == [str(MIN_PASSWORD_LEN)]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_pages_read_the_constant(page):
    """MUTATION: hardcode 12 in AccountSection - red."""
    src = page.read_text(encoding="utf-8")
    assert "import { MIN_PASSWORD_LEN } from '@/lib/passwordPolicy';" in src
    assert not re.search(r"const MIN_PASSWORD_LEN\s*=", src)
    # The password fields' own checks: `password.length < N`, `next.length < N` and the
    # password input's minLength. The username (2) and the code (6) are other rules.
    literal = re.compile(r"(?:password|next)\.length\s*<\s*\d+|minLength=\{(?!2\})\d+\}")
    assert not literal.search(src), "a literal password minimum is back"


@pytest.mark.parametrize("locale", sorted(p.stem for p in (ROOT / "web" / "messages").glob("*.json")))
def test_the_sentences_take_the_number(locale):
    catalogue = json.loads((ROOT / "web" / "messages" / f"{locale}.json").read_bytes())
    for key in ("passwordSubtitle", "errPwShort"):
        value = catalogue["onboarding"][key]
        assert "{n}" in value and not re.search(r"\d", value), (key, value)
    assert "{n}" in catalogue["account"]["tooShort"]
    assert catalogue["onboarding"]["errCommonPassword"] and catalogue["account"]["errCommonPassword"]
