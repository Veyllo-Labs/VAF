# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Co-authored-by trailer on VAF-authored commits.

The trailer marks commits whose CONTENT VAF authored (project versioning
commits, the coder's final commit, GitHub file commits) and must stay off
user-initiated commits. Format contract: exactly two blank lines between the
message body and the trailer block, which requires --cleanup=verbatim at the
git call sites (default cleanup collapses consecutive blank lines).
"""

import subprocess

import pytest

from vaf.core.config import Config
from vaf.tools.git_coauthor import SetGitCoauthorTool
from vaf.tools.project_git import ProjectRollbackTool, apply_coauthor_trailer

DEFAULT_IDENTITY = "VAF Agent <noreply@veyllo.app>"
TRAILER = f"Co-authored-by: {DEFAULT_IDENTITY}"


@pytest.fixture
def coauthor_config(monkeypatch):
    """In-memory Config so tests never touch the user's ~/.vaf/config.json."""
    store = {"git_coauthor_enabled": True, "git_coauthor_identity": DEFAULT_IDENTITY}
    monkeypatch.setattr(
        Config, "get", classmethod(lambda cls, key, default=None: store.get(key, default))
    )
    monkeypatch.setattr(
        Config, "set", classmethod(lambda cls, key, value: store.__setitem__(key, value))
    )
    return store


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _last_message(path) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=path, capture_output=True, text=True, check=True
    )
    return out.stdout


class TestApplyCoauthorTrailer:
    def test_appends_trailer_with_two_blank_lines(self, coauthor_config):
        assert apply_coauthor_trailer("Fix the parser") == f"Fix the parser\n\n\n{TRAILER}"

    def test_idempotent(self, coauthor_config):
        once = apply_coauthor_trailer("Fix the parser")
        assert apply_coauthor_trailer(once) == once

    def test_disabled_flag_leaves_message_unchanged(self, coauthor_config):
        coauthor_config["git_coauthor_enabled"] = False
        assert apply_coauthor_trailer("Fix the parser") == "Fix the parser"

    def test_empty_identity_leaves_message_unchanged(self, coauthor_config):
        coauthor_config["git_coauthor_identity"] = "  "
        assert apply_coauthor_trailer("Fix the parser") == "Fix the parser"

    def test_custom_identity(self, coauthor_config):
        coauthor_config["git_coauthor_identity"] = "Custom Bot <bot@example.org>"
        assert apply_coauthor_trailer("x").endswith("Co-authored-by: Custom Bot <bot@example.org>")

    def test_config_failure_never_blocks_the_commit(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(Config, "get", classmethod(_boom))
        assert apply_coauthor_trailer("Fix the parser") == "Fix the parser"


class TestCommitIntegration:
    def test_project_git_commit_carries_trailer_verbatim(self, tmp_path, coauthor_config):
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("hello")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        result = ProjectRollbackTool._commit(str(tmp_path), "Backup before rollback (VAF)")

        assert result.returncode == 0
        body = _last_message(tmp_path)
        # exactly two blank lines between body and trailer block survive verbatim cleanup
        assert f"Backup before rollback (VAF)\n\n\n{TRAILER}" in body

    def test_coder_final_commit_carries_trailer(self, tmp_path, coauthor_config):
        from vaf.tools.coder import _final_commit

        _init_repo(tmp_path)
        (tmp_path / "main.py").write_text("print('hi')\n")

        status = _final_commit(str(tmp_path), "Task complete: demo")

        assert "commit" in status.lower() or "Git:" in status
        assert f"Task complete: demo\n\n\n{TRAILER}" in _last_message(tmp_path)

    def test_disabled_commit_has_no_trailer(self, tmp_path, coauthor_config):
        coauthor_config["git_coauthor_enabled"] = False
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("hello")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        ProjectRollbackTool._commit(str(tmp_path), "Backup before rollback (VAF)")

        assert "Co-authored-by" not in _last_message(tmp_path)


class TestSetGitCoauthorTool:
    def test_disable_from_chat(self, coauthor_config):
        out = SetGitCoauthorTool().run(enabled=False)
        assert "DISABLED" in out
        assert coauthor_config["git_coauthor_enabled"] is False
        assert apply_coauthor_trailer("msg") == "msg"

    def test_enable_from_chat(self, coauthor_config):
        coauthor_config["git_coauthor_enabled"] = False
        out = SetGitCoauthorTool().run(enabled=True)
        assert "ENABLED" in out
        assert coauthor_config["git_coauthor_enabled"] is True

    def test_string_boolean_coercion_for_weak_models(self, coauthor_config):
        SetGitCoauthorTool().run(enabled="false")
        assert coauthor_config["git_coauthor_enabled"] is False
        SetGitCoauthorTool().run(enabled="true")
        assert coauthor_config["git_coauthor_enabled"] is True

    def test_invalid_identity_rejected_without_changes(self, coauthor_config):
        out = SetGitCoauthorTool().run(enabled=True, identity="not an identity")
        assert "Error" in out
        assert coauthor_config["git_coauthor_identity"] == DEFAULT_IDENTITY

    def test_custom_identity_applied(self, coauthor_config):
        SetGitCoauthorTool().run(enabled=True, identity="Custom Bot <bot@example.org>")
        assert coauthor_config["git_coauthor_identity"] == "Custom Bot <bot@example.org>"
