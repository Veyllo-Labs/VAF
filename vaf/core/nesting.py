# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How deeply a decoded value nests, measured without recursion.

A value that arrived as JSON, or a dict handed over already built, can be nested far deeper
than any recursive walk survives: ``json.loads`` decodes up to about 10,000 levels on 3.13,
while ``copy.deepcopy`` gives up at about 500 and a plain recursive copy at about 1,000. A
check that is meant to stop such a value before those walks cannot itself recurse, so this
one keeps its own stack.

Pure and stdlib only, so a protocol module that must stay dependency-free can share it.
"""
from __future__ import annotations

from typing import Any

# The container types the recursive walks over decoded arguments descend into
# (`tool_dispatch.make_json_serializable` treats a tuple like a list).
_CONTAINERS = (dict, list, tuple)


def nesting_depth(value: Any, limit: int | None = None) -> int:
    """The number of container levels in ``value``: a scalar is 0, ``{}`` is 1, ``{"a": [()]}`` is 3.

    Only ``dict``, ``list`` and ``tuple`` count, matched by ``isinstance``; a dict's keys are
    not looked into, because JSON keys are strings.

    With ``limit``, the walk stops as soon as a container at level ``limit + 1`` is reached,
    so the answer is ``min(depth, limit + 1)`` and a value 100,000 levels deep costs no more
    than one ``limit + 1`` deep. Each container is remembered by identity together with the
    deepest level it was expanded at, and is expanded again only when it is reached deeper:
    a sub-object shared by many paths costs its node count instead of its path count (a value
    built by sharing one list twice per level has 2**n paths and n nodes), and a value that
    contains itself climbs one level per round until it reaches ``limit + 1``. Without a
    limit such a cycle never ends, so give one whenever the value may come from code rather
    than from a JSON decoder.
    """
    if not isinstance(value, _CONTAINERS):
        return 0
    deepest = 0
    expanded: dict[int, int] = {}
    stack = [(value, 1)]
    while stack:
        node, level = stack.pop()
        key = id(node)
        if level <= expanded.get(key, 0):
            continue
        expanded[key] = level
        if level > deepest:
            deepest = level
            if limit is not None and level > limit:
                return level
        for child in (node.values() if isinstance(node, dict) else node):
            if isinstance(child, _CONTAINERS):
                stack.append((child, level + 1))
    return deepest
