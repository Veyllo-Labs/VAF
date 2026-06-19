"""
VAF Web Fetch Tool - The Ultimate Web Reader
Combines: SSL Fallback, UA Rotation, Caching, Table Flattening, 
Text-Density Extraction, Navigation Filtering, and Keyword Search.
"""
import re
import os
import json
import time
import hashlib
import urllib3
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.core.config import Config

# Browser-like request headers come from vaf.tools._browser_headers.browser_headers()
# (imported where used) — a full, consistent set, not just a rotated User-Agent.
DOMAIN_LAST_FETCH: Dict[str, float] = {}
MIN_DELAY = 1.0 

class WebFetchTool(BaseTool):
    name = "webfetch"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Retrieves content from a URL and converts it to readable Markdown. "
        "IMPORTANT: For long pages, tracking sites (DHL, UPS), or when searching for specific info, "
        "ALWAYS provide 'search_terms' (e.g., ['Status', 'Delivery', 'August']). "
        "This extracts relevant sections with context and moves them to the top, "
        "preventing the important info from being cut off by system truncation."
    )
    
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
            "extract_main": {"type": "boolean", "description": "Try to extract main content only (default: true)"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
            "use_cache": {"type": "boolean", "description": "Use cached version if available (default: true)"},
            "cache_ttl": {"type": "integer", "description": "Cache lifetime in seconds (default: 3600)"},
            "user_agent": {"type": "string", "description": "Custom User-Agent string"},
            "max_length": {"type": "integer", "description": "Max length of returned text (default: 20000)"},
            "search_terms": {
                "type": "array", 
                "items": {"type": "string"},
                "description": "Terms to search for. Matching sections will be moved to the top."
            }
        },
        "required": ["url"]
    }

    def _get_cache_path(self, url: str) -> Path:
        cache_dir = Config.APP_DIR / "tmp" / "webfetch_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return cache_dir / f"{url_hash}.json"

    def _get_cached_data(self, url: str, ttl: int) -> Optional[Dict]:
        cache_path = self._get_cache_path(url)
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if time.time() - data.get("timestamp", 0) < ttl:
                        return data
            except Exception: pass
        return None

    def _save_to_cache(self, url: str, content: str, content_type: str):
        try:
            with open(self._get_cache_path(url), "w", encoding="utf-8") as f:
                json.dump({"url": url, "timestamp": time.time(), "content": content, "type": content_type}, f, ensure_ascii=False)
        except Exception: pass

    def run(self, **kwargs) -> str:
        url = kwargs.get("url", "").strip()
        extract_main = kwargs.get("extract_main", True)
        max_length = kwargs.get("max_length", 20000)
        timeout = kwargs.get("timeout", 30)
        use_cache = kwargs.get("use_cache", True)
        cache_ttl = kwargs.get("cache_ttl", 3600)
        search_terms = kwargs.get("search_terms", [])
        
        if not url: return "Error: No URL provided"
        
        # 1. Validate & Parse
        try:
            parsed = urlparse(url)
            if not parsed.scheme:
                url = "https://" + url
                parsed = urlparse(url)
        except Exception as e: return f"Error: Invalid URL: {e}"

        # 2. Rate Limiting
        domain = parsed.netloc
        if domain in DOMAIN_LAST_FETCH:
            elapsed = time.time() - DOMAIN_LAST_FETCH[domain]
            if elapsed < MIN_DELAY: time.sleep(MIN_DELAY - elapsed)
        DOMAIN_LAST_FETCH[domain] = time.time()

        # 3. Fetch (with Cache & SSL Fallback)
        full_text = ""
        content_type = "text/html"
        cached_data = self._get_cached_data(url, cache_ttl) if use_cache else None
        
        if cached_data:
            full_text = cached_data["content"]
            content_type = cached_data.get("type", "text/html")
        else:
            try:
                import requests
                from vaf.tools._browser_headers import browser_headers
                # Full, consistent browser header set (not just UA + Accept) — a thin
                # header set is itself a bot tell. Honours a caller-supplied user_agent.
                headers = browser_headers(user_agent=kwargs.get("user_agent"))
                try:
                    res = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
                except requests.exceptions.SSLError:
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    res = requests.get(url, headers=headers, timeout=timeout, verify=False)
                
                if res.status_code != 200: return f"Error: Site returned status {res.status_code}"
                full_text = res.text
                content_type = res.headers.get("Content-Type", "")
                self._save_to_cache(url, full_text, content_type)
            except Exception as e: return f"Error fetching {url}: {e}"

        # 4. Processing
        if "application/json" in content_type:
            return f"JSON from {url}:\n\n{full_text[:max_length]}"

        try:
            from bs4 import BeautifulSoup
            import html2text
            soup = BeautifulSoup(full_text, "html.parser")
            
            # Metadata & Iframes
            title = soup.title.get_text(strip=True) if soup.title else ""
            iframes = [ifr.get("src") for ifr in soup.find_all("iframe") if ifr.get("src")]
            is_js_heavy = len(soup.find_all("script")) > 15
            
            # Clean Technical Noise
            for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            
            # Table Flattening (Layout tables)
            for table in soup.find_all("table"):
                if not table.find("th"): table.unwrap()
            for tag in soup.find_all(["tbody", "tr", "td", "thead", "tfoot"]):
                tag.unwrap()
            
            # Main Content Extraction
            main_area = None
            if extract_main:
                for sel in ["main", "article", "#content", ".content", ".article-body"]:
                    cand = soup.select_one(sel)
                    if cand and len(cand.get_text(strip=True)) > 300:
                        main_area = cand
                        break
                if not main_area: # Text density heuristic
                    max_t = 0
                    for div in soup.find_all(["div", "section"]):
                        tl = len(div.get_text(strip=True))
                        if tl > max_t and tl < len(full_text) * 0.9:
                            max_t, main_area = tl, div
            
            html_chunk = str(main_area) if main_area else str(soup.body or soup)
            
            # Convert to Markdown
            h2t = html2text.HTML2Text()
            h2t.ignore_links, h2t.body_width, h2t.unicode_snob = False, 0, True
            markdown = h2t.handle(html_chunk)
            markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
            
            # 5. Semantic Re-Ordering & Filtering
            lines = markdown.split("\n")
            
            # Move Nav Links to bottom
            content_start = 0
            for i, line in enumerate(lines[:50]):
                if len(line.strip()) > 50 and "[" not in line[:10]:
                    content_start = i
                    break
            if content_start > 5:
                markdown = "\n".join(lines[content_start:]) + "\n\n---\n### Navigation (Moved):\n" + "\n".join(lines[:content_start])
            
            # Keyword Search Results (Prioritize)
            search_header = ""
            if search_terms:
                matches = []
                paras = markdown.split("\n\n")
                for term in search_terms:
                    for i, p in enumerate(paras):
                        if term.lower() in p.lower():
                            ctx = (paras[i-1] + "\n" if i>0 else "") + f"**MATCH: {p}**" + ("\n" + paras[i+1] if i<len(paras)-1 else "")
                            matches.append(ctx)
                            break
                if matches:
                    search_header = "\n## Key Matches:\n" + "\n---\n".join(matches[:3]) + "\n\n---\n"

            # 6. Final Assembly
            res_lines = [f"# {title}" if title else "", f"Source: {url}", search_header]
            if iframes: res_lines.append(f"[INFO] Page has {len(iframes)} iframes.")
            if is_js_heavy and len(markdown) < 1000: res_lines.append("[NOTE] Site uses heavy JS.")
            res_lines.append("\n" + markdown[:max_length])
            if len(markdown) > max_length: res_lines.append("\n... (truncated)")
            
            return "\n".join([l for l in res_lines if l]).replace("\n\n\n", "\n\n")

        except Exception as e: return f"Error parsing content: {e}"
