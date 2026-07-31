# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The security event kinds are one list with several readers. Keep them one list.

MEASURED DRIFT (2026-07-31). Four places named the kinds and no two agreed: the contract
comment in `vaf/core/security_events.py` named 7, the dashboard's `evKindLabel` named the
same 7, the table in `docs/security/SECURITY_DASHBOARD.md` named 12, and the code emitted
14. The consequence was not cosmetic - the seven unlisted kinds include every skill event
and both mail events, so a reader opening the event list would have been shown raw
identifiers, and the design doc described a log that had grown two kinds past it.

This is CLAUDE.md Rule 2 in its usual shape: a central registry with copies, drifted
because nothing failed when they did. Prose asking people to remember has now been tried
and has now failed, so this guard exists instead.

WHAT IT DOES NOT DO. It does not make unknown kinds illegal at runtime: auditing must
never drop an event because bookkeeping disagrees. The registry is a contract for
CONSUMERS, and this test is the thing that keeps the contract true.
"""
import ast
import json
import re
from pathlib import Path

import pytest

from vaf.core.security_events import SECURITY_EVENT_KINDS

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "web" / "components" / "NotificationsModal.tsx"
DESIGN_DOC = REPO / "docs" / "security" / "SECURITY_DASHBOARD.md"

# Every function whose FIRST positional argument is an event kind: the writer itself, the
# skill helper, and the two per-module wrappers. The wrappers are in the list because
# their own call to the writer passes a variable - scanning only the writer would see
# `log_security_event(kind, ...)` and find nothing, which is how a scan can measure zero
# and look like proof.
EMITTERS = {
    "log_security_event",
    "emit_skill_security_event",
    "_emit_sk",              # web_server's import alias for emit_skill_security_event
    "_emit_security_event",  # auth/middleware.py wrapper
    "_log_auth_failure",     # api/auth_routes.py wrapper
}


def _constants(node) -> list[str]:
    """String constants a kind expression can evaluate to (literal or if/else)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _constants(node.body) + _constants(node.orelse)
    return []


def _emitted_kinds() -> dict[str, set[str]]:
    """Kind -> the files that emit it, by static scan of the emitter call sites."""
    found: dict[str, set[str]] = {}
    for path in sorted((REPO / "vaf").rglob("*.py")):
        try:
            tree = ast.parse(path.read_bytes())
        except SyntaxError:  # pragma: no cover - a broken file fails louder elsewhere
            continue
        # Kinds bound to a local name (`kind = "a" if cond else "b"`), so an emitter
        # called with that name still resolves. rescan.py picks its kind exactly so.
        bound: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        values = _constants(node.value)
                        if values:
                            bound.setdefault(target.id, []).extend(values)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in EMITTERS:
                continue
            kinds = _constants(node.args[0])
            if not kinds and isinstance(node.args[0], ast.Name):
                kinds = bound.get(node.args[0].id, [])
            for kind in kinds:
                # as_posix(), not str(): `relative_to` yields backslashes on Windows, so a
                # plain string would only ever match on the developer's platform. This is
                # the second time that has turned CI red in one round - the frozen paths in
                # tests/test_api_key_baseline.py had it first.
                found.setdefault(kind, set()).add(path.relative_to(REPO).as_posix())
    return found


def _ui_labelled_kinds() -> set[str]:
    """Kinds `evKindLabel` in the dashboard has a case for."""
    source = DASHBOARD.read_text(encoding="utf-8")
    block = re.search(r"const evKindLabel[\s\S]*?\n  \};", source)
    assert block, "evKindLabel not found - the dashboard's label switch moved or was renamed"
    return set(re.findall(r"case '([a-z_]+)':", block.group(0)))


def _ui_label_keys() -> set[str]:
    source = DASHBOARD.read_text(encoding="utf-8")
    block = re.search(r"const evKindLabel[\s\S]*?\n  \};", source)
    return set(re.findall(r"t\('([A-Za-z0-9_]+)'\)", block.group(0)))


def test_every_emitted_kind_is_in_the_registry():
    """The direction that matters: something reaches the log that no consumer knows."""
    emitted = _emitted_kinds()
    missing = {k: sorted(v) for k, v in emitted.items() if k not in SECURITY_EVENT_KINDS}
    assert not missing, (
        "these kinds are written but not declared in SECURITY_EVENT_KINDS: "
        f"{json.dumps(missing, indent=2, sort_keys=True)}"
    )


def test_the_registry_has_no_dead_rows():
    """A declared kind nobody emits is a promise to a reader that never arrives."""
    emitted = set(_emitted_kinds())
    dead = sorted(set(SECURITY_EVENT_KINDS) - emitted)
    assert not dead, f"declared but never emitted: {dead}"


def test_the_scan_actually_finds_the_emit_sites():
    """The guard's own floor.

    A static scan that silently matches nothing passes both tests above while measuring
    exactly nothing - the 'probe that measures nothing' failure in tests/README.md. So
    pin two things the scan MUST see: a plain literal call, and the wrapper lane, which
    is the one a naive scan misses.
    """
    emitted = _emitted_kinds()
    assert len(emitted) >= 14, f"the scan found only {len(emitted)} kinds; it is broken"
    assert "vaf/auth/middleware.py" in emitted["ip_blocked"]
    assert "vaf/skills/rescan.py" in emitted["skill_scan_alert"], (
        "the conditional-kind lane is unseen; rescan.py picks its kind with an if/else"
    )


@pytest.mark.parametrize("kind", sorted(SECURITY_EVENT_KINDS))
def test_the_dashboard_labels_every_kind(kind):
    """Otherwise the event list shows a reader `skill_blocked` instead of a sentence."""
    assert kind in _ui_labelled_kinds(), f"evKindLabel has no case for '{kind}'"


def test_every_dashboard_label_exists_in_both_catalogs():
    """A missing translation key renders as the key itself, which is worse than English."""
    keys = _ui_label_keys()
    assert keys, "no translation keys found in evKindLabel"
    for locale in ("en", "de"):
        catalog = json.loads((REPO / "web" / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
        flat = json.dumps(catalog)
        missing = sorted(k for k in keys if f'"{k}"' not in flat)
        assert not missing, f"{locale}.json is missing: {missing}"


def test_the_design_doc_table_lists_every_kind():
    """The doc was one of the four disagreeing copies; it does not get to drift again."""
    doc = DESIGN_DOC.read_text(encoding="utf-8")
    missing = sorted(k for k in SECURITY_EVENT_KINDS if f"`{k}`" not in doc)
    assert not missing, f"docs/security/SECURITY_DASHBOARD.md does not document: {missing}"
