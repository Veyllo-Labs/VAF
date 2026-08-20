# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whare Wananga -- VAF tool self-learning subsystem.

The tool_knowledge store (persistence + schema) plus the loop that fills it: the
predict-then-verify runner, the background job manager, the eager and teacher
workers, the retrain queue, and Action-Tag know-how delivery.

Re-exported here is what other subsystems consume: the store readers, and
`active_runs()` for anything that needs to know a run is in flight without
already knowing which tool it is for.
"""

from vaf.whare_wananga.jobs import active_runs
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
    "active_runs",
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
