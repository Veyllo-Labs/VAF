"""
Repair Report Tool

Deterministically checks an HTML report for empty/too-short sections and regenerates
those sections using `research_agent` in bounded context.

This tool does NOT write to disk — it returns repaired HTML so workflows can save it via write_file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from vaf.cli.ui import UI
from vaf.tools.base import BaseTool
from vaf.tools.research_agent import ResearchAgentTool, _detect_language


def _strip_tags(s: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", s or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt

def _word_count_from_html(s: str) -> int:
    txt = _strip_tags(s)
    if not txt:
        return 0
    return len(re.findall(r"\b\w+\b", txt, flags=re.UNICODE))


def _find_h1_topic(html: str) -> Optional[str]:
    m = re.search(r"(?is)<h1[^>]*>\s*Research Report:\s*(.*?)\s*</h1>", html or "")
    if m:
        return _strip_tags(m.group(1))
    return None


@dataclass(frozen=True)
class Section:
    title: str
    start: int
    end: int
    body_html: str


def _extract_sections(html: str) -> List[Section]:
    """
    Extract <h2> sections up to (but not including) the Sources section.
    """
    html = html or ""
    # Work on body content only
    body = html
    # Find all h2 positions
    matches = list(re.finditer(r"(?is)<h2[^>]*>\s*(.*?)\s*</h2>", body))
    sections: List[Section] = []
    for i, m in enumerate(matches):
        title = _strip_tags(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)

        # Stop before Sources
        if title.lower() == "sources":
            break

        body_html = body[m.end():end]
        sections.append(Section(title=title, start=start, end=end, body_html=body_html))
    return sections


class RepairReportTool(BaseTool):
    name = "repair_report"
    description = "Check an HTML report and regenerate empty/too-short sections (returns repaired HTML)."

    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Main topic (optional; inferred from <h1> if missing)"},
            "content": {"type": "string", "description": "HTML report content"},
            "language": {"type": "string", "description": "Force language: de|en (optional)"},
            # Preferred: word-based thresholds (align with research_agent)
            "min_words_target": {"type": "integer", "default": 500},
            "min_words_ok_ratio": {"type": "number", "default": 0.8},
            # Backward compat (deprecated): char-based thresholds
            "min_chars_empty": {"type": "integer", "default": 150},
            "min_chars_ok": {"type": "integer", "default": 500},
        },
        "required": ["content"],
    }

    def run(self, **kwargs) -> str:
        html = str(kwargs.get("content") or "")
        if not html.strip():
            return html

        topic = str(kwargs.get("topic") or "").strip()
        if not topic:
            inferred = _find_h1_topic(html)
            if inferred:
                topic = inferred
        if not topic:
            topic = "Report"

        forced_lang = (kwargs.get("language") or "").strip().lower()
        lang = forced_lang if forced_lang in ("de", "en") else _detect_language(topic)

        min_words_target = int(kwargs.get("min_words_target", 500) or 500)
        min_words_ok_ratio = float(kwargs.get("min_words_ok_ratio", 0.8) or 0.8)
        min_words_target = max(150, min(min_words_target, 1200))
        min_words_ok_ratio = max(0.5, min(min_words_ok_ratio, 0.95))
        min_words_ok = max(50, int(min_words_target * min_words_ok_ratio))

        min_chars_empty = int(kwargs.get("min_chars_empty", 150) or 150)
        min_chars_ok = int(kwargs.get("min_chars_ok", 500) or 500)

        sections = _extract_sections(html)
        if not sections:
            return html

        agent = ResearchAgentTool()

        replacements: List[Tuple[int, int, str]] = []
        for sec in sections:
            words = _word_count_from_html(sec.body_html)
            visible_len = len(_strip_tags(sec.body_html))

            # Prefer word-based thresholds; fall back to char-based if needed
            ok_by_words = words >= min_words_ok
            ok_by_chars = visible_len >= min_chars_ok
            if ok_by_words or (words == 0 and ok_by_chars):
                continue

            level = "empty" if (words == 0 and visible_len < min_chars_empty) or (words > 0 and words < max(30, min_words_ok // 8)) else "short"
            if words > 0:
                UI.event("Repair", f"Fixing section '{sec.title}' ({level}, {words} words)", style="warning")
            else:
                UI.event("Repair", f"Fixing section '{sec.title}' ({level}, {visible_len} chars)", style="warning")

            # Generate just this section as a fragment.
            # For "short" sections, prefer append-style expansion (keeps existing content, avoids full rewrite).
            existing_section_html = f"<h2>{sec.title}</h2>{sec.body_html}".strip()
            fragment = agent.run(
                topic=topic,
                format="html_fragment",
                sections=[sec.title],
                language=lang,
                max_results=5,
                deep=(level == "empty"),
                min_words_target=min_words_target,
                min_words_ok_ratio=min_words_ok_ratio,
                min_chars_empty=min_chars_empty,  # backward compat
                min_chars_ok=min_chars_ok,        # backward compat
                existing_section_html=(existing_section_html if level == "short" else ""),
            )
            fragment = str(fragment or "").strip()
            if not fragment:
                continue

            # Replace entire section block (from <h2>..</h2> start to next h2)
            replacements.append((sec.start, sec.end, fragment))

        if not replacements:
            return html

        # Apply replacements from back to front so indices stay valid
        out = html
        for start, end, frag in sorted(replacements, key=lambda x: x[0], reverse=True):
            out = out[:start] + frag + out[end:]

        return out


