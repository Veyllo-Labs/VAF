# Project Documentation

This directory contains comprehensive documentation for the Veyllo Agentic Framework (VAF). It serves as the primary resource for understanding the system's architecture, features, and integration details.

## Contents

### Core Systems
- **SELF_LEARNING.md**: Overview of how VAF learns from usage (current: RAG/memory; template for future self-learning extensions).
- **MEMORY_SYSTEM.md**: Self-learning RAG memory (improves with use via session compaction and memory_save), encryption, vector search, and graph visualization.
- **SOUL_SYSTEM.md**: Agent personality and rules (Soul, identity.json). Distinct from the human user profile.
- **USER_IDENTITY.md**: Current user profile (user_identity.json), update_user_identity tool, and Settings UI.
- **CONTEXT_MANAGEMENT.md**: Dynamic system prompt and token optimization strategies.
- **ACTION_TAG.md**: The `<Action>` tag — agent declares the tool it is about to use; separate collapsible Action panel in the Web UI, persistence, and LLM-context behavior.
- **WHARE_WANANGA.md**: Tool self-learning subsystem — learns per-tool `tool_knowledge` (Aronui/Tuatea/Tuarua facets); the built store/schema and the planned learning loop.
- **GATEWAY.md**: Persistent gateway server and multi-channel access.
- **WEB_UI.md**: Browser-based dashboard and WebSocket API.
- **DOCUMENT_EDITOR_NATIVE_DOCX.md**: Native DOCX editor architecture, import/export model, editor split, and Gotenberg's role.

### Speech & Voice
- **SPEECH_FEATURES.md**: Complete TTS/STT integration across CLI, Web UI, and Telegram.
- **DOCKER_SERVICES.md**: Docker containers for TTS (Piper), STT (Whisper), Gotenberg (Office→PDF), database, and Redis.

### Messaging & Integration
- **TELEGRAM_INTEGRATION.md**: Telegram bot with voice message support and user whitelisting.
- **WHATSAPP_INTEGRATION.md**: WhatsApp bridge (Baileys), voice, documents, whitelist, and best practices.
- **CONNECTIONS.md**: External service connections and authentication.
- **GITHUB_INTEGRATION.md**: GitHub OAuth, agent tools (list repos, read file, issues/PRs), and troubleshooting.
- **AUTOMATIONS.md**: Scheduled automations (create_automation), automation calendar, and planner (notes/todos).
- **Thinking-Mode.md**: Background thinking mode when the user is idle (todos, automations, one question via messenger, sessions in chat list).
- **CALENDAR_INTEGRATION.md**: Google/Microsoft calendar tools and Calendar Dashboard.
- **API_INTEGRATION.md**: Integration with various LLM providers and APIs.
- **WEBUI_WEBSOCKET_FLOW.md**: WebSocket flow, session scoping, and debugging.

### Sub-Agents
- **CODER_ARCHITECTURE.md**: Deep dive into the Coder sub-agent's design and logic.
- **SUBAGENT_IPC.md**: Inter-process communication between agents.
- **SUBAGENT_FILE_EXTRACTION.md**: File handling in sub-agents.

### Security & Execution
- **SANDBOXING.md**: Secure code execution using Docker.
- **SANDBOX_MODULES.md**: Sandboxed module system.
- **NETWORK_FEATURES.md**: Network-mode security model, OAuth session binding, and operational hardening checks (`vaf doctor` / `vaf security doctor`).

### Platform & Setup
- **SYSTEM_TRAY.md**: Tray architecture and platform notes (Windows vs macOS implementation details).
- **WINDOWS_SETUP.md**: Windows-specific setup and troubleshooting.
- **DESIGN.md**: UI design tokens and component styles.
- **MODELL_UND_PROVIDER_WECHSEL.md**: Switching between local models and API providers.

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
