# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Deterministic report filename generator (cross-platform).

Used by workflows to save reports into the user's Documents directory with a short, readable name.
Example: "künstliche intelligenz" -> "kunstliche_intelligenz_research.html"
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from vaf.core.platform import Platform
from vaf.tools.base import BaseTool


def _slugify_words(text: str) -> list[str]:
    text = (text or "").strip().lower()
    # Remove accents/umlauts into ascii where possible
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Keep letters/numbers/spaces
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    return text.split(" ")


class ReportFilenameTool(BaseTool):
    name = "report_filename"
    permission_level = "read"
    side_effect_class = "none"
    description = "Generate a short report filename in the user's Documents folder based on a topic."

    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "The report topic"},
            "ext": {"type": "string", "description": "File extension (html|md)", "default": "html"},
            "max_words": {"type": "integer", "description": "How many topic words to include (default: 2)", "default": 2},
            "suffix": {"type": "string", "description": "Suffix before extension (default: research)", "default": "research"},
        },
        "required": ["topic"],
    }

    def run(self, **kwargs) -> str:
        topic = str(kwargs.get("topic") or "").strip()
        ext = str(kwargs.get("ext") or "html").strip().lstrip(".").lower()
        max_words = int(kwargs.get("max_words", 2) or 2)
        suffix = str(kwargs.get("suffix") or "research").strip().lower()

        if ext not in ("html", "md", "markdown"):
            ext = "html"
        if ext == "markdown":
            ext = "md"
        max_words = max(1, min(max_words, 6))

        # SMART EXTRACTION: If topic is long/complex, use LLM to extract keywords
        # This avoids filenames like "can_you_please_research.html"
        words = _slugify_words(topic)
        
        if len(words) > 4:
            try:
                # Fast, cheap call to extract keywords
                prompt = (
                    f"Extract exactly {max_words} main keywords for a filename from this request.\n"
                    f"Request: \"{topic}\"\n"
                    f"Keywords (space separated):"
                )
                
                content = self.query_llm(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=20,
                    temperature=0.0
                )
                
                if content:
                    llm_words = _slugify_words(content)
                    if llm_words:
                        words = llm_words
            except:
                pass # Fallback to dumb slugify on error

        base_words = words[:max_words] if words else ["report"]
        base = "_".join(base_words)
        name = f"{base}_{suffix}.{ext}"

        out_path: Path = Platform.documents_dir() / name
        return str(out_path)


