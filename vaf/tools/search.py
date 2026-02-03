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
    description = """Search the web for information. Automatically fetches full page content for accurate data extraction.

**USE THIS FOR:**
- Weather queries: web_search("weather [location] today") → Returns actual temperatures, conditions
- News queries: web_search("latest news [topic]") → Returns headlines and summaries
- Facts/definitions: web_search("what is X") → Returns specific facts and data
- Person info: web_search("who is [person]") → Returns biographical data
- Quick lookups: web_search("current [X]") → Returns current data/status

**IMPORTANT:** You can call this tool MULTIPLE TIMES in ONE response!
Example: User asks "Weather + News" → Call web_search TWICE (weather, then news)

**TIP:** For multiple searches, consider max_results=3 to keep context manageable.

**Safe search:** If the user wants only trusted/safer websites (e.g. "nur vertrauenswürdige Quellen", "only trusted sites"), use trusted_sources_only=true.

**DON'T use research_agent or workflows for simple lookups!**"""

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
            "trusted_sources_only": {
                "type": "boolean",
                "description": "If true, restrict results to trusted sources only (news, tech, academic from VAF sources). Safer search. Default: false",
            },
        },
        "required": ["query"]
    }

    def run(self, **kwargs) -> str:
        query = kwargs.get('query', '')
        max_results = int(kwargs.get("max_results", 5) or 5)
        deep = bool(kwargs.get("deep", True))  # Changed from False to True - always fetch full pages!
        open_in_browser = kwargs.get("open_in_browser", None)
        trusted_sources_only = bool(kwargs.get("trusted_sources_only", False))
        return_raw = bool(kwargs.get("return_raw", False))  # Internal: return raw results dict
        user_question = kwargs.get("user_question", query)  # Extract original user question (fallback to query)
        if not query:
            return "Error: No query provided." if not return_raw else []

        try:
            max_results = max(1, min(max_results, 10))

            # ═══════════════════════════════════════════════════════════════
            # SOURCE FILTERING: trusted_sources_only OR smart intent-based
            # ═══════════════════════════════════════════════════════════════
            query_with_filter = query
            if trusted_sources_only:
                # Restrict to high-trust domains (built-in + user custom from settings), exclude disabled
                try:
                    from vaf.core.sources import get_source_manager
                    disabled = set(Config.get("trusted_sources_disabled") or [])
                    domains = [d for d in get_source_manager().get_domains_with_min_trust(min_score=7, limit=12) if d.lower() not in disabled]
                    custom = Config.get("trusted_sources_custom") or {}
                    for cat_sources in custom.values():
                        for s in cat_sources:
                            for d in (s.get("domains") or []):
                                if d and d.lower() not in disabled and d not in domains:
                                    domains.append(d)
                    domains = domains[:15]
                    if domains:
                        site_filter = " (" + " OR ".join(f"site:{d}" for d in domains) + ")"
                        query_with_filter = query + site_filter
                        UI.event("Web Search", f"Trusted sources only ({len(domains)} sites)", style="dim")
                except Exception:
                    pass
            else:
                # Smart source selection by intent (news/tech/academic)
                try:
                    from vaf.core.query_analyzer import analyze_query
                    intent = analyze_query(query)
                    if intent.suggested_sources and intent.confidence > 0.7:
                        site_filter = " (" + " OR ".join(f"site:{domain}" for domain in intent.suggested_sources[:10]) + ")"
                        query_with_filter = query + site_filter
                        UI.event("Smart Search", f"Using {intent.intent_type} sources ({len(intent.suggested_sources)} sites)", style="dim")
                except Exception:
                    pass

            # 1) Search (with or without site filter)
            raw = DDGS().text(query_with_filter, max_results=max_results, safesearch="strict")
            results = list(raw) if raw else []
            
            # If no results with filter, retry without filter
            if not results and query_with_filter != query:
                UI.event("Smart Search", "No results with source filter - retrying without filter", style="dim")
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
                    # Detect language from user question
                    lang = "German" if any(word in user_question.lower() for word in ["wie", "was", "wann", "wo", "wer", "warum"]) else "English"
                    lang_instruction = "Antworte auf Deutsch." if lang == "German" else "Answer in English."
                    
                    prompt = f"""User Question: "{user_question}"

Page Title: {page_title}
Page URL: {page_url}

Page Content:
{page_content}

{lang_instruction}

CRITICAL INSTRUCTIONS:
- Extract SPECIFIC data: numbers, temperatures, dates, facts, names
- Example for weather: "Temperature: 5°C, Conditions: Partly cloudy, Humidity: 78%"
- Example for news: "Headline: [title], Key point: [summary]"
- DON'T say "page has no information" - extract what IS available!
- If truly no relevant data, say: "No specific data in snippet - visit source for details"
- Be precise and factual (2-3 sentences max)
- Use ONLY information from this page (not your training data)

Answer:"""

                    answer = self.query_llm(
                        messages=[
                            {"role": "system", "content": "You are a helpful assistant that answers questions based on web page content. Always use information from the provided page, not your training data."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=250,
                        temperature=0.2
                    )
                    
                    if answer:
                        return answer
                    else:
                        UI.event("Debug", f"LLM returned empty answer for '{page_title[:30]}'", style="dim")
                        return None
                except Exception as e:
                    UI.event("Debug", f"LLM answer failed for '{page_title[:30]}': {str(e)[:50]}", style="dim")
                    return None
            
            def synthesize_final_answer(user_question: str, all_answers: list) -> str:
                """Create ONE final synthesized answer from multiple source answers."""
                try:
                    # Detect language from user question
                    lang = "German" if any(word in user_question.lower() for word in ["wie", "was", "wann", "wo", "wer", "warum"]) else "English"
                    lang_instruction = "Antworte auf Deutsch." if lang == "German" else "Answer in English."
                    
                    # Combine all answers into one text
                    all_answers_text = ""
                    for i, ans in enumerate(all_answers, 1):
                        all_answers_text += f"\n\nSource {i} ({ans['title']}):\n{ans['answer']}"
                    
                    prompt = f"""User Question: "{user_question}"

Multiple web search results:
{all_answers_text}

{lang_instruction}

Task: Synthesize ONE clear, concise answer (2-4 sentences) that combines the most relevant and accurate information from all sources.
- Focus on directly answering the user's question
- Include specific facts, dates, numbers when available
- If sources disagree, mention the most reliable/recent information
- Be natural and conversational

Final Answer:"""

                    answer = self.query_llm(
                        messages=[
                            {"role": "system", "content": "You are a helpful assistant that synthesizes information from multiple sources into a clear, concise answer."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=300,
                        temperature=0.3
                    )
                    
                    if answer:
                        return answer
                    else:
                        # DEBUG: Empty answer - save snapshot for analysis
                        import json
                        from datetime import datetime
                        debug_dir = Platform.data_dir() / "debug" / "web_search"
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        debug_file = debug_dir / f"empty_synthesis_{timestamp}.json"
                        
                        debug_data = {
                            "timestamp": timestamp,
                            "user_question": user_question,
                            "all_answers": all_answers,
                            "prompt": prompt,
                            "response": "EMPTY"
                        }
                        
                        with open(debug_file, 'w', encoding='utf-8') as f:
                            json.dump(debug_data, f, indent=2, ensure_ascii=False)
                        
                        UI.event("Debug", f"Empty synthesis - saved snapshot: {debug_file.name}", style="yellow")
                        return None
                except Exception as e:
                    UI.event("Debug", f"Final synthesis error: {str(e)[:60]}", style="dim")
                    return None

            summary = title
            preview_limit = min(max_results, 10) if deep else 0

            # Collect links for optional auto-open (dedupe)
            links = []
            seen = set()
            all_answers = []  # Collect answers from each page
            
            # HARD LIMIT: Stop if summary gets too large (approx 4000 tokens)
            # REDUCED from 12000 to 8000 to prevent context overflow (Issue #VAF-CTX-001)
            # When multiple web_search calls are made, 12000 chars each = 36000+ total chars
            MAX_SUMMARY_CHARS = 8000

            for i, res in enumerate(results, 1):
                # Check total size before adding more
                if len(summary) > MAX_SUMMARY_CHARS:
                    summary += f"\n\n[Stopped reading further results to prevent context overflow. {len(results) - i + 1} results omitted.]"
                    break
                    
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
                    # REDUCED from 2000 to 1500 chars to prevent context overflow (Issue #VAF-CTX-001)
                    page_content = fetch_text(link)
                    if page_content and len(page_content) > 1500:
                        page_content = page_content[:1500]
                elif snippet:
                    # Use snippet if deep=False (limit snippet length too)
                    page_content = snippet[:500] if len(snippet) > 500 else snippet
                
                if page_content:
                    UI.event("Web Search", f"Analyzing {page_title[:40]}...", style="dim")
                    answer = answer_question_with_page(user_question, page_title, page_content, link or "")
                    if answer:
                        all_answers.append({
                            "title": page_title,
                            "url": link,
                            "answer": answer
                        })
                        # DEBUG: Show individual answer summary (full answer for debugging)
                        UI.event("Debug", f"Summary {len(all_answers)}: {answer}", style="dim")
                        # Don't add individual answers to summary - we'll synthesize them later
                    elif deep:
                        # Fallback to preview if LLM fails
                        summary += f"   - Preview: {page_content[:300]}...\n"

                summary += "\n"

            # Create ONE final synthesized answer from all sources (not individual answers)
            if all_answers:
                # DEBUG: Show all collected answers before synthesis
                UI.event("Debug", f"Collected {len(all_answers)} answers for synthesis", style="dim")
                for idx, ans in enumerate(all_answers, 1):
                    # Show full answer for debugging (no truncation)
                    UI.event("Debug", f"  {idx}. {ans['title']}: {ans['answer']}", style="dim")
                
                UI.event("Web Search", "Synthesizing final answer...", style="dim")
                
                # RETRY LOOP: Try synthesis multiple times until we get an answer
                final_answer = None
                max_retries = 3
                retry_count = 0
                
                while not final_answer and retry_count < max_retries:
                    if retry_count > 0:
                        UI.event("Web Search", f"Retrying synthesis (attempt {retry_count + 1}/{max_retries})...", style="yellow")
                    
                    final_answer = synthesize_final_answer(user_question, all_answers)
                    
                    if not final_answer:
                        retry_count += 1
                        if retry_count < max_retries:
                            import time
                            time.sleep(1)  # Wait 1s before retry
                
                if final_answer:
                    # DEBUG: Show final synthesized answer (full text for debugging)
                    UI.event("Debug", f"Final synthesis result: {final_answer}", style="dim")
                    
                    summary += f"\n### 🎯 Answer\n{final_answer}\n\n"
                    summary += "**Sources:**\n"
                    for idx, ans in enumerate(all_answers, 1):
                        summary += f"{idx}. [{ans['title']}]({ans['url']})\n"
                else:
                    # Final fallback after all retries failed
                    UI.event("Warning", f"Synthesis failed after {max_retries} attempts - showing raw data", style="yellow")
                    summary += "\n### 📊 Results (Synthesis unavailable - raw answers)\n"
                    for idx, ans in enumerate(all_answers, 1):
                        # Show full answers if synthesis completely failed
                        summary += f"{idx}. **{ans['title']}**: {ans['answer']}\n"
                        summary += f"   [Source]({ans['url']})\n\n"
            else:
                # Fallback 2: No LLM answers generated (LLM calls failed or snippets insufficient)
                # Snippets are already shown above - the agent can use them directly
                # Just add a note for transparency
                summary += f"\n💡 **Note:** Found {len(results)} search results (shown above with snippets). "
                summary += "LLM synthesis was not available. Visit source links for full details.\n"

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
