# Project Documentation

This directory contains comprehensive documentation for the Veyllo Agentic Framework (VAF). It serves as the primary resource for understanding the system's architecture, features, and integration details.

## Contents

Docs are grouped into category folders. Full index by category:

### overview — architecture & building on VAF
- [ARCHITECTURE.md](ARCHITECTURE.md) — What VAF is: the framework/harness layering (engine, framework surface, product) and the public boundary (stable vs internal).
- [EMBEDDING.md](EMBEDDING.md) — Install VAF as a slim library and build on it: `from vaf import Agent`, config, writing tools, shipping tools as pip packages.
- [CONFIG_SCHEMA.md](setup/CONFIG_SCHEMA.md) — Configuration reference: all config keys by area, with defaults and which ones matter for embedding.
- [TOOLS_CATALOG.md](agents/TOOLS_CATALOG.md) — Catalog of the built-in tools the agent loads, grouped by area.

### setup/ — install, services, deployment
- [LINUX_SETUP.md](setup/LINUX_SETUP.md) — Linux setup and troubleshooting.
- [WINDOWS_SETUP.md](setup/WINDOWS_SETUP.md) — Windows-specific setup.
- [MACOS_SETUP.md](setup/MACOS_SETUP.md) — macOS setup (Apple Silicon/Intel, Metal, Homebrew).
- [FIRST_RUN.md](setup/FIRST_RUN.md) — First-run setup wizard walkthrough (admin, Soul, connections, 2FA).
- [DOCKER_SERVICES.md](setup/DOCKER_SERVICES.md) — TTS/STT/Gotenberg/DB/Redis containers.
- [NGINX_REVERSE_PROXY.md](setup/NGINX_REVERSE_PROXY.md) — Reverse proxy + HTTPS (`nginx-vaf-https.conf.example`).
- [GATEWAY.md](setup/GATEWAY.md) — Persistent gateway server, multi-channel access.
- [RELEASING.md](setup/RELEASING.md) — Cutting a release; how `vaf update` updates installed clients.
- [SERVER_MODE.md](setup/SERVER_MODE.md) — Standalone llama-server mode.
- [PROCESS_MANAGEMENT.md](setup/PROCESS_MANAGEMENT.md) — Process lifecycle management.
- [NETWORK_FEATURES.md](setup/NETWORK_FEATURES.md) — Network-mode security model, doctor checks.

### llm/ — models, providers, backend
- [LLM_BACKEND_FACTS.md](llm/LLM_BACKEND_FACTS.md) — Backend selection (API/server/library), local model facts.
- [API_INTEGRATION.md](llm/API_INTEGRATION.md) — API providers and keys.
- [PROVIDER_MODES.md](llm/PROVIDER_MODES.md) — Catalog of provider/model-specific behavior (DeepSeek, Gemma local mode) and the additive/gated principle.
- [DYNAMIC_MODEL_SELECTION.md](llm/DYNAMIC_MODEL_SELECTION.md) — Live model discovery per API provider.
- [MODEL_AND_PROVIDER_SWITCHING.md](llm/MODEL_AND_PROVIDER_SWITCHING.md) — Switching local ↔ API at runtime.
- [LAZY_LOAD_RAM_ANALYSIS.md](llm/LAZY_LOAD_RAM_ANALYSIS.md) — Model lazy-load and RAM analysis.

