# VAF Soul System Specification

**Version:** 1.0.0  
**Status:** Production  
**Owner:** VAF Core Team

---

## Table of Contents
1. [Introduction](#1-introduction)
2. [Architecture Overview](#2-architecture-overview)
3. [Module Descriptions](#3-module-descriptions)
4. [API Specifications](#4-api-specifications)
5. [Deployment Guide](#5-deployment-guide)
6. [Maintenance Procedures](#6-maintenance-procedures)
7. [Glossary of Terms](#7-glossary-of-terms)

---

## 1. Introduction

The Soul system defines the **agent's** personality and rules. It is stored as files in the admin workspace and injected into the system prompt so the agent answers in a consistent way.

- **Consistency**: The same Soul is used for all users on the same instance.
- **Separation**: The **human user's** profile (name, language, preferences) is stored separately in `user_identity.json`. See [USER_IDENTITY.md](USER_IDENTITY.md).

---

## 2. Architecture Overview
The Soul System operates as a bridge between the **User Workspace** and the **LLM Inference Engine**.

### Data flow

1. **Storage**: Under `~/.vaf/users/<owner>/`, where `<owner>` is the machine owner's account name from `local_admin_username` (`admin` only when the account is called that): `soul.md` (personality and rules) and `identity.json` (agent display: name, emoji, theme). The **current user's** profile is in `user_identity.json` per user; see [USER_IDENTITY.md](USER_IDENTITY.md).
2. **Access**: `UserWorkspace` in `vaf/auth/user_workspace.py` reads and writes these files.
3. **API**: `user_persona_routes.py` exposes GET/PUT for persona and soul.
4. **Prompt**: `SystemPromptManager.build_prompt()` injects the Soul (and agent identity) into the system prompt each turn.

---

## 3. Module Descriptions

### 3.1 Core Truths

The agent's main mission and how it refers to itself (name in `identity.json`). Keeps answers aligned with that role.

### 3.2 Boundaries

Rules and limits (e.g. no small talk, no external actions without confirmation). The Soul text is injected so the model can follow them.

### 3.3 Vibe

Tone and style (e.g. concise, formal). Defined in the Soul markdown and applied to every reply.

### 3.4 Continuity

The Soul can be updated over time (manually in Settings or by the agent suggesting edits). Stored in `soul.md`. The default Continuity text (wizard suggestions and the backend fallback soul) names the agent's REAL persistence lane explicitly: recall via the `memory_search` tool, persist via `memory_save`. It deliberately does not speak of "memory files" - there is no such file (see RAG Maintenance below), and a soul that points at files sends the agent looking for something that does not exist instead of calling its tools.

The lane does not DEPEND on the soul's text: `build_prompt` appends a code-owned
continuity addendum (`SOUL_CONTINUITY_ADDENDUM` in `vaf/core/system_prompt.py`) to the
persona block after the soul content, on the soul path and the no-soul fallback path
alike. It is invisible in `soul.md` and not user-editable, so a soul whose author removed
(or never chose) the continuity lines still carries the memory contract - as a condensed,
never-contradicting echo of the `<memory_instructions>` block, deliberately repeated in
the section the model reads as its identity so it cannot be overlooked. The embedder
persona override deliberately does NOT carry it: an override replaces the persona
wholesale, and an embedder may not even register these tools.

A second code-owned addendum rides the persona block under the same delivery rule
(`build_capability_addendum` in `vaf/core/system_prompt.py`, appended on the soul path
and the fallback path, never on the embedder override): the capability answer. The
identity text bans the generic assistant self-description when someone asks what the
agent can do, and this addendum defines the GOOD answer instead - turn the question
around, ask what the user wants, make clear the agent adapts to them, and offer
examples that fit this user and channel. Its claims are grounded in the live registry:
the tool count is the session's real count, and each ability line (build a missing
tool or skill via `create_agent_tool` / `create_skill`, put a team on a problem via
sub-agents and `create_agent_workflow`, standing orders via `create_automation`)
appears only when those tools are actually registered, so the prompt cannot promise
what the runtime would refuse. Guard: `tests/test_capability_answer_prompt.py`.

Both addenda are part of the public facade (`vaf.SOUL_CONTINUITY_ADDENDUM`,
`vaf.build_capability_addendum`), so an embedder whose `system_prompt` override
replaces the persona can re-add or adapt either under their own voice; the
embedder-facing half is documented in [EMBEDDING.md](../EMBEDDING.md) and pinned by
`tests/contract/test_contract_persona_addenda.py`.

---

## 4. API Specifications

### `GET /api/user/persona`

Returns `identity` (agent name, emoji, theme), `user_identity` (current user profile; see [USER_IDENTITY.md](USER_IDENTITY.md)), and `soul` (markdown). Used by Settings and the User Identity modal.

### `PUT /api/user/identity`

Updates `identity.json` (agent display only: name, emoji, theme). Payload: optional `name`, `emoji`, `theme`.

### `PUT /api/user/soul`

Updates `soul.md`. Payload: `{ "content": "markdown string" }`.

---

## 5. Deployment Guide

### Initial Setup (Onboarding)
Upon the first launch of VAF, the system detects if an Admin exists. During the **Bootstrap Process**:
1. User creates Admin credentials.
2. The **Soul Wizard** launches automatically.
3. Steps 1-4 guide the admin through defining Core Truths, Boundaries, Vibe, and Continuity.
4. On completion, the workspace files are generated, and the agent is initialized.

---

## 6. Maintenance Procedures

### Updating the Persona
Admins can refine the agent at any time via **Settings > Persona & Memory**. 
- **Manual Edit**: Directly edit the Markdown in the provided text areas.
- **Wizard Reset**: Re-run the Soul Wizard to overwrite the existing personality.

### RAG Maintenance
Long-term facts are stored via the **memory_save** tool and auto-capture; they are indexed in the RAG database. Use the **Memory** page (or Settings > Persona > View Graph) to inspect and manage memories. There is no separate MEMORY.md file; RAG is populated from tool usage and optional auto-capture.

---

## 7. Glossary

- **Soul**: The markdown in `soul.md` that defines the agent's personality and rules. Injected into the system prompt.
- **identity.json**: Agent display (name, emoji, theme). Used in the Soul block. Not the human user's profile.
- **user_identity.json**: The current human user's profile (name, language, preferences, do's/don'ts). See [USER_IDENTITY.md](USER_IDENTITY.md).
- **User scope ID**: Identifier that scopes RAG and user data per user when multiple users share the same instance.
