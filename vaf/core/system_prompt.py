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
    
    def __init__(self, tools: List[Any] = None, model_name: str = "VQ-1"):
        """
        Initialize the prompt manager with available tools and model name.
        
        Args:
            tools: List of tool instances available to the agent
            model_name: The name of the underlying AI model
        """
        self.tools = tools or []
        self.active_modules: Dict[str, bool] = {}
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
- **Clarify Ambiguity:** If a user's request is vague or missing critical details (e.g., location for weather, specific file for editing), ASK for clarification BEFORE using tools.
- When uncertain, acknowledge it rather than guessing
- Always respond in the user's language
- Execute tasks efficiently using available tools
- Explain your actions briefly when helpful

## ⚠️ CRITICAL: NO HALLUCINATIONS (ANY LANGUAGE!)
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
- **Clarify Ambiguity:** If a user's request is vague or missing critical details, ASK for clarification BEFORE using tools.
- When uncertain, acknowledge it
- Respond in the user's language
- Use available tools effectively

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
- **🚨 PERSON QUERIES (ANY LANGUAGE!):** When asked about a person ("Who is...", "Wer ist...", "Quién es...", "Qui est...", "谁是...", "Kim...", etc.) and you don't recognize them with 100% certainty → CALL `web_search` IMMEDIATELY! Do NOT guess or invent information!
- **VERIFY FACTS:** Do NOT guess or hallucinate information about people, places, or specific entities. If uncertain, **MUST** use `web_search`.
- **SIMPLE QUESTIONS → web_search:** For simple info questions (weather, facts, news, "what is X?"), use `web_search` DIRECTLY. Do NOT create automations for simple lookups!
- **Refine Queries:** If the user's query is too broad (e.g., "weather tomorrow" without location), ask for specifics BEFORE searching.
- Use web_search tool for current/real-time information
- Cross-reference multiple sources when possible
- Cite sources and provide links
- Distinguish between facts and opinions
- Be thorough but concise in summaries
""",
            
            "filesystem": """
## File System Guidelines
- Always confirm before overwriting important files
- Use appropriate file encodings (UTF-8 default)
- Create directories as needed
- Use relative paths when possible
- Check file existence before reading
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
- Delegate complex research to research_agent
- Delegate code analysis to coding_agent
- Delegate documentation tasks to librarian_agent
- Sub-agents run asynchronously - results arrive later
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
        for module_name, is_active in self.active_modules.items():
            if is_active and module_name in self.modules:
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
You have access to {len(tool_names)} tools: {', '.join(tool_names[:10])}{'...' if len(tool_names) > 10 else ''}

Use tools proactively to accomplish tasks. Don't ask for permission - just use them when appropriate."""
    
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
        
        # Check each module's keywords
        for module_name, keywords in self.module_keywords.items():
            # Activate module if any keyword is found
            is_relevant = any(kw in user_lower for kw in keywords)
            self.active_modules[module_name] = is_relevant
    
    def get_active_modules(self) -> List[str]:
        """Get list of currently active module names."""
        return [name for name, active in self.active_modules.items() if active]
    
    def activate_module(self, module_name: str) -> None:
        """Manually activate a module."""
        if module_name in self.modules:
            self.active_modules[module_name] = True
    
    def deactivate_module(self, module_name: str) -> None:
        """Manually deactivate a module."""
        self.active_modules[module_name] = False
    
    def reset_modules(self) -> None:
        """Reset all modules to inactive."""
        self.active_modules = {}

