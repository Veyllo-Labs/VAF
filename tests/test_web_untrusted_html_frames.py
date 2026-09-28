# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Content the app did not write must never run as the app.

The origin guard (tests/test_foreign_origin_guard.py) refuses pages from OTHER origins. It
cannot help when foreign markup runs under the app's OWN origin - and the web UI did exactly
that in several places, measured in Chromium:

* the HTML viewer framed any workspace .html with ``allow-scripts allow-same-origin``: the
  file read the parent's ``vaf_token`` from localStorage (Chrome itself warns that this
  combination "can escape its sandboxing");
* the document editor's frame had the same pair, so an ``onerror`` attribute or a nested
  ``srcdoc`` frame in an opened file ran as the viewer;
* the research report print used an UNSANDBOXED frame, and the report paper put model output
  built from web pages into the app's document through ``dangerouslySetInnerHTML``;
* the editor's text extraction and PDF export parsed the file with ``innerHTML`` in the app's
  document, and the desktop's offscreen PDF page re-ran the file's scripts with JavaScript on.

These are source checks, because the failure is a pattern in the source: a new frame, a new
sandbox string, a new raw-HTML sink. Each rule names the one place that may differ and why.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB_ROOTS = [REPO / "web" / d for d in ("app", "components", "lib", "hooks")]


def _web_sources():
    for root in WEB_ROOTS:
        for path in sorted(root.rglob("*")):
            if path.suffix in (".ts", ".tsx") and "node_modules" not in path.parts:
                yield path, path.read_text(encoding="utf-8")


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


# --------------------------------------------------------------------------- sandbox values

_SANDBOX_LITERAL = re.compile(
    r"""sandbox=\{?\s*["']([^"']*)["']"""                        # sandbox="..." / sandbox={'...'}
    r"""|setAttribute\(\s*["']sandbox["']\s*,\s*["']([^"']*)["']"""  # setAttribute('sandbox', '...')
    r"""|const\s+\w*SANDBOX\w*\s*=\s*["']([^"']*)["']"""          # const EDITOR_FRAME_SANDBOX = '...'
)


def _sandbox_values():
    for path, src in _web_sources():
        for m in _SANDBOX_LITERAL.finditer(src):
            value = next(g for g in m.groups() if g is not None)
            yield _rel(path), src.count("\n", 0, m.start()) + 1, value


def test_no_frame_gets_scripts_and_the_apps_origin_together():
    """With both flags a framed page is the app: it reads the parent's storage and can remove
    its own sandbox. Scripts OR the app's origin, never both."""
    found = list(_sandbox_values())
    assert len(found) >= 5, f"the sandbox scan found too little to trust: {found}"
    both = [(f, line, v) for f, line, v in found
            if "allow-scripts" in v.split() and "allow-same-origin" in v.split()]
    assert not both, f"frames with allow-scripts AND allow-same-origin: {both}"


def test_a_sandbox_value_is_a_literal_this_scan_can_read():
    """sandbox={SOMETHING} must name a constant this file defines as a literal, or the rule
    above is blind to it."""
    unreadable = []
    for path, src in _web_sources():
        for m in re.finditer(r"sandbox=\{\s*([A-Za-z_]\w*)\s*\}", src):
            name = m.group(1)
            if not re.search(rf"""const\s+{name}\s*=\s*["'][^"']*["']""", src):
                unreadable.append((_rel(path), name))
    assert not unreadable, f"sandbox values the guard cannot read: {unreadable}"


# --------------------------------------------------------------------------- frames without one

# The frames that may stay unsandboxed, and why. Keyed by file and the src expression.
UNSANDBOXED_FRAMES = {
    # The interactive browser's viewer: KasmVNC's own page, served by this backend under
    # /api/browser-vnc/ with a ticket, needs its scripts and posts to the parent. It shows a
    # remote browser as pixels; no remote markup reaches this origin (BROWSER_AGENT.md).
    ("web/components/SubAgentWindow.tsx", "interactive.streamUrl"),
    ("web/components/SubAgentWindow.tsx", "agentWatchStream.streamUrl"),
}


def _jsx_iframe_tags(src: str):
    for m in re.finditer(r"<iframe\b", src):
        end = src.find(">", m.end())
        # a JSX attribute value can hold '>' inside braces; extend to the tag's real end
        depth, i = 0, m.end()
        while i < len(src):
            ch = src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                end = i
                break
            i += 1
        yield src.count("\n", 0, m.start()) + 1, src[m.start():end + 1]


def test_every_jsx_frame_is_sandboxed_or_named():
    missing = []
    for path, src in _web_sources():
        for line, tag in _jsx_iframe_tags(src):
            if "sandbox=" in tag:
                continue
            src_expr = re.search(r"src=\{([^}]*)\}", tag)
            key = (_rel(path), src_expr.group(1).strip() if src_expr else "")
            if key not in UNSANDBOXED_FRAMES:
                missing.append((key[0], line, key[1]))
    assert not missing, f"<iframe> without a sandbox and not on the named list: {missing}"


