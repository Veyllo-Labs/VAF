# VAF Architecture Documentation

This document provides a deep dive into VAF's internal architecture, focusing on the context management system, sub-agents, and token optimization strategies.

## Table of Contents

1. [Dynamic System Prompt](#dynamic-system-prompt)
2. [Context Management](#context-management)
3. [Empty Response Handling and Retries](#empty-response-handling-and-retries)
4. [Sub-Agents](#sub-agents)
5. [Web Search & Deep Research](#web-search--deep-research)
6. [Context Compression](#context-compression)

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

### Module retention (decay)

Modules remain active for a number of turns after they are triggered instead of being removed immediately. This avoids rapid switching when the user alternates between topics. The system uses **Dynamic Decay** based on your context limit (`n_ctx`):

| Context Limit | Base Decay | Coding Module | Research/Filesystem |
|---------------|------------|---------------|----------------------|
| **Small (≤ 12k)** | 2 turns | 3 turns | 2 turns |
| **Medium (≤ 20k)**| 2 turns | 4 turns | 2-3 turns |
| **Large (> 20k)** | 3 turns | 5 turns | 3-4 turns (Default) |

### Implementation

Implemented in `vaf/core/system_prompt.py`:

```python
class SystemPromptManager:
    # Retention counts are now dynamic (initialized in __init__ based on max_tokens)
    
    def __init__(self, agent_tools, max_tokens=8192):
        self.max_tokens = max_tokens
        # Dynamic limits are set here...
        if max_tokens <= 12000:
            self.decay_start = 2
            self.module_decay_turns = {"coding": 3, "research": 2, "filesystem": 2}
        # ...
```

## Context Management

### Overview

VAF uses a **Cursor-style context management system** that tracks, compresses, and archives conversation history. The system is **VRAM-aware**, meaning it adapts its behavior based on your configured context limit (`n_ctx`).

```
┌─────────────────────────────────────────────────────────────┐
│                    CONTEXT MANAGEMENT                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📊 REAL-TIME TOKEN TRACKING (Self-Calibration)     │    │
│  │  ├─ Precise API usage: From OpenAI/DeepSeek/Claude  │    │
│  │  ├─ Dynamic n_ctx: Auto-boosts to 128k for APIs    │    │
│  │  ├─ Tool Overhead: Precise calculation              │    │
│  │  └─ Safety: Proactive tool reduction (Core set)     │    │
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

### Real-Time API Token Tracking (Self-Calibration)

Unlike local models where VAF uses a local tokenizer, API providers (OpenAI, DeepSeek, Anthropic, Google) provide exact usage data after each request. VAF now utilizes this data for "Self-Calibration":

1. **Facts over Estimation**: After every API call, VAF captures the exact `input_tokens` and `output_tokens` reported by the provider.
2. **Context Persistence**: The context bar in the Web UI now reflects the *actual* state of the API's context window, including the exact cost of tool schemas and system prompts.
3. **Dynamic Context Windows**: When an API backend is active, VAF automatically adjusts the context limit (`n_ctx`) to **128,000 tokens** (unless manually set higher). This prevents premature compression and allows full use of modern "Long Context" models.
4. **Transparent Continuation**: If a response is cut off due to the output token limit (`finish_reason: length`), VAF's API backend transparently requests a continuation, stitching the parts together seamlessly for the user.

### VRAM-Aware Efficiency

To support systems with limited VRAM (e.g., 11k or 16k context limits), the manager dynamically adjusts its aggressive levels:

| Context Limit (`n_ctx`) | Compression Trigger | Recent Memory | Strategy |
|-------------------------|---------------------|---------------|----------|
| **Very small (≤ 8k)**   | **70%** usage       | **8 messages**| More raw turns kept; GitHub/web_search results preserved |
| **Small (≤ 12k)**       | **70%** usage       | **6 messages**| Aggressive pruning, Core tools only |
| **Medium (≤ 20k)**      | **75%** usage       | **8 messages**| Proactive compression, 25% output buffer |
| **Large (> 20k)**       | **85%** usage       | **10 messages**| Standard Cursor-style (Default) |

### RAG and memory context (pre-generation injection)

Retrieval for the current turn runs in the **input phase**, before the LLM is called. The model output stream does not trigger retrieval.

1. **When**: Each chat entry (Web UI via headless runner, gateway, automation) runs a memory search on the **user message** before calling the agent. The result is passed as `memory_context` into `chat_step`.
2. **Placement**: The model sees it in the first prompt under the system block **"## Memory context (relevant to this query)"**, either as pre-retrieved snippets or a placeholder when none match.
3. **Query refinement**: For short, user-oriented questions (e.g. "who am I", "what do you remember", "my preferences"), the search query is expanded before retrieval so that profile and compaction memories are more likely to match. This is controlled by `memory_rag_refine_query` in config (default: true).
4. **memory_search tool**: Use for **follow-up or different short queries only** (e.g. "user name", "user preferences"). Do not pass full thinking or `<think>` content; the tool rejects such input and directs the model to use the Memory context block for the current turn.

### Persistent Hybrid Architecture (Agatic vNext)

In addition to the RAM-based context above, VAF now employs a **Persistent Hybrid Architecture** that maintains a "brain on disk". This ensures the Main Agent knows "where it is" and "what it's doing" even after restarts.

```
┌─────────────────────────────────────────────────────────────┐
│              PERSISTENT LAYER (.vaf/main/)                  │
├─────────────────────────────────────────────────────────────┤
│  ├─ user_intent.md           (The "North Star")             │
│  ├─ team_state.json          (Orchestration Status)         │
│  ├─ working_memory.json      (Scratchpad / Plans)           │
│  └─ subagent_validation.json (Result retry counter)         │
└─────────────────────────────────────────────────────────────┘
```

**Files managed by `MainPersistenceManager`:**
- **`user_intent.md`**: Stores the original user request. Read-only for sub-agents, updated only by user input. Acts as an "Intent Lock".
- **`team_state.json`**: Tracks the status of all sub-agents (`running`, `completed`, `needs_clarification`).
- **`working_memory.json`**: Persists notes, plan, and tasks across sessions. Each list is limited to 500 entries (oldest dropped when exceeded). Notes and plan entries store an optional timestamp (date/time) for time context. **Tasks** have two states: `pending` (in progress or waiting on something) and `done`; tasks marked done are automatically removed 12 hours after being marked. The agent should replace or clear notes/plan on a new user task or after completion so working memory does not grow without bound; use `add_task` / `mark_task_done` for checkable steps.
- **`subagent_validation.json`**: Stores the retry count for sub-agent result validation (resets on new user message; see Sub-Agent Result Validation).

### Workspace Awareness

VAF is now **CWD-Aware** (Current Working Directory). It understands the difference between "Scaffolding" (New Project) and "Engineering" (Existing Project).

**`WorkspaceManager` Logic:**
1. **Detection**: Scans for `.git`, `.vaf`, `package.json`, etc.
2. **Anchor**: If inside a project, VAF anchors the Coder Agent to the current directory.
3. **Scaffold**: If the user asks to "create a new project", VAF breaks out to `~/Documents/VAF_Projects/`.
4. **Application directory**: When the process runs from the application directory, the system prompt shows a neutral workspace label instead of the path so the agent does not use that directory for file operations.

### Intent Locking & Validation

To prevent "Context Drift" (where the agent gets distracted by sub-agent status reports), VAF implements an **Intent Lock**:

1. **Snapshot**: User intent is saved immediately to `user_intent.md`.
2. **Sub-Agent Result Validation**: Before injecting a sub-agent result as Background Intel, an LLM judges whether it fulfills the user's intent. The LLM must output `</true>` or `</false>`. If `</false>`, a retry instruction is injected so the Main Agent calls the sub-agent again with an explicit task. After 20 consecutive `</false>` results, the system instructs the Main Agent to stop retrying and inform the user of the actual status. The retry count is stored in `subagent_validation.json` and reset when the user sends a new message.
3. **Background Intel**: Valid sub-agent results are injected as "Background Intelligence", explicitly marked as *supporting data*, not new commands.
4. **Final Answer Validator**: Before answering, VAF checks:
   - Is this a "Meta-Response" (e.g., "I have processed the files")?
   - Does it answer the original question?
   - **Action**: If it's just meta-talk, the system blocks the output and forces the agent to provide the actual answer.

### Empty Response Handling and Retries

When the model returns no user-facing answer (e.g. only `<think>` content or empty text), the main agent treats this as an **empty response** and retries instead of accepting it. Implementation is in `vaf/core/agent.py` (chat loop).

- **First empty (retry count 0):** History is set to the current snapshot (system + user) **plus one assistant message** containing the model’s last output (including thinking). A system nudge is appended instructing the model to provide a clear final answer or call tools. No temperature change; one short delay then retry. This lets the model see its own reasoning and the explicit “answer now” instruction.
- **Second and later empties:** History is reset to the snapshot only (system + user; tool calls and tool results from the turn are preserved). Thinking-only assistant messages are dropped. The same nudge is added, temperature is varied (sweep) to break loops, and at high retry counts proactive context clearing and emergency context management run before a hard stop (after a fixed number of retries).

This two-phase approach reduces endless “think-only” loops while still using the first failed attempt as context for one retry.

**API backend – delayed retries and system-log-only fallback:** When using an API backend (e.g. OpenAI, DeepSeek), after 3 consecutive empty responses the agent performs up to 4 delayed retries (3 seconds between each). A system log entry is emitted before each retry. If all 4 retries still yield no answer, the agent sends the message "API returned empty responses repeatedly. Please try again." as a **system log only** (via `new_log`); no assistant message is added. The headless runner detects the `[SYSTEM_LOG_ONLY]` return value and does not emit `agent_message_update`, so the Web UI shows a timeline (system) entry only, not a bot speech bubble.

**False promise retry:** When the model says it will use a tool (e.g. "Let me search…") but does not emit a tool call, the agent treats this as a false promise, appends a correction to history and retries. As with empty-response retry, the backend sends `clear_last_assistant` so the Web UI removes the faulty assistant message; only the retry response is shown.

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
- The VAF installation directory is not allowed as a target; the agent is instructed not to delegate such tasks.

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
    
    prompt = f\"\"\"User Question: \"{user_question}\"

Page Content:
{page_content}

Based ONLY on this page, answer the question.\"\"\"

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

Compression is triggered by **Adaptive Thresholds** based on your context limit:
- **≤ 12k**: 70% usage
- **≤ 20k**: 75% usage
- **Default**: 85% usage

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

AFTER COMPRESSION (8 messages, 2,000 tokens):
┌─────────────────────────────────────────────────┐
│ System Prompt (kept)                            │
│ ─────────────────────────────────────────────   │
│ [CONTEXT GLUE]                                  │
│ 🎯 Intent: Create website with contact form     │
│ 📁 State: Created index.html, style.css         │
│ 📝 Summary: User requested website creation...  │
│ ─────────────────────────────────────────────   │
│ User: "Add a contact form" (recent)             │
│ Assistant: [Response...] (recent)               │
│ ... (last 6-10 messages kept raw)               │
└─────────────────────────────────────────────────┘
```

### Strategy

1. **Archive**: Full history saved to `~/.vaf/context_archive/` (restorable via `/restore`)
2. **Keep**: System prompt + dynamic recent window (6-10 messages)
3. **Compress**: Old messages → Intent Context + State Context + Narrative Summary
4. **Density**: Small context limits trigger **High-Density Summary** mode
5. **Result**: ~70% token reduction while preserving critical information

### Plan-Act-Summarize Pattern (Recursive Task Decomposition)

The Main Agent can now decompose complex tasks into steps and execute them one at a time, surviving arbitrary context lengths. This is the same principle the Coder Agent uses (`create_fresh_context_for_task`), but applied to the Main Agent via an **Orchestrator Prompt Module** and a **checkpoint tool**.

**Loop:**

```
┌─────────────────────────────────────────────────────────────┐
│         PLAN-ACT-SUMMARIZE LOOP (Main Agent)                │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. PLAN: Write step-by-step plan to working_memory         │
│     └─ update_working_memory(plan=["Step 1", "Step 2", …]) │
│                          │                                  │
│                          ▼                                  │
│  2. ACT: Execute ONE step using appropriate tools           │
│     └─ e.g. web_search, read_file, python_sandbox           │
│                          │                                  │
│                          ▼                                  │
│  3. PERSIST: Save result to working_memory                  │
│     └─ update_working_memory(add_notes=["Step 1 result…"])  │
│                          │                                  │
│                          ▼                                  │
│  4. CHECKPOINT (optional): Free context space               │
│     └─ checkpoint_context(summary="Steps 1-3 done…")        │
│     └─ Archives history, keeps system prompt + glue          │
│                          │                                  │
│                          ▼                                  │
│  5. NEXT STEP: Continue from working_memory plan             │
│     └─ Plan + notes survive → agent knows where it left off  │
│                                                             │
│  Repeat until all steps complete.                           │
└─────────────────────────────────────────────────────────────┘
```

**Building Blocks:**

| Component | Role |
|-----------|------|
| `user_intent.md` | Stores the original request (Intent Lock — never changes mid-task) |
| `working_memory.json` | Stores plan, notes, tasks (survives compression + checkpoint) |
| `update_working_memory` | Tool to write plan steps, add notes, mark tasks done |
| `checkpoint_context` | Tool to archive history and start fresh (proactive wiping) |
| Orchestrator Prompt Module | Activated by router for multi-step keywords; instructs Plan-Act-Summarize |
| Context Compression | Existing reactive compression (70–85% threshold) as a safety net |

**Why this works for small models:**
- The plan lives in `working_memory.json`, not in chat history
- Each step's result is persisted as a note before the next step
- `checkpoint_context` proactively frees context (instead of waiting for the 70–85% threshold)
- After a checkpoint, the context glue restores the narrative summary, so the model knows what happened
- A 4k-context model can complete a 20-step task this way

**Activation:**
The Orchestrator prompt module activates automatically when the tool router detects multi-step keywords (e.g. "step by step", "compare", "for each", "batch", "summarize all"). It can also be activated manually via `prompt_manager.activate_module("orchestrator")`.

**Conditional hard enforcement (small context):** When the orchestrator module is active and context is small (`n_ctx` ≤ 12k), the agent enforces that a plan exists in working memory before allowing "heavy" tools (e.g. `read_file`, `web_search`, sub-agents such as `librarian_agent`, `coding_agent`, `research_agent`). If the model tries to call a heavy tool without a plan, it receives a block message and must call `update_working_memory(plan=[...])` first. In addition, at most 2 heavy tool calls per turn are allowed; on the 3rd, the model is asked to summarize progress and use `checkpoint_context` if needed. This prevents small-context models from skipping the plan and flooding the context with tool output.

### Small context (e.g. llama.cpp with low n_ctx)

When `n_ctx` is small (e.g. 4k–12k), fewer messages are kept raw and compression is more aggressive. To reduce "context loss" (e.g. the model forgetting that it already used GitHub or other tools):

- **Very small (≤ 8k):** The manager keeps **8** recent messages raw (instead of 6) so roughly 1–2 full tool-using turns remain visible.
- **Preserved tool results:** Besides `set_todos`, `write_file`, `read_file`, the compressed history preserves truncated results from **GitHub tools** (`github_list_repos`, `github_get_file`, `github_list_issues`, `github_list_pulls`) and **web_search**. So the model can retain that it already accessed e.g. the repo.
- **Tools used in summary:** For small contexts (≤ 16k), the context summary includes a "Tools used this session" line (last 10 tools). This reduces confusion about connectivity or which tools were already called.

**Recommendation:** Use `n_ctx` ≥ 8k (ideally 12k+) for tool-heavy conversations; the above mitigations help when you must use a lower limit.

### Implementation

See `vaf/core/context.py`:

```python
def compress(self, history: List[Dict]) -> List[Dict]:
    """Adaptive Cursor-style compression."""
    # 1. Archive full history for restoration
    self._archive_history(history)
    
    # 2. Update Intent and State from ALL messages
    for msg in history:
        if msg.get("role") == "user":
            self.update_intent(msg.get("content", ""))
        self.update_state(msg)
    
    # 3. Build compressed history
    system_prompt = history[0]
    recent_messages = history[-self.recent_memory_size:]
    
    # 4. Build context summary (High-Density if n_ctx is small)
    context_summary = self._build_context_summary()
    
    # ...
```

---

## Summary

VAF's architecture is designed around **context efficiency**:

| Component | Strategy | Token Savings |
|-----------|----------|---------------|
| System Prompt | Dynamic modules | 40-80% |
| Module Decay | Dynamic retention | token preservation |
| Sub-Agents | Isolated contexts | No overflow |
| Web Search | Per-page processing | 80% |
| Deep Research | Topic-by-topic | No overflow |
| Compression | Adaptive triggers | 70% |
| Plan-Act-Summarize | Persistent plan + step results | Unbounded steps |

**Result**: You can have longer, more productive conversations without hitting context limits!
