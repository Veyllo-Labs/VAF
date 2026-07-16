# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Public marker constants for special engine return values.

The engine communicates handled failures and control-flow outcomes as
STRINGS inside the normal return channel (docs/CORE_AGENT.md, return
contract). Embedders used to copy those literals out of the docs; compare
against these constants instead so your handling cannot silently break.

Usage:

    from vaf import Agent, markers

    answer = agent.run("...")
    if answer.startswith(markers.SYSTEM_LOG_ONLY):
        ...  # handled failure, not a real answer

The values are pinned against the engine source by
tests/test_markers_sync.py: renaming a literal in the engine without
updating it here fails CI (Rule-2 guard instead of a second silent copy).
"""

# Handled degradation after exhausted internal retries; prefix of the
# returned status string, not a real answer.
SYSTEM_LOG_ONLY = "[SYSTEM_LOG_ONLY]"

# The user (or a stop request) ended the turn. NOTE: a mid-stream stop that
# already produced text returns the partial answer instead - match this
# marker, but do not rely on it as the only stop signal.
GENERATION_STOPPED = "[Generation stopped by user]"

# Loop protection ended the turn (wall clock or tool-turn budget). The
# returned string CONTAINS this marker with a warning-emoji prefix - match
# by substring, never by prefix.
LOOP_PROTECTION = "[LOOP_PROTECTION]"

# A delegated sub-agent task was accepted and runs asynchronously; the rest
# of the string is a human-readable status.
ASYNC_ACK = "[ASYNC_ACK]"

# A confirmation-gated tool was refused in non-interactive mode; appears in
# TOOL RESULTS (event sink / history), not as the run() return value.
TOOL_CONFIRMATION_REQUIRED = "requires confirmation"

__all__ = [
    "SYSTEM_LOG_ONLY",
    "GENERATION_STOPPED",
    "LOOP_PROTECTION",
    "ASYNC_ACK",
    "TOOL_CONFIRMATION_REQUIRED",
]
