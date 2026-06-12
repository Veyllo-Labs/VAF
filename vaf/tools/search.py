import re
import requests
import os
import threading
from urllib.parse import quote_plus, unquote, parse_qs, urlparse

from bs4 import BeautifulSoup

from vaf.cli.ui import UI
from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.tools.base import BaseTool


def _search_google(query: str, max_results: int) -> tuple[list, str | None]:
    """Try to get search results from Google (https://www.google.com/search?q=...).
    Returns (list of dicts with keys title, href, body; reason).
    reason is None if results returned, else 'blocked'|'no_results'|'error' for fallback hint."""
    out = []
    try:
        # Google expects spaces as + in query string: "wie wird das wetter" -> wie+wird+das+wetter
        url = "https://www.google.com/search?q=" + quote_plus(query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        r = requests.get(url, timeout=8, headers=headers)
        if r.status_code != 200:
            return ([], "error")
        raw = r.text.lower()
        if "unusual traffic" in raw or "captcha" in raw or "denied" in raw:
            return ([], "blocked")
        soup = BeautifulSoup(r.text, "html.parser")

        seen_hrefs: set[str] = set()

        def add_result(title: str, href: str, snippet: str) -> bool:
            if not href or href.startswith("/") or "google.com" in href or href in seen_hrefs:
                return False
            seen_hrefs.add(href)
            title = (title or "").strip() or "No title"
            out.append({"title": title, "href": href, "body": (snippet or "")[:500]})
            return True

        # Strategy 1: classic div.g blocks
        for div in soup.select("div.g"):
            if len(out) >= max_results:
                break
            link_el = div.find("a", href=True)
            if not link_el:
                continue
            href = (link_el.get("href") or "").strip()
            title_el = div.find("h3")
            title = (title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)) or ""
            snippet_el = (
                div.find("div", class_=lambda c: c and "VwiC3b" in str(c))
                or div.find("div", class_=lambda c: c and "IsZvec" in str(c))
                or div.find("div", class_=lambda c: c and "aCOpRe" in str(c))
                or div.find("span", class_=lambda c: c and "st" in str(c).lower())
            )
            snippet = (snippet_el.get_text(strip=True) if snippet_el else "") or ""
            add_result(title, href, snippet)

        # Strategy 2: div.yuRUbf (link container) when div.g finds nothing
        if len(out) < max_results and soup.select("div.yuRUbf"):
            for yu in soup.select("div.yuRUbf"):
                if len(out) >= max_results:
                    break
                link_el = yu.find("a", href=True)
                if not link_el:
                    continue
                href = (link_el.get("href") or "").strip()
                h3 = yu.find("h3")
                title = (h3.get_text(strip=True) if h3 else link_el.get_text(strip=True)) or ""
                parent = yu.parent
                snippet = ""
                if parent:
                    for cls in ("VwiC3b", "IsZvec", "aCOpRe", "s"):
                        sel = parent.find("div", class_=lambda c: c and cls in str(c))
                        if sel:
                            snippet = sel.get_text(strip=True)[:500]
                            break
                add_result(title, href, snippet)

        # Strategy 3: any h3 with parent/sibling <a href="http..."> (catch alternate markup)
        if len(out) < max_results:
            for h3 in soup.find_all("h3"):
                if len(out) >= max_results:
                    break
                a = h3.find_parent("a") or h3.find_next("a")
                if not a or not a.get("href"):
                    continue
                href = (a.get("href") or "").strip()
                title = h3.get_text(strip=True) or ""
                nxt = h3.find_parent("div")
                snippet = ""
                if nxt:
                    nxt = nxt.find_next_sibling()
                    if nxt:
                        snippet = nxt.get_text(strip=True)[:500]
                add_result(title, href, snippet)

        if not out:
            return ([], "no_results")
        return (out, None)
    except Exception:
        return ([], "error")


