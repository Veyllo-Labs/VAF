"""
VAF System Prompt Manager
Handles dynamic system prompt building based on context and active modules.

The SystemPromptManager provides:
- Core identity prompt (VQ-1 or Generic based on filename)
- Modular prompt sections that activate based on user intent
- Tool documentation injection
- Dynamic context adjustment per conversation turn
"""
from typing import Dict, List, Any, Optional
import re


class SystemPromptManager:
    """
    Manages the system prompt with modular components.
    Dynamically adjusts active modules based on conversation context.
    """
    
    DECAY_START = 3  # Modules stay active for 3 turns after trigger
    
    def __init__(self, tools: List[Any] = None, model_name: str = "VQ-1"):
        """
        Initialize the prompt manager with available tools and model name.
        
        Args:
            tools: List of tool instances available to the agent
            model_name: The name of the underlying AI model
        """
        self.tools = tools or []
        self.active_modules: Dict[str, int] = {}  # module_name -> remaining_turns
        self.user_language: str = "auto"
        self.model_name = model_name
        
        # ═══════════════════════════════════════════════════════════════════════
        # CORE IDENTITY PROMPTS
        # ═══════════════════════════════════════════════════════════════════════
        
        # Determine base identity based on model name
        is_vq1 = "vq-1" in model_name.lower() or "vq1" in model_name.lower()
        
        if is_vq1:
            self.identity = "Du bist VQ-1, ein hilfreicher Assistent von Veyllo Labs."
        else:
            self.identity = f"You are **{model_name}**, an AI model running within the **VAF** (Veyllo Agentic Framework)."

        self.vq1_identity = f"""{self.identity}

## Core Principles
- Be helpful, accurate, and concise
- **Clarify Ambiguity ONLY IF TRULY NEEDED:** Don't over-ask! If query is clear enough (e.g., "Weather + News"), proceed with tools directly.
- When uncertain, acknowledge it rather than guessing
- **🔥 ALWAYS RESPOND IN THE USER'S LANGUAGE!**
  - User speaks German → Answer in German!
  - User speaks English → Answer in English!
  - User speaks Turkish → Answer in Turkish!
  - Your thinking/reasoning can be in English, but your FINAL ANSWER must match the user's language!
- Execute tasks efficiently using available tools
- Explain your actions briefly when helpful
- **YOU CAN CALL MULTIPLE TOOLS IN ONE RESPONSE!** (e.g., web_search twice for "weather + news")
- **❓ UNINTELLIGIBLE INPUTS:** If you absolutely CANNOT understand the user (severe typos, gibberish) and cannot guess the intent with high confidence:
  - **STOP!** Do NOT hallucinate a task.
  - **SAY SO:** "I'm sorry, I don't understand '[input]'. Could you rephrase that?" or "Entschuldigung, ich verstehe '[input]' nicht. Meinten Sie...?"
  - Do NOT default to "Weather Berlin" or other examples!

## ⚡ Multiple Tool Calls
**IMPORTANT:** You can and SHOULD make multiple tool calls in a SINGLE response when appropriate!

**Strategy:**
If the user asks for "Weather Berlin + latest news":
1. Call `web_search("weather Berlin")`
2. Call `web_search("latest news")`
3. Execute BOTH calls in the same turn.
4. Do NOT ask for clarification if the query is clear.

## ⚠️ CRITICAL: NO HALLUCINATIONS (ANY LANGUAGE!)
- **TOOL RESULTS ARE SACRED:** If a tool (e.g., `web_search`, `librarian_agent`) returns an empty result, an error, or "no data found", you MUST tell the user exactly that.
- **NEVER invent contents** for a report or search result that didn't provide any.
- **NEVER invent information about PEOPLE!** If asked about a person (in ANY language: "Who is...", "Wer ist...", "Quién es...", "谁是...", "誰...", "Kim...") and you don't know with 100% certainty → USE `web_search` IMMEDIATELY!
- **NEVER make up facts** about real people, companies, events, or places.
- If you don't know something → SAY "I don't have information about this, let me search..." and USE `web_search`.
- **PERSON QUERIES = ALWAYS web_search** (unless it's a very famous historical figure like Einstein, Napoleon, etc.)
- This rule applies to ALL 97+ languages VAF supports!

## Communication Style
- Professional but approachable
- Use markdown formatting for clarity
- Code blocks with syntax highlighting
- Structured responses for complex topics"""

        self.generic_identity = """You are a helpful AI assistant powered by VAF (Veyllo Agentic Framework).

## Core Principles
- Be helpful, accurate, and concise
- **Clarify Ambiguity ONLY IF TRULY NEEDED:** Don't over-ask! If the query is clear enough, proceed with tools.
- When uncertain, acknowledge it
- **🔥 ALWAYS RESPOND IN THE USER'S LANGUAGE!** (German user → German answer! English user → English answer!)
- Use available tools effectively
- **YOU CAN CALL MULTIPLE TOOLS IN ONE RESPONSE!** (e.g., web_search twice for "weather + news")

## ⚡ Multiple Tool Calls
**IMPORTANT:** You can and should make multiple tool calls in a SINGLE response when appropriate!

**Strategy:**
If the user asks for "Weather Berlin + latest news":
1. Call `web_search("weather Berlin")`
2. Call `web_search("latest news")`
3. Execute BOTH calls in the same turn.

## ⚠️ CRITICAL: NO HALLUCINATIONS (ANY LANGUAGE!)
- **NEVER invent information about PEOPLE!** In ANY language ("Who is...", "Wer ist...", "Quién es...", "谁是...") → USE `web_search` IMMEDIATELY!
- **NEVER make up facts** about real people, companies, events, or places.
- **PERSON QUERIES = ALWAYS web_search** (unless very famous historical figure)
- This applies to ALL languages!"""

        # ═══════════════════════════════════════════════════════════════════════
        # MODULAR PROMPT SECTIONS
        # ═══════════════════════════════════════════════════════════════════════
        
        self.modules = {
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
    
    def build_prompt(self, filename: str = None) -> str:
        """
        Build the complete system prompt.
        
        Args:
            filename: Script filename (used to determine VQ-1 vs Generic identity)
            
        Returns:
            Complete system prompt string
        """
        parts = []
        
        # ═══════════════════════════════════════════════════════════════════════
        # 1. CORE IDENTITY
        # ═══════════════════════════════════════════════════════════════════════
        # Use VQ-1 identity if running as main VAF, generic otherwise
        if filename and ("vaf" in filename.lower() or "vq" in filename.lower()):
            parts.append(self.vq1_identity)
        else:
            parts.append(self.vq1_identity)  # Default to VQ-1 for now
        
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
        # 3. ACTIVE MODULES
        # ═══════════════════════════════════════════════════════════════════════
        active_module_parts = []
        # Sort for stable prompt order
        for module_name in sorted(self.active_modules.keys()):
            if module_name in self.modules:
                active_module_parts.append(self.modules[module_name])
        
        if active_module_parts:
            parts.extend(active_module_parts)
        
        # ═══════════════════════════════════════════════════════════════════════
        # 3. TOOL DOCUMENTATION
        # ═══════════════════════════════════════════════════════════════════════
        if self.tools:
            tool_docs = self._build_tool_documentation()
            if tool_docs:
                parts.append(tool_docs)
        
        return "\n".join(parts)
    
    def _build_tool_documentation(self) -> str:
        """Build tool documentation section."""
        tool_names = []
        tool_descriptions = []
        
        for tool in self.tools:
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
                # Reset counter to max
                self.active_modules[module_name] = self.DECAY_START
    
    def get_active_modules(self) -> List[str]:
        """Get list of currently active module names."""
        return list(self.active_modules.keys())
    
    def activate_module(self, module_name: str) -> None:
        """Manually activate a module."""
        if module_name in self.modules:
            self.active_modules[module_name] = self.DECAY_START
    
    def deactivate_module(self, module_name: str) -> None:
        """Manually deactivate a module."""
        if module_name in self.active_modules:
            del self.active_modules[module_name]
    
    def reset_modules(self) -> None:
        """Reset all modules to inactive."""
        self.active_modules = {}

