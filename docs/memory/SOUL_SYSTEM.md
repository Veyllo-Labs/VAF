# 🧠 VAF Soul System Specification

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

1. **Storage**: Under `~/.vaf/users/admin/`: `soul.md` (personality and rules) and `identity.json` (agent display: name, emoji, theme). The **current user's** profile is in `user_identity.json` per user; see [USER_IDENTITY.md](USER_IDENTITY.md).
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

The Soul can be updated over time (manually in Settings or by the agent suggesting edits). Stored in `soul.md`.

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
