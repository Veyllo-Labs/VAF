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
    description = "Searches the web and performs deep research on top results."

    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }

    def run(self, **kwargs) -> str:
        query = kwargs.get('query', '')
        if not query:
            return "Error: No query provided."

        try:
            # 1. Search (Deep Research: Get detailed snippets)
            results = DDGS().text(query, max_results=10, safesearch='strict') # 10 high quality
            if not results: return "No results found."
            
            summary = "### Web Search Results (Deep Research)\n"
            
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

            for i, res in enumerate(results):
                title = res['title']
                link = res['href']
                snippet = res['body']
                
                content = ""
                # Deep fetch for top 10
                if i < 10:
                     UI.event("Deep Research", f"Reading {link[:30]}...", style="dim")
                     page_text = fetch_text(link)
                     if page_text:
                         content = f"\n  [Full Content Preview]: {page_text}..."
                
                summary += f"- **{title}**\n  Snippet: {snippet}\n  Link: {link}{content}\n\n"
                
            return summary
        except Exception as e:
            return f"Error: {e}"