### memory/ — memory, context, learning, identity
- [MEMORY_SYSTEM.md](memory/MEMORY_SYSTEM.md) — Self-learning RAG memory, encryption, vector search, graph.
- [SELF_LEARNING.md](memory/SELF_LEARNING.md) — How VAF learns (the lanes) and a template for new ones.
- [CONTEXT_MANAGEMENT.md](memory/CONTEXT_MANAGEMENT.md) — Dynamic system prompt and token optimization.
- [CONTEXT_COMPRESSION_FLOW.md](memory/CONTEXT_COMPRESSION_FLOW.md) — History compression flow.
- [CONTEXT_GLUE.md](memory/CONTEXT_GLUE.md) — Context assembly between turns.
- [SESSION_MANAGEMENT.md](memory/SESSION_MANAGEMENT.md) — Session lifecycle and storage.
- [SESSION_CONTEXT_SYSTEM_PROMPT.md](memory/SESSION_CONTEXT_SYSTEM_PROMPT.md) — Per-session context in the system prompt.
- [USER_IDENTITY.md](memory/USER_IDENTITY.md) — User profile and `update_user_identity`.
- [SOUL_SYSTEM.md](memory/SOUL_SYSTEM.md) — Agent personality and rules (Soul).
- [WHARE_WANANGA.md](memory/WHARE_WANANGA.md) — Tool self-learning subsystem.

### agents/ — tools, sub-agents, workflows, reasoning
- [AGENT_LOOP.md](agents/AGENT_LOOP.md) — High-level map of the main agent turn loop (`chat_step`).
- [TOOL_ROUTER_ARCHITECTURE.md](agents/TOOL_ROUTER_ARCHITECTURE.md) — Tool router and per-turn scoping.
- [TOOL_INPUT_REPAIR.md](agents/TOOL_INPUT_REPAIR.md) — Validating and repairing model-supplied tool arguments before dispatch.
- [TOOL_SUPERVISION.md](agents/TOOL_SUPERVISION.md) — Tool supervision/safety.
- [ACTION_TAG.md](agents/ACTION_TAG.md) — The `<Action>` declaration tag.
- [CODER_ARCHITECTURE.md](agents/CODER_ARCHITECTURE.md) — Coder sub-agent design.
- [SUBAGENT_IPC.md](agents/SUBAGENT_IPC.md) — Inter-process communication between agents.
- [SUBAGENT_FILE_EXTRACTION.md](agents/SUBAGENT_FILE_EXTRACTION.md) — File handling in sub-agents.
- [BROWSER_AGENT.md](agents/BROWSER_AGENT.md) — Browser automation agent.
- [RESEARCH_AND_DOCUMENT_WORKFLOWS.md](agents/RESEARCH_AND_DOCUMENT_WORKFLOWS.md) — Research and document workflows.
- [WORKFLOW_SELECTION.md](agents/WORKFLOW_SELECTION.md) — Workflow matching/selection.
- [SKILLS.md](agents/SKILLS.md) — Agent Skills (SKILL.md): the second routing tier, `use_skill` progressive disclosure, and the security scanner.
- [FRONT_OFFICE.md](agents/FRONT_OFFICE.md) — Front-office routing.
- [THINKING_WORKSPACE.md](agents/THINKING_WORKSPACE.md) — Thinking-mode workspace.
- [Thinking-Mode.md](agents/Thinking-Mode.md) — Background thinking mode when idle.
- [MCP_INTEGRATION.md](agents/MCP_INTEGRATION.md) — MCP exposed as a tool system.

### documents/ — document reading, creation, editing
- [DOCUMENT_READING.md](documents/DOCUMENT_READING.md) — PDF/Office extraction to Markdown.
- [DOCUMENT_CREATION.md](documents/DOCUMENT_CREATION.md) — Document creation.
- [DOCUMENT_EDITOR_NATIVE_DOCX.md](documents/DOCUMENT_EDITOR_NATIVE_DOCX.md) — Native DOCX editor architecture.
- [LIBRARIAN_CONFIGURATION.md](documents/LIBRARIAN_CONFIGURATION.md) — Librarian configuration.

### integrations/ — connections & messengers
- [CONNECTIONS.md](integrations/CONNECTIONS.md) — External service connections and auth.
- [TELEGRAM_INTEGRATION.md](integrations/TELEGRAM_INTEGRATION.md) — Telegram bot.
- [WHATSAPP_INTEGRATION.md](integrations/WHATSAPP_INTEGRATION.md) — WhatsApp bridge (Baileys).
- [GITHUB_INTEGRATION.md](integrations/GITHUB_INTEGRATION.md) — GitHub OAuth and agent tools.
- [CALENDAR_INTEGRATION.md](integrations/CALENDAR_INTEGRATION.md) — Google/Microsoft calendar.