def _search_brave_api(query: str, max_results: int) -> list:
    """Brave Search API. Returns list of {title, href, body} or [] on failure."""
    key = (Config.get("api_key_brave_search") or "").strip()
    if not key:
        return []
    try:
        url = "https://api.search.brave.com/res/v1/web/search?q=" + quote_plus(query)
        headers = {"X-Subscription-Token": key, "Accept": "application/json"}
        r = requests.get(url, timeout=10, headers=headers)
        if r.status_code != 200:
            label = "Rate limit" if r.status_code == 429 else f"HTTP {r.status_code}"
            UI.event("Web Search", f"Brave API: {label}", style="dim")
            return []
        data = r.json()
        results = (data.get("web") or {}).get("results") or []
        out = []
        for item in results[:max_results]:
            title = (item.get("title") or "").strip() or "No title"
            href = (item.get("url") or "").strip()
            body = (item.get("description") or "").strip()[:500]
            if href:
                out.append({"title": title, "href": href, "body": body})
        return out
    except Exception:
        return []


def _search_google_cse(query: str, max_results: int) -> list:
    """Google Custom Search JSON API. Returns list of {title, href, body} or [] on failure."""
    key = (Config.get("api_key_google_search") or "").strip()
    cx = (Config.get("google_search_engine_id") or "").strip()
    if not key or not cx:
        return []
    try:
        num = min(max(1, max_results), 10)
        url = "https://www.googleapis.com/customsearch/v1"
        params = {"key": key, "cx": cx, "q": query, "num": num}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            label = "Rate limit" if r.status_code == 429 else f"HTTP {r.status_code}"
            UI.event("Web Search", f"Google CSE: {label}", style="dim")
            return []
        data = r.json()
        items = data.get("items") or []
        out = []
        for item in items:
            title = (item.get("title") or "").strip() or "No title"
            href = (item.get("link") or "").strip()
            body = (item.get("snippet") or "").strip()[:500]
            if href:
                out.append({"title": title, "href": href, "body": body})
        return out
    except Exception:
        return []


def _search_duckduckgo(query: str, max_results: int) -> list:
    """
    Direct DuckDuckGo Lite search — no third-party package.
    Uses lite.duckduckgo.com (plain HTML, no bot-challenge JS unlike the main endpoint).
    Uses requests + BeautifulSoup (both already VAF dependencies).
    Returns list of {title, href, body} identical to other search functions.
    """
    try:
        import time
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        # DDG may return 202 (bot challenge) under rate limiting — retry up to 3x with 4s wait
        r = None
        for attempt in range(3):
            r = s.post("https://lite.duckduckgo.com/lite/", data={"q": query}, timeout=10)
            if r.status_code == 200:
                break
            UI.event("Web Search", f"DuckDuckGo: HTTP {r.status_code} (attempt {attempt+1}/3) — retrying in 4s", style="dim")
            if attempt < 2:
                time.sleep(4)
        if r is None or r.status_code != 200:
            UI.event("Web Search", f"DuckDuckGo: failed after 3 attempts (HTTP {r.status_code if r else 'timeout'})", style="warning")
            _note_provider_error(f"DuckDuckGo failed after 3 attempts (HTTP {r.status_code if r else 'timeout'})")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select("a.result-link")
        snippets = soup.select("td.result-snippet")
        out = []
        seen: set[str] = set()

        for a, snippet_el in zip(links, snippets):
            if len(out) >= max_results:
                break
            href = unquote(a.get("href", ""))
            if not href or not href.startswith("http") or href in seen:
                continue
            seen.add(href)
            title = a.get_text(strip=True) or "No title"
            body = snippet_el.get_text(strip=True)[:500] if snippet_el else ""
            out.append({"title": title, "href": href, "body": body})

        if not out:
            UI.event("Web Search", "DuckDuckGo: 200 OK but 0 results parsed from HTML", style="dim")
        return out
    except Exception as _e:
        UI.event("Web Search", f"DuckDuckGo: exception — {str(_e)[:80]}", style="warning")
        _note_provider_error(f"DuckDuckGo: {str(_e)[:120]}")
        return []


# Provider failures are swallowed per-call (each provider falls through to the
# next), which once made a full search outage (DDG IP-blocked, no API keys)
# look like "no results found". Callers like the research agent read this to
# tell outages from genuine no-hits.
_PROVIDER_ERRORS: list = []
_PROVIDER_ERRORS_LOCK = threading.Lock()


def _note_provider_error(message: str) -> None:
    try:
        with _PROVIDER_ERRORS_LOCK:
            if len(_PROVIDER_ERRORS) < 20:
                _PROVIDER_ERRORS.append(str(message)[:160])
    except Exception:
        pass


