# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Coder CONTENT_ONLY cleanup must never delete a user workspace.

Live incident 2026-07-03: the workflow engine injected the per-chat workspace as
project_path while the task text still matched CONTENT_ONLY mode; the cleanup then
rmtree'd the user's per-chat folder including a freshly written file. The cleanup
now goes through _cleanup_content_only_dir, which only removes vaf_content_* dirs
under the system temp directory - exactly what the CONTENT_ONLY branch creates.
"""
import tempfile
from pathlib import Path

from vaf.tools.coder import _cleanup_content_only_dir


def test_real_content_only_temp_dir_is_removed():
    d = tempfile.mkdtemp(prefix="vaf_content_")
    (Path(d) / "artifact.txt").write_text("x")
    _cleanup_content_only_dir(d)
    assert not Path(d).exists()


def test_injected_user_workspace_survives(tmp_path):
    """THE regression: a per-chat workspace passed in as project_path must survive."""
    ws = tmp_path / "VAF_Projects" / "ab12cd34" / "green123456" / "Workflow"
    ws.mkdir(parents=True)
    f = ws / "draft.txt"
    f.write_text("user data")
    _cleanup_content_only_dir(str(ws))
    assert f.exists() and f.read_text() == "user data"


def test_tempdir_without_our_prefix_survives():
    """Even inside the system temp dir, only OUR vaf_content_* dirs are fair game."""
    d = tempfile.mkdtemp(prefix="user_stuff_")
    try:
        _cleanup_content_only_dir(d)
        assert Path(d).exists()
    finally:
        Path(d).rmdir()


def test_prefix_lookalike_outside_tempdir_survives(tmp_path, monkeypatch):
    """A vaf_content_* NAME outside the temp root is not ours either.

    pytest's tmp_path itself lives under the real temp root, so relocate the
    temp root for this test to make tmp_path genuinely 'outside'.
    """
    fake_tmp_root = tmp_path / "faketmp"
    fake_tmp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp_root))
    d = tmp_path / "vaf_content_fake"
    d.mkdir()
    (d / "keep.txt").write_text("keep")
    _cleanup_content_only_dir(str(d))
    assert (d / "keep.txt").exists()