### web-ui/ — frontend, design, voice
- [WEB_UI.md](web-ui/WEB_UI.md) — Browser dashboard and WebSocket API.
- [WEBUI_WEBSOCKET_FLOW.md](web-ui/WEBUI_WEBSOCKET_FLOW.md) — WebSocket flow, session scoping, debugging.
- [WORKFLOW_UI_COMPONENTS.md](web-ui/WORKFLOW_UI_COMPONENTS.md) — Workflow UI components.
- [AgentAvatar.md](web-ui/AgentAvatar.md) — Agent avatar.
- [WelcomeGreeting.md](web-ui/WelcomeGreeting.md) — Welcome greeting.
- [WINDOW_TILING_DESIGN.md](web-ui/WINDOW_TILING_DESIGN.md) — Window tiling design.
- [DESIGN.md](web-ui/DESIGN.md) — UI design tokens and component styles.
- [SPEECH_FEATURES.md](web-ui/SPEECH_FEATURES.md) — TTS/STT integration.
- [NETWORK_TAB.md](web-ui/NETWORK_TAB.md) — Network tab UI.

### security/ — sandboxing & isolation
- [SANDBOXING.md](security/SANDBOXING.md) — Secure code execution via Docker.
- [SANDBOX_MODULES.md](security/SANDBOX_MODULES.md) — Sandboxed module system.
- [USER_ISOLATION.md](security/USER_ISOLATION.md) — Per-user scope isolation.

### platform/ — tray, automations, i18n
- [SYSTEM_TRAY.md](platform/SYSTEM_TRAY.md) — Tray architecture and platform notes.
- [AUTOMATIONS.md](platform/AUTOMATIONS.md) — Automations, timers, planner.
- [I18N.md](platform/I18N.md) — Internationalization.
- [TRANSLATION_SYSTEM.md](platform/TRANSLATION_SYSTEM.md) — Translation system.
- [VOCABULARY_BOOK.md](platform/VOCABULARY_BOOK.md) — Backend canned phrases (multilingual nudge, expandable).
- [UUID.md](platform/UUID.md) — UUID/ID scheme.
- [ABOUT.md](platform/ABOUT.md) — About VAF.

See individual files for detailed documentation.

---

## Quick Start

### Start Docker Services

The installer (`install.sh` / `install.ps1`) handles this automatically—it detects changes to `docker-compose.memory.yml` after `git pull`, starts Docker if needed, and applies updates. To start services manually:

```bash
docker compose -f docker-compose.memory.yml up -d
```

### Verify Services
```bash
docker ps --filter "name=vaf-"
```

### Key Ports
| Service | Port | Description |
|---------|------|-------------|
| PostgreSQL | 5432 | Memory database |
| Redis | 6379 | Cache |
| TTS | 5002 | Text-to-Speech |
| STT | 5003 | Speech-to-Text |
| Gotenberg | 5005 | Document conversion (Office→PDF) |
| Web UI | 3000 | Browser interface |
| API | 8000 | Backend API |

---

## Usage for Developers

Developers should consult these documents when:
- Implementing new tools or sub-agents.
- Troubleshooting core system behavior (e.g., context compression).
- Setting up external bridges like Telegram or Discord.
- Modifying the Web UI or Gateway architecture.
- Adding speech features or new TTS/STT engines.

---

## Writing Style

When adding new documentation:
- Use clear, technical English.
- Include architecture diagrams or flowcharts where applicable (using ASCII art or code blocks).
- Provide practical examples for configuration and usage.
- Include troubleshooting sections for common issues.
- Ensure all links between documents are maintained and valid.
