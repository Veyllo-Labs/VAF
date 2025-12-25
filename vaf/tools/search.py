import re
import requests
import warnings

# Best Practice: Try new package first, fallback to legacy with suppression
try:
    from ddgs import DDGS
except ImportError:
    # Fallback for older installations (suppress the rename warning)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from duckduckgo_search import DDGS

from vaf.cli.ui import UI
from vaf.tools.base import BaseTool

class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web. Supports quick search and optional deep page previews for top results."

    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {
                "type": "integer",
                "description": "How many results to return (default: 5)",
            },
            "deep": {
                "type": "boolean",
                "description": "If true, fetch a short text preview from top results (slower). Default: false",
            },
        },
        "required": ["query"]
    }

    def run(self, **kwargs) -> str:
        query = kwargs.get('query', '')
        max_results = int(kwargs.get("max_results", 5) or 5)
        deep = bool(kwargs.get("deep", False))
        if not query:
            return "Error: No query provided."

        try:
            max_results = max(1, min(max_results, 10))

            # 1) Search
            raw = DDGS().text(query, max_results=max_results, safesearch="strict")
            results = list(raw) if raw else []
            if not results:
                return "No results found."

            title = "### Web Search Results\n"
            title += f"Query: {query}\n\n"
            
            # Helper to fetch text
            def fetch_text(url):
                try:
                    # Chrome User-Agent
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
                    r = requests.get(url, timeout=4, headers=headers)
                    if r.status_code != 200: return None
                    
                    html = r.text
                    # 1. Remove Script and Style elements completely
                    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
                    
                    # 2. Basic strip tags
                    text = re.sub(r'<[^>]+>', ' ', html)
                    
                    # 3. Clean whitespace
                    text = re.sub(r'\s+', ' ', text).strip()
                    
                    return text[:3000] # Limit to 3000 chars
                except: return None

            summary = title
            preview_count = 0
            preview_limit = 3 if deep else 0

            for i, res in enumerate(results, 1):
                page_title = res.get("title", "").strip()
                link = res.get("href", "").strip()
                snippet = res.get("body", "").strip()

                summary += f"{i}. **{page_title}**\n"
                if snippet:
                    summary += f"   - Snippet: {snippet}\n"
                if link:
                    summary += f"   - Source: {link}\n"

                if deep and link and preview_count < preview_limit:
                    UI.event("Web Search", f"Reading {link[:60]}...", style="dim")
                    page_text = fetch_text(link)
                    if page_text:
                        summary += f"   - Preview: {page_text[:800]}...\n"
                    preview_count += 1

                summary += "\n"

            return summary.strip()
        except Exception as e:
            return f"Error: {e}"
