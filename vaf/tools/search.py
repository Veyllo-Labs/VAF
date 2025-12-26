import re
import requests
import warnings
import os

# Best Practice: Try new package first, fallback to legacy with suppression
try:
    from ddgs import DDGS
except ImportError:
    # Fallback for older installations (suppress the rename warning)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from duckduckgo_search import DDGS

from vaf.cli.ui import UI
from vaf.core.config import Config
from vaf.core.platform import Platform
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
            "open_in_browser": {
                "type": "boolean",
                "description": "If true, open result links in the default browser (tabs). Default: from settings.",
            },
        },
        "required": ["query"]
    }

    def run(self, **kwargs) -> str:
        query = kwargs.get('query', '')
        max_results = int(kwargs.get("max_results", 5) or 5)
        deep = bool(kwargs.get("deep", False))
        open_in_browser = kwargs.get("open_in_browser", None)
        return_raw = bool(kwargs.get("return_raw", False))  # Internal: return raw results dict
        user_question = kwargs.get("user_question", query)  # Extract original user question (fallback to query)
        if not query:
            return "Error: No query provided." if not return_raw else []

        try:
            max_results = max(1, min(max_results, 10))

            # 1) Search
            raw = DDGS().text(query, max_results=max_results, safesearch="strict")
            results = list(raw) if raw else []
            if not results:
                return [] if return_raw else "No results found."
            
            # If return_raw is True, return the raw results list
            if return_raw:
                return results

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
                    
                    return text[:5000]  # Increased for better context
                except: return None

            def answer_question_with_page(user_question: str, page_title: str, page_content: str, page_url: str) -> str:
                """Use separate LLM context to answer user question based on single page."""
                try:
                    model_name = Config.get("model", "") or ""
                    if not model_name:
                        return None
                    
                    # Detect language from user question
                    lang = "German" if any(word in user_question.lower() for word in ["wie", "was", "wann", "wo", "wer", "warum"]) else "English"
                    lang_instruction = "Antworte auf Deutsch." if lang == "German" else "Answer in English."
                    
                    prompt = f"""User Question: "{user_question}"

Page Title: {page_title}
Page URL: {page_url}

Page Content:
{page_content}

{lang_instruction}

Based ONLY on the information from this page, answer the user's question.
- If the page contains the answer, provide it clearly and accurately
- If the page doesn't contain relevant information, say so
- Be concise but complete
- Use dates, times, and facts from the page (not from your training data)

Answer:"""

                    res = requests.post(
                        "http://127.0.0.1:8080/v1/chat/completions",
                        json={
                            "model": model_name,
                            "messages": [
                                {"role": "system", "content": "You are a helpful assistant that answers questions based on web page content. Always use information from the provided page, not your training data."},
                                {"role": "user", "content": prompt}
                            ],
                            "max_tokens": 400,
                            "temperature": 0.2,
                        },
                        timeout=30,
                    )
                    
                    if res.status_code == 200:
                        msg = res.json()["choices"][0]["message"]
                        answer = msg.get("content", "").strip()
                        return answer
                    return None
                except Exception as e:
                    UI.event("Debug", f"LLM answer failed: {e}", style="dim")
                    return None

            summary = title
            preview_limit = min(max_results, 10) if deep else 0

            # Collect links for optional auto-open (dedupe)
            links = []
            seen = set()
            all_answers = []  # Collect answers from each page

            for i, res in enumerate(results, 1):
                page_title = res.get("title", "").strip()
                link = res.get("href", "").strip()
                snippet = res.get("body", "").strip()

                summary += f"{i}. **{page_title}**\n"
                if snippet:
                    summary += f"   - Snippet: {snippet}\n"
                if link:
                    summary += f"   - Source: {link}\n"
                    if link and link not in seen:
                        seen.add(link)
                        links.append(link)
                    # Always show link in TUI (unless suppressed, e.g. when a Live TUI is active)
                    suppress = os.environ.get("VAF_SUPPRESS_WEB_SEARCH_EVENTS", "").strip().lower() in ("1", "true", "yes")
                    if not suppress:
                        UI.event("Web Search", f"Reading {link[:60]}...", style="dim")

                # For each result, analyze it with separate LLM context
                page_content = None
                if deep and link and i <= preview_limit:
                    # Fetch full page content if deep=True
                    page_content = fetch_text(link)
                elif snippet:
                    # Use snippet if deep=False
                    page_content = snippet
                
                if page_content:
                    UI.event("Web Search", f"Analyzing {page_title[:40]}...", style="dim")
                    answer = answer_question_with_page(user_question, page_title, page_content, link or "")
                    if answer:
                        all_answers.append({
                            "title": page_title,
                            "url": link,
                            "answer": answer
                        })
                        summary += f"   - Answer: {answer}\n"
                    elif deep:
                        # Fallback to preview if LLM fails
                        summary += f"   - Preview: {page_content[:800]}...\n"

                summary += "\n"

            # Add summary of all answers at the end
            if all_answers:
                summary += "\n### Summary of Answers\n"
                for idx, ans in enumerate(all_answers, 1):
                    summary += f"{idx}. **{ans['title']}**: {ans['answer']}\n"
                    if ans['url']:
                        summary += f"   Source: {ans['url']}\n"
                    summary += "\n"

            # Optional UX: auto-open links in browser (tabs)
            if open_in_browser is None:
                open_in_browser = bool(Config.get("ux_auto_open_links"))

            # Never auto-open in non-interactive mode
            import time
            noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")
            if open_in_browser and not noninteractive and links:
                max_tabs = int(Config.get("ux_auto_open_max_tabs", 8) or 8)
                max_tabs = max(1, min(max_tabs, 20))
                for url in links[:max_tabs]:
                    ok = Platform.open_url(url)
                    if not ok:
                        UI.event("Web Search", f"⚠️ Could not open: {url[:60]}...", style="warning")
                    # Small delay between opens to avoid overwhelming the browser
                    time.sleep(0.3)

            return summary.strip()
        except Exception as e:
            return f"Error: {e}"
