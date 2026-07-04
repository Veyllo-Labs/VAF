# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""DOCUMENT phase behaviour (_run_document_phase + helpers).

Pins the safety-critical guarantees the adversarial review demanded:
  - the phase writes ONLY README/docs (positive allowlist), never source;
  - create-mode without an LLM answer writes a deterministic README;
  - update-mode without an LLM answer NEVER overwrites an existing README with a stub;
  - update-mode with an answer updates the README;
  - a run that changed only docs (or nothing) is a no-op.
The LLM call is stubbed so the test is deterministic and offline.
"""
import subprocess

from vaf.tools.coder import (
    CodingAgentTool,
    _deterministic_readme,
    _doc_write_allowed,
    _existing_docs,
    _is_readme_name,
    _strip_md_fence,
    _strip_reasoning,
)


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.local", *args],
                   cwd=cwd, capture_output=True, text=True, check=True)


def _seed_repo(tmp_path, with_readme=False):
    d = str(tmp_path)
    _git(d, "init", "-q")
    (tmp_path / "app.py").write_text("print('hi')\n")
    if with_readme:
        (tmp_path / "README.md").write_text("# app\n\nOriginal hand-written docs.\n")
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "init")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
    return d, base


def _tool_with_llm(reply):
    t = CodingAgentTool()
    t.query_llm = lambda *a, **k: reply
    return t


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_doc_write_allowlist():
    assert _doc_write_allowed("README.md")
    assert _doc_write_allowed("README.rst")
    assert _doc_write_allowed("readme")            # no extension
    assert _doc_write_allowed("docs/api.md")
    assert _doc_write_allowed("docs/sub/guide.md")
    assert not _doc_write_allowed("app.py")
    assert not _doc_write_allowed("src/main.py")
    assert not _doc_write_allowed("sub/README.md")      # only a top-level README
    assert not _doc_write_allowed("readme_utils.py")    # source file, NOT a README
    assert not _doc_write_allowed("readmeGen.js")
    assert not _doc_write_allowed("app/docs/handler.py")  # not a top-level docs/ dir


def test_is_readme_name():
    assert _is_readme_name("README.md") and _is_readme_name("readme") and _is_readme_name("Readme.rst")
    assert not _is_readme_name("readme_utils.py")
    assert not _is_readme_name("readme.py")
    assert not _is_readme_name("readmeGen.js")


def test_strip_md_fence():
    assert _strip_md_fence("```markdown\n# t\nx\n```") == "# t\nx"
    assert _strip_md_fence("# plain\nno fence") == "# plain\nno fence"


def test_strip_reasoning_removes_think_block():
    # Reasoning models (DeepSeek) leak <think>...</think> into content; it must be dropped.
    assert _strip_reasoning("<think>lots of reasoning</think>\n# Real README\ntext") == "# Real README\ntext"
    assert _strip_reasoning("# no think here") == "# no think here"
    assert _strip_reasoning("<think>only reasoning, no answer") == ""  # unclosed -> empty


def test_doc_phase_strips_leaked_reasoning(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=False)
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    reply = "<think>I will write a readme now.</think>\n# app\n\nClean docs.\n"
    _tool_with_llm(reply)._run_document_phase(d, base)
    txt = (tmp_path / "README.md").read_text()
    assert txt.startswith("# app")
    assert "<think>" not in txt and "I will write" not in txt


def test_deterministic_readme_lists_only_code():
    out = _deterministic_readme("/tmp/proj", ["app.py", "README.md", "core/x.py"])
    assert "app.py" in out and "core/x.py" in out
    assert "README.md" not in out


# ── the phase ────────────────────────────────────────────────────────────────

def test_create_mode_writes_llm_readme(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=False)
    (tmp_path / "feature.py").write_text("def f(): return 1\n")  # a change this run
    note = _tool_with_llm("# app\n\nA generated readme.\n")._run_document_phase(d, base)
    assert "created" in note
    assert (tmp_path / "README.md").read_text().startswith("# app")
    assert "generated readme" in (tmp_path / "README.md").read_text()


def test_create_mode_fallback_is_deterministic(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=False)
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    note = _tool_with_llm(None)._run_document_phase(d, base)  # LLM unavailable
    assert "created" in note
    txt = (tmp_path / "README.md").read_text()
    assert "feature.py" in txt  # deterministic README lists the changed code


def test_update_mode_never_overwrites_on_empty_llm(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=True)
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    before = (tmp_path / "README.md").read_text()
    note = _tool_with_llm("")._run_document_phase(d, base)  # LLM gives nothing
    assert "kept existing" in note
    assert (tmp_path / "README.md").read_text() == before  # untouched, no stub


def test_update_mode_applies_llm_answer(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=True)
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    note = _tool_with_llm("# app\n\nUpdated docs mentioning feature.\n")._run_document_phase(d, base)
    assert "updated" in note
    assert "feature" in (tmp_path / "README.md").read_text()


def test_no_changes_is_noop(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=False)
    # nothing changed since base
    note = _tool_with_llm("# should not be written\n")._run_document_phase(d, base)
    assert note == ""
    assert not (tmp_path / "README.md").exists()


def test_only_docs_changed_is_noop(tmp_path):
    d, base = _seed_repo(tmp_path, with_readme=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# guide\n")  # only a doc changed
    note = _tool_with_llm("# X\n")._run_document_phase(d, base)
    assert note == ""


def test_phase_writes_only_readme_never_source(tmp_path):
    # Even if the LLM returns content, the phase must only ever write the README target,
    # never a source file - source content must be byte-identical after the phase.
    d, base = _seed_repo(tmp_path, with_readme=False)
    (tmp_path / "feature.py").write_text("SENTINEL = 42\n")
    src_before = (tmp_path / "feature.py").read_text()
    (tmp_path / "app.py").write_text("print('hi')\n")  # unchanged content, still tracked
    _tool_with_llm("# app\n\ndocs\n")._run_document_phase(d, base)
    assert (tmp_path / "feature.py").read_text() == src_before
    assert (tmp_path / "app.py").read_text() == "print('hi')\n"


def test_readme_prefixed_source_file_is_never_overwritten(tmp_path):
    # A source file like readme_utils.py must NOT be picked as the README nor clobbered.
    d, base = _seed_repo(tmp_path, with_readme=False)
    (tmp_path / "readme_utils.py").write_text("SENTINEL = 1\n")
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "add util")
    newbase = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    _tool_with_llm("# proj\n\ndocs\n")._run_document_phase(d, newbase)
    assert (tmp_path / "readme_utils.py").read_text() == "SENTINEL = 1\n"  # untouched
    assert (tmp_path / "README.md").exists()  # a real README was created instead


def test_symlink_readme_is_not_written_through(tmp_path):
    # README.md as a symlink to a file OUTSIDE the project must never be written through.
    d, base = _seed_repo(tmp_path, with_readme=False)
    outside = tmp_path.parent / "outside_secret.md"
    outside.write_text("OUTSIDE SECRET\n")
    (tmp_path / "README.md").symlink_to(outside)
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "link")
    newbase = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    _tool_with_llm("# pwned\n\nx\n")._run_document_phase(d, newbase)
    assert outside.read_text() == "OUTSIDE SECRET\n"  # target outside project untouched


def test_update_mode_keeps_long_readme(tmp_path):
    # A long curated README must not be replaced by a shorter regeneration (truncation loss).
    d, base = _seed_repo(tmp_path, with_readme=False)
    big = "# Big\n\n" + ("detail line\n" * 900)  # > 8000 chars
    (tmp_path / "README.md").write_text(big)
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "big readme")
    newbase = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
    (tmp_path / "feature.py").write_text("def f(): return 1\n")
    note = _tool_with_llm("# Big\n\nshort regeneration\n")._run_document_phase(d, newbase)
    assert "kept existing" in note
    assert (tmp_path / "README.md").read_text() == big  # untouched


def test_strip_reasoning_preserves_body_think_mention():
    doc = "# Title\n\nWe document closing tags like </think> in examples.\n"
    assert _strip_reasoning(doc) == doc


def test_strip_md_fence_preserves_doc_with_code_block():
    doc = "# T\n\n```python\nprint(1)\n```\n\nmore text\n"
    out = _strip_md_fence(doc)
    assert out.startswith("# T")               # title not eaten
    assert "```python\nprint(1)\n```" in out    # inner code block intact
