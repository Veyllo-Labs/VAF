# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""python_sandbox must not let the model 'save' files to a non-persistent path.

The sandbox runs in a Docker container isolated from the host filesystem (only a scratch
/workspace volume, no bind-mount to the user's Documents/VAF_Projects). A write to a
host/workspace path lands in the container's ephemeral layer and is discarded when the run
ends, even though the code's own print("Saved: ...") reports success. The guard detects that
intent and redirects the model to write_file (which persists), while leaving scratch use alone.
"""
import pytest

from vaf.tools.python_sandbox import PythonSandboxTool

guard = PythonSandboxTool._blocked_persistence_write


def _blocked(code: str) -> bool:
    return guard(code) is not None


# An example write to a host persistence path that does not survive an isolated run.
BUG_CODE = (
    'content = "..."\n'
    'open("/home/user/Documents/VAF_Projects/a74e6e21/session01/example_notes.md", "w").write(content)\n'
    'print("Saved: /home/user/Documents/VAF_Projects/a74e6e21/session01/example_notes.md")'
)


def test_the_exact_vanishing_bug_is_blocked():
    assert _blocked(BUG_CODE)
    msg = guard(BUG_CODE)
    assert "write_file" in msg  # steers to the right tool


@pytest.mark.parametrize("code", [
    'open("/home/user/Documents/VAF_Projects/x/out.md", "w").write(t)',
    'df.to_csv("/home/user/Documents/VAF_Projects/x/out.csv")',
    'from pathlib import Path; Path("/home/user/Documents/report.md").write_text(t)',
    'open("/Users/user/Documents/x.txt", "a").write(t)',
    'open(f"{workspace}/notes.md", "w").write(t)  # workspace under VAF_Projects\n# VAF_Projects',
])
def test_persistence_writes_blocked(code):
    assert _blocked(code)


@pytest.mark.parametrize("code", [
    'print(2 ** 32)',                                   # pure compute
    'open("/tmp/scratch.csv", "w").write(x)',           # /tmp scratch
    'open("data.csv", "w").write(x)',                   # relative -> /workspace scratch
    'open("/workspace/tmp.json", "w").write(x)',        # explicit scratch volume
    'open("/home/user/Documents/VAF_Projects/x/in.csv", "r").read()',  # READ, not write
    'import pandas as pd; print(pd.DataFrame({"a": [1]}).describe())',  # df compute, no write
    'import sys; sys.stdout.write("hello")',            # stdout, not a file
])
def test_scratch_read_and_compute_allowed(code):
    assert not _blocked(code)


def test_empty_code_is_allowed():
    assert guard("") is None
