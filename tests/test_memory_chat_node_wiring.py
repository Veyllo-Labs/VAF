# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The chat node on the Memory page (web/components/memory, web/app/memory).

One node per messenger chat the agent learned from; clicking it pins the same result set a
tag click pins, the details view offers ONE delete that empties the whole namespace through
the store and the DELETE /api/memory/chat route, the Chat legend toggle hides the node and
its members together, and the footer count leaves hub nodes out. web/ has no test runner of
its own, so these are the structural pins in the idiom of test_memory_tag_result_wiring.py.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "components" / "memory"


def _code(path: Path) -> str:
    lines = path.read_bytes().decode("utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith(("//", "*", "/*")))


def test_a_chat_click_pins_the_same_result_set_a_tag_does():
    graph = _code(WEB / "MemoryGraph.tsx")
    block = graph[graph.index("clickNode"):graph.index("clickNode") + 900]
    assert "isChat" in block and "isTag" in block and "showTagResults(" in block


def test_the_chat_toggle_hides_the_hub_and_its_members_together():
    graph = _code(WEB / "MemoryGraph.tsx")
    assert "{ type: 'chat', label: 'Chat', color: CHAT_COLOR }" in graph
    assert "(isChat || n.data.chatKey) ? CHAT_COLOR" in graph, "a chat memory wears the colour of the row that hides it"
    reducer = graph[graph.index("nodeReducer:"):graph.index("edgeReducer:")]
    assert "(attrs.isChat || attrs.chatKey) ? 'chat'" in reducer, \
        "a chat memory must follow the Chat toggle, not the Conversation one, or the hub is orphaned"
    edges = graph[graph.index("edgeReducer:"):graph.index("renderer.on('clickNode'")]
    assert "attrs.kind === 'chat'" in edges and "attrs.kind === 'tag' && !showTagEdgesRef.current" in edges


def test_the_footer_count_leaves_hub_nodes_out():
    graph = _code(WEB / "MemoryGraph.tsx")
    footer = graph[graph.rindex("memories\n") - 200:graph.rindex("memories\n")]
    assert "isChatNode" in footer and "isTagNode" in footer


def test_the_details_delete_goes_through_the_store_and_the_namespace_route():
    panel = _code(WEB / "MemoryDetailPanel.tsx")
    assert "function ChatDetailsView(" in panel
    assert "deleteChatNamespace(chatKey)" in panel
    assert "connectedMemoriesForTag(nodes, edges" in panel
    assert "useTranslations('modals')" in panel, "the new chrome strings come from the catalogue"
    assert "Chat Details" not in panel and "Delete chat memories" not in panel
    store = _code(WEB / "stores/memoryStore.ts")
    impl = store[store.rindex("deleteChatNamespace: async"):]
    assert "/api/memory/chat/${encodeURIComponent(chatKey)}" in impl
    assert "clearTagResults()" in impl[:impl.index("fetchGraph")], \
        "the pinned result set would otherwise keep naming a chat that is gone"


def test_both_hubs_take_the_short_details_panel_and_their_own_icon():
    page = _code(ROOT / "web" / "app" / "memory" / "page.tsx")
    assert "selectedNodeId?.startsWith('chat-')" in page
    panel = _code(WEB / "RagQueryPanel.tsx")
    assert "activeTagNodeId.startsWith('chat-')" in panel and "MessageSquare" in panel


def test_every_catalogue_carries_the_chat_strings():
    import json
    keys = {"chatDetails", "chatLearnedHere", "chatMemoriesCount", "chatShowInSearch", "chatDeleteButton",
            "chatDeleteBody", "chatDeleteConfirm", "chatDeleteCancel", "chatDeleting", "chatClose",
            "chatCollapse", "chatExpand"}
    for path in sorted((ROOT / "web" / "messages").glob("*.json")):
        memory = json.loads(path.read_text(encoding="utf-8"))["modals"]["memory"]
        assert keys <= set(memory), f"{path.name} lacks {keys - set(memory)}"
