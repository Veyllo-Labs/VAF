# Self-Learning in VAF

This document describes how VAF learns from usage and improves over time. It is the central place to understand the **current** self-learning mechanisms and to **add new ones** as the framework is extended.

## What “self-learning” means here

**Self-learning** in VAF means: the system gets better with use without manual configuration. Most lanes learn about the **user** (so the agent becomes more personalized and consistent); one lane learns about the agent's own **tools** (so it calls them more reliably). The durable state each produces is reused in later runs.

- **Current scope:** (1) Long-term memory (RAG): facts, preferences, and context from conversation. (2) User profile / identity: name, language, location, do's/don'ts, preferences. (3) Document learning (`learn_document`). (4) Attachment-scoped retrieval lane (ephemeral, session + user scoped, with optional transfer to long-term memory). (5) Tool know-how (Whare Wananga): the agent learns to use its own tools correctly and feeds that know-how back at runtime.
- **Future scope:** Additional subsystems (e.g. workflow patterns, feedback loops) can be documented in this file as they are added.

---

## Current self-learning systems

### 1. Long-term memory (RAG)

The main self-learning component today is the **Memory System** (RAG). It improves with every conversation.

| Mechanism | What it does | Where it’s documented |
|-----------|--------------|------------------------|
| **Session compaction** | Every N user turns (default 15) in the Web UI, the LLM is asked to extract durable memories from the recent dialogue. Those are stored in RAG. No user action required. | [MEMORY_SYSTEM.md – Session Compaction](MEMORY_SYSTEM.md#session-compaction-background) |
| **memory_save tool** | The agent can store a fact or preference immediately when the user shares it (e.g. “I prefer meetings on Tuesday”). | [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md); tool usage in agent/system prompt |
| **Pre-reply retrieval** | Before each reply, the user message (and optionally a refined query) is used to run semantic search over RAG. The top snippets are injected as “Memory context” in the prompt. | [CONTEXT_MANAGEMENT.md – RAG and memory context](CONTEXT_MANAGEMENT.md#rag-and-memory-context-pre-generation-injection) |

**Effect over time:** More chats → more memories in RAG → better retrieval → more relevant context in each reply. Learning is **per user** (`user_scope_id`). Only the main user’s Web UI conversations are compacted; contact chats (Telegram, WhatsApp, Discord) are not written to RAG (data protection).

**Implementation:** `vaf/memory/rag.py` (compaction, ingest, search), `vaf/core/headless_runner.py` (compaction trigger), tools `memory_save` / `memory_search`.

### 2. User profile (identity)

The **user profile** (`user_identity.json`) is a structured description of the current human user: name, preferred language, location (city/country), timezone, date/time format, **preferences**, **do’s**, and **don’ts**. The model learns the user better by updating this profile when the user says things like “call me Mert”, “I prefer German”, “always be concise”, or “don’t use emojis”.

| Mechanism | What it does | Where it’s documented |
|-----------|--------------|------------------------|
| **update_user_identity tool** | The agent updates the user profile when the user states their name, language, location, rules (do’s/don’ts), or preferences. Parameters include `add_preference`, `add_do`, `add_dont`, `name`, `language`, `city`, `country`, `main_messenger`, `timezone`, etc. | [USER_IDENTITY.md – Tool: update_user_identity](USER_IDENTITY.md#tool-update_user_identity) |
| **System prompt injection** | Every turn, the “User identity (current user)” block is built from `user_identity.json` and injected into the system prompt, so the model always sees the latest name, preferences, do’s, and don’ts. | [USER_IDENTITY.md – System prompt injection](USER_IDENTITY.md#system-prompt-injection) |

**Effect over time:** As the user interacts, the agent fills in and refines the profile (name, language, do’s/don’ts, preferences). Later replies automatically respect these rules and preferences because they are in the system prompt every turn. Learning is **per user** (per username / `user_identity.json`).

**Implementation:** `vaf/tools/user_identity.py` (`UpdateUserIdentityTool`), `vaf/auth/user_workspace.py` (read/write `user_identity.json`), `vaf/core/system_prompt.py` (injection of user identity block).

### 3. Document learning (learn_document)

When the user provides a **document** (PDF, TXT, or MD), the agent can learn it into RAG via the **`learn_document`** tool. The document is extracted to Markdown and split into sections; for each section, one LLM call produces a contextual summary that becomes the memory title (the embedding/retrieval key) and is prepended to the section text. Each section is stored as one memory with `type=document` under a single document tag (e.g. `doc-tora`), alongside one `document_index` root holding the document summary. The agent can then answer questions about the document using retrieval.

| Mechanism | What it does | Where it's documented |
|-----------|--------------|------------------------|
| **learn_document tool** | The agent extracts Markdown, splits into sections, runs one contextual-summary LLM call per section (the summary becomes the memory title), and ingests each section plus a `document_index` root under a shared tag. | [MEMORY_SYSTEM.md – Document memories](MEMORY_SYSTEM.md#document-memories-learn_document) |

**Effect over time:** User-provided documents become queryable knowledge; the memory graph shows one tag per document with many purple document nodes. Learning is per user (`user_scope_id`).

**Implementation:** `vaf/tools/learn_document.py` (`ingest_document_knowledge`, shared with `learn_attached_knowledge`); `vaf/core/agent.py` (`_generate_for_document_extraction`).

### 4. Attachment-scoped learning lane (ephemeral + transfer)

For active Web UI work with attached documents, VAF uses a **separate ephemeral retrieval lane**. This keeps temporary attachment context separate from long-term memory.

| Mechanism | What it does | Where it's documented |
|-----------|--------------|------------------------|
| **Attachment index (ephemeral)** | On sidebar document updates, extracted content is indexed with `source=attachment_ephemeral`, scoped by `session_id + user_scope_id`, and expires via TTL (default 24h). Retrieval injects top-k snippets into the current turn. | [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md), [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) |
| **learn_attached_knowledge tool** | Transfers selected attachment knowledge into long-term memory only after explicit confirmation (`confirm_learn=true`). Created memories use `type=knowledge` and origin metadata (attachment transfer). | [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md), tool `learn_attached_knowledge` |

**Effect over time:** During active sessions, large attachments are handled via snippet retrieval (not full prepend), reducing token pressure. Important knowledge can be promoted intentionally into durable memory as `knowledge` nodes.

**Implementation:** `vaf/memory/attachment_rag.py`, `vaf/core/web_server.py`, `vaf/core/headless_runner.py`, `vaf/tools/learn_attached_knowledge.py`, `vaf/memory/rag.py` (lane separation), memory graph type mapping in `vaf/memory/graph.py` and `web/components/memory/MemoryGraph.tsx`.

---

### 5. Tool know-how (Whare Wananga)

Distinct from the lanes above (which learn about the *user*), this lane learns how the agent should *use its own tools* correctly, and feeds that know-how back at runtime.

| Mechanism | What it does | Where it's documented |
|-----------|--------------|------------------------|
| **Predict-then-verify training** | Offline, sandboxed practice per tool: the agent predicts a call's outcome, runs it, an LLM judge grades pass/fail, then a final challenge. The result is a `tool_knowledge` record with three baskets (when-to-use, pitfalls, procedure). Side-effecting tools are safety-tiered (full-probe / error-path / declare / gated). | [WHARE_WANANGA.md](WHARE_WANANGA.md) |
| **Proactive delivery** | After the tool router scopes a turn, each selected tool's learned pitfalls (`tuatea`) are appended to its tool-schema description, so the model sees them before it forms the call. | [WHARE_WANANGA.md](WHARE_WANANGA.md) (Delivery) |
| **Reactive delivery** | When a tool call fails, the failed tool's fuller know-how is re-fed into the loop so the natural retry is informed; the error is classified known-vs-novel. | [WHARE_WANANGA.md](WHARE_WANANGA.md) (Delivery) |
| **Runtime re-learning** | A novel, learnable runtime error (environmental/transient ones are filtered out) is distilled into a new learned pitfall from the real observation -- closing the learn-from-use loop, so the proactive/reactive lanes then carry it. | [WHARE_WANANGA.md](WHARE_WANANGA.md) (Delivery) |
| **Eager training (opt-in)** | A background scanner can proactively train safe, configured, not-yet-learned tools one at a time -- off by default (`whare_wananga_eager_enabled`), and never send/communication or irreversible tools. | [WHARE_WANANGA.md](WHARE_WANANGA.md) |
| **Teacher/Noho co-learning (opt-in)** | After a weak LOCAL run, a stronger configured API model co-learns the tool with the student over the same loop -- off by default (`whare_wananga_teacher_enabled`), only when the student is local + an API is configured. | [WHARE_WANANGA.md](WHARE_WANANGA.md) (Teacher/Noho) |

**Effect over time:** the agent makes fewer malformed tool calls (the learned pitfalls sit in front of it both before a call and after a failure); a changed tool definition invalidates its now-stale know-how until the tool is re-trained.

**Implementation:** `vaf/whare_wananga/` (`store.py`, `runner.py`, `jobs.py`, `delivery.py`, `runtime.py`, `eager.py`, `teacher.py`, `cli.py`), the `Agent.TOOLS` schema hook + tool-loop error nudge / re-learn trigger in `vaf/core/agent.py`, the `query_llm` provider/model override in `vaf/tools/base.py`, and `web/components/TrainingDashboard.tsx`. Records live globally at `~/.vaf/whare_wananga/<tool>.json`.

---

## Extending the self-learning system

When you add a **new** self-learning mechanism (e.g. learning from feedback, from automation outcomes, or from tool-usage patterns), add it here so the picture stays complete.

**Template for a new subsection:**

```markdown
### N. [Name of the subsystem]

| Mechanism | What it does | Where it’s documented |
|-----------|--------------|------------------------|
| **…** | … | Link to detailed doc or code |

**Effect over time:** …

**Implementation:** Main modules or entry points.
```

Then:

1. Add a row to the table in **Current self-learning systems** (or a new subsection under it).
2. Optionally add a dedicated doc (e.g. `SOME_FEATURE.md`) and link it from here.
3. If it affects configuration or UX, update [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md), [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md), or the relevant feature doc.

---

## See also

- [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) — RAG architecture, compaction, API, configuration
- [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md) — How memory context is injected into the prompt
- [USER_IDENTITY.md](USER_IDENTITY.md) — User profile (name, do’s/don’ts, preferences); the agent updates it via `update_user_identity` so the model learns the user better over time
