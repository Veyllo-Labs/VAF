# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Textual stays OFF the base import graph.

Every `vaf` invocation imports the CLI dispatch modules; only the `vaf run`
app lane may pay for Textual. A module-level `import textual` anywhere on the
base graph would tax every command (including the slim install's) with the
whole framework - this guard runs in a subprocess because the test session
itself legitimately loads textual for the app tests.
"""
import subprocess
import sys


def test_base_cli_import_does_not_load_textual():
    probe = (
        "import sys\n"
        "import vaf.cli.cmd.run\n"
        "import vaf.cli.tui\n"
        "assert 'textual' not in sys.modules, 'textual leaked into the base import graph'\n"
        "print('clean')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
