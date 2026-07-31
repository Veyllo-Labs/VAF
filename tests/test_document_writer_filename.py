# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where `document_writer` is allowed to write, and why it needed two answers.

The census that bounded the file-tool lane turned this up as its sharpest single find, and
it is the only one of them that WRITES. `filename` was cast to `str` and joined onto the
documents directory - and in pathlib an absolute path swallows the base: `base / "/etc/passwd"`
is `/etc/passwd`, and `../` walks out. The suffix allowlist (.txt/.md/.docx) limits WHAT gets
written, never WHERE.

Not a theoretical shape either. A live incident already put a path-shaped value in this
parameter: a variable extractor turned a video URL into `filename="//www.youtube.com"`
(recorded in tests/test_workflow_tool_overlay.py).

THIS TOOL IS THE FIRST CONSUMER OF `file_access`, and that is the point of the fix rather
than a detail of it. The gap is closed by DECLARING the mode - `BaseTool` installs the
per-user boundary around `run()` - instead of writing another copy of the four-step
installation that eleven tools had hand-rolled and five had forgotten. If the primitive had
not existed, this file would have been the twelfth hand-build.

TWO GUARDS, because they answer different questions and neither covers the other:

  the NAME rule    keeps the parameter to its documented meaning ("Filename with extension").
                   Without it an absolute name escapes the base directory entirely.
  the DECLARATION  is what stops a perfectly well-formed name from being written into another
                   tenant's tree. The name rule alone would not: "notes.md" is a bare name.
"""
from pathlib import Path

import pytest

from vaf.tools.document_writer import DocumentWriterTool


# ── the name rule ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "/etc/passwd",                    # absolute: pathlib discards the base entirely
    "/tmp/anywhere.md",
    "../../escape.md",                # traversal
    "sub/dir/notes.md",               # even a harmless-looking relative path is not a NAME
    "//www.youtube.com",              # the shape from the live incident
])
def test_a_path_shaped_filename_is_refused(bad):
    out = DocumentWriterTool().run(filename=bad, content="x", document_type="doc")
    assert out.startswith("Tool Error:"), f"{bad!r} was accepted: {out[:120]!r}"
    assert "write_file" in out, "the refusal should point at the tool that does take a path"


def test_an_ordinary_filename_still_works(tmp_path, monkeypatch):
    """The control. Without it every assertion above would also hold for a tool that refuses
    everything."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = DocumentWriterTool().run(filename="report.md", content="hello", document_type="doc")
    assert not out.startswith("Tool Error:"), out[:160]


def test_the_suffix_allowlist_does_not_stand_in_for_the_name_rule():
    """`.md` is allowed, so a path with an allowed suffix passes the suffix check and would
    have been written wherever it pointed. Naming this separately because "there is already a
    check" was the reason this gap survived the earlier rounds."""
    out = DocumentWriterTool().run(filename="/tmp/allowed_suffix.md", content="x",
                                   document_type="doc")
    assert out.startswith("Tool Error:")


# ── the declaration, which the name rule cannot replace ──────────────────────

def test_the_per_user_boundary_is_declared_not_hand_built():
    """The consumer half. A bare name is not enough on its own - "notes.md" is well-formed and
    would still land in whatever directory the caller's output dir resolves to, so the tenant
    boundary has to come from somewhere. It comes from the declaration, which also means this
    tool did not become the twelfth hand-rolled copy."""
    assert DocumentWriterTool.file_access == "write"
    assert {"user_scope_id", "user_role"} <= set(DocumentWriterTool.identity_kwargs), (
        "declaring a mode without the identity that resolves it is refused at class-definition "
        "time; if this ever regresses the tool would run unconfined while looking confined"
    )
    assert getattr(DocumentWriterTool.run, "_vaf_jailed", False), (
        "run() is not wrapped - the declaration exists but nothing acts on it"
    )
    import inspect
    assert "user_jail(" not in inspect.getsource(DocumentWriterTool.run), (
        "the boundary is hand-installed again; the point of the declaration is that this tool "
        "is a consumer of the primitive rather than another copy of it"
    )
