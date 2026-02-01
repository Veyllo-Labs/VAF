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
The **VAF Soul System** is a sophisticated personality and behavioral framework designed to give Veyllo Agentic Framework (VAF) instances a consistent, human-like, yet technically precise character. Inspired by the "Soul as Code" philosophy, it separates the agent's core identity from its operational logic, allowing for a highly customizable and transparent AI persona.

### Measurable Outcomes:
- **Consistency**: 100% of system prompts across all connected devices will share the same Admin-defined identity.
- **Onboarding Efficiency**: New administrators can define a complete persona in under 2 minutes using the Soul Wizard.
- **Isolation**: 0% leakage of RAG data between standard users while maintaining a shared personality.

---

## 2. Architecture Overview
The Soul System operates as a bridge between the **User Workspace** and the **LLM Inference Engine**.

### Data Flow:
1. **Storage Layer**: Persona data is stored in `~/.vaf/users/admin/` as `soul.md` (Rules) and `identity.json` (Visuals).
2. **Access Layer**: The `UserWorkspace` class manages secure I/O operations for these files.
3. **API Layer**: `user_persona_routes.py` exposes endpoints for the Web UI to modify the soul.
4. **Injection Layer**: The `SystemPromptManager` dynamically reads the Admin's soul and prepends it to the LLM context for every turn, regardless of which user is logged in.

---

## 3. Module Descriptions

### 3.1 Core Truths
**Definition**: The fundamental, immutable mission and identity of the agent.  
**Outcome**: The agent will consistently refer to itself by its defined name and stick to its primary mission (e.g., "Technical Assistant") during role-play or identity queries.

### 3.2 Boundaries
**Definition**: Strict behavioral and ethical limits.  
**Outcome**: The agent will refuse to engage in prohibited actions or styles (e.g., "No small talk") with a 95% adherence rate in standard inference tests.

### 3.3 Vibe
**Definition**: The aesthetic and linguistic style of communication.  
**Outcome**: Responses will consistently reflect the chosen tone (e.g., "Concise", "Formal") as measured by linguistic analysis of output length and vocabulary choice.

### 3.4 Continuity
**Definition**: The mechanism for long-term evolution and persistence.  
**Outcome**: The agent will proactively suggest updates to its own `MEMORY.md` or `soul.md` when it detects a significant shift in user requirements.

---

## 4. API Specifications

### `GET /api/user/persona`
- **Description**: Retrieves the current identity, soul, and memory markdown.
- **Access**: Admin only.

### `PUT /api/user/soul`
- **Description**: Updates the `soul.md` file.
- **Payload**: `{ "content": "markdown string" }`
- **Access**: Admin only.

### `POST /api/user/memory/sync`
- **Description**: Re-indexes the `MEMORY.md` file into the RAG vector database.
- **Access**: Admin only.

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
If the agent fails to recall facts documented in `MEMORY.md`, use the **"Sync to RAG"** button in the Persona tab. This flushes the existing index for that user and rebuilds it from the current Markdown source.

---

## 7. Glossary of Terms
- **Soul**: The collective system instructions defining the agent's character.
- **RAG (Retrieval-Augmented Generation)**: The process of fetching relevant facts from external storage to ground LLM responses.
- **Multi-Tenancy**: The ability for multiple users to use the same system while keeping their data (memory) isolated.
- **User Scope ID**: A unique identifier used to partition the RAG database per user.
