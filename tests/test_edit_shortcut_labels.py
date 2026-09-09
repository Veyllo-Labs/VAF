# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The undo and redo tooltips name the keys of the keyboard in front of the person
(web/hooks/useEditShortcuts.ts): the catalogue carries a `{shortcut}` placeholder and the
localized name of the Control key, and every caller of the two keys fills the placeholder.
next-intl renders a missing ICU argument as the literal placeholder, and no type checks it,
so this guard does.

MUTATION: drop the argument from one editor's tooltip and the caller test goes red.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
_CALL = re.compile(r"tc\('(undoWithShortcut|redoWithShortcut)'([^)]*)\)")


def _tsx_files():
    return [p for sub in ("app", "components") for p in (WEB / sub).rglob("*.tsx") if "node_modules" not in p.parts]


def test_every_caller_fills_the_shortcut_placeholder():
    callers, bare = [], []
    for p in _tsx_files():
        src = p.read_text(encoding="utf-8")
        for m in _CALL.finditer(src):
            callers.append(p.name)
            if "shortcut:" not in m.group(2):
                bare.append(f"{p.relative_to(ROOT).as_posix()}: tc('{m.group(1)}'{m.group(2)})")
        if _CALL.search(src):
            assert "useEditShortcuts(tc('ctrlKey'))" in src, f"{p.name} names a shortcut it did not resolve"
    assert not bare, "a tooltip that renders the literal placeholder:\n" + "\n".join(bare)
    assert len(set(callers)) == 4, sorted(set(callers))


def test_the_seven_catalogues_carry_the_placeholder_and_the_control_keys_name():
    for p in sorted((WEB / "messages").glob("*.json")):
        common = json.loads(p.read_text(encoding="utf-8"))["common"]
        assert "{shortcut}" in common["undoWithShortcut"] and "{shortcut}" in common["redoWithShortcut"], p.name
        for key in ("undoWithShortcut", "redoWithShortcut"):
            assert "Ctrl" not in common[key] and "Strg" not in common[key], (p.name, key)
        assert common["ctrlKey"] in ("Ctrl", "Strg"), p.name


def test_the_hook_resolves_the_platform_in_an_effect():
    src = (WEB / "hooks" / "useEditShortcuts.ts").read_text(encoding="utf-8")
    assert "useEffect(() => {" in src and "navigator" in src, "the platform is read after mount, so server and client render agree"
    assert "\\u2318Z" in src and "\\u21e7\\u2318Z" in src
