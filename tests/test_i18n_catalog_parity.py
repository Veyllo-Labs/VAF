# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The message catalogues must not drift apart.

Nothing guarded this: `rg -l "messages/de.json|messages/en.json" tests/ scripts/`
came back empty, while every UI change adds keys to both files by hand. A key
present in one locale only renders as its own raw key in the other, and a
placeholder present in one only throws at render time in next-intl, which turns
a translation slip into a blank panel rather than an untranslated word.
"""
import json
import re
from pathlib import Path

import pytest

_MESSAGES = Path(__file__).resolve().parents[1] / "web" / "messages"
_LOCALES = sorted(p.stem for p in _MESSAGES.glob("*.json"))
_REFERENCE = "en"

# An ICU argument: the identifier right after an opening brace AND followed by
# the end of the block or a comma. The trailing condition is what separates
# {count} / {count, plural, ...} from the literal prose inside a plural branch
# ("=1 {A message could not be sent}" is text, not an argument named A).
_PLACEHOLDER = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=[},])")


def _load(locale):
    return json.loads((_MESSAGES / f"{locale}.json").read_bytes())


def _flatten(node, prefix=""):
    flat = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def test_the_reference_locale_is_present():
    assert _REFERENCE in _LOCALES, f"no {_REFERENCE}.json in web/messages"
    assert len(_LOCALES) > 1, "parity needs a second locale to compare against"


@pytest.mark.parametrize("locale", [loc for loc in _LOCALES if loc != _REFERENCE])
def test_every_locale_has_the_same_keys(locale):
    reference = _flatten(_load(_REFERENCE))
    other = _flatten(_load(locale))
    missing = sorted(set(reference) - set(other))
    extra = sorted(set(other) - set(reference))
    assert not missing, f"{locale}.json is missing {len(missing)} key(s): {missing[:10]}"
    assert not extra, f"{locale}.json has {len(extra)} key(s) {_REFERENCE}.json lacks: {extra[:10]}"


@pytest.mark.parametrize("locale", [loc for loc in _LOCALES if loc != _REFERENCE])
def test_every_locale_takes_the_same_placeholders(locale):
    reference = _flatten(_load(_REFERENCE))
    other = _flatten(_load(locale))
    drifted = []
    for path, text in reference.items():
        if not isinstance(text, str) or not isinstance(other.get(path), str):
            continue
        want = set(_PLACEHOLDER.findall(text))
        got = set(_PLACEHOLDER.findall(other[path]))
        if want != got:
            drifted.append(f"{path}: {_REFERENCE}={sorted(want)} {locale}={sorted(got)}")
    assert not drifted, "placeholder drift (next-intl throws on a missing one):\n" + "\n".join(drifted[:10])
