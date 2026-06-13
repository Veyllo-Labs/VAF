# Research Agent

The research agent (`vaf/tools/research_agent.py`, tool name `research_agent`) produces multi-section HTML/Markdown reports from web research. It runs as a sub-agent in its own process (IPC terminal spawn, like the coder) and streams a live view into the WebUI.

## Pipeline (per run)

1. **Title** — `_generate_title()`: short instruction-free topics are used verbatim (no LLM call); otherwise one LLM call with reasoning-leak protection (think-block stripping, "Title:" extraction, rejection of chain-of-thought and few-shot echoes, fallback to the raw topic).
2. **Plan** — `_generate_plan()` produces `SectionSpec`s (titles, query suffixes); target words per section come from `min_words_target`.
3. **Query profile** — once per run: required/exclusion concepts and fallback queries.
4. **Per section:**
   - `_augment_queries()` builds up to 4 short search queries. `_compress_query()` reduces prompt-style topics to a handful of keywords (a 117-char topic sentence as the only query once produced 0 hits across all sections). LLM failures fall back to deterministic keyword queries; placeholder echoes ("Query 1") are rejected.
   - `_search_parallel()` runs the queries concurrently, deduplicates by href, then keyword gate, quality filter (score tiers 7 → 4 → 1 with warnings), graceful-degradation fallback queries and an LLM relevance filter.
   - `_summarize_section_html()` writes the section (2200 tokens, retry at 1800). Every output passes `_sanitize_section_output()`: think blocks stripped, text cut to the first HTML tag, missing `<h2>` added, pure chain-of-thought rejected so the retry path runs — a run once filled all sections with "Thinking Process: ...".
   - Empty/short sections trigger a deep-search retry and an append-expand pass within the section time budget.
5. **Finalize** — assemble HTML/MD, save into the chat workspace (`resolve_agent_output_dir`), open in the Document Editor, clear checkpoints.

## Robustness

- **Checkpoints/Resume:** finished sections and collected sources are checkpointed; an aborted run resumes instead of restarting.
- **Timeouts:** per web search, per section LLM call, per section, and an overall run timeout — a stalled stage finalizes the report with what exists.
- **Search outage detection:** provider failures (DDG rate limit, missing API keys, network) are collected via `get_search_provider_errors()` (`vaf/tools/search.py`). A section with zero raw results reports "SEARCH UNAVAILABLE - <reason>" and writes an honest placeholder ("Suchdienst nicht erreichbar ... Brave/Google-API-Schluessel konfigurieren") instead of pretending there were no hits. Telemetry event: `section_search_outage`.
- **Internal knowledge fallback:** when every web provider fails or finds nothing, the search chain falls back to VAF's long-term memory (see `docs/llm/API_INTEGRATION.md`). Memory hits arrive as `memory://` results labeled "Internes Wissen" and are never presented as web sources.
- **Reasoning models:** all helper calls tolerate Qwen-style reasoning output (generous token budgets, `reasoning_content` fallback, verdict/JSON parsing from the END of the text).
- **Streaming section writer (local provider):** sections stream from the local llama server with an IDLE timeout instead of a fixed total timeout (`_stream_section_completion`) — the model may think as long as tokens keep arriving; reasoning deltas count as liveness but are never collected, and aborting closes the response so the server slot is freed for the retry. SSE lines are decoded explicitly as UTF-8 (llama-server declares no charset; the requests default of ISO-8859-1 turned every umlaut into mojibake). Live word-count progress feeds the WebUI outline during writing.
- **Numbered citations:** sections cite with `[n]` markers against ONE global numbered source list (the prompt hands the model the section's sources with their global numbers; sidebar, report file and markers share the same numbering). Per-section source paragraphs/URL lists the model still writes are stripped deterministically (`_strip_section_source_blocks`); the report appends the global list as an ordered list. The WebUI renders `[n]` as superscript citation chips.
- **Section normalization:** loose text after a heading is wrapped in `<p>` (`_wrap_loose_section_text`) and runaway headings — an unclosed `<h2>` or body text packed into the heading — are clamped back to a clean title (`_clamp_runaway_heading`), so sections never render entirely in heading style.

## WebUI live view

During a run the agent emits `research_state` events (hash+time throttled, telemetry `research_state_emitted`): topic, stage, outline with per-section status (`planned/searching/writing/done/error`) and word progress, finished section HTML, sources (title/domain) and loop count. The SubAgent window renders a paper-style document viewer (the newest section types out, then swaps to rendered HTML), outline progress, clickable source citations, an activity feed and a status bar. Payload details: `docs/web-ui/WEBUI_WEBSOCKET_FLOW.md`.

The paper viewer shows the report as true DIN A4 sheets (210 x 297 mm, 20 mm margins) with automatic page breaks. The report HTML flows through a CSS multi-column container whose column box equals the A4 content area; the browser's layout engine places the breaks line-accurately (inline markup preserved), and each column is displayed as one fixed sheet (`A4ResearchPaper` in `web/components/SubAgentWindow.tsx`). Sheets scale down to fit narrow windows (fit-width, like a PDF viewer) without changing the page geometry. The print button in the window header renders the identical sheet markup and CSS into a hidden iframe with `@page size: A4; margin: 0` and one `page-break-after` per sheet, so the printout matches the preview exactly. When the report file is saved, the agent additionally sends `document_ready` so the WebUI opens it in the Document Editor.

## Output location

Reports are saved into the chat's workspace folder (`VAF_Projects/<uid[:8]>/<session_id>/`) and appear in the WebUI workspace browser; without session context the legacy `VAF_Research` directory is used.
