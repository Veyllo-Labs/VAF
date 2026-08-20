# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tool bundle drift guard (CLAUDE Rule 2: CI guard over prose rule).

`category` decides which bundle a tool appears under in every human-facing tool
list. It used to be a de-facto attribute: read in six places, declared by ten
classes out of a hundred and thirty-seven, normalised nowhere and documented
nowhere - so all but the GitHub tools rendered as one undifferentiated pile.

Single source of truth for the vocabulary: TOOL_CATEGORIES in
vaf/core/tool_contract.py. Extend THERE first, then give the label a key in both
message catalogues.

The scan below is static (ast), so no tool is imported and no side effect of a
tool module can influence the result.
"""
import ast
import json
import re
from pathlib import Path

from vaf.core.messaging_connections import KNOWN_CHANNELS
from vaf.core.tool_contract import TOOL_CATEGORIES

REPO = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO / "vaf" / "tools"


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` pairs, so a tool whose name comes from a
    constant (cloud_storage.py) is still recognised."""
    out: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _declared_tools() -> list[tuple[str, int, str, str | None]]:
    """(file, line, tool_name, declared_category) for every in-tree tool class."""
    found: list[tuple[str, int, str, str | None]] = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_bytes().decode("utf-8"))
        consts = _module_string_constants(tree)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            name = category = None
            line = cls.lineno
            for stmt in cls.body:
                if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                    continue
                target = stmt.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                value = stmt.value
                literal = value.value if (isinstance(value, ast.Constant)
                                          and isinstance(value.value, str)) else None
                if literal is None and isinstance(value, ast.Name):
                    literal = consts.get(value.id)
                if target.id == "name" and literal is not None:
                    name, line = literal, stmt.lineno
                elif target.id == "category" and literal is not None:
                    category = literal
            if name is not None:
                found.append((path.name, line, name, category))
    return found


def test_every_in_tree_tool_declares_a_bundle():
    """A tool without a declaration lands in the catch-all pile, which is how
    a hundred and twelve of them ended up indistinguishable. Removing a single
    `category` line turns this red."""
    missing = [f"{f}:{line} {name}" for f, line, name, cat in _declared_tools() if cat is None]
    assert not missing, (
        "tool classes without a category declaration:\n  " + "\n  ".join(missing)
    )


def test_declared_bundles_come_from_the_vocabulary():
    """The vocabulary is open at RUNTIME so a third-party or MCP tool can name
    its own bundle. In-tree it is closed, otherwise a typo silently invents one."""
    unknown = [
        f"{f}:{line} {name} -> {cat!r}"
        for f, line, name, cat in _declared_tools()
        if cat is not None and cat not in TOOL_CATEGORIES
    ]
    assert not unknown, (
        "categories outside TOOL_CATEGORIES (vaf/core/tool_contract.py):\n  "
        + "\n  ".join(unknown)
    )


def test_every_messaging_channel_has_a_bundle():
    """The channel keys are welded to the messaging SSOT rather than copied by
    hand - a fifth hand-written platform list is exactly the drift CLAUDE Rule 2
    was written for."""
    missing = sorted(set(KNOWN_CHANNELS) - set(TOOL_CATEGORIES))
    assert not missing, (
        f"channels in KNOWN_CHANNELS without a bundle in TOOL_CATEGORIES: {missing}"
    )


def test_the_vocabulary_has_no_duplicates():
    assert len(TOOL_CATEGORIES) == len(set(TOOL_CATEGORIES))


def _bundle_order_from_ui() -> list[str]:
    """TOOL_BUNDLE_ORDER out of web/lib/toolBundles.ts, read as text.

    Deliberately not parsed as TypeScript: the point is to notice that the two
    lists drifted, and a regex over the literal notices exactly that without
    dragging a JS toolchain into the Python suite.
    """
    source = (REPO / "web" / "lib" / "toolBundles.ts").read_bytes().decode("utf-8")
    start = source.index("export const TOOL_BUNDLE_ORDER")
    body = source[start:source.index("] as const;", start)]
    return re.findall(r"'([a-z0-9_]+)'", body)