def test_every_scripted_frame_gets_its_sandbox_before_insertion():
    """createElement('iframe') frames: the sandbox must be set before the frame is inserted -
    a sandbox added afterwards does not apply to its first document (measured: an unsandboxed
    about:blank ran the file's handlers although the attribute was present)."""
    bad = []
    for path, src in _web_sources():
        for m in re.finditer(r"const\s+(\w+)\s*=\s*document\.createElement\(\s*['\"]iframe['\"]\s*\)", src):
            var = m.group(1)
            rest = src[m.end():]
            sandbox_at = re.search(rf"{var}\.setAttribute\(\s*['\"]sandbox['\"]", rest)
            insert_at = re.search(rf"appendChild\(\s*{var}\s*\)", rest)
            line = src.count("\n", 0, m.start()) + 1
            if not sandbox_at or (insert_at and insert_at.start() < sandbox_at.start()):
                bad.append((_rel(path), line, var))
    assert not bad, f"createElement('iframe') without a sandbox set before insertion: {bad}"


# --------------------------------------------------------------------------- raw HTML sinks

# dangerouslySetInnerHTML sites, per file, and why each is safe.
RAW_HTML_SITES = {
    # The static theme/locale bootstrap: a string this file writes, no input.
    "web/app/layout.tsx": 1,
    # The research/document paper: sections pass sanitizeUntrustedHtml (web/lib/sanitize.ts)
    # before anything else touches them (pinned below).
    "web/components/SubAgentWindow.tsx": 2,
}


def test_raw_html_is_only_set_where_named():
    found = {}
    for path, src in _web_sources():
        n = src.count("dangerouslySetInnerHTML")
        if n and _rel(path) != "web/lib/sanitize.ts":
            found[_rel(path)] = n
    assert found == RAW_HTML_SITES


def test_research_sections_are_cleaned_before_the_app_renders_or_prints_them():
    src = (REPO / "web/components/SubAgentWindow.tsx").read_text(encoding="utf-8")
    assert "rawSectionsHtml.map(sanitizeUntrustedHtml)" in src, "the report paper renders raw sections"
    assert "decorate(sectionsHtml.map(sanitizeUntrustedHtml).join(''))" in src, "the print renders raw sections"
    assert "<script>" not in src.split("function printResearchReport", 1)[1].split("function A4ResearchPaper", 1)[0], (
        "the print document carries its own script again; its frame has no allow-scripts")


def test_the_untrusted_fragment_config_keeps_markup_from_drawing_over_the_app():
    src = (REPO / "web/lib/sanitize.ts").read_text(encoding="utf-8")
    for attr in ("'style'", "'class'", "'id'"):
        assert attr in src.split("FORBID_ATTR", 1)[1].split("]", 1)[0], attr
    assert "if (!DOMPurify.isSupported) return '';" in src, "must fail closed without a DOM"


def test_editor_content_is_never_parsed_into_the_apps_document():
    page = (REPO / "web/app/page.tsx").read_text(encoding="utf-8")
    assert "innerHTML = documentEditorState.content" not in page
    assert "new DOMParser().parseFromString(documentEditorState.content" in page
    editor = (REPO / "web/components/DocumentEditor.tsx").read_text(encoding="utf-8")
    assert "contentDiv.innerHTML = sanitizeDocumentCopy(body.innerHTML)" in editor
    assert "contentDiv.innerHTML = body.innerHTML" not in editor
    # measured: a position:fixed block in the file drew over the app from the offscreen copy
    assert "wrapper.style.contain = 'layout paint'" in editor


def test_a_document_copy_keeps_its_formatting_but_not_the_apps_hooks():
    """The PDF export needs the editor's inline style (alignment, highlights) and images, so it
    cannot use the fragment policy; id and class would reach the app's CSS and globals."""
    src = (REPO / "web/lib/sanitize.ts").read_text(encoding="utf-8")
    copy = src.split("const DOCUMENT_COPY", 1)[1].split("};", 1)[0]
    assert "FORBID_ATTR: ['class', 'id']" in copy
    assert "'style'" in copy.split("FORBID_TAGS", 1)[1].split("]", 1)[0]
    assert "'style'" not in copy.split("FORBID_ATTR", 1)[1].split("]", 1)[0], "inline style carries the formatting"
    assert "export function sanitizeDocumentCopy" in src and src.count("if (!DOMPurify.isSupported) return '';") == 2


def test_the_desktop_pdf_page_runs_no_script():
    """render_pdf loads the editor's HTML as a FRESH document with a file:/// base: what sat
    inert in the sandboxed editor frame would run there, with file access (measured in
    QtWebEngine: an onerror attribute ran with JavaScript on, not with it off)."""
    src = (REPO / "vaf/core/desktop_window.py").read_text(encoding="utf-8")
    render = src.split("def render(self, html, mode, name):", 1)[1].split("_pdf_renderer = _PdfRenderer()", 1)[0]
    js_off = render.find("WebAttribute.JavascriptEnabled, False")
    assert js_off != -1, "the offscreen PDF page runs JavaScript"
    assert js_off < render.find("page.setHtml("), "JavaScript must be off before the HTML loads"


@pytest.mark.parametrize("path", ["web/components/HtmlViewer.tsx"])
def test_the_html_viewer_runs_scripts_under_an_opaque_origin(path):
    """Interactive reports keep working (allow-scripts); the app's origin is what goes."""
    src = (REPO / path).read_text(encoding="utf-8")
    assert 'sandbox="allow-scripts allow-forms"' in src
