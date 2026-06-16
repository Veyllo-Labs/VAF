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
│  │  ├─ Identity (Model Name)                           │   │
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

When a session is loaded (e.g. on Web UI session switch or when processing a chat task), the ContextManager state (intent, state, narrative summary) is restored from the session’s `runtime_state` if present. So the agent keeps high-level context across session switches; see **SESSION_MANAGEMENT.md** (Session switch / LOAD_SESSION).

### Real-Time API Token Tracking (Self-Calibration)

Unlike local models where VAF uses a local tokenizer, API providers (OpenAI, DeepSeek, Anthropic, Google) provide exact usage data after each request. VAF uses this for display and calibration:

1. **Facts over Estimation**: After every API call, VAF captures the exact `input_tokens` and `output_tokens` reported by the provider.
2. **Context Bar (Web UI)**: The context bar reflects the effective context fill. In API mode the displayed total is the maximum of the last request usage and a history-based estimate (so loading an old session shows the correct fill before any new request). Estimates use weighted ratios: 2.8 chars/token for code (e.g. messages containing ```), 3.6 for plain text; tool schemas (local/server) use 3.0. When a local server is available, the breakdown (system / history / tools) can use the server’s `/tokenize` endpoint for precision.
3. **Session-Scoped Display**: Context updates (`context_status`) are sent only to the Web UI tab that has that session open (`_push_session_update(session_id, ...)`), so multiple tabs do not see each other’s token stats.
4. **Dynamic Context Windows**: When an API backend is active, VAF automatically adjusts the context limit (`n_ctx`) to **128,000 tokens** (unless manually set higher). This prevents premature compression and allows full use of modern "Long Context" models.
5. **Transparent Continuation**: If a response is cut off due to the output token limit (`finish_reason: length`), VAF's API backend transparently requests a continuation, stitching the parts together seamlessly for the user.
6. **Limit tracks the real window**: the compression / overflow limit is re-derived from the configured `n_ctx` every turn (`get_token_usage`) and the context manager is re-synced (logged `[CTX-LIMIT]`). A manager built before `n_ctx` was raised can therefore never pin compression to the 32 768 floor while the model and server actually run at, e.g., 128 000 (which had caused premature "Compressing…/CRITICAL OVERFLOW" at a fraction of the real window).
7. **Compression never grows**: if the summary (context summary + resume block) would come out *larger* than the input (observed: 30 725 → 43 754 tokens, which then tripped the overflow), it is dropped and only the system turn + recent messages are kept — always smaller than the input. The full history is archived for `/restore`.

### VRAM-Aware Efficiency

The Context Manager dynamically adjusts its behavior based on the configured context limit (`n_ctx`). This ensures that small local models remain stable while large-context APIs can leverage their full potential.

> **Note:** VAF clamps `n_ctx` to a minimum of 32 768 in `Config.load` (lower values are raised on load), so the ≤ 8k / ≤ 12k rows below are not reachable in normal operation; they are kept for reference only.

| Context Limit (`n_ctx`) | Trigger | Recent Memory | Strategy |
|-------------------------|---------|---------------|----------|
| **Very small (≤ 8k)**   | **70%** | **8 messages**| Maximum pruning; core tools only preserved. |
| **Small (≤ 12k)**       | **70%** | **6 messages**| Aggressive compression; minimal recent window. |
| **Medium (≤ 20k)**      | **75%** | **8 messages**| Proactive compression; balanced history. |
| **Large (≤ 64k)**       | **85%** | **50 msgs**   | Extended raw history for local long-context models. |
| **API Boost (≤ 128k)**  | **85%** | **100 msgs**  | **Standard API Mode.** Preserves ~50 full turns raw. |
| **Ultra (> 128k)**      | **90%** | **200 msgs**  | Maximum retention for Gemini 1.5 Pro / Claude 3.5. |

### Seamless Tool Compression

To prevent the context window from being flooded by large tool outputs (which would trigger aggressive history pruning), VAF implements **Seamless Compression**. Certain tools have their output pruned *before* entering the chat history, while key facts are extracted into the permanent State Context.

**Supported Tools:**
- **Filesystem:** `read_file`, `list_files`, `github_get_file`, `github_list_repos`
- **Search:** `web_search`, `web_fetch`
- **Communication:** `mail_inbox`, `whatsapp_inbox`, `telegram_inbox`, `list_email_accounts`

**Best Practice:** When dealing with large datasets (e.g., reading a 2000-line log file or listing 50 emails), the agent sees a pruned version (head/tail) in history, but knows the full content is processed. This maintains conversational continuity without losing context "depth".

### Per-Turn Intermediate-Step Squash (Tool Memory)

After every turn, once the final answer is produced, VAF squashes the turn's intermediate steps (tool calls, tool results, reasoning) out of the live history and replaces them with a single compact `[Context: …]` summary, so long conversations stay lean.

The summary records **each tool's outcome** — `OK` or `FAILED` plus a short single-line snippet of the result/error — not just the tool names. This way the agent still knows *what it did* and *which errors it hit* on later turns (for example it can report a previous `python_exec → FAILED: Object of type PosixPath is not JSON serializable` instead of guessing). The summary is persisted with the session and, unlike other operational system messages, is restored on reload, so this memory survives session switches and restarts. See [SESSION_MANAGEMENT.md](SESSION_MANAGEMENT.md) for the persisted format.

This is distinct from threshold-based [Context Compression](#context-compression): the squash runs every turn for lean history, whereas compression only fires when the context-usage threshold is crossed.

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
- **`team_state.json`**: Tracks the status of all sub-agents (`running`, `completed`, `failed`, `needs_clarification`). A finished agent is shown as `done HH:MM` / `failed HH:MM` and kept for a few main-agent turns (`TEAM_DONE_PRUNE_TURNS`) so the main agent registers the completion, then removed from the team list. See [SUBAGENT_IPC.md](../agents/SUBAGENT_IPC.md#team-state-synchronization).
- **`working_memory.json`**: Persists notes, plan, and tasks across sessions, with distinct roles:
  - **`plan`** = the agent's **high-level approach** (a line or two: how it will tackle the intent). Short and stable; the plan gate only requires this approach, not a full step list.
  - **`tasks`** = the **concrete, ordered steps** that carry out the plan. Two states: `pending` (in progress or waiting on something) and `done`; done tasks are removed 12 hours after being marked. This is where multi-step work is tracked and kept on course — a per-step reminder focuses the model on the first pending task each turn, and the plan gate requires a **real (non-placeholder) plan** before a state-changing tool runs — a filler entry like "test" or "Neuer Test-Plan hier" (a plan made only of generic words, or near-empty) does NOT open the gate, so a weak model cannot bypass it with junk; the gate's loop-cap still proceeds after repeated blocks so it never hard-locks.
  - **`notes`** = facts/observations worth remembering.

  Each list is limited to 500 entries (oldest dropped when exceeded); notes/plan entries store an optional timestamp. Set `notes`/`plan`/`tasks` to replace a list, `add_notes`/`add_plan`/`add_task` to append, `mark_task_done(index)` to complete one step, or `mark_all_done` to complete every pending task in one call (used when the user says they are finished — otherwise the model has to loop `mark_task_done` by index and tends to lose track). The agent should reset what no longer applies on a new user task. Appends are **deduplicated**: an `add_task`/`add_notes`/`add_plan` whose normalized text (collapsed whitespace, case-insensitive) already exists is skipped, and a full `tasks` replacement collapses duplicate texts (a kept task stays `done` if any of its duplicates was done, so cleanup never un-finishes a step). This stops a confused model from piling up the same entry many times in one turn — observed: the same task appended five times after a "mark everything done" request. The tool result states when an add was a no-op ("that task was already in the list — not re-added") so the model does not blindly retry. Two safeguards keep plan and tasks aligned: a **plan-without-tasks reminder** — steps never belong in the plan, so when the agent has a plan but no tasks, a per-turn line tells it to break the plan into tasks (`plan_without_tasks_reminder_enabled`); it goes silent once any task exists (the current-step reminder takes over); and an **overwrite guard** — replacing the whole task list while steps are still pending is bounced once ("are you sure?", with the pending steps listed) and proceeds on a re-call within `task_overwrite_confirm_window_seconds`. The bounce message is phrased **imperatively** ("STOP — this is NOT an internal note … act on it") because small local models otherwise dismissed it as an internal reminder and silently dropped the user's tracked steps; the agent must handle it — `mark_task_done` the finished step or keep the others, not ignore it — and the system prompt reinforces "mark the finished step done immediately". In Thinking Mode, updates are additionally mirrored to Thinking Workspace snapshots for auditability.
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

**Tool-loop protection:** To avoid unbounded tool-call loops the agent enforces a two-stage limit per user turn:

- **Soft limit (50 turns):** A `[System: …]` user message is injected into history reminding the agent of the original user intent and asking it to refocus and complete the task efficiently. The agent continues normally after this — no output is blocked.
- **Hard limit (75 turns):** A second injection tells the agent it may not call any more tools, instructs it to inform the user of its current progress, and asks the user if it should continue in a new reply. If the agent calls another tool despite this message, an immediate hard stop (`[LOOP_PROTECTION]`) is enforced and the function returns.

Set `"tool_loop_unlimited": true` in config to disable the hard limit entirely (the soft reminder at 50 still fires). Redundant calls (same tool and same arguments as an already-executed call, when no new user message has arrived since) are blocked on a **separate** loop-protection counter — never the empty-response counter, so a block can no longer cause a silent abort. After a few consecutive redundant blocks the agent is forced to answer in **plain text** (tools disabled for the rest of that turn) instead of looping. In thinking mode a `thinking_done` tool call exits the loop immediately. All limit events are logged in `logs/backend_YYYY-MM-DD.log` under `[LOOP_PROTECTION]` / `soft_limit_reminder` / `hard_stop_injection`. **Emergency dead-loop breaker:** underneath all of the above, **≥10 tool executions within 5 seconds aborts the whole turn immediately** (logged `[EMERGENCY_LOOP_BREAK]`) — a time-based circuit breaker that stops a runaway loop in seconds instead of grinding to the 75-turn hard limit. The **Stop button is also honoured *between* tools** within a single multi-call response, not only at the loop top, so stopping no longer has to drain the whole batch. See also **Thinking-Mode.md** → [Loop protection (API cost safety)](../agents/Thinking-Mode.md#loop-protection-api-cost-safety).

**API backend – delayed retries and system-log-only fallback:** When using an API backend (e.g. OpenAI, DeepSeek), after 3 consecutive empty responses the agent performs up to 4 delayed retries (3 seconds between each). A system log entry is emitted before each retry. If all 4 retries still yield no answer, the agent sends the message "API returned empty responses repeatedly. Please try again." as a **system log only** (via `new_log`); no assistant message is added. The headless runner detects the `[SYSTEM_LOG_ONLY]` return value and does not emit `agent_message_update`, so the Web UI shows a timeline (system) entry only, not a bot speech bubble.

**Empty / thinking-only retry (foreground only):** If a turn produces only reasoning (a `<think>` block) or an empty reply — no final answer and no tool call — the agent injects a "give a direct answer or call the tool" nudge and retries (`clear_last_assistant` removes the faulty bubble). It is **on in the foreground** so the turn always closes, and **off in background thinking runs** (`VAF_THINKING_MODE`), where it otherwise spammed "Empty response detected…" while the user was idle. `empty_response_retry_enabled` (default off) forces it on everywhere, including thinking runs. Without foreground recovery a thinking-only reply would never close and the Web UI would hang on a loading thinking block.

**False promise retry (opt-in, off by default):** Controlled by `false_promise_detection_enabled` (default `false`). It is off by default because the forced retry caused retry loops and false positives, especially on weak local models. When enabled: if the model says it will use a tool (e.g. "Let me search…") but does not emit a tool call, the agent treats this as a false promise, appends a correction to history and retries; as with empty-response retry, the backend sends `clear_last_assistant` so the Web UI removes the faulty assistant message and only the retry response is shown.

**Result grounding:** The (opt-in) false-promise check above catches a *forward-looking* promise ("I'll run this now") with no tool call. The complementary case — handled here and **on by default** — is a *backward-looking* confabulated result — a reply that asserts a concrete tool OUTCOME that never happened, e.g. "Workflow failed: Tool not found" when the workflow was never actually run. After a final text reply, the agent checks whether it claims a concrete outcome (a success, a failure, a saved/created file, a specific error, or a result/count) that the turn's ACTUAL tool results do not support — including a result for a tool that was never run this turn. A cheap keyword/regex pre-filter gates an LLM judge, so ordinary replies cost nothing; on a mismatch the reply is bounced back for correction (same `clear_last_assistant` + history-correction flow as false promise), capped at `result_grounding_max_retries` (default 2) before it proceeds so it can never loop. Toggle with `result_grounding_enabled` (default on). Any validator failure is treated as "grounded" so the guard never blocks a reply.

**`clear_last_assistant` is suppressed in thinking mode.** All retry/correction flows (empty response, false promise, result grounding) and the team-await hold emit `clear_last_assistant` to drop the *just-produced* faulty bubble before the correction. These all route through one guard, `_clear_last_assistant_ui`, which is a **no-op during a thinking (background) run** (`_emit_to_web_ui()` is False then). Rationale: in a background pass the "last assistant" bubble is the user's *previous real answer*, not anything produced this turn — clearing it would replace a real message (observed: a thinking pass wiped a research answer and showed "Nothing actionable"). Background runs must only ever append below, never replace.

### Best Practices for Long Conversations

To maintain maximum "depth" and accuracy in very long sessions (especially when using API providers like DeepSeek or OpenAI):

1. **Leverage the 128k Boost:** Ensure your `provider` is set to an API service. VAF automatically boosts the internal `n_ctx` to 128,000, allowing the system to keep up to **100 messages** raw before compression even starts.
2. **Use `checkpoint_context` for Milestones:** If you are working on a massive multi-step task (e.g., building a full app), use the `checkpoint_context` tool after completing a major phase. This archives the "noise" of implementation details while keeping your plan and high-level progress in the "Stable Progress Glue".
3. **Trust Seamless Compression:** Don't worry about reading large files or listing many emails. VAF prunes these automatically. If you need the model to "remember" a specific detail from a large output, simply acknowledge it in chat (e.g., "I see the error on line 452")—this saves the fact into the State Context.
4. **Prefer `memory_save` for Permanent Facts:** For information that should survive even across different chat sessions (e.g., your birthday, server IP addresses, specific project paths), use the `memory_save` tool. This moves data from transient chat context into the permanent RAG database.


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
│  1. PLAN: Set the approach + steps in working_memory        │
│     └─ plan=["approach"]; steps tracked as tasks (add_task) │
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
│     └─ Archives history, keeps system prompt + glue         │
│                          │                                  │
│                          ▼                                  │
│  5. NEXT STEP: Continue from the tracked tasks + notes      │
│     └─ Plan + tasks + notes survive → knows where it stopped│
│                                                             │
│  Repeat until all steps complete.                           │
└─────────────────────────────────────────────────────────────┘
```

**Building Blocks:**

| Component | Role |
|-----------|------|
| `user_intent.md` | Stores the original request (Intent Lock — never changes mid-task) |
| `working_memory.json` | Stores plan, notes, tasks (survives compression + checkpoint) |
| `update_working_memory` | Tool to set the approach (plan), track steps as tasks, add notes, mark tasks done |
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

#### Plan enforcement — "explore freely, but plan before you act"

The pattern above lets the agent *own* a plan; two always-on mechanisms make sure any model — from a small local model up to a large one — actually *follows* it instead of skipping or abandoning steps. The guiding principle is **"explore freely, but plan before you act":** reading and searching stay open (the agent needs them to form a plan), but committing to a state-changing action requires a plan first. Both mechanisms are independent of context size and sit on top of the conditional small-context enforcement above. The plan, tasks, and team they read from are stored per session (see [CONTEXT_GLUE.md](CONTEXT_GLUE.md) and [SESSION_MANAGEMENT.md](SESSION_MANAGEMENT.md)), so one chat's plan never drives another.

**Current-step reminder.** Whenever the working-memory plan has pending tasks, the agent injects a compact current-step reminder into the `<working_memory>` block each turn: the first pending task (derived, so it auto-advances as tasks are completed) together with the list index to pass to `update_working_memory(mark_task_done=...)`. Tasks are rendered with their `[i]` index so the model knows which one to close. The reminder is silent when no pending task exists (plain chat is unaffected) and is governed by the `plan_step_reminder_enabled` kill-switch. See [CONTEXT_GLUE.md](CONTEXT_GLUE.md) for where this block is injected. As a companion check, if the agent marks a *later* task done while an *earlier* one is still pending, `update_working_memory` appends a soft out-of-order nudge ("did you skip it?") to its result — a reminder, never a block (`plan_drift_nudge_enabled`).

**Pending-task auto-continue.** The current-step reminder is rebuilt from working memory at the start of each turn, so on its own it only re-fires when the user sends the next message. An agent that produces a final text answer while tasks are still pending would therefore yield and leave the list unworked. To close that gap, when a turn ends with a final answer (no tool call) and pending tasks remain, the agent re-injects the current-step reminder as a system "continue" message and loops again within the same user turn instead of yielding. The continuation counts as one tool turn, so it shares the existing soft-50 / hard-75 loop budget (see Tool Loop Protection) rather than adding a second counter — a pure-text continue loop cannot run past the hard stop. It does not fire when the reply is a question the user must answer first, detected in this order: the persisted `waiting_for_reply` state (the same explicit signal Thinking Mode uses when the agent reaches the user via a messenger tool), then a small yes/no validation-LLM classifier over the reply text, falling back to a last-line "?" heuristic when the classifier is disabled or unavailable. It is also suppressed in a background Thinking-Mode pass and when disabled. Governed by `autocontinue_pending_tasks_enabled` and `autocontinue_question_classifier_enabled`.

**Plan gate (main agent only).** Before the main agent runs a state-changing tool (`permission_level` `write` or `dangerous`, except `python_sandbox`), a plan must exist in working memory; read/search tools (`read_file`, `web_search`, `research_agent`) are never gated, so the agent can explore freely to build the plan. The block is satisfied in the same turn by calling `update_working_memory(plan=[...])` first (`update_working_memory` is a `system` tool, so it is never gated); a one-line plan with a verify step is enough — that verify habit also complements the result-grounding guard. After a few consecutive blocks the tool proceeds anyway so nothing hard-locks. Sub-agents (e.g. the coder) are never gated — they run their own task loops untouched. Governed by `plan_gate_enabled` / `plan_gate_max_blocks`. See [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md) for the `permission_level` contract this builds on.

**Team-await (don't declare done while the Team is still working).** Async sub-agents return immediately, so the main agent can claim the overall task is complete while one is still running. When a reply asserts overall completion, the agent checks the live sub-agent state (`get_active_tasks_for_current_session`) and bounces the claim back while any sub-agent is **genuinely running**, asking it to wait for the result (it arrives via the Team status / pending results) or stop the sub-agent first. **Anti-stuck by design:** crashed/stale sub-agents are reaped first (`check_zombies`) and never block; a finished sub-agent leaves the active list so the block lifts on its own; and after `team_await_max_blocks` bounces the claim proceeds anyway, so the agent can never get stuck waiting. Governed by `team_await_enabled` / `team_await_max_blocks`. See [SUBAGENT_IPC.md](../agents/SUBAGENT_IPC.md) for the active-task / heartbeat state it reads. This complements the result-grounding guard (which catches a fabricated sub-agent result that never arrived).

**Anti-spin guard (plan forever, never act).** A weak model can churn the **bookkeeping** tools — `update_working_memory`, `update_intent`, `add_task` — over and over, re-planning the same task with slightly varying text, without ever calling the tool that does the actual work (observed: a contract request that fired ~8 `update_working_memory` calls in a row, then gave up and used the wrong tool, while a 19k-char reasoning dump returned zero content). The other loop guards miss this: the **redundant-call block** needs *exactly equal* arguments (the near-duplicate task texts differ), the **emergency breaker** needs ≥10 calls within 5s (these are seconds apart), and the **hard stop** is 75 turns away. The anti-spin guard (`Agent._anti_spin_step`, called per tool in the execution loop) counts **consecutive** bookkeeping calls; **any other tool resets the streak** (real progress). At `anti_spin_max_planning_calls` (default 4) it injects a firm nudge — *"stop planning, call the tool the task actually needs, or answer in plain text"* — and two calls later it **disables tools for one turn** so the model must act or answer instead of planning again. The current call still runs and the nudge is deferred via `_post_tc_messages` (no tool-message reordering). The bookkeeping set is deliberately **narrow** (`_BOOKKEEPING_TOOLS`): other `system`-permission tools (`thinking_done`, `batch`, the builders, `request_clarification`, `memory_save`) are real actions and never count as spin. Governed by `anti_spin_enabled` / `anti_spin_max_planning_calls`; events log under `[ANTI_SPIN]`. This complements the redundant-call block (exact-duplicate spam) and the emergency breaker (fast runaway) by covering the *slow near-duplicate planning* case neither catches.

### Small context (e.g. llama.cpp with low n_ctx)

When `n_ctx` is small (e.g. 4k–12k), fewer messages are kept raw and compression is more aggressive. To reduce "context loss" (e.g. the model forgetting that it already used GitHub or other tools):

- **Very small (≤ 8k):** The manager keeps **8** recent messages raw (instead of 6) so roughly 1–2 full tool-using turns remain visible.
- **Preserved tool results:** Besides `set_todos`, `write_file`, `read_file`, the compressed history preserves truncated results from **GitHub tools** (`github_list_repos`, `github_get_file`, `github_list_issues`, `github_list_pulls`) and **web_search**. So the model can retain that it already accessed e.g. the repo.
- **Tools used in summary:** For small contexts (≤ 16k), the context summary includes a "Tools used this session" line (last 10 tools). This reduces confusion about connectivity or which tools were already called.

**Recommendation:** VAF enforces a minimum `n_ctx` of **32 768** — lower values are clamped up when the configuration is loaded (`Config.load`). With 100+ tools the system prompt alone (~5.5k tokens) plus all tool schemas (~6k tokens) requires at least this much headroom. The small-context mitigations above are therefore not reachable in normal operation and are kept for reference only.

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
