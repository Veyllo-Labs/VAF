# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The phishing scorer reads only verdict flags that something produces, and its doc says so.

A `lookalike` flag was scored while nothing appended it, and EMAIL_CLIENT.md promised that a
look-alike domain raised the score. A reader without a producer is a promise the code does not
keep; this guard keeps the two in sync.

MUTATIONS, each turns the named test red:
- the `lookalike` branch re-added to `_phishing_score` -> test_every_flag_read_is_produced;
- the doc's old sentence back -> test_the_doc_lists_what_is_scored.
"""
import ast
import inspect
import re
from pathlib import Path

from vaf.tools import mail_utils

ROOT = Path(__file__).resolve().parents[1]


def _flags_read() -> set:
    tree = ast.parse(inspect.getsource(mail_utils._phishing_score))
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and any(isinstance(op, ast.In) for op in node.ops)
                and any(isinstance(c, ast.Name) and c.id == "flags" for c in node.comparators)):
            out.add(node.left.value)
    return out


def _flags_produced() -> set:
    src = (ROOT / "vaf" / "mail" / "authenticity.py").read_text(encoding="utf-8")
    return set(re.findall(r'flags\.append\("([a-z_]+)"\)', src))


def test_every_flag_read_is_produced():
    read, produced = _flags_read(), _flags_produced()
    assert read and produced, (read, produced)
    assert read <= produced, read - produced


def test_the_doc_lists_what_is_scored():
    doc = (ROOT / "docs" / "integrations" / "EMAIL_CLIENT.md").read_text(encoding="utf-8")
    flat = " ".join(doc.split())
    assert "a lookalike domain 3" not in flat and "lookalike domain raise the score" not in flat
    assert "punycode_domain" in flat and "look-alike domains are not scored" in flat.lower()