def reset_search_provider_errors() -> None:
    with _PROVIDER_ERRORS_LOCK:
        _PROVIDER_ERRORS.clear()


def get_search_provider_errors() -> list:
    with _PROVIDER_ERRORS_LOCK:
        return list(_PROVIDER_ERRORS)


def get_web_search_results(query: str, max_results: int) -> tuple[list, str, str | None]:
    """Try Brave API -> Google CSE API -> scrape Google -> DuckDuckGo. Returns (results, source_name, fallback_hint)."""
    fallback_hint = None

    # 1) Brave API
    brave_key = (Config.get("api_key_brave_search") or "").strip()
    if brave_key:
        results = _search_brave_api(query, max_results)
        if results:
            return (results, "Brave", None)
    else:
        UI.event("Web Search", "Brave API: no key configured — skipping", style="dim")

    # 2) Google Custom Search API
    google_key = (Config.get("api_key_google_search") or "").strip()
    google_cx  = (Config.get("google_search_engine_id") or "").strip()
    if google_key and google_cx:
        results = _search_google_cse(query, max_results)
        if results:
            return (results, "Google CSE", None)
    else:
        UI.event("Web Search", "Google CSE: no key configured — skipping", style="dim")

    # 3) Scrape Google
    results, google_reason = _search_google(query, max_results)
    if results:
        return (results, "Google", None)

    # 4) DuckDuckGo fallback (direct HTML — no third-party package)
    UI.event("Web Search", "Google: no results or blocked – using DuckDuckGo", style="dim")
    results = _search_duckduckgo(query, max_results)
    fallback_hint = {"blocked": "Google blockiert.", "no_results": "Google: keine Treffer.", "error": "Google: Fehler."}.get(google_reason, "Google: keine Treffer.")
    if results:
        return (results, "DuckDuckGo", fallback_hint)

    # 5) Last resort: VAF's own long-term memory (RAG). When every web provider
    # fails (rate limit, no API keys, network down) — or the topic is internal
    # and the web genuinely knows nothing — the knowledge base often does.
    internal = _search_internal_knowledge(query, max_results)
    if internal:
        UI.event("Web Search", f"Falling back to internal knowledge: {len(internal)} snippet(s)", style="dim")
        return (internal, "Internal Knowledge (RAG)", "Websuche nicht verfügbar — Treffer stammen aus dem internen Langzeitgedächtnis.")
    return (results, "DuckDuckGo", fallback_hint)


def _search_internal_knowledge(query: str, max_results: int) -> list:
    """Search VAF's long-term memory (RAG) and shape hits like web results.

    Results carry memory:// hrefs and an internal_knowledge marker so reports
    and the UI can label them honestly as memory, not web sources.
    """
    try:
        scope = None
        try:
            from vaf.core.session import get_manager
            sid = os.environ.get("VAF_SESSION_ID", "").strip()
            if sid:
                uid = (get_manager().load(sid).metadata or {}).get("user_scope_id")
                if uid:
                    from uuid import UUID
                    scope = UUID(str(uid))
        except Exception:
            scope = None

        from vaf.memory.rag import run_memory_search_sync
        raw = run_memory_search_sync(
            query=query, k=max(1, min(max_results, 8)),
            user_scope_id=scope, caller="websearch_fallback",
        )
    except Exception as e:
        _note_provider_error(f"internal knowledge: {str(e)[:100]}")
        return []
    if not raw or not raw.strip():
        return []

    results = []
    for i, block in enumerate(re.split(r'\n\n---\n\n', raw)[:max_results], 1):
        m = re.match(r'\[Source \d+\]\s*\(Relevance:\s*(\d+)%\)\s*\n([\s\S]*)', block.strip())
        text = (m.group(2) if m else block).strip()
        if not text:
            continue
        relevance = f" ({m.group(1)}% relevant)" if m else ""
        first_line = text.split("\n", 1)[0][:80]
        results.append({
            "title": f"Internes Wissen: {first_line}{relevance}",
            "href": f"memory://internal/{i}",
            "body": text[:500],
            "source": "internal_knowledge",
        })
    return results


