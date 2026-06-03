"""Whare Wananga -- VAF tool self-learning subsystem.

Currently only the tool_knowledge store (persistence + schema). The learning loop
(predict-then-verify sandbox practice) and Action-Tag know-how injection build on top.
"""

from vaf.whare_wananga.store import (
    SCHEMA_VERSION,
    new_record,
    compute_tool_hash,
    load,
    save,
    list_tools,
    delete,
    learned_state,
    is_learned,
    learned_states,
    STATE_UNLEARNED,
    STATE_LEARNING,
    STATE_LEARNED,
    STATE_STALE,
)

__all__ = [
    "SCHEMA_VERSION",
    "new_record",
    "compute_tool_hash",
    "load",
    "save",
    "list_tools",
    "delete",
    "learned_state",
    "is_learned",
    "learned_states",
    "STATE_UNLEARNED",
    "STATE_LEARNING",
    "STATE_LEARNED",
    "STATE_STALE",
]