def test_the_web_ui_knows_every_bundle():
    """A key the UI does not list renders in an unlabelled trailing group. The
    UI may carry EXTRA keys ('custom' is a frontend-only distinction), but never
    fewer than the framework can produce."""
    missing = sorted(set(TOOL_CATEGORIES) - set(_bundle_order_from_ui()))
    assert not missing, (
        f"bundles missing from web/lib/toolBundles.ts TOOL_BUNDLE_ORDER: {missing}"
    )


def test_both_message_catalogues_label_every_bundle():
    """An unlabelled bundle would render its raw key as a heading. Both locales
    must carry every key - the parity guard only compares the two catalogues
    against each other, so a key missing from BOTH would slip through it."""
    for locale in ("en", "de"):
        path = REPO / "web" / "messages" / f"{locale}.json"
        groups = json.loads(path.read_bytes().decode("utf-8"))["modals"]["tools"]["groups"]
        missing = sorted(set(TOOL_CATEGORIES) - set(groups))
        assert not missing, f"{locale}.json modals.tools.groups is missing: {missing}"
        assert groups.get("custom"), f"{locale}.json needs a label for the custom-tools bundle"


def test_the_canonical_labels_cover_the_vocabulary():
    """CATEGORY_LABELS is what the CLI, the TUI and list_tools print; a key
    without one would be title-cased from its slug."""
    from vaf.core.tool_contract import CATEGORY_LABELS

    assert set(CATEGORY_LABELS) == set(TOOL_CATEGORIES), (
        f"CATEGORY_LABELS vs TOOL_CATEGORIES: {set(CATEGORY_LABELS) ^ set(TOOL_CATEGORIES)}"
    )


# ── The reserved custom namespace ───────────────────────────────────────────


def test_the_custom_namespace_is_reserved_for_uploaded_tools():
    """A bundle says where a tool comes from. A tool that ships with VAF must
    never sit in the namespace reserved for user uploads, and vice versa."""
    from vaf.core.tool_contract import CUSTOM_CATEGORY_PREFIX

    offenders = [
        f"{f}:{line} {name} -> {cat!r}"
        for f, line, name, cat in _declared_tools()
        if cat is not None and (cat == CUSTOM_CATEGORY_PREFIX
                                or cat.startswith(f"{CUSTOM_CATEGORY_PREFIX}_"))
    ]
    assert not offenders, (
        "in-tree tools may not declare a bundle in the reserved custom namespace:\n  "
        + "\n  ".join(offenders)
    )
    assert not any(c == CUSTOM_CATEGORY_PREFIX or c.startswith(f"{CUSTOM_CATEGORY_PREFIX}_")
                   for c in TOOL_CATEGORIES)


def test_an_uploaded_tool_lands_in_the_custom_namespace(tmp_path, monkeypatch):
    """The stamp is applied by the LOADER, so every surface gets it without
    knowing the rule. Reverting the stamp turns this red.

    The store is redirected at `get_custom_tools_dir`, not through HOME or an
    env var: Platform.data_dir() reads neither, so a test that only sets those
    writes into the real user's tool store.
    """
    from vaf.core import custom_tools_registry as registry

    monkeypatch.setattr(registry, "get_custom_tools_dir", lambda: tmp_path)

    cases = {
        "declared_bundle": ('category = "github"', "custom_github"),
        "no_bundle": ("", "custom"),
        "already_namespaced": ('category = "custom_github"', "custom_github"),
        "declares_general": ('category = "general"', "custom"),
    }
    for tool_name, (declaration, expected) in cases.items():
        source = (
            "from vaf.tools.base import BaseTool\n\n\n"
            f"class {tool_name.title().replace('_', '')}(BaseTool):\n"
            f'    name = "{tool_name}"\n'
            '    description = "A tool a user uploaded."\n'
            f"    {declaration}\n"
            "\n"
            "    def run(self, **kwargs):\n"
            '        return "ok"\n'
        )
        registry.save_tool_file(f"{tool_name}.py", source)
        registry.register_tool(tool_name, f"{tool_name}.py",
                               created_by="tester", shared_with=["*"])
        loaded = registry.load_custom_tool_class(tool_name)
        assert loaded is not None, tool_name
        assert loaded.category == expected, f"{tool_name}: {loaded.category} != {expected}"
        # And the resolver agrees, so the CLI, the TUI and list_tools do too.
        from vaf.core.tool_contract import tool_category
        assert tool_category(tool_name, loaded()) == expected
