# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Structural guard for the tag result set in the memory UI (web/components/memory).

web/ has no test runner (no jest, no vitest, no `test` script) and CI runs no
npm/tsc step, so an assertion over the .tsx source is the only guard that can
exist here. It shows the wiring EXISTS, never that it WORKS - a live click is
still the proof.

Each assertion is red when its fix is reverted:
- the connected-memories derivation is ONE exported helper, not a per-panel copy
  (the detail panel used to walk the edges itself),
- a tag click pins a result set instead of only selecting a node,
- a search and a tag replace each other (that is what "the list stays until I
  search or click another tag" means),
- the detail panel no longer owns a second list of the same memories, and offers
  the way back to the pinned list,
- the graph rebuild is keyed on the STRUCTURE, not on the node array identity:
  highlightNodes() replaces that array on every search, and keying the build on
  it re-ran ForceAtlas2 and reshuffled the layout under the user.
"""
from pathlib import Path

WEB = Path("web/components/memory")


def _code(path: Path) -> str:
    """Source without comment LINES (line level, never a block-comment regex)."""
    lines = path.read_bytes().decode("utf-8").splitlines()
    return "\n".join(ln for ln in lines
                     if not ln.lstrip().startswith(("//", "*", "/*")))


def test_the_connected_memories_derivation_is_shared():
    store = _code(WEB / "stores/memoryStore.ts")
    assert "export function connectedMemoriesForTag(" in store
    for panel in ("RagQueryPanel.tsx", "MemoryDetailPanel.tsx"):
        src = _code(WEB / panel)
        # The CALL, not the import: an import survives the call being replaced
        # by a local copy, and would prove nothing.
        assert "connectedMemoriesForTag(nodes, edges" in src, \
            f"{panel} stopped calling the shared helper"
        assert "edge.source === selectedNodeId" not in src, \
            f"{panel} hand-rolls the edge walk again"


def test_a_tag_click_pins_a_result_set():
    graph = _code(WEB / "MemoryGraph.tsx")
    i = graph.index("clickNode")
    block = graph[i:i + 900]
    assert "showTagResults(" in block, "a tag click no longer fills the search panel"
    assert "isTag" in block, "the tag branch is gone - a tag id would hit selectMemory"


def test_search_and_tag_replace_each_other():
    store = _code(WEB / "stores/memoryStore.ts")
    # rindex: the first hit is the interface declaration, not the implementation
    show = store[store.rindex("showTagResults:"):store.rindex("clearTagResults:")]
    assert "ragResult: null" in show, "a pinned tag must clear a stale search result"
    search = store[store.index("searchMemories: async"):]
    search = search[:search.index("fetchStats:")]
    assert search.count("activeTagNodeId: null") >= 2, \
        "a search must clear the pinned tag in BOTH branches (empty query too)"


def test_the_detail_panel_hands_the_list_over():
    panel = _code(WEB / "MemoryDetailPanel.tsx")
    # Both halves, exact forms: a substring check on the bare name survives a
    # rename and would prove nothing.
    assert "onShowInSearch: () => void;" in panel, "the return-path prop is gone"
    assert "onShowInSearch={" in panel, "the return path is declared but never passed"
    assert "onClick={onShowInSearch}" in panel, "the return-path button lost its handler"
    assert "Connected Memories\n" not in panel, \
        "the duplicated browse list is back in the detail panel"
    assert "showTagResults(" in panel, "the tag chips lost their mobile entry point"


def test_the_graph_rebuild_is_keyed_on_structure_and_content():
    graph = _code(WEB / "MemoryGraph.tsx")
    assert "const structureKey" in graph
    key = graph[graph.index("const structureKey"):]
    key = key[:key.index("[storeNodes, storeEdges],")]
    assert "n.data.label" in key, \
        "a renamed memory would never reach the canvas (ids-only key)"
    assert key.count(".sort()") >= 2, (
        "nodes AND edges must be sorted: an unsorted key relayouts on the "
        "updated_at reorder that follows any edit")
    # The build effect must not depend on the node ARRAY: highlightNodes
    # replaces it on every search, which would re-run ForceAtlas2. Anchored on
    # code, never on a comment - _code() strips those.
    assert "}, [structureKey" in graph, "the build effect lost its structural key"
    assert "}, [storeNodes, storeEdges]" not in graph, \
        "the build effect keys on the node array again - every search relayouts"
    assert "highlightedRef" in graph, "highlights must restyle via the reducer"


def test_a_pinned_tag_never_writes_the_highlight_url_parameter():
    """fetchGraph turns ragSources into an &highlight= URL parameter, so a hub
    tag would put hundreds of UUIDs in the query string."""
    store = _code(WEB / "stores/memoryStore.ts")
    show = store[store.rindex("showTagResults:"):store.rindex("clearTagResults:")]
    assert "highlightNodes(ids, false)" in show, \
        "a pinned tag writes its whole membership into ragSources again"


def test_clicking_empty_space_resets_the_panel():
    """The clean-slate gesture: an empty-space click drops the selection AND the
    pinned result set, so the panel cannot keep showing a list for something the
    graph no longer marks."""
    graph = _code(WEB / "MemoryGraph.tsx")
    i = graph.index("clickStage")
    block = graph[i:i + 600]
    assert "clearTagResults()" in block, "an empty-space click keeps a stale tag list"
    assert "clearRagResult()" in block, "an empty-space click keeps stale search results"
