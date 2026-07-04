# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Phase-aware coder meta-file guard.

The coder must never let a model dump planning/scratch files (PLAN.md, NOTES.md, ...)
into the project, in ANY phase. README is a real deliverable and is allowed ONLY in the
dedicated documentation phase - blocked during planning/build exactly as before. These
tests pin that split and prove parity with the pre-refactor blocklist for build phases.
"""
from vaf.tools.coder import (
    _ALWAYS_BLOCKED_META,
    _DOC_GATED_BASENAMES,
    _meta_file_block_reason,
)

# The exact set that the old inline _META_FILE_PATTERNS blocked in every phase.
_OLD_BLOCKED = {
    "plan.md", "structure.md", "structure_plan.md", "notes.md",
    "todo.md", "design.md", "layout.md", "readme.md", "read_chunks.py",
}
_BUILD_PHASES = ("main", "task_0", "task_1", "task_7")


def test_scratch_files_blocked_in_every_phase():
    for name in _ALWAYS_BLOCKED_META:
        for phase in _BUILD_PHASES + ("document",):
            assert _meta_file_block_reason(name, phase) is not None, (name, phase)


def test_readme_blocked_in_build_phases_allowed_in_document():
    for phase in _BUILD_PHASES:
        assert _meta_file_block_reason("README.md", phase) is not None
    assert _meta_file_block_reason("README.md", "document") is None
    assert _meta_file_block_reason("readme.md", "document") is None  # case-insensitive


def test_arbitrary_docs_are_not_gated():
    # Only the README basename is doc-gated; other docs are normal deliverables.
    for path in ("docs/api.md", "article.md", "docs/guide.md", "CHANGELOG.md"):
        for phase in _BUILD_PHASES + ("document",):
            assert _meta_file_block_reason(path, phase) is None, (path, phase)


def test_build_phase_parity_with_old_blocklist():
    # In any build phase, exactly the old 9-name set must be blocked (nothing more,
    # nothing less) among a representative candidate list.
    candidates = _OLD_BLOCKED | {
        "app.py", "index.html", "styles.css", "main.py", "docs/api.md", "article.md",
    }
    for phase in _BUILD_PHASES:
        blocked = {c for c in candidates if _meta_file_block_reason(c, phase) is not None}
        assert blocked == _OLD_BLOCKED, (phase, blocked ^ _OLD_BLOCKED)


def test_document_phase_lifts_only_readme():
    # In the document phase the ONLY difference vs a build phase is that README is allowed.
    candidates = _OLD_BLOCKED | {"app.py", "docs/api.md"}
    build_blocked = {c for c in candidates if _meta_file_block_reason(c, "task_1")}
    doc_blocked = {c for c in candidates if _meta_file_block_reason(c, "document")}
    assert build_blocked - doc_blocked == {"readme.md"}
    assert doc_blocked == _ALWAYS_BLOCKED_META


def test_basename_only_not_substring():
    # A path whose dir contains a blocked name must not be blocked by substring.
    assert _meta_file_block_reason("myplan.md", "task_1") is None
    assert _meta_file_block_reason("src/todo_list.py", "task_1") is None
    assert _DOC_GATED_BASENAMES == {"readme.md"}
