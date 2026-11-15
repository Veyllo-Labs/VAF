# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf.core.nesting.nesting_depth`: the depth walk the tool funnel and the app link share.

It has to answer for values every recursive walk chokes on (tens of thousands of levels, a
dict that contains itself, a list shared twice per level), so it keeps its own stack, stops
at ``limit + 1`` and expands a shared container once per depth it is reached at, not once per
path. Deep values are built with loops here, never with recursion.

MUTATION: drop the identity memo and the shared-value case blows its time bound (2**22
paths); count only dict and list and the tuple case goes red; recurse instead of keeping a
stack and the 50,000-level case raises `RecursionError`.
"""
import time

import pytest

from vaf.core.nesting import nesting_depth


def _deep_list(depth: int) -> list:
    value: list = []
    for _ in range(depth - 1):
        value = [value]
    return value


def _shared(levels: int) -> dict:
    """``{"plan": [[...]]}`` where each of ``levels`` lists holds the next one twice: 2**levels
    paths over ``levels + 1`` lists, true depth ``levels + 2``."""
    node: list = []
    for _ in range(levels):
        node = [node, node]
    return {"plan": node}


@pytest.mark.parametrize("value,expected", [
    (None, 0), (1, 0), ("text", 0), (b"[[[", 0), ({}, 1), ([], 1), ((), 1),
    ({"a": [()]}, 3), ({"plan": "x"}, 1), ({"a": [[1]]}, 3),
    ([1, [2, [3]], {"k": {"m": []}}], 4), ([1, [2, [3]], {"k": 5}], 3),
])
def test_nesting_depth_counts_container_levels(value, expected):
    assert nesting_depth(value) == expected


def test_nesting_depth_of_a_tuple_counts_like_a_list():
    assert nesting_depth((((1,),),)) == 3
    assert nesting_depth({"a": ({"b": ()},)}) == 4


def test_nesting_depth_handles_fifty_thousand_levels_without_a_limit():
    assert nesting_depth(_deep_list(50_000)) == 50_000


def test_nesting_depth_stops_one_past_the_limit():
    assert nesting_depth(_deep_list(100_000), limit=100) == 101
    assert nesting_depth(_deep_list(100), limit=100) == 100
    assert nesting_depth(_deep_list(101), limit=100) == 101
    assert nesting_depth(_deep_list(7), limit=100) == 7


def test_nesting_depth_of_a_value_that_contains_itself_ends_at_the_limit():
    loop: dict = {"x": 1}
    loop["self"] = loop
    assert nesting_depth(loop, limit=100) == 101
    ring_a: list = []
    ring_b: list = [ring_a]
    ring_a.append(ring_b)
    assert nesting_depth({"plan": ring_a}, limit=5) == 6


def test_nesting_depth_of_a_shared_value_costs_its_nodes_not_its_paths():
    value = _shared(22)
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        depth = nesting_depth(value)
        best = min(best, time.perf_counter() - start)
        assert depth == 24
    assert best < 0.25, f"{best:.3f} s for 23 lists: the walk follows paths, not nodes"
    assert nesting_depth(value, limit=100) == 24
    assert nesting_depth(value, limit=10) == 11


def test_nesting_depth_reaches_a_shared_value_at_its_deepest_level():
    """A list reached shallow first and deep later must be measured from the deep place."""
    inner = [[[]]]                       # depth 3
    value = [inner, [[[inner]]]]         # inner at level 2 and at level 5
    assert nesting_depth(value) == 7
    assert nesting_depth([[[[inner]]], inner]) == 7
