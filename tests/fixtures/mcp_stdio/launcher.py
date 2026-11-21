# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A wrapper that runs a server as its child and waits, for tests/test_mcp_stdio_deadline.py.

Run as ``python launcher.py SCRIPT ARGS...``: it starts ``python SCRIPT ARGS...`` with the
standard handles inherited, keeps its own copies of them open while the child runs, and
exits with the child's code. That is what the python.exe of a Windows virtual environment
does, and a shell script that does not ``exec``: the end of the server's output never
arrives while the wrapper lives.
"""
import subprocess
import sys

raise SystemExit(subprocess.call([sys.executable, *sys.argv[1:]]))
