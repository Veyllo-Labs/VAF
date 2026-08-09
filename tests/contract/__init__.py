# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Package marker, load-bearing for imports rather than cosmetic.

Without it, pytest imports this directory's conftest.py under the bare module
name 'conftest', where it can shadow the enclosing suite's own top-level
conftest (in this repo: `from conftest import bind_chat_stages` in older
tests resolved to THIS file and failed collection). As a package, the
conftest lives at '<dirname>.conftest' and can never collide - the same holds
inside an embedder's CI. Keep the vendored directory name a valid Python
identifier (see README.md).
"""
