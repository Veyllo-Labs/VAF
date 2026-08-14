# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The MAIN app must never believe it is a sub-agent child.

Measured live: the backend was restarted from the terminal a finished coder
child had run in, inherited VAF_IN_SUBAGENT_TERMINAL=1 plus the child's stale
task id, and from then on every coder ran invisibly in-process under that
stale id while the event bridge took the child branch inside the main process.
Rule 4.5 guards these markers within a process; this suite pins the guard at
process BIRTH and the two main entry points that must call it.
"""
from pathlib import Path

from vaf.core.platform import _SUBAGENT_ENV_MARKERS, scrub_inherited_subagent_env

ROOT = Path(__file__).resolve().parents[1]


_MARKERS_A_CHILD_LEAVES_BEHIND = (
    "VAF_IN_SUBAGENT_TERMINAL", "VAF_TASK_ID", "VAF_AGENT_TYPE",
    "VAF_SPAWN_MODE", "VAF_PARENT_CWD", "VAF_SESSION_ID", "VAF_ROOM_ID",
)


def test_every_inherited_child_marker_is_scrubbed(monkeypatch):
    """MUTATION: drop VAF_IN_SUBAGENT_TERMINAL (or any sibling) from the list.

    One surviving marker is enough to relapse: the terminal flag alone flips
    the bridge into the child branch and suppresses every spawn. The keys are
    OUR OWN list on purpose - seeding from the module's would let a shrunken
    module list pass vacuously (it did, in this test's first version)."""
    for key in _MARKERS_A_CHILD_LEAVES_BEHIND:
        monkeypatch.setenv(key, "stale-value")
    scrub_inherited_subagent_env()
    import os
    for key in _MARKERS_A_CHILD_LEAVES_BEHIND:
        assert os.environ.get(key) is None, f"{key} survived the birth scrub"


def test_a_clean_environment_stays_untouched(monkeypatch):
    for key in _SUBAGENT_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    scrub_inherited_subagent_env()  # must not raise, must not invent keys
    import os
    assert all(os.environ.get(k) is None for k in _SUBAGENT_ENV_MARKERS)


def test_both_main_entry_points_scrub_at_birth():
    """MUTATION: remove the call from run_server or from the tray's run_app.

    The tray and the standalone server are the two ways the main process is
    born; a scrub in only one leaves the other lane open to the same relapse.
    """
    server_src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    server_head = server_src.split("def run_server(", 1)[1][:600]
    assert "scrub_inherited_subagent_env()" in server_head, (
        "run_server no longer scrubs inherited child markers")

    tray_src = (ROOT / "vaf" / "tray.py").read_text(encoding="utf-8")
    tray_head = tray_src.split("def run_app():", 1)[1][:600]
    assert "scrub_inherited_subagent_env()" in tray_head, (
        "the tray's run_app no longer scrubs inherited child markers")
