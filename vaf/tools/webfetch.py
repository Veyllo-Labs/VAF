"""
VAF Web Fetch Tool - Fetch and convert web pages
Retrieves URL content and converts HTML to Markdown
"""
import re
from typing import Dict, Any
from urllib.parse import urlparse

from vaf.tools.base import BaseTool


class WebFetchTool(BaseTool):
    """Fetch content from URLs and convert to readable text."""
    
    name = "webfetch"
    description = """Fetch content from a URL and convert it to readable text/markdown.

Use this tool to:
- Read documentation pages
- Fetch API references
- Get content from GitHub READMEs
- Read blog posts or articles
- Access any publicly available web page

Examples:
- webfetch(url="https://docs.python.org/3/tutorial/") - Python docs
- webfetch(url="https://github.com/user/repo") - GitHub repo
- webfetch(url="https://api.example.com/docs") - API documentation"""
    
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch"
            },
            "extract_main": {
                "type": "boolean",
                "description": "Try to extract main content only (default: true)"
            }
        },
        "required": ["url"]
    }
    
    def run(self, **kwargs) -> str:
        url = kwargs.get("url", "")
        extract_main = kwargs.get("extract_main", True)
        max_length = kwargs.get("max_length", 50000)
        
        if not url or not url.strip():
            return "Error: No URL provided"
        
        # Parse and validate URL
        try:
            parsed = urlparse(url)
            if not parsed.scheme:
                url = "https://" + url
                parsed = urlparse(url)
            
            if parsed.scheme not in ("http", "https"):
                return f"Error: Invalid URL scheme: {parsed.scheme}"
        except Exception as e:
            return f"Error: Invalid URL: {e}"
        
        # Check dependencies
        try:
            import requests
            from bs4 import BeautifulSoup
            import html2text
        except ImportError as e:
            missing = str(e).split("'")[1] if "'" in str(e) else str(e)
            return f"Error: Missing dependency: {missing}. Install with: pip install requests beautifulsoup4 html2text"
        
        try:
            # Fetch the URL
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 VAF/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            
            response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
            response.raise_for_status()
            
            content_type = response.headers.get("Content-Type", "")
            
            # Handle JSON
            if "application/json" in content_type:
                return f"JSON content from {url}:\n\n{response.text[:max_length]}"
            
            # Handle plain text
            if "text/plain" in content_type:
                return f"Text content from {url}:\n\n{response.text[:max_length]}"
            
            # Parse HTML
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Remove unwanted elements
            for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
                tag.decompose()
            
            # Try to extract main content
            main_content = None
            if extract_main:
                for selector in ["main", "article", "#content", "#main", ".content", ".main", ".post"]:
                    main_content = soup.select_one(selector)
                    if main_content:
                        break
            
            html_content = str(main_content) if main_content else str(soup.body or soup)
            
            # Convert to Markdown
            h2t = html2text.HTML2Text()
            h2t.ignore_links = False
            h2t.ignore_images = True
            h2t.body_width = 0
            h2t.unicode_snob = True
            
            markdown = h2t.handle(html_content)
            markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
            
            # Get page title
            title = ""
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text().strip()
            
            # Truncate if needed
            if len(markdown) > max_length:
                markdown = markdown[:max_length] + "\n\n... (content truncated)"
            
            result = []
            if title:
                result.append(f"# {title}")
            result.append(f"Source: {url}")
            result.append("")
            result.append(markdown)
            
            return "\n".join(result)
            
        except Exception as e:
            return f"Error fetching {url}: {e}"