class WebSearchTool(BaseTool):
    name = "web_search"
    permission_level = "read"
    side_effect_class = "none"
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

            # 1) Search: Brave API -> Google CSE API -> scrape Google -> DuckDuckGo
            results, search_source, fallback_hint = get_web_search_results(query_with_filter, max_results)
            # If no results with filter, retry without filter
            if not results and query_with_filter != query:
                UI.event("Smart Search", "No results with source filter - retrying without filter", style="dim")
                results, search_source, fallback_hint = get_web_search_results(query, max_results)
            
            if not results:
                return [] if return_raw else "No results found. (All search APIs returned empty — possible rate limit or network issue)"
            
            # If return_raw is True, return the raw results list
            if return_raw:
                return results

            if search_source == "Internal Knowledge (RAG)":
                # Unmistakable header: a user once read this as a working web
                # search because the generic "Web Search Results" framing buried
                # the fallback label.
                title = (
                    "### INTERNAL KNOWLEDGE — NOT WEB RESULTS\n"
                    "**The web search providers are unreachable (rate limit / network / missing API keys). "
                    "The following snippets come from VAF's own long-term memory. "
                    "Tell the user explicitly that the web could not be reached and these are memory results.**\n\n"
                )
            else:
                title = f"### Web Search Results ({search_source})\n"
                if fallback_hint:
                    title += f"*{fallback_hint}*\n\n"
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
                        max_tokens=600,  # small so a reasoning model finishes fast inside the per-page budget; query_llm falls back to reasoning_content if cut off
                        temperature=0.2,
                        timeout=30,
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
                        max_tokens=1000,  # bounded so the final synthesis fits the time budget; query_llm falls back to reasoning_content if cut off
                        temperature=0.3,
                        timeout=35,
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

            # Time budget: stay well under the tool-timeout wrapper, which HARD-KILLS web_search at
            # tool_timeout_seconds and discards everything. We self-stop early and return the snippets
            # (already added below) plus whatever page answers we managed to gather. The per-page deep
            # fetch + LLM synthesis is the slow part with a reasoning model, so it is gated on the budget.
            import time as _t
            try:
                from vaf.core.bounded_run import agent_timeout_seconds as _ats
                _tool_budget = float(_ats("web_search"))
            except Exception:
                _tool_budget = 120.0
            _budget = max(40.0, _tool_budget - 10.0)          # safety margin before the hard kill
            _t_start = _t.monotonic()
            # A synthesis call can run up to its own timeout PAST the moment it starts, so leave headroom:
            # the final synthesis (~35s) must START before _deadline, and per-page work (~30s each) must
            # START before _perpage_deadline so the last one still finishes before the final begins.
            _deadline = _t_start + max(20.0, _budget - 40.0)
            _perpage_deadline = _t_start + max(10.0, _budget - 70.0)

            for i, res in enumerate(results, 1):
                # Respect stop button between result pages
                try:
                    from vaf.core.subagent_ipc import get_current_session_id as _gcsi
                    _sid = _gcsi()
                    if _sid:
                        from vaf.core.task_queue import TaskQueue as _TQ
                        if _TQ().should_stop(_sid):
                            summary += "\n\n[Web search aborted by user.]"
                            break
                except Exception:
                    pass

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

                # For each result, analyze it with separate LLM context — but only while we are within
                # the per-page time budget. Once it passes we stop fetching/synthesising and just let the
                # already-added snippets stand, so the tool returns gracefully before the hard kill.
                page_content = None
                _within_budget = _t.monotonic() < _perpage_deadline
                if deep and link and i <= preview_limit and _within_budget:
                    # Fetch full page content if deep=True
                    # REDUCED from 2000 to 1500 chars to prevent context overflow (Issue #VAF-CTX-001)
                    page_content = fetch_text(link)
                    if page_content and len(page_content) > 1500:
                        page_content = page_content[:1500]
                elif snippet and _within_budget:
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

            # Synthesize ONE final answer -- but only from sources that actually carried data. The
            # per-page step emits a "No specific data in snippet ..." marker when a fetched page held only
            # navigation / boilerplate (common for JavaScript-rendered sites, e.g. weather pages). Feeding
            # those into synthesis yields an empty answer and pointless retries; detect and skip them.
            _NO_DATA_MARKERS = ("no specific data", "no relevant data", "only shows navigation")
            usable_answers = [a for a in all_answers
                              if not any(m in (a.get("answer") or "").lower() for m in _NO_DATA_MARKERS)]

            if usable_answers and _t.monotonic() >= _deadline:
                # Out of time budget: skip the (slow) final synthesis and return the gathered page
                # answers raw, so the tool finishes before the hard kill instead of being discarded.
                UI.event("Web Search", "Time budget reached - returning gathered answers (synthesis skipped).", style="warning")
                summary += "\n### 📊 Results (gathered; final synthesis skipped to stay responsive)\n"
                for idx, ans in enumerate(usable_answers, 1):
                    summary += f"{idx}. **{ans['title']}**: {ans['answer']}\n"
                    summary += f"   [Source]({ans['url']})\n\n"
            elif usable_answers:
                # DEBUG: Show all collected answers before synthesis
                UI.event("Debug", f"Collected {len(usable_answers)} answer(s) for synthesis", style="dim")
                for idx, ans in enumerate(usable_answers, 1):
                    # Show full answer for debugging (no truncation)
                    UI.event("Debug", f"  {idx}. {ans['title']}: {ans['answer']}", style="dim")

                UI.event("Web Search", "Synthesizing final answer...", style="dim")

                # RETRY LOOP: Try synthesis a couple of times in case of a transient empty generation
                final_answer = None
                max_retries = 2
                retry_count = 0

                while not final_answer and retry_count < max_retries:
                    if retry_count > 0:
                        UI.event("Web Search", f"Retrying synthesis (attempt {retry_count + 1}/{max_retries})...", style="yellow")

                    final_answer = synthesize_final_answer(user_question, usable_answers)

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
                    for idx, ans in enumerate(usable_answers, 1):
                        summary += f"{idx}. [{ans['title']}]({ans['url']})\n"
                else:
                    # Final fallback after all retries failed
                    UI.event("Warning", f"Synthesis failed after {max_retries} attempts - showing raw data", style="yellow")
                    summary += "\n### 📊 Results (Synthesis unavailable - raw answers)\n"
                    for idx, ans in enumerate(usable_answers, 1):
                        # Show full answers if synthesis completely failed
                        summary += f"{idx}. **{ans['title']}**: {ans['answer']}\n"
                        summary += f"   [Source]({ans['url']})\n\n"
            elif all_answers:
                # Had per-page results, but none carried extractable data (navigation / boilerplate only --
                # typical for JavaScript-rendered pages). Skip synthesis entirely: no empty generation, no
                # retries, no debug snapshots. Report it clearly so the agent tells the user honestly.
                UI.event("Web Search", "Pages had no extractable data (navigation/boilerplate only) - skipping synthesis", style="yellow")
                summary += ("\n### Note\nThe fetched pages did not contain the concrete data (only navigation / "
                            "page structure -- typical for JavaScript-rendered sites). Tell the user the live "
                            "values could not be retrieved from these pages, and offer the source links below.\n\n"
                            "**Sources:**\n")
                for idx, ans in enumerate(all_answers, 1):
                    summary += f"{idx}. [{ans['title']}]({ans['url']})\n"
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

            # Follow-up nudge ONLY for a shallow (snippet-only) search. With deep=True (the default) the
            # pages were already fetched and analysed above, so telling the model "these are short snippets,
            # read the full page via webfetch" is false and actively harmful: observed the model think
            # "Ich sollte die Seite lesen" and re-call web_search (-> redundant-block, no answer) instead of
            # answering from the content it already had. Only nudge toward webfetch on a bare-snippet search.
            if links and not deep:
                _ex = links[0]
                lead = (
                    "**NEXT STEP (read this first):** the results below are short search snippets, NOT full "
                    "pages. If they do not already contain the concrete answer the user asked for (e.g. the "
                    f"actual weather values, a price, a date, specific details), call `webfetch(\"{_ex}\")` on "
                    "one of the source links to read the full page, then answer from its content. Do NOT just "
                    "hand the user the links.\n\n---\n\n"
                )
                summary = lead + summary

            return summary.strip()
        except Exception as e:
            return f"Error: {e}"
