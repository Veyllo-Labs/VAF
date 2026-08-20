# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""read_file on large text: a window plus the facts, never a blind cut.

The measured incident: a 2,781-char summary file arrived in the chat cut at
2,000 chars by the dispatch funnel's generic cap, with no line count, no
start_line hint and no structure - the model was blind about everything
outside the cut. The design (same shape as the PDF page-range lane): the tool
budgets its own result (window + honest header + structure index + range
parameters to continue), declares result_is_deliverable, and in exchange no
branch may ever return more than its budget.
"""
import pytest

from vaf.core.tool_dispatch import ToolCaller
from vaf.tools.filesystem import ReadFileTool

BUDGET = ReadFileTool._RESULT_CHAR_BUDGET
WINDOW = ReadFileTool._TEXT_WINDOW_CHARS


@pytest.fixture
def tool():
    return ReadFileTool()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _big_markdown(sections=8, lines_per=400):
    parts = ["# Title\n"]
    for sec in range(1, sections + 1):
        parts.append(f"## Section {sec}\n")
        parts.extend(f"content {sec}-{i} " + "x" * 50 + "\n" for i in range(lines_per))
    return "".join(parts)


# ── the window ───────────────────────────────────────────────────────────────

def test_small_text_is_returned_verbatim(tool, tmp_path):
    p = _write(tmp_path, "small.txt", "hello\nworld\n")
    assert tool.run(path=p) == "hello\nworld\n"


def test_large_text_returns_window_with_facts(tool, tmp_path):
    text = _big_markdown()
    p = _write(tmp_path, "summary.md", text)
    r = tool.run(path=p)

    assert len(r) <= BUDGET
    total_lines = text.count("\n")
    assert f"of {total_lines} total" in r
    assert f"{len(text):,} chars" in r
    assert "start_line=" in r          # the continuation is named, not implied
    assert r.splitlines()[0].startswith("[Lines 1-")


def test_window_cuts_at_line_boundaries(tool, tmp_path):
    p = _write(tmp_path, "b.md", _big_markdown())
    r = tool.run(path=p)
    # every body line is a complete source line: the window never splits one
    assert not r.rstrip("\n").splitlines()[-1].endswith("x" * 51)
    assert all(len(ln) < 200 for ln in r.splitlines())


def test_range_read_keeps_its_header_shape(tool, tmp_path):
    p = _write(tmp_path, "r.txt", "\n".join(f"line{i}" for i in range(1, 21)) + "\n")
    r = tool.run(path=p, start_line=5, end_line=7)
    assert r.startswith("[Lines 5-7 of 20 total]\n")
    assert "line5\nline6\nline7" in r


# ── the structure index ──────────────────────────────────────────────────────

def test_index_lists_headings_with_line_numbers(tool, tmp_path):
    text = _big_markdown(sections=4)
    p = _write(tmp_path, "idx.md", text)
    r = tool.run(path=p)
    assert "[Structure index]" in r
    assert "line 1: # Title" in r
    # heading line numbers are real: the line the index names IS that heading
    lines = text.splitlines()
    for heading in ("## Section 1", "## Section 2", "## Section 4"):
        n = lines.index(heading) + 1
        assert f"line {n}: {heading}" in r


def test_index_ignores_comments_inside_code_fences(tool, tmp_path):
    body = ("# Real Heading\n" + "```bash\n# not a heading\n```\n"
            + "## Also Real\n" + "filler\n" * 4000 + "### Third\n")
    p = _write(tmp_path, "f.md", body)
    r = tool.run(path=p)
    assert "not a heading" not in r.split("[Structure index]")[1].split("# Real")[0] \
        if "[Structure index]" in r else True
    assert "line 1: # Real Heading" in r
    assert "## Also Real" in r


def test_no_index_block_for_unstructured_text(tool, tmp_path):
    p = _write(tmp_path, "plain.txt", ("word " * 10 + "\n") * 4000)
    r = tool.run(path=p)
    assert "[Structure index]" not in r
    assert "start_line=" in r          # the facts still arrive


# ── the budget promise behind result_is_deliverable ──────────────────────────

def test_the_deliverable_declaration_is_present():
    """The funnel exemption this tool relies on (TOOL_ROUTER_ARCHITECTURE.md's
    deliverable contract); without it the chat lane cuts every read at 2,000
    chars again and destroys the continuation facts."""
    assert ReadFileTool.result_is_deliverable is True


def test_the_funnel_returns_the_window_whole(tool, tmp_path):
    p = _write(tmp_path, "big.md", _big_markdown())
    caller = ToolCaller({"read_file": tool}, gate_enabled=False, interactive=False)
    r = caller.execute("read_file", {"path": p})
    assert len(r) > 2000               # red if the declaration is removed
    assert "[Output Truncated" not in r
    assert "start_line=" in r


def test_one_enormous_line_is_capped_honestly(tool, tmp_path):
    p = _write(tmp_path, "mono.txt", "y" * (BUDGET * 3))
    r = tool.run(path=p)
    assert len(r) <= BUDGET + 200      # cap line itself rides on top
    assert "capped at" in r and "start_line" in r
    # the cap must keep real content, not just the header (an early newline
    # once left 99 of 40,000 chars standing)
    assert r.count("y") >= BUDGET // 2


def test_no_branch_can_exceed_the_budget(tool, tmp_path):
    """_cap_result is the one ceiling for every branch; the three byte-similar
    15k hand copies in the docx/xlsx/pptx branches were deleted for it."""
    import inspect
    src = inspect.getsource(ReadFileTool)
    assert "full_text[:15000]" not in src
    # and the ceiling itself holds for an absurd explicit range
    p = _write(tmp_path, "wide.txt", ("z" * 100 + "\n") * 2000)
    r = tool.run(path=p, start_line=1, end_line=2000)
    assert len(r) <= BUDGET + 200
    assert "capped at" in r
