# VAF Architecture Documentation

This document provides a deep dive into VAF's internal architecture, focusing on the context management system, sub-agents, and token optimization strategies.

## Table of Contents

1. [Dynamic System Prompt](#dynamic-system-prompt)
2. [Context Management](#context-management)
3. [Sub-Agents](#sub-agents)
4. [Web Search & Deep Research](#web-search--deep-research)
5. [Context Compression](#context-compression)

---

## Dynamic System Prompt

### The Problem

Traditional LLM agents use **static system prompts** that load all instructions at once:

```
┌─────────────────────────────────────────────────────────────┐
│  STATIC SYSTEM PROMPT (~4,000 tokens)                       │
│  ├─ Identity & Time                                         │
│  ├─ ALL Tool Rules (even if not needed)                     │
│  ├─ ALL Examples (even if not relevant)                     │
│  ├─ Automation Rules (even for simple chat)                 │
│  └─ Coding Rules (even for web search)                      │
└─────────────────────────────────────────────────────────────┘

Result: Start at 50% context usage before user says anything!
```

### The Solution: Prompt Router

VAF uses a **modular system prompt** that loads only what's needed:

```
┌─────────────────────────────────────────────────────────────┐
│              DYNAMIC SYSTEM PROMPT ARCHITECTURE             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  🔷 CORE MODULE (Always Active) ~400 tokens         │   │
│  │  ├─ Identity (VQ-1 / Model Name)                    │   │
│  │  ├─ Time, OS, CWD                                   │   │
│  │  ├─ Language Settings                               │   │
│  │  └─ Base Rules (call tools, don't talk about them)  │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                 │
│           ┌──────────────┼──────────────┐                  │
│           ▼              ▼              ▼                  │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐           │
│  │ 🔍 RESEARCH │ │ 💻 CODER   │ │📁 LIBRARIAN│           │
│  │   MODULE    │ │    MODULE   │ │   MODULE    │            │
│  │ (~400 tok)  │ │  (~500 tok) │ │ (~300 tok)  │            │
│  │             │ │             │ │             │            │
│  │ web_search  │ │ coding_agent│ │librarian    │            │
│  │ rules       │ │ code rules  │ │file queries │            │
│  └─────────────┘ └─────────────┘ └─────────────┘           │
│                          │                                 │
│                          ▼                                 │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  ⚡ AUTOMATION MODULE (On-Demand) ~300 tokens       │   │
│  │  └─ Scheduling rules, create_automation tool        │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Token Savings

| Scenario | Static Prompt | Dynamic Prompt | Savings |
|----------|--------------|----------------|---------|
| Small Talk ("Hello!") | ~4,000 tokens | ~800 tokens | **80%** |
| Web Search | ~4,000 tokens | ~1,200 tokens | **70%** |
| Coding Task | ~4,000 tokens | ~1,400 tokens | **65%** |
| File Analysis | ~4,000 tokens | ~1,100 tokens | **72%** |
| All Modules Active | ~4,000 tokens | ~2,400 tokens | **40%** |

### Sticky Context with Decay

Modules don't disappear immediately - they use a **decay mechanism** to prevent context "flicker":

```python
# Module stays active for 3 messages after being triggered
DECAY_START = 3

# Example conversation:
User: "Create a Python script"     → 💻 Coder activated (3 turns remaining)
User: "Add error handling"         → 💻 Coder stays active (reset to 3)
User: "What's the weather?"        → 💻 Coder (2), 🔍 Researcher (3)
User: "Thanks!"                    → 💻 Coder (1), 🔍 Researcher (2)
User: "Bye"                        → 💻 Coder removed, 🔍 Researcher (1)
```

### Implementation

The system is implemented in `vaf/core/system_prompt.py`:

```python
class SystemPromptManager:
    DECAY_START = 3  # Messages until module deactivates
    
    def __init__(self, agent_tools):
        self.tools = agent_tools
        self.active_modules = {}  # module_name -> remaining_turns
        
    def analyze_context(self, user_input: str):
        """Analyze input and activate relevant modules."""
        # 1. Decay existing modules
        for mod in list(self.active_modules.keys()):
            self.active_modules[mod] -= 1
            if self.active_modules[mod] <= 0:
                del self.active_modules[mod]
        
        # 2. Activate modules based on keyword triggers
        if any(kw in user_input.lower() for kw in ["code", "script", "create"]):
            self.active_modules["coder"] = self.DECAY_START
        if any(kw in user_input.lower() for kw in ["search", "who is", "weather"]):
            self.active_modules["researcher"] = self.DECAY_START
        # ... etc
    
    def build_prompt(self, filename: str) -> str:
        """Build prompt from active modules only."""
        prompt = self._build_core()  # Always included
        
        if "researcher" in self.active_modules:
            prompt += MODULE_RESEARCHER
        if "coder" in self.active_modules:
            prompt += MODULE_CODER
        # ... etc
        
        return prompt
```

## Context Management

### Overview

VAF uses a **Cursor-style context management system** that tracks, compresses, and archives conversation history.

```
┌─────────────────────────────────────────────────────────────┐
│                    CONTEXT MANAGEMENT                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📊 TOKEN TRACKING                                  │    │
│  │  ├─ Text: ~4 chars/token                            │    │
│  │  ├─ Code: ~3.5 chars/token                          │    │
│  │  ├─ Tools: DYNAMIC CALCULATION (Realtime)           │    │
│  │  │  └─ Calculates exact overhead of active tools    │    │
│  │  └─ 10% safety margin for special tokens            │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  🎯 INTENT CONTEXT                                  │   │
│  │  ├─ Primary Goal                                    │    │
│  │  ├─ Sub-tasks                                       │    │
│  │  ├─ Constraints                                     │    │
│  │  └─ Keywords                                        │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📁 STATE CONTEXT (RAM / Compression)               │   │
│  │  ├─ Files created/modified/read                     │    │
│  │  ├─ Errors encountered                              │    │
│  │  ├─ Tools used                                      │    │
│  │  ├─ Key decisions                                   │    │
│  │  └─ Code snippets                                   │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  💾 ARCHIVE                                         │   │
│  │  └─ Full history saved to ~/.vaf/context_archive/   │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### RAG and Memory Context (Pre-Generation Injection)

RAG retrieval for the current turn is done in the **input processing phase**, strictly **before** the LLM is called. There is no RAG trigger on the model output stream.

1. **When**: Every chat entry point (Web UI via headless runner, gateway, automation) runs a memory search on the **user message** before calling the agent. The retrieved chunks are passed as `memory_context` into `chat_step`.
2. **Where it appears**: The model receives context in the first prompt via the system block **"## Memory context (relevant to this query)"**. That block is filled with the pre-retrieved snippets (or a placeholder if none were found).
3. **memory_search tool**: The `memory_search` tool is for **follow-up or different short queries only** (e.g. "user name", "user preferences"). It must **not** be used with full thinking or `<think>` content; the tool rejects such queries and instructs the model to use the Memory context block for this turn.

### Persistent Hybrid Architecture (Agatic vNext)

In addition to the RAM-based context above, VAF now employs a **Persistent Hybrid Architecture** that maintains a "brain on disk". This ensures the Main Agent knows "where it is" and "what it's doing" even after restarts.

```
┌─────────────────────────────────────────────────────────────┐
│              PERSISTENT LAYER (.vaf/main/)                  │
├─────────────────────────────────────────────────────────────┤
│  ├─ user_intent.md       (The "North Star")                 │
│  ├─ team_state.json      (Orchestration Status)             │
│  └─ working_memory.json  (Scratchpad / Plans)               │
└─────────────────────────────────────────────────────────────┘
```

**Files managed by `MainPersistenceManager`:**
- **`user_intent.md`**: Stores the original user request. Read-only for sub-agents, updated only by user input. Acts as an "Intent Lock".
- **`team_state.json`**: Tracks the status of all sub-agents (`running`, `completed`, `needs_clarification`).
- **`working_memory.json`**: Persists notes and plans across sessions.

### Workspace Awareness

VAF is now **CWD-Aware** (Current Working Directory). It understands the difference between "Scaffolding" (New Project) and "Engineering" (Existing Project).

**`WorkspaceManager` Logic:**
1. **Detection**: Scans for `.git`, `.vaf`, `package.json`, etc.
2. **Anchor**: If inside a project, VAF anchors the Coder Agent to the current directory.
3. **Scaffold**: If the user asks to "create a new project", VAF breaks out to `~/Documents/VAF_Projects/`.

### Intent Locking & Validation

To prevent "Context Drift" (where the agent gets distracted by sub-agent status reports), VAF implements an **Intent Lock**:

1. **Snapshot**: User intent is saved immediately to `user_intent.md`.
2. **Background Intel**: Sub-agent results are injected as "Background Intelligence", explicitly marked as *supporting data*, not new commands.
3. **Final Answer Validator**: Before answering, VAF checks:
   - Is this a "Meta-Response" (e.g., "I have processed the files")?
   - Does it answer the original question?
   - **Action**: If it's just meta-talk, the system blocks the output and forces the agent to provide the actual answer.

### Configuration

- **Default Limit**: 8,192 tokens (configurable via `vaf settings`)


VAF uses specialized sub-agents for complex tasks. Each sub-agent has its **own isolated context**.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    MAIN AGENT                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Context: ~1,500 tokens (dynamic prompt)             │   │
│  └──────────────────────────────────────────────────────┘   │
│                          │                                  │
│       ┌──────────────────┼──────────────────┐               │
│       ▼                  ▼                  ▼               │
│  ┌─────────┐       ┌─────────┐       ┌─────────┐            │
│  │ CODER   │       │RESEARCH │       │LIBRARIAN│            │
│  │ AGENT   │       │ AGENT   │       │ AGENT   │            │
│  │         │       │         │       │         │            │
│  │ Own CTX │       │ Own CTX │       │ Own CTX │            │
│  │ Own Hist│       │ Own Hist│       │ Own Hist│            │
│  └─────────┘       └─────────┘       └─────────┘            │
│       │                  │                  │               │
│       └──────────────────┼──────────────────┘               │
│                          ▼                                  │
│              Only results returned to main                  │
└─────────────────────────────────────────────────────────────┘
```

### Coder Agent (`coding_agent`)

The Coder Agent uses **hierarchical contexts** where each task gets its own isolated context:

```
┌─────────────────────────────────────────────────────────────┐
│              CODER AGENT - HIERARCHICAL CONTEXT             │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────┐    │
│  │  MAIN CONTEXT (Planning Phase)                      │    │
│  │  ├─ System Prompt with template rules               │    │
│  │  ├─ Task List from set_todos                        │    │
│  │  └─ Template Files Info                             │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│        ┌─────────────────┼─────────────────┐                │
│        ▼                 ▼                 ▼                │
│  ┌───────────┐    ┌───────────┐    ┌───────────┐            │
│  │  TASK 1   │    │  TASK 2   │    │  TASK N   │            │
│  │ CONTEXT   │    │ CONTEXT   │    │ CONTEXT   │            │
│  │           │    │           │    │           │            │
│  │ Fresh     │    │ Fresh     │    │ Fresh     │            │
│  │ Manager   │    │ Manager   │    │ Manager   │            │
│  │           │    │           │    │           │            │
│  │ Task-only │    │ Task-only │    │ Task-only │            │
│  │ History   │    │ History   │    │ History   │            │
│  └───────────┘    └───────────┘    └───────────┘            │
└─────────────────────────────────────────────────────────────┘
```

**Benefits:**
- ✅ **No Context Pollution**: Each task starts fresh
- ✅ **Better Focus**: Task-specific prompts
- ✅ **Efficient**: Only relevant context per task
- ✅ **Scalable**: Handle many tasks without overflow

**Implementation** (`vaf/tools/coder.py`):

```python
def create_fresh_context_for_task(task_idx: int, current_task: str):
    """Creates a completely fresh context for a new task."""
    # Create NEW ContextManager for this task (isolated)
    task_context_manager = ContextManager(max_tokens=max_tokens)
    
    # Build task-specific system prompt
    fresh_system_prompt = f"""You are a Senior software developer Sub-agent.
    
## YOUR CURRENT TASK (Task {task_idx + 1})
**{current_task}**

Focus ONLY on this task. When finished, call `task_done`."""
    
    # Create fresh history (only system + user, no old history)
    task_history = [
        {"role": "system", "content": fresh_system_prompt},
        {"role": "user", "content": f"Start working on: {current_task}"}
    ]
    
    return task_context_manager, task_history
```

### Research Agent (`research_agent`)

The Research Agent processes topics **section-by-section** to avoid context overflow:

```
┌─────────────────────────────────────────────────────────────┐
│              RESEARCH AGENT - TOPIC BY TOPIC                │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   User: "Research Machine Learning"                         │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📋 TOPIC SPLITTER                                  │   │
│  │  └─ Splits into 10 sections (Intro, Methods, ...)   │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│    ┌─────────────────────┼─────────────────────┐            │
│    ▼                     ▼                     ▼            │
│  ┌───────┐          ┌───────┐          ┌───────┐            │
│  │ SEC 1 │          │ SEC 2 │          │ SEC N │            │
│  │ Intro │          │Methods│          │  ...  │            │
│  ├───────┤          ├───────┤          ├───────┤            │
│  │🔍 Web │         │🔍 Web │          │🔍 Web │           │
│  │ Search│          │ Search│          │ Search│            │
│  │🧠 LLM │         │🧠 LLM │          │🧠 LLM │           │
│  │ Call  │          │ Call  │          │ Call   │           │
│  │📝 Text│         │📝 Text│          │📝 Text│            │
│  └───────┘          └───────┘          └───────┘            │
│  ISOLATED           ISOLATED           ISOLATED             │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📄 FINAL HTML REPORT                               │    │
│  │  └─ All sections assembled                          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

**Key Design Principle** (from `vaf/tools/research_agent.py`):

```python
"""
VAF Research Agent - Topic-by-topic web research with bounded context.

This tool is designed to avoid "exceed_context_size_error" by:
- Splitting a research task into sections (topics)
- Running web_search per section
- Calling the model per section with only that section's context
- Assembling a final HTML report
"""
```

### Librarian Agent (`librarian_agent`)

The Librarian Agent handles file system queries with its own context:

- Own ContextManager
- Proactive compression at 85%
- Specialized for file operations

---

## Web Search & Deep Research

### Web Search Context Isolation

Each web page is processed in a **separate LLM call** to prevent context pollution:

```
┌─────────────────────────────────────────────────────────────┐
│                    WEB SEARCH FLOW                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│             User: "Who is Mert Can Elsner"                  │ 
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  🔍 SEARCH ENGINE                                   │   │
│  │  └─ Returns 5 URLs                                  │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│    ┌─────────────────────┼─────────────────────┐            │
│    ▼                     ▼                     ▼            │
│  ┌───────┐          ┌───────┐          ┌───────┐            │
│  │ URL 1 │          │ URL 2 │          │ URL N │            │
│  ├───────┤          ├───────┤          ├───────┤            │
│  │Fetch  │          │Fetch  │          │Fetch  │            │
│  │Page   │          │Page   │          │Page   │            │
│  ├───────┤          ├───────┤          ├───────┤            │
│  │Separate          │Separate          │Separate            │
│  │LLM Call          │LLM Call          │LLM Call            │
│  │(~400 tok)        │(~400 tok)        │(~400 tok)          │
│  └───────┘          └───────┘          └───────┘            │
│  ISOLATED           ISOLATED           ISOLATED             │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📝 SUMMARIZED RESULTS                              │    │
│  │  └─ Only summaries returned to main context         │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

**Implementation** (`vaf/tools/search.py`):

```python
def answer_question_with_page(user_question: str, page_content: str) -> str:
    """Use separate LLM context to answer question based on single page."""
    
    prompt = f"""User Question: "{user_question}"

Page Content:
{page_content}

Based ONLY on this page, answer the question."""

    # Separate LLM call with isolated context (~400 tokens max)
    res = requests.post(
        "http://127.0.0.1:8080/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,  # Limited!
            "temperature": 0.2,
        },
    )
    return res.json()['choices'][0]['message']['content']
```

### Comparison: Old vs New

| Aspect | Old (Single Context) | New (Isolated Contexts) |
|--------|---------------------|-------------------------|
| 10 web pages | 10 × 2,000 = 20,000 tokens ❌ | 10 × 400 = 4,000 tokens ✅ |
| Deep research | 1 huge prompt ❌ | 10 small prompts ✅ |
| Context overflow | Frequent | Never |
| Quality | Diluted attention | Focused responses |

---

## Context Compression

### Trigger

Compression is triggered when context usage reaches **85%** (configurable).

### Process

```
BEFORE COMPRESSION (100 messages, 7,500 tokens):
┌─────────────────────────────────────────────────┐
│ System Prompt                                   │
│ User: "Create a website"                        │
│ Assistant: [Long response with code...]         │
│ User: "Add a contact form"                      │
│ Assistant: [Long response...]                   │
│ ... (95 more messages)                          │
└─────────────────────────────────────────────────┘

                ⬇️ COMPRESSION ⬇️

AFTER COMPRESSION (12 messages, 2,000 tokens):
┌─────────────────────────────────────────────────┐
│ System Prompt (kept)                            │
│ ─────────────────────────────────────────────   │
│ [COMPRESSED CONTEXT]                            │
│ 🎯 Intent: Create website with contact form     │
│ 📁 State: Created index.html, style.css         │
│ 📝 Summary: User requested website creation...  │
│ ─────────────────────────────────────────────   │
│ User: "Add a contact form" (recent)             │
│ Assistant: [Response...] (recent)               │
│ ... (last 10 messages kept raw)                 │
└─────────────────────────────────────────────────┘
```

### Strategy

1. **Archive**: Full history saved to `~/.vaf/context_archive/` (restorable via `/restore`)
2. **Keep**: System prompt + last 10 messages (raw)
3. **Compress**: Old messages → Intent Context + State Context + Narrative Summary
4. **Result**: ~70% token reduction while preserving critical information

### Implementation

See `vaf/core/context.py`:

```python
def compress(self, history: List[Dict]) -> List[Dict]:
    """Cursor-style compression."""
    # 1. Archive full history for restoration
    self._archive_history(history)
    
    # 2. Update Intent and State from ALL messages
    for msg in history:
        if msg.get("role") == "user":
            self.update_intent(msg.get("content", ""))  # Session goal (user_intent), not preferences/language
        self.update_state(msg)
    
    # 3. Build compressed history
    system_prompt = history[0]  # Always keep
    recent_messages = history[-self.recent_memory_size:]  # Keep raw
    
    # 4. Build context summary
    context_summary = self._build_context_summary()
    
    # 5. Construct new history
    new_history = [system_prompt]
    if context_summary:
        new_history.append({"role": "system", "content": context_summary})
    new_history.extend(recent_messages)
    
    return new_history
```

---

## Summary

VAF's architecture is designed around **context efficiency**:

| Component | Strategy | Token Savings |
|-----------|----------|---------------|
| System Prompt | Dynamic modules | 40-80% |
| Sub-Agents | Isolated contexts | No overflow |
| Web Search | Per-page processing | 80% |
| Deep Research | Topic-by-topic | No overflow |
| Compression | Cursor-style | 70% |

**Result**: You can have longer, more productive conversations without hitting context limits!

