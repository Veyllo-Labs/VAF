# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A2A: rooms in which several agents hold one conversation.

The package is deliberately import-light and stdlib-only at module level. A room
peer may be a sub-agent child process, a foreign agent driving the CLI, or VAF on
a slim install, and none of those can be asked to carry a web framework or a
validation library just to read a message.

Layout, and why each piece is separate:

- ``frame``  the wire contract. Pure data, no I/O. A third-party implementation
             only has to agree with this module.
- ``store``  the room on disk. One file per frame, write-once, encrypted through
             the same primitive sessions use.
- ``room``   membership, roles and the rules that decide what a peer may emit.

See docs/agents/A2A_PROTOCOL.md for the specification a stranger implements from.
"""
