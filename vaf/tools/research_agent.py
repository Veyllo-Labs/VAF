"""
VAF Research Agent - Topic-by-topic web research with bounded context.

This tool is designed to avoid "exceed_context_size_error" by:
- Splitting a research task into sections (topics)
- Running web_search per section
- Calling the model per section with only that section's context
- Assembling a final HTML report
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import requests

from vaf.cli.ui import UI, AnimatedHeader
from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.tools.base import BaseTool
from vaf.tools.search import WebSearchTool


@dataclass(frozen=True)
class SectionSpec:
    title: str
    query_suffix: str


def _extract_urls(web_search_output: str) -> List[str]:
    # Matches lines like: "- Source: https://..."
    urls = re.findall(r"(?im)^\s*-\s*Source:\s*(https?://\S+)\s*$", web_search_output or "")
    # De-dupe while preserving order
    out: List[str] = []
    seen = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _truncate(s: str, max_chars: int) -> str:
    s = s or ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n\n[...truncated...]"

def _visible_text_len(html_fragment: str) -> int:
    """
    Rough heuristic for "how much content" is in an HTML fragment:
    strip tags + collapse whitespace.
    """
    txt = re.sub(r"<[^>]+>", " ", html_fragment or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return len(txt)

def _detect_language(text: str) -> str:
    """
    Very small heuristic: return 'de' for obviously German input, else 'en'.
    """
    t = (text or "").lower()
    if any(w in t for w in ["über", "künst", "dass", "und", "für", "recherche", "analyse", "was ist", "wie "]):
        return "de"
    # German umlauts/ß
    if any(ch in t for ch in ["ä", "ö", "ü", "ß"]):
        return "de"
    return "en"

def _strip_answer_artifacts(s: str) -> str:
    # Remove standalone "Answer" lines which some models prepend.
    s = re.sub(r"(?im)^\s*answer\s*$", "", s or "")
    return s.strip()

def _strip_untrusted_links(html: str, allowed: Sequence[str]) -> str:
    """
    Remove/harden links not in allowed sources (prevents example.com hallucinations).
    Keeps link text but removes href for untrusted URLs.
    """
    allowed_set = set(allowed or [])

    def repl(m: re.Match) -> str:
        href = m.group(1)
        label = m.group(2)
        if href in allowed_set:
            return m.group(0)
        # Drop the link, keep label
        return label

    # <a href="URL">label</a>
    html = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', repl, html, flags=re.IGNORECASE | re.DOTALL)
    return html


class ResearchAgentTool(BaseTool):
    """
    Sub-agent style tool that produces a research report (HTML by default)
    without using huge single-shot prompts.
    """

    name = "research_agent"
    description = (
        "Topic-by-topic web research that avoids context overflow. "
        "Produces an HTML report by running web_search per section and summarizing each section separately."
    )

    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Main topic to research"},
            "format": {"type": "string", "description": "Output format: html | markdown | html_fragment", "default": "html"},
            "max_results": {"type": "integer", "description": "web_search results per section (1-10)", "default": 5},
            "deep": {"type": "boolean", "description": "Enable deep previews in web_search (slower)", "default": False},
            "language": {"type": "string", "description": "Force output language: de | en (optional)"},
            "min_chars_empty": {"type": "integer", "description": "If section text < this, treat as empty and retry", "default": 150},
            "min_chars_ok": {"type": "integer", "description": "If section text < this, treat as too short and expand once", "default": 500},
            "sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional explicit section titles. If omitted, uses a sensible default set of 10 sections.",
            },
        },
        "required": ["topic"],
    }

    def run(self, **kwargs) -> str:
        topic = (kwargs.get("topic") or "").strip()
        out_format = (kwargs.get("format") or "html").strip().lower()
        max_results = int(kwargs.get("max_results", 5) or 5)
        deep = bool(kwargs.get("deep", False))
        forced_lang = (kwargs.get("language") or "").strip().lower()
        min_chars_empty = int(kwargs.get("min_chars_empty", 150) or 150)
        min_chars_ok = int(kwargs.get("min_chars_ok", 500) or 500)
        section_titles: Optional[Sequence[str]] = kwargs.get("sections")

        if not topic:
            return "Error: No topic provided."
        if out_format not in ("html", "markdown", "html_fragment"):
            return "Error: format must be 'html', 'markdown', or 'html_fragment'."

        max_results = max(1, min(max_results, 10))
        lang = forced_lang if forced_lang in ("de", "en") else _detect_language(topic)
        min_chars_empty = max(50, min(min_chars_empty, 2000))
        min_chars_ok = max(min_chars_empty + 1, min(min_chars_ok, 5000))

        # Default 10-section plan
        defaults: List[SectionSpec] = [
            SectionSpec("Overview", ""),
            SectionSpec("Definition & Core Concepts", "definition core concepts"),
            SectionSpec("History & Milestones", "history milestones timeline"),
            SectionSpec("Methods & Techniques", "methods techniques approaches"),
            SectionSpec("Tools & Ecosystem", "tools frameworks libraries"),
            SectionSpec("Real-World Use Cases", "use cases examples applications"),
            SectionSpec("Pros & Cons", "advantages disadvantages pros cons"),
            SectionSpec("Risks, Pitfalls & Limitations", "risks limitations pitfalls common mistakes"),
            SectionSpec("Ethics, Safety & Governance", "ethics safety regulation governance"),
            SectionSpec("Latest Developments (2024/2025)", "2024 2025 latest updates news research"),
        ]

        if section_titles:
            # Build a user-defined section list (query suffix = title)
            specs = [SectionSpec(str(t).strip(), str(t).strip()) for t in section_titles if str(t).strip()]
            if not specs:
                specs = defaults
        else:
            specs = defaults

        # Lightweight "sub-agent" banner (similar feel to Librarian/Coder)
        try:
            UI.console.print(AnimatedHeader("Collaboration Mode Active", "Main Agt", "Researcher"))
        except Exception:
            pass
        UI.event("Research", f"Starting topic-by-topic research: {topic} (lang={lang})", style="dim")

        web = WebSearchTool()
        rendered_sections: List[str] = []
        all_sources: List[str] = []

        for idx, spec in enumerate(specs, 1):
            section_query = topic if not spec.query_suffix else f"{topic} {spec.query_suffix}"
            UI.event("Research", f"Section {idx}/{len(specs)}: {spec.title}", style="info")

            # Do NOT auto-open per-section; open sources once at the end if enabled.
            results = web.run(query=section_query, max_results=max_results, deep=deep, open_in_browser=False)
            sources = _extract_urls(results)
            for u in sources:
                if u not in all_sources:
                    all_sources.append(u)

            section_html = self._summarize_section_html(
                topic=topic,
                title=spec.title,
                web_results=_truncate(results, 4500),
                sources=sources,
                lang=lang,
            )

            # Quality check + retry/expand based on content length.
            text_len = _visible_text_len(section_html)
            if text_len < min_chars_empty:
                UI.event("Research", f"↻ Section empty (<{min_chars_empty}) — retrying with deep search", style="warning")
                retry_results = web.run(query=section_query, max_results=min(10, max_results + 2), deep=True, open_in_browser=False)
                retry_sources = _extract_urls(retry_results)
                for u in retry_sources:
                    if u not in all_sources:
                        all_sources.append(u)
                section_html = self._summarize_section_html(
                    topic=topic,
                    title=spec.title,
                    web_results=_truncate(retry_results, 4500),
                    sources=retry_sources,
                    lang=lang,
                )
                text_len = _visible_text_len(section_html)

            if min_chars_empty <= text_len < min_chars_ok:
                UI.event("Research", f"↻ Section short (<{min_chars_ok}) — expanding once", style="dim")
                section_html = self._summarize_section_html(
                    topic=topic,
                    title=spec.title,
                    web_results=_truncate(results, 3000),
                    sources=sources,
                    lang=lang,
                )
            rendered_sections.append(section_html)

        if out_format == "html_fragment":
            # Return only fragments (useful for patching missing sections)
            return "\n\n".join(rendered_sections).strip()

        if out_format == "markdown":
            md = self._assemble_markdown(topic, rendered_sections, all_sources)
            return md

        html = self._assemble_html(topic, rendered_sections, all_sources, lang=lang)

        # Optional UX: open sources in browser once (tabs) for transparency
        try:
            import os
            noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")
            if bool(Config.get("ux_auto_open_links", False)) and not noninteractive and all_sources:
                max_tabs = int(Config.get("ux_auto_open_max_tabs", 8) or 8)
                max_tabs = max(1, min(max_tabs, 20))
                for url in all_sources[:max_tabs]:
                    ok = Platform.open_url(url)
                    if not ok:
                        UI.event("Research", f"⚠️ Could not open: {url[:60]}...", style="warning")
                    # Small delay between opens
                    time.sleep(0.3)
        except Exception:
            pass
        return html

    def _summarize_section_html(self, topic: str, title: str, web_results: str, sources: Sequence[str], lang: str) -> str:
        """
        Call the model for ONE section only (bounded input), return an HTML fragment.
        """
        model_name = Config.get("model", "") or ""
        lang_instruction = "Write in German." if lang == "de" else "Write in English."
        prompt = (
            "Write ONE section of an HTML research report.\n"
            f"Main topic: {topic}\n"
            f"Section title: {title}\n\n"
            f"{lang_instruction}\n"
            "Use ONLY the provided web search results as evidence.\n"
            "Do NOT invent sources. Do NOT use example.com or placeholder links.\n"
            "Return ONLY an HTML fragment (no <html>, no <head>, no <body>).\n"
            "Structure:\n"
            "- <h2>Section title</h2>\n"
            "- 1-3 short paragraphs\n"
            "- <ul> with 3-6 key bullets\n"
            "- If uncertain, say so briefly.\n\n"
            "Web search results:\n"
            f"{web_results}\n\n"
            "Cite 2-4 of these sources inline where relevant (as plain URLs):\n"
            + "\n".join(sources[:6])
        )

        def call(max_tokens: int, temperature: float) -> str:
            res = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": f"You are a concise research assistant. {lang_instruction}"},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=90,
            )
            if res.status_code != 200:
                err = (res.text or "")[:250]
                return f"<h2>{title}</h2><p><strong>Error:</strong> Server {res.status_code}: {err}</p>"
            msg = res.json()["choices"][0]["message"]
            return (msg.get("content") or "").strip()

        try:
            content = call(max_tokens=900, temperature=0.2)
            content = _strip_answer_artifacts(content)
            content = _strip_untrusted_links(content, sources)
            if content:
                return content

            # Retry once with slightly different settings and a shorter web_results payload.
            retry_prompt = prompt.replace(web_results, _truncate(web_results, 2000))
            res = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": f"You are a concise research assistant. {lang_instruction}"},
                        {"role": "user", "content": retry_prompt},
                    ],
                    "max_tokens": 700,
                    "temperature": 0.3,
                },
                timeout=90,
            )
            if res.status_code == 200:
                msg = res.json()["choices"][0]["message"]
                content = _strip_answer_artifacts((msg.get("content") or "").strip())
                content = _strip_untrusted_links(content, sources)
                if content:
                    return content

            # Final deterministic fallback: never return an empty section.
            if lang == "de":
                return (
                    f"<h2>{title}</h2>"
                    "<p><em>Hinweis:</em> Für diesen Abschnitt konnte kein sauberer Abschnitt generiert werden; "
                    "hier ist eine kurze, evidenzbasierte Zusammenfassung aus den Suchergebnissen.</p>"
                    "<ul><li>Siehe die Quellenliste am Ende des Reports für Details.</li></ul>"
                )
            return (
                f"<h2>{title}</h2>"
                "<p><em>Note:</em> Could not generate a clean section; here is a short evidence-based placeholder.</p>"
                "<ul><li>See the Sources section at the end of the report for details.</li></ul>"
            )
        except Exception as e:
            return f"<h2>{title}</h2><p><strong>Error:</strong> {type(e).__name__}: {e}</p>"

    def _assemble_html(self, topic: str, sections: Sequence[str], sources: Sequence[str], lang: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        source_items = "\n".join(f'<li><a href="{u}">{u}</a></li>' for u in sources[:30])
        sections_html = "\n\n".join(sections)
        # Remove any standalone "Answer" artifacts that slipped through.
        sections_html = _strip_answer_artifacts(sections_html)
        return (
            "<!doctype html>\n"
            f"<html lang=\"{lang}\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\" />\n"
            f"  <title>Research Report: {topic}</title>\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "  <style>\n"
            "    body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;line-height:1.5}\n"
            "    h1{margin:0 0 8px 0}\n"
            "    .meta{color:#666;margin:0 0 24px 0}\n"
            "    h2{margin-top:28px;border-top:1px solid #eee;padding-top:18px}\n"
            "    ul{padding-left:20px}\n"
            "    code{background:#f6f6f6;padding:2px 6px;border-radius:6px}\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            f"  <h1>Research Report: {topic}</h1>\n"
            f"  <p class=\"meta\">Generated: {now}</p>\n"
            f"{sections_html}\n"
            "  <h2>Sources</h2>\n"
            "  <ul>\n"
            f"{source_items}\n"
            "  </ul>\n"
            "</body>\n"
            "</html>\n"
        )

    def _assemble_markdown(self, topic: str, sections: Sequence[str], sources: Sequence[str]) -> str:
        # Sections are HTML fragments; keep it simple and include them as-is.
        # If you want, we can later generate markdown sections instead.
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        out = [f"# Research Report: {topic}", f"_Generated: {now}_", ""]
        out.extend(sections)
        out.append("\n## Sources\n")
        out.extend([f"- {u}" for u in sources[:30]])
        return "\n".join(out)


