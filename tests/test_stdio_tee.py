# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""stdio tee: a terminal-started tray must produce a followable log file.

The tee is fd-level so CHILD processes inherit it (the frontend's output has to
land in the file too), and it must pass everything through to the original
descriptors (the person watching the terminal keeps seeing lines). The decision
function declines when a launcher already redirects stdout to a file or when
systemd owns the output. Everything runs in subprocesses - installing the tee
in the pytest process would fight the capture machinery."""
import os
import subprocess
import sys
from pathlib import Path

_SHOULD_TEE_CODE = "from vaf.core.stdio_tee import should_tee_stdio; print(should_tee_stdio())"


def _run(code, **kwargs):
    env = {**os.environ}
    for var in ("VAF_LOG_TO_JOURNAL", "JOURNAL_STREAM"):
        env.pop(var, None)
    env.update(kwargs.pop("env_extra", {}))
    return subprocess.run([sys.executable, "-c", code], env=env, timeout=60, **kwargs)


def test_tee_duplicates_own_and_child_output_and_passes_through(tmp_path):
    log = tmp_path / "vaf_run.log"
    code = (
        "from vaf.core.stdio_tee import tee_stdio_to_file\n"
        f"assert tee_stdio_to_file({str(log)!r})\n"
        "print('parent-line', flush=True)\n"
        "import subprocess, sys, time\n"
        "subprocess.run([sys.executable, '-c', \"print('child-line')\"])\n"
        "time.sleep(0.5)\n"
    )
    result = _run(code, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    content = log.read_text()
    assert "parent-line" in content
    assert "child-line" in content, "children inherit the teed descriptors"
    assert "parent-line" in result.stdout, "passthrough must keep the terminal live"
    assert "child-line" in result.stdout


def test_should_tee_wants_a_pipe_but_declines_unit_and_file_redirects(tmp_path):
    result = _run(_SHOULD_TEE_CODE, capture_output=True, text=True)
    assert "True" in result.stdout, "piped stdout has no followable log - tee wanted"

    result = _run(_SHOULD_TEE_CODE, capture_output=True, text=True,
                  env_extra={"VAF_LOG_TO_JOURNAL": "1"})
    assert "False" in result.stdout, "our unit says the journal is canonical"

    # An unrelated terminal-inherited INVOCATION_ID must NOT disable the tee -
    # on systemd-managed desktops every terminal carries one (live finding).
    result = _run(_SHOULD_TEE_CODE, capture_output=True, text=True,
                  env_extra={"INVOCATION_ID": "abc123"})
    assert "True" in result.stdout

    out_file = tmp_path / "redirected.log"
    with open(out_file, "w") as fh:
        _run(_SHOULD_TEE_CODE, stdout=fh, stderr=subprocess.PIPE)
    assert "False" in out_file.read_text(), "a launcher-owned file must not be teed into twice"


def test_journal_stream_only_counts_when_it_matches_fd1(monkeypatch):
    import vaf.core.stdio_tee as tee_mod
    monkeypatch.delenv("VAF_LOG_TO_JOURNAL", raising=False)

    st = os.fstat(1)
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
    assert tee_mod._stdout_is_journal_stream() is True

    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino + 999}")
    assert tee_mod._stdout_is_journal_stream() is False, \
        "a stale JOURNAL_STREAM from a parent must not claim fd 1"

    monkeypatch.setenv("JOURNAL_STREAM", "garbage")
    assert tee_mod._stdout_is_journal_stream() is False
