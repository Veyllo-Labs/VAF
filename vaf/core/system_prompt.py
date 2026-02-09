"""
VAF System Prompt Manager
Handles dynamic system prompt building based on context and active modules.

The SystemPromptManager provides:
- Core identity prompt (VQ-1 or Generic based on filename)
- Modular prompt sections that activate based on user intent
- Tool documentation injection
- Dynamic context adjustment per conversation turn
"""
from typing import Dict, List, Any, Optional, Union
import re
import os
import logging
from pathlib import Path
from datetime import datetime
from vaf.core.main_persistence import MainPersistenceManager
from vaf.core.platform import Platform
from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log, append_domain_log_block


class SystemPromptManager:
    """
    Manages the system prompt with modular components.
    Dynamically adjusts active modules based on conversation context.
    """
    
    DECAY_START = 3  # Default turns until module deactivates
    # Per-module turn count (higher = stays active longer); missing modules use DECAY_START
    MODULE_DECAY_TURNS: Dict[str, int] = {"coding": 5, "research": 4, "filesystem": 3}
    
    def __init__(self, tools: List[Any] = None, model_name: str = "VQ-1", agent_instance: Any = None, username: str = "admin"):
        """
        Initialize the prompt manager with available tools and model name.
        
        Args:
            tools: List of tool instances available to the agent
            model_name: The name of the underlying AI model
            agent_instance: Reference to the parent Agent instance (for workspace access)
            username: The current user's username
        """
        self.tools = tools or []
        self.active_modules: Dict[str, int] = {}  # module_name -> remaining_turns
        self.user_language: str = "auto"
        self.model_name = model_name
        self.agent = agent_instance # Store reference
        self.username = username
        
        # Initialize Persistence Manager (lazy load in build_prompt if needed, or here)
        try:
            self.mpm = MainPersistenceManager(os.getcwd())
        except Exception:
            self.mpm = None
        
        # ═══════════════════════════════════════════════════════════════════════
        # CORE IDENTITY PROMPTS (Fallbacks - ONLY used if soul.md is missing!)
        # ═══════════════════════════════════════════════════════════════════════
        # IMPORTANT: These fallbacks do NOT mention "AI", "VAF", "model", "assistant"
        # The model should not know what it is - only the soul.md defines its identity.
        # If soul.md exists, these are NEVER used.

        self.fallback_identity = """## Core Principles
- Be helpful, accurate, and concise
- **Clarify Ambiguity:** If critical information is missing (e.g., location for weather, specific file for reading), **ASK FIRST** before calling any tools. Do not guess parameters.
- When uncertain, acknowledge it rather than guessing
- **🔥 ALWAYS RESPOND IN THE USER'S LANGUAGE!**
  - User speaks German → Answer in German!
  - User speaks English → Answer in English!
  - Your thinking/reasoning can be in English, but your FINAL ANSWER must match the user's language!

## 🧠 Thinking Format
**IMPORTANT:** When you need to reason or think through a problem, wrap your thoughts in `<think>` tags:
```
<think>
Your internal reasoning here...
</think>

Your actual response to the user here.
```
- The `<think>` block is for your internal reasoning process
- Content inside `<think>` tags will be shown separately in the UI
- Your final answer should come AFTER the `</think>` tag
- **Tool calls must be in the main response (after `</think>`), not inside `<think>`**, so they are executed
- Keep your thinking concise but thorough
- Execute tasks efficiently using available tools
- Explain your actions briefly when helpful
- **YOU CAN CALL MULTIPLE TOOLS IN ONE RESPONSE!** (e.g., web_search twice for "weather + news")
- **❓ UNINTELLIGIBLE INPUTS:** If you absolutely CANNOT understand the user (severe typos, gibberish) and cannot guess the intent with high confidence:
  - **STOP!** Do NOT hallucinate a task.
  - **SAY SO:** "I'm sorry, I don't understand '[input]'. Could you rephrase that?" or "Entschuldigung, ich verstehe '[input]' nicht. Meinten Sie...?"
  - Do NOT default to "Weather Berlin" or other examples!

## Communication Style
- Professional but approachable
- Use markdown formatting for clarity
- Code blocks with syntax highlighting
- Structured responses for complex topics"""

        # ═══════════════════════════════════════════════════════════════════════
        # MODULAR PROMPT SECTIONS
        # ═══════════════════════════════════════════════════════════════════════
        
        self.modules = {
# ... (rest of class remains same) ...

            "coding": """
## Coding Guidelines
- Write clean, maintainable, well-documented code
- Use type hints in Python where appropriate
- Follow existing project conventions when editing files
- Test changes mentally before applying
- Prefer small, focused edits over large rewrites
- Use appropriate error handling
""",
            
            "research": """
## Research Guidelines

### ⚡ SIMPLE LOOKUPS (Use web_search directly - NO sub-agents, NO workflows!)

**You CAN and SHOULD make multiple web_search calls in ONE response!**

**Examples:**
```
User: "Weather Berlin + latest news"
✅ CORRECT:
   1. web_search("weather Berlin today")
   2. web_search("latest news today")
   → Give user BOTH results immediately!

❌ WRONG: Start research_agent/deep_research workflow
❌ WRONG: Ask "What news topics do you want?"
```

**Simple Lookup Types:**
- **Weather:** web_search("weather [location] today") 
- **News:** web_search("latest news [topic]")
- **Facts:** web_search("what is X")
- **Definitions:** web_search("define X")
- **Quick info:** web_search("X current status")

**CRITICAL:** If user asks for 2-3 simple things (weather, news, facts), 
call web_search 2-3 times IN THE SAME RESPONSE! Don't use workflows or sub-agents!

**TIP:** For multiple web_search calls, use max_results=3 to keep context manageable:
- Example: web_search("weather Berlin", max_results=3) + web_search("news", max_results=3)
- **Trusted/safe search:** If user wants only trusted sources (e.g. "nur vertrauenswürdige Quellen", "only safe/trusted sites"), use web_search(..., trusted_sources_only=true).

---

### 🔬 COMPREHENSIVE RESEARCH (Use research_agent/deep_research):

**Use ONLY when:**
- User explicitly says "recherchiere", "research", "umfassende Analyse"
- Task requires 10+ sources and deep analysis
- Multi-perspective research needed
- Market analysis, trend reports, academic research

**DON'T use for:**
- ❌ Simple weather + news queries
- ❌ Quick fact lookups
- ❌ Current status checks
- ❌ Person info (use web_search!)

---

### 🚨 Critical Rules:

1. **PERSON QUERIES:** Unknown person? → web_search IMMEDIATELY!
2. **VERIFY FACTS:** Don't guess → use web_search
3. **MULTIPLE SIMPLE QUESTIONS:** Call web_search multiple times, NOT research_agent!
4. **NO OVERTHINKING:** "Wetter + Nachrichten" = 2x web_search, NOT workflow!
5. **DON'T ASK FOR CLARIFICATION** if the query is clear enough (e.g., "news" = "latest news")

### Best Practices:
- Make multiple tool calls in ONE response when appropriate
- Cross-reference multiple sources
- Cite sources and provide links
- Distinguish facts from opinions
""",
            
            "filesystem": """
## File System Guidelines
- You can read and analyze local files by using the `librarian_agent`.
- Always confirm before overwriting important files.

### 📂 Handling Local Files and Summaries (CRITICAL)
If the user provides a local file path (e.g., `file:///...` or `C:\\...`) and asks you to read or summarize it:
1.  **DELEGATE to `librarian_agent`**: Call the `librarian_agent` tool.
2.  **FORMULATE the task**: The task for the librarian should be 'read file <path>'.
    - Example: `librarian_agent(task="read file /path/to/your/report.html")`
3.  **DO NOT SAY** "I can't read files" or "I don't have access". Delegate to the `librarian_agent`.

### 🔍 Extracting File Paths from Context
**CRITICAL:** When sub-agent results mention file paths, EXTRACT and USE them directly!

**Common patterns to look for:**
- "📄 Saved to: [path]"
- "Output: [path]"
- "File: [path]"
- "Ausgabe: [path]" (German)
- "Output saved to: successfully to [path]"

**Example:**
```
Sub-Agent Result: "📄 Saved to: /path/to/your/report.html"
User: "Kannst du die Datei ansehen?"

✅ CORRECT: librarian_agent(task="read file /path/to/your/report.html")
❌ WRONG: Ask user for file path (it's already in context!)
❌ WRONG: Say "I can't read files"
```

**Best Practice:** Look for the "🔗 EXTRACTED FILE PATHS" section in sub-agent results for ready-to-use paths!
""",
            
            "git": """
## Git Guidelines
- Write clear, descriptive commit messages
- Use conventional commit format when appropriate
- Check status before commits
- Don't commit sensitive data
""",
            
            "automation": """
## Automation Guidelines
- Validate inputs before executing commands
- Use appropriate timeouts
- Handle errors gracefully
- Log important actions
- Respect system resources
""",
            
            "subagent": """
## Sub-Agent Delegation

### When to Use Sub-Agents:
✅ **research_agent** - ONLY for comprehensive research (10+ sources, detailed analysis)
✅ **coding_agent** - Code generation, analysis, review
✅ **librarian_agent** - File reading, document parsing
✅ **document_agent** - Complex document creation (contracts, reports)

### When NOT to Use Sub-Agents:
❌ **Simple lookups** - Use web_search directly (weather, news, facts)
❌ **Multiple simple questions** - Use web_search multiple times, NOT research_agent
❌ **Quick info** - Direct tools are faster than sub-agents

### Examples:
- ✅ "Research AI market trends 2026" → research_agent (comprehensive)
- ❌ "Weather + News" → web_search (2 calls, NOT research_agent!)
- ❌ "What's the weather today?" → web_search (NOT research_agent!)

Sub-agents run asynchronously - results arrive later
- Don't guess sub-agent results - wait for them
""",
        }
        
        # ═══════════════════════════════════════════════════════════════════════
        # KEYWORD DETECTION FOR MODULE ACTIVATION
        # ═══════════════════════════════════════════════════════════════════════
        
        self.module_keywords = {
            "coding": [
                "code", "function", "class", "debug", "error", "fix", "implement", 
                "refactor", "bug", "syntax", "compile", "program", "script", "method",
                "variable", "loop", "if", "else", "return", "import", "module",
                "python", "javascript", "typescript", "rust", "java", "c++", "html", "css"
            ],
            "research": [
                "search", "find", "research", "look up", "what is", "who is", 
                "how does", "why does", "when did", "where is", "latest", "news",
                "current", "today", "information about", "tell me about",
                # Person queries (MUST trigger web_search!)
                "person", "people", "biography", "born", "founder", "ceo", "creator",
                # Weather/facts queries (simple lookups, NOT automation!)
                "weather", "wetter", "temperature", "temperatur", "forecast", "vorhersage",
                "tomorrow", "morgen", "heute", "how will", "wie wird",
                # German
                "suche", "finde", "recherchiere", "wer ist", "was ist", "wie ist",
                "warum", "wann", "wo ist", "aktuell", "nachrichten", "infos über",
                "sag mir", "kannst du mir sagen", "erzähl mir",
                # Turkish
                "kim", "kimdir", "nedir", "nasıl", "ne zaman", "nerede",
                "söyleyebilir misiniz", "hakkında", "bilgi",
                # Spanish
                "quién es", "qué es", "buscar", "busca", "cómo", "cuándo", "dónde",
                "información sobre", "dime", "clima", "tiempo",
                # French
                "qui est", "qu'est-ce que", "chercher", "rechercher", "comment",
                "quand", "où est", "météo", "informations sur",
                # Portuguese
                "quem é", "o que é", "buscar", "pesquisar", "como", "quando", "onde",
                "informações sobre", "tempo", "clima",
                # Italian
                "chi è", "cos'è", "cercare", "cerca", "come", "quando", "dove",
                "informazioni su", "meteo",
                # Russian
                "кто такой", "кто это", "что такое", "искать", "найти", "как",
                "когда", "где", "погода", "информация о",
                # Chinese
                "谁是", "什么是", "搜索", "查找", "怎么", "什么时候", "在哪里",
                "天气", "关于",
                # Japanese
                "誰", "何", "検索", "調べる", "どうやって", "いつ", "どこ", "天気",
                # Korean
                "누구", "무엇", "검색", "찾기", "어떻게", "언제", "어디", "날씨",
                # Arabic
                "من هو", "ما هو", "بحث", "كيف", "متى", "أين", "طقس",
                # Dutch
                "wie is", "wat is", "zoeken", "weer", "wanneer", "waar",
                # Polish
                "kto to", "co to", "szukaj", "pogoda", "kiedy", "gdzie"
            ],
            "filesystem": [
                "file", "read", "write", "create", "delete", "move", "copy",
                "folder", "directory", "path", "save", "load", "open", "list"
            ],
            "git": [
                "git", "commit", "push", "pull", "branch", "merge", "clone",
                "repository", "repo", "checkout", "stash", "diff", "log"
            ],
            "automation": [
                "automate", "script", "batch", "schedule", "run", "execute",
                "command", "terminal", "shell", "process"
            ],
            "subagent": [
                "research agent", "coding agent", "librarian", "delegate",
                "sub-agent", "subagent", "background task"
            ],
        }
    
    def _format_relative_time(self, ts: float) -> str:
        """Format timestamp as relative time for system prompt.
        Steps: just now -> minutes (< 1h) -> hours (< 24h) -> yesterday (1d) -> days (< 30d) -> months (< 12mo) -> years.
        """
        now = datetime.now().timestamp()
        delta_sec = max(0, now - ts)
        if delta_sec < 60:
            return "just now" if self.user_language != "de" else "gerade eben"
        if delta_sec < 3600:  # < 1 hour: show minutes
            m = int(delta_sec / 60)
            if self.user_language == "de":
                return f"vor {m} Min." if m != 1 else "vor 1 Min."
            return f"{m} min ago" if m != 1 else "1 min ago"
        if delta_sec < 86400:  # < 24 hours: show hours
            h = int(delta_sec / 3600)
            if self.user_language == "de":
                return f"vor {h} Std." if h != 1 else "vor 1 Std."
            return f"{h} hour ago" if h == 1 else f"{h} hours ago"
        if delta_sec < 172800:  # 1 day: yesterday
            return "yesterday" if self.user_language != "de" else "gestern"
        SEC_PER_DAY = 86400
        DAYS_30 = 30 * SEC_PER_DAY
        DAYS_365 = 365 * SEC_PER_DAY
        if delta_sec < DAYS_30:  # 2–29 days: show days
            d = int(delta_sec / SEC_PER_DAY)
            if self.user_language == "de":
                return f"vor {d} Tagen" if d != 1 else "vor 1 Tag"
            return f"{d} days ago" if d != 1 else "1 day ago"
        if delta_sec < DAYS_365:  # 30 days – 1 year: show months (approx 30 days = 1 month)
            months = int(delta_sec / DAYS_30)
            if self.user_language == "de":
                return f"vor {months} Monaten" if months != 1 else "vor 1 Monat"
            return f"{months} months ago" if months != 1 else "1 month ago"
        # 1 year and above: show years
        years = int(delta_sec / DAYS_365)
        if self.user_language == "de":
            return f"vor {years} Jahren" if years != 1 else "vor 1 Jahr"
        return f"{years} years ago" if years != 1 else "1 year ago"

    def _format_channel(self, source: str) -> str:
        """Display name for channel in prompt (WebUI, Telegram, CLI)."""
        s = (source or "").strip().lower()
        if s == "telegram":
            return "Telegram"
        if s == "cli":
            return "CLI"
        return "WebUI"

    def build_prompt(
        self,
        filename: str = None,
        username: Optional[str] = None,
        user_scope_id: Optional[Union[str, Any]] = None,
        current_source: Optional[str] = None,
        last_interaction: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Build the complete system prompt.

        Args:
            filename: Script filename (used to determine VQ-1 vs Generic identity)
            username: Current user's username (for identity.json and User identity block)
            user_scope_id: Current user's scope ID (for cached profile summary from RAG)
            current_source: Current channel: "web", "telegram", or "cli" (for "Currently chatting in ...")
            last_interaction: Optional dict with "ts", "source", "preview" from last_interaction store

        Returns:
            Complete system prompt string
        """
        parts = []
        
        # 1. CORE IDENTITY & PERSONA (Soul)
        
        def _log_soul(msg: str) -> None:
            append_domain_log("prompt", f"[SOUL] {msg}")

        _log_soul("build_prompt persona block entered")

        # Attempt to load Admin Persona (Global for all users)
        persona_loaded = False
        try:
            from vaf.auth.user_workspace import get_user_workspace
            # Soul and Identity bind to the Admin account
            ws = get_user_workspace("admin")
            identity = ws.get_identity()
            soul = ws.get_soul()
            
            # Construct identity
            persona_parts = []
            # Use the name from identity.json - this is the Soul's name, not "AI" or "VAF"
            soul_name = identity.get('name') or 'Assistant'  # Neutral fallback, not "VAF" or "AI"
            soul_emoji = identity.get('emoji') or ''
            # Combine name and emoji in one clean line: "You are **Nobel933DarkBlue** 🧿."
            if soul_emoji:
                persona_parts.append(f"You are **{soul_name}** {soul_emoji}.")
            else:
                persona_parts.append(f"You are **{soul_name}**.")
            persona_parts.append(
                "\nYour Soul determines the way and voice of every answer. "
                "Every answer must be thought through with the Soul – in this personality, not as a generic assistant. "
                "Do not describe yourself as an 'AI assistant', 'trained to help', or list generic capabilities; "
                "answer only in the voice and style defined in your Soul:\n\n"
            )
            persona_parts.append("## Your Personality & Rules (Soul)\n")
            persona_parts.append(soul)
            
            # Technical instructions
            persona_parts.append("\n## Technical Instructions")
            persona_parts.append("### Thinking Format")
            persona_parts.append("IMPORTANT: When you think through a problem, wrap your thoughts in `<think>` tags:")
            persona_parts.append("```\n<think>\nYour internal reasoning here...\n</think>\n\nYour actual response to the user here.\n```")
            parts.append("\n".join(persona_parts))
            persona_loaded = True
            soul_len = len(soul) if soul else 0
            _log_soul(f"Soul/identity loaded name={soul_name} soul_len={soul_len}")
        except Exception as e:
            _log_soul(f"Soul/identity load failed: {e}")

        if not persona_loaded:
            _log_soul("Using fallback identity (soul.md not found)")
            # Use generic fallback that does NOT reveal what model/system this is
            # The model should not know it's "AI", "VAF", "VQ-1" etc. - only soul.md defines identity
            parts.append(self.fallback_identity)

        # Memory Recall instructions (Clawbot-inspired approach)
        parts.append("""
## 🧠 Memory Recall
**BEFORE answering anything about:**
- Prior conversations, work, or decisions
- Dates, deadlines, or scheduled events
- People, names, or relationships
- User preferences, habits, or settings
- Todos, tasks, or projects
- Facts the user told you before

**→ FIRST run `memory_search` with a SHORT query** (e.g. "user name", "project deadline", "last conversation about X").
Then use the results to answer. Do NOT guess from your training data!

### Memory Tools:
| Tool | When to Use | Examples |
|------|-------------|----------|
| `memory_search` | **Look up** any stored facts | "user name", "project X", "last meeting" |
| `memory_save` | **Save** general facts, projects, notes | "Project VAF uses Docker", "Meeting scheduled for Friday" |
| `update_user_identity` | **Save PERSONAL user info** (name, language, preferences, do's/don'ts, main_messenger) | "My name is Mert", "I prefer German", "Send it via Telegram" |
| `send_telegram` | **Send a message to the user via Telegram** (when they asked to receive something there; use if main_messenger is Telegram or user said "via Telegram") | Send summary, result, or notification |
| `send_discord` | **Send a message to the user via Discord** (when they asked to receive something there; use if main_messenger is Discord or user said "via Discord") | Send summary, result, or notification |
| `read_mail` | **Read recent emails** from a connected account (account_id = email address) | Check inbox, summarize emails |
| `send_mail` | **Send an email** from a connected account (account_id, to, subject, body) | Send email on behalf of user |

### When to use which SAVE tool:
- **Personal info about the USER** → `update_user_identity`
  - Name, nickname, language, preferences, do's, don'ts
  - Example: "Ich heiße Mert" → `update_user_identity(name="Mert")`
  - Example: "Antworte immer auf Deutsch" → `update_user_identity(preferred_language="de")`
- **Everything else** → `memory_save`
  - Projects, facts, deadlines, notes, decisions
  - Example: "Merke dir: VAF nutzt PostgreSQL" → `memory_save(content="VAF uses PostgreSQL")`

### Rules:
- **Memory context** for this turn is injected below as `## Memory context (relevant to this query)`. Check it FIRST before calling memory_search.
- Pass SHORT queries to memory_search (e.g. "user preferences", NOT your full reasoning)
- Do NOT use memory_save for lookups - it's for SAVING only
- When user asks "who am I?" or "what do you remember?" → check Memory context below, then memory_search if needed
""")



        # ═══════════════════════════════════════════════════════════════════════
        # 2. CURRENT TIME & DATE
        # ═══════════════════════════════════════════════════════════════════════
        from datetime import datetime
        now = datetime.now()
        
        # Localized date formatting
        if self.user_language == "de":
            # German formatting: Donnerstag, 01.01.2026 12:00:00
            days = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
            day_name = days[now.weekday()]
            time_str = f"Heute ist {day_name}, der {now.strftime('%d.%m.%Y %H:%M:%S')}."
        else:
            # Default/English: Thursday, 2026-01-01 12:00:00
            time_str = f"Today is {now.strftime('%A, %Y-%m-%d %H:%M:%S')}."
            
        parts.append(f"\n## Current Time\n{time_str}\n")

        # ═══════════════════════════════════════════════════════════════════════
        # 2b. LAST INTERACTION & CURRENT CHANNEL (optional)
        # ═══════════════════════════════════════════════════════════════════════
        if current_source or last_interaction:
            line_parts = []
            if last_interaction:
                ts = last_interaction.get("ts")
                src = last_interaction.get("source", "web")
                preview = (last_interaction.get("preview") or "").strip()
                display_name = username
                if username:
                    try:
                        from vaf.auth.user_workspace import get_user_workspace
                        ws = get_user_workspace(username)
                        display_name = ws.get_user_identity().get("name", username)
                    except Exception:
                        display_name = username
                else:
                    display_name = "the user"
                rel = self._format_relative_time(ts) if ts is not None else ""
                chan = self._format_channel(src)
                line_parts.append(f"Last user {display_name} interaction: {rel} via {chan}.")
                if preview:
                    line_parts.append(f" (About: {preview})")
            if current_source:
                chan = self._format_channel(current_source)
                line_parts.append(f" Currently chatting in {chan}.")
            if line_parts:
                block = "".join(line_parts).strip()
                if block:
                    parts.append(f"\n## Session context\n{block}\n")

        # ═══════════════════════════════════════════════════════════════════════
        # 3. WORKSPACE CONTEXT (CWD Awareness)
        # ═══════════════════════════════════════════════════════════════════════
        if self.agent and hasattr(self.agent, 'workspace'):
            ws_info = self.agent.workspace.get_context_info()
            parts.append(f"""
## 📂 WORKSPACE CONTEXT
**Current Working Directory:** `{ws_info['cwd']}`
**Project Root:** `{ws_info['project_root']}`
**Inside Project:** {'Yes' if ws_info['is_in_project'] else 'No'}
""")

        # ═══════════════════════════════════════════════════════════════════════
        # 4. ACTIVE MODULES
        # ═══════════════════════════════════════════════════════════════════════
        active_module_parts = []
        # Sort for stable prompt order
        for module_name in sorted(self.active_modules.keys()):
            if module_name in self.modules:
                active_module_parts.append(self.modules[module_name])
        
        if active_module_parts:
            parts.extend(active_module_parts)
        
        # ═══════════════════════════════════════════════════════════════════════
        # 4. PERSISTENT CONTEXT INJECTION (Brain)
        # ═══════════════════════════════════════════════════════════════════════
        if self.mpm:
            try:
                persistent_context = self.mpm.build_context_injection()
                parts.append(persistent_context)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════════════════
        # 5. TOOL DOCUMENTATION
        # ═══════════════════════════════════════════════════════════════════════
        if self.tools:
            tool_docs = self._build_tool_documentation()
            if tool_docs:
                parts.append(tool_docs)
        
        # ═══════════════════════════════════════════════════════════════════════
        # 6. USER IDENTITY & INSTRUCTIONS (High Priority - End of Prompt)
        # ═══════════════════════════════════════════════════════════════════════
        if username or user_scope_id:
            user_data = {}
            known_facts = ""
            
            if username:
                try:
                    from vaf.auth.user_workspace import get_user_workspace
                    ws = get_user_workspace(username)
                    ui = ws.get_user_identity()
                    user_data["name"] = ui.get("name", username)
                    if ui.get("preferred_language"):
                        user_data["preferred_language"] = ui.get("preferred_language")
                    if ui.get("preferences"):
                        user_data["preferences"] = ui.get("preferences")
                    if ui.get("dos"):
                        user_data["dos"] = ui.get("dos")
                    if ui.get("donts"):
                        user_data["donts"] = ui.get("donts")
                    if ui.get("main_messenger") and str(ui.get("main_messenger")).strip().lower() in ("telegram", "discord", "slack"):
                        user_data["main_messenger"] = (ui.get("main_messenger") or "").strip().lower()
                except Exception:
                    user_data["name"] = username

            if user_scope_id:
                try:
                    scope_str = str(user_scope_id)
                    cache_dir = Path(Config.APP_DIR) / "user_profile_cache"
                    cache_file = cache_dir / f"{scope_str}.txt"
                    if cache_file.exists():
                        known_facts = cache_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

            # Construct JSON-like block
            import json
            user_json = json.dumps(user_data, indent=2, ensure_ascii=False)
            
            identity_block = f"""
## 👤 CURRENT USER CONTEXT (High Priority)
You are talking to the following user. 
**CRITICAL:** You MUST adapt your personality, language, and behavior to this profile.

```json
{user_json}
```
"""
            if known_facts:
                identity_block += f"\n**Known facts from memory:**\n{known_facts}\n"

            identity_block += """
**INSTRUCTIONS:**
1. **CHECK** the `dos` and `donts` list above before generating every response.
2. **ADAPT** your tone to the `preferences` (e.g. if 'concise', be concise).
3. **LANGUAGE:** If `preferred_language` is set, **ALWAYS** answer in that language (unless explicitly asked otherwise).
4. **GREETING:** If this is the start of a conversation, greet the user by their `name` naturally (don't say "Hello [Name]", say "Hey [Name]" or similar based on your Soul).
"""
            # Messaging connections: only when at least one channel is available
            try:
                from vaf.core.messaging_connections import get_messaging_connections
                conn = get_messaging_connections(username=username, user_scope_id=user_scope_id)
                avail = conn.get("available") or []
                main = conn.get("main_messenger")
                if avail:
                    channel_names = [c.capitalize() for c in avail]
                    identity_block += "\n## Messaging connections (proactive messages)\n"
                    identity_block += f"This user has the following messaging channels available for proactive messages: {', '.join(channel_names)}.\n"
                    if main:
                        identity_block += f"Preferred channel for proactive messages: {main.capitalize()}.\n"
                    else:
                        identity_block += "Preferred channel is not set yet.\n"
                    identity_block += (
                        "When the user asks you to send them something (e.g. a summary, a file, or a notification), "
                        "if preferred channel is not set, ask once: e.g. \"Soll ich es dir per Discord, Telegram oder Slack schicken?\" / \"Should I send it via Discord, Telegram or Slack?\". "
                        "Store their answer with `update_user_identity(main_messenger=\"telegram\")` (or discord/slack). "
                        "Then use the matching tool: `send_telegram`, `send_discord`, or `send_slack` depending on the preferred channel or user request (e.g. use send_telegram when main_messenger is Telegram or they said \"via Telegram\").\n"
                    )
            except Exception:
                pass
            parts.append(identity_block)

        full_prompt = "\n".join(parts)
        try:
            append_domain_log_block("prompt", "[SYSTEM_FULL]", full_prompt.splitlines())
        except Exception as e:
            logging.warning("System prompt full log write failed: %s", e)
        return full_prompt
    
    def _build_tool_documentation(self) -> str:
        """Build tool documentation section. When agent has _active_tools set, only document those tools."""
        tool_names = []
        tool_descriptions = []
        tools_to_doc = self.tools
        if self.agent and getattr(self.agent, "_active_tools", None) is not None:
            active = set(self.agent._active_tools)
            tools_to_doc = [t for t in self.tools if getattr(t, "name", None) in active]
        
        for tool in tools_to_doc:
            name = None
            description = None
            
            # Try different ways to get tool info
            if hasattr(tool, 'name'):
                name = tool.name
            elif hasattr(tool, '__class__'):
                name = tool.__class__.__name__
            
            if hasattr(tool, 'description'):
                description = tool.description
            elif hasattr(tool, '__doc__') and tool.__doc__:
                description = tool.__doc__.split('\n')[0]  # First line only
            
            if name:
                tool_names.append(name)
                if description:
                    tool_descriptions.append(f"- **{name}**: {description}")
        
        if not tool_names:
            return ""
        
        # Short summary for prompt (full docs are in tool definitions)
        return f"""
## Available Tools
You have access to {len(tool_names)} tools: {', '.join(sorted(tool_names))}

Use tools proactively to accomplish tasks. Don't ask for permission - just use them when appropriate.

### ⚡ Multiple Tool Calls in ONE Response
**You CAN and SHOULD call multiple tools in a SINGLE response when the user asks for multiple simple things!**

**Example:**
- User: "Weather Berlin + latest news"
- ✅ Call web_search("weather Berlin") AND web_search("latest news") in ONE response
- ❌ DON'T call just one tool and wait
- ❌ DON'T ask for clarification if the query is clear
- ❌ DON'T start workflows/sub-agents for simple lookups"""
    
    def analyze_context(self, user_input: str, language: str = "auto") -> None:
        """
        Analyze user input and activate relevant modules.
        
        This is called before each response to dynamically adjust
        the system prompt based on what the user is asking about.
        
        Args:
            user_input: The user's message
            language: Detected user language (iso code)
        """
        if not user_input:
            return
        
        self.user_language = language
        user_lower = user_input.lower()
        
        # 1. Decay existing modules
        # We iterate over a list of keys because we might delete items
        for module in list(self.active_modules.keys()):
            self.active_modules[module] -= 1
            if self.active_modules[module] <= 0:
                del self.active_modules[module]
        
        # 2. Check keywords and activate/reset modules
        for module_name, keywords in self.module_keywords.items():
            # Activate module if any keyword is found
            if any(kw in user_lower for kw in keywords):
                # Reset counter to module-specific or default decay turns
                self.active_modules[module_name] = self.MODULE_DECAY_TURNS.get(module_name, self.DECAY_START)
    
    def get_active_modules(self) -> List[str]:
        """Get list of currently active module names."""
        return list(self.active_modules.keys())
    
    def activate_module(self, module_name: str) -> None:
        """Manually activate a module."""
        if module_name in self.modules:
            self.active_modules[module_name] = self.MODULE_DECAY_TURNS.get(module_name, self.DECAY_START)
    
    def deactivate_module(self, module_name: str) -> None:
        """Manually deactivate a module."""
        if module_name in self.active_modules:
            del self.active_modules[module_name]
    
    def reset_modules(self) -> None:
        """Reset all modules to inactive."""
        self.active_modules = {}

