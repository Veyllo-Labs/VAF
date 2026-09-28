# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One HTML sanitizer, two policies: mail bodies and research sections.

Research sections are model output written from web pages, so a page the model read can
steer them, and the web UI renders them into the app's own document. They reached it with
no filter at all (``_sanitize_section_output`` only strips leaked reasoning), and the saved
report put the topic and every source URL into markup unescaped. The mail layer had the one
nh3 call in the tree; it is now the shared ``vaf.core.html_sanitize.sanitize_html``, and a
second direct call is refused, so the next producer of HTML uses the same boundary.
"""
import re
from pathlib import Path

from vaf.core.html_sanitize import sanitize_html

REPO = Path(__file__).resolve().parents[1]

HOSTILE = (
    '<h2 class="fixed inset-0 z-50" onclick="steal()">Title</h2>'
    '<p id="vaf_token" style="position:fixed">Text [1] with <a href="https://source.example/a">a source</a>'
    ' and <a href="javascript:alert(1)">a trap</a></p>'
    '<img src=x onerror="fetch(\'/api/secrets\')">'
    '<iframe srcdoc="<script>parent.x=1</script>"></iframe>'
    '<script>document.title="ran"</script><style>body{display:none}</style>'
    '<form action="/api/secrets"><input name="a"></form>'
    '<ul><li>Point</li></ul><table><tr><th colspan="2">Head</th></tr><tr><td>Cell</td></tr></table>'
)


def test_the_document_policy_keeps_structure_and_drops_everything_that_runs_or_draws():
    clean = sanitize_html(HOSTILE)
    for kept in ("<h2>Title</h2>", "<ul><li>Point</li></ul>", "<th colspan=\"2\">Head</th>", "<td>Cell</td>",
                 'href="https://source.example/a"'):
        assert kept in clean, kept
    for gone in ("onclick", "onerror", "javascript:", "<iframe", "<script", "document.title", "<style",
                 "display:none", "<form", "<input", "<img", "class=", "id=", "style="):
        assert gone not in clean, gone
    assert 'rel="noopener noreferrer nofollow"' in clean


def test_a_research_section_is_cleaned_where_it_enters_the_section_list():
    from vaf.tools.research_agent import _clean_section_html
    clean = _clean_section_html(HOSTILE)
    assert "onerror" not in clean and "<script" not in clean and "class=" not in clean
    assert "<h2>Title</h2>" in clean


def test_every_section_the_research_agent_keeps_passes_the_sanitizer():
    """The live paper, the saved report and the Markdown export all read this list - including
    sections resumed from a checkpoint file and the fallbacks that carry the planned title."""
    src = (REPO / "vaf/tools/research_agent.py").read_text(encoding="utf-8")
    appends = list(re.finditer(r"rendered_sections\.append\(([^\n]*)\)", src))
    assert len(appends) >= 4, [a.group(1) for a in appends]
    raw = []
    for a in appends:
        arg = a.group(1)
        if arg.startswith("_clean_section_html("):
            continue
        # A bare name counts only when its LAST assignment before the append is the sanitizer.
        assigned = list(re.finditer(rf"\b{re.escape(arg)}\s*=\s*([^\n]*)", src[:a.start()]))
        if not (assigned and assigned[-1].group(1).startswith("_clean_section_html(")):
            raw.append(arg)
    assert not raw, f"sections kept without the sanitizer: {raw}"


def test_the_checkpoint_holds_the_cleaned_section():
    """The generated section is cleaned ONCE and that value is checkpointed and counted - the
    checkpoint file used to receive the raw model output."""
    src = (REPO / "vaf/tools/research_agent.py").read_text(encoding="utf-8")
    clean_at = src.index("section_html = _clean_section_html(section_html)")
    write_at = src.index("section_checkpoint.write_text(section_html", clean_at)
    assert src.index("rendered_sections.append(section_html)", clean_at) < write_at
    assert "word_count = _visible_word_count(section_html)" in src[clean_at:write_at]


def test_the_saved_report_escapes_the_topic_and_the_source_urls():
    from vaf.tools.research_agent import ResearchAgentTool
    report = ResearchAgentTool._assemble_html(
        None, 'Q"><script>alert(1)</script>', ["<h2>S</h2><p>t</p>"],
        ['https://x.example/"><img src=x onerror=alert(1)>'], "en")
    assert "<script>alert(1)</script>" not in report
    assert "<img src=x onerror" not in report
    assert "&quot;&gt;&lt;script&gt;" in report
    assert 'href="https://x.example/&quot;&gt;&lt;img' in report


def test_only_web_addresses_become_source_links():
    """The web UI renders each live source as a link's href; React 18 only warns about a
    javascript: href, it still renders it."""
    src = (REPO / "vaf/tools/research_agent.py").read_text(encoding="utf-8")
    block = src.split("def _rs_add_sources(results) -> None:", 1)[1].split("except Exception:", 1)[0]
    assert 'urlparse(url).scheme.lower() not in ("http", "https")' in block


def test_there_is_one_nh3_call_in_the_tree():
    """A second direct call would be a second boundary with its own gaps - the mail policy and
    the document policy are parameters of the same function."""
    callers = []
    for path in (REPO / "vaf").rglob("*.py"):
        if "nh3.clean(" in path.read_text(encoding="utf-8", errors="replace"):
            callers.append(path.relative_to(REPO).as_posix())
    assert callers == ["vaf/core/html_sanitize.py"]
    mail = (REPO / "vaf/mail/service.py").read_text(encoding="utf-8")
    assert "from vaf.core.html_sanitize import sanitize_html" in mail


def test_the_saved_report_links_only_web_addresses_and_keeps_the_numbering():
    """Escaping keeps a URL inside its href; it does not stop a javascript: target. A source
    that is not a web address stays in the list as text, so the [n] citations still count right."""
    from vaf.tools.research_agent import ResearchAgentTool
    report = ResearchAgentTool._assemble_html(
        None, "T", ["<h2>S</h2><p>see [2]</p>"],
        ["https://a.example/", "javascript:alert(document.cookie)", "https://c.example/"], "en")
    assert 'href="javascript:' not in report
    assert "<li>javascript:alert(document.cookie)</li>" in report
    items = re.findall(r"<li>.*?</li>", report)
    assert len(items) == 3 and 'href="https://c.example/"' in items[2]
