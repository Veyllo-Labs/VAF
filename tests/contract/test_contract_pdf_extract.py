# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.extract_pdf_markdown` (docs/EMBEDDING.md, "What is and isn't stable").

The documented promise: the exact call signature (with `first_page` and
`cancel` keyword-only), a result dict whose NINE keys are the contract
(`num_pages` as the backward-compat alias of `total_pages`), and - when the
`vaf[pdf]` extra is absent - an ImportError that names the remedy before any
file access happens. `import vaf` stays cheap: the extra is needed at call
time only.
"""
import inspect
import os
import subprocess
import sys
from pathlib import Path

import vaf


# The nine documented result keys - EXACTLY these, no more, no fewer.
DOCUMENTED_KEYS = {
    "markdown",
    "total_pages",
    "pages_read",
    "first_page",
    "truncated",
    "used_ocr",
    "method",
    "ocr_unavailable_reason",
    "num_pages",
}


def test_the_signature_is_the_documented_one():
    """EMBEDDING.md spells the signature out, keyword-only markers included:
    extract_pdf_markdown(path, max_pages=None, ocr_fallback=True, *,
    first_page=1, cancel=None). An embedder calling positionally or by
    keyword relies on every part of this."""
    sig = inspect.signature(vaf.extract_pdf_markdown)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == [
        "file_path",
        "max_pages",
        "ocr_fallback",
        "first_page",
        "cancel",
    ]
    assert params[0].default is inspect.Parameter.empty
    assert params[1].default is None
    assert params[2].default is True
    assert params[3].default == 1
    assert params[4].default is None
    kinds = {p.name: p.kind for p in params}
    for name in ("file_path", "max_pages", "ocr_fallback"):
        assert kinds[name] is inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"{name} must stay passable positionally"
        )
    for name in ("first_page", "cancel"):
        assert kinds[name] is inspect.Parameter.KEYWORD_ONLY, (
            f"{name} is documented as keyword-only"
        )


def test_the_result_dict_carries_exactly_the_documented_nine_keys(monkeypatch):
    """The result dict IS the contract: nine keys, num_pages always equal to
    total_pages (documented backward-compat alias), and truncated False when
    the caller saw the whole document.

    The extraction engine is stubbed at the module attribute (test-harness
    seam, the established repo pattern) so this pin needs no PDF library:
    the internal convention is a (markdown, total_pages, pages_read) tuple.
    ocr_fallback=False keeps the OCR lane (and its config reads) out;
    cancel=lambda: False keeps the run deterministic.
    """
    import vaf.core.pdf_extract as pdf_extract  # module-attribute seam for the engine stub; facade re-export source

    fake_markdown = (
        "--- Page 1 ---\n\n# Title\n\nBody text long enough to clear the sparse-content gate.\n\n"
        "--- Page 2 ---\n\nMore body text on the second page.\n\n"
        "--- Page 3 ---\n\nFinal page body text."
    )

    def fake_pdfplumber(file_path, max_pages, first_page=1, cancel=None):
        return fake_markdown, 3, 3

    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber", fake_pdfplumber)

    result = vaf.extract_pdf_markdown(
        "/synthetic/no-such.pdf", ocr_fallback=False, cancel=lambda: False
    )

    assert set(result) == DOCUMENTED_KEYS
    # Types and values of every documented field.
    assert result["markdown"] == fake_markdown
    assert isinstance(result["markdown"], str)
    for key in ("total_pages", "num_pages", "pages_read", "first_page"):
        assert isinstance(result[key], int) and not isinstance(result[key], bool), (
            f"{key} must be a plain int"
        )
    assert isinstance(result["method"], str)
    assert isinstance(result["ocr_unavailable_reason"], str)
    # num_pages is the documented backward-compat alias of total_pages.
    assert result["total_pages"] == 3
    assert result["num_pages"] == result["total_pages"]
    assert result["pages_read"] == 3
    assert result["first_page"] == 1
    # Full read from page 1: the caller saw the whole document.
    assert result["truncated"] is False
    assert result["used_ocr"] is False
    assert result["ocr_unavailable_reason"] == ""
    # The primary engine ran, so the method names it.
    assert result["method"] == "pdfplumber"


# Meta-path blocker: raising ModuleNotFoundError from find_spec is the only
# way to shadow INSTALLED packages, so this pin holds on fully-provisioned
# machines too. Self-contained on purpose - a vendored copy has no repo
# helpers to borrow.
_BLOCKER = """\
import sys

_FORBIDDEN = frozenset(("pdfplumber", "PyPDF2"))


class _PdfExtraBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _FORBIDDEN:
            raise ModuleNotFoundError(
                "No module named %r (blocked: simulating a base install)" % fullname,
                name=fullname,
            )
        return None


sys.meta_path.insert(0, _PdfExtraBlocker())

_preloaded = sorted(m.split(".")[0] for m in sys.modules if m.split(".")[0] in _FORBIDDEN)
if _preloaded:
    sys.stderr.write("BLOCKER_INEFFECTIVE: preloaded: %s\\n" % ", ".join(_preloaded))
    sys.exit(97)
"""

_PROBE = """
import vaf

try:
    vaf.extract_pdf_markdown("/nonexistent.pdf", ocr_fallback=False)
except ImportError as exc:
    message = str(exc)
    if "vaf[pdf]" not in message:
        sys.stderr.write("REMEDY_MISSING_FROM_MESSAGE: %r\\n" % message)
        sys.exit(3)
    print("IMPORTERROR_OK")
except FileNotFoundError:
    sys.stderr.write("FILE_WAS_ACCESSED_BEFORE_THE_IMPORT_CHECK\\n")
    sys.exit(4)
else:
    sys.stderr.write("NO_ERROR_RAISED\\n")
    sys.exit(5)
"""


def test_missing_pdf_extra_raises_importerror_naming_the_remedy(tmp_path):
    """Without the extra, the documented behavior is an ImportError whose
    message names the remedy ('vaf[pdf]') - and it fires BEFORE any file
    access, which the probe proves by passing a path that does not exist: a
    FileNotFoundError would mean the file was opened first. Pinned by
    substring, not full prose."""
    env = dict(os.environ)
    # Make the vaf under test importable from tmp_path: the directory holding
    # the imported package works for both the in-repo tree and site-packages.
    pkg_parent = str(Path(vaf.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _BLOCKER + _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),  # never a directory with a vaf/ source tree in it
        env=env,
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "IMPORTERROR_OK" in proc.stdout


def test_the_facade_name_is_the_engine_function_itself():
    """EMBEDDING.md names extract_pdf_markdown as one of the deliberate
    vaf.core exceptions re-exported on the facade: identity, not a wrapper,
    so the two can never drift."""
    import vaf.core.pdf_extract as pdf_extract  # re-export source documented in EMBEDDING.md

    assert vaf.extract_pdf_markdown is pdf_extract.extract_pdf_markdown
