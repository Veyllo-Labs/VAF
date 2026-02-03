# Project Documentation

This directory contains comprehensive documentation for the Veyllo Agentic Framework (VAF). It serves as the primary resource for understanding the system's architecture, features, and integration details.

## Contents

### Core Systems
- **MEMORY_SYSTEM.md**: RAG-powered memory storage with encryption, vector search, and graph visualization.
- **SOUL_SYSTEM.md**: Agent personality and rules (Soul, identity.json). Distinct from the human user profile.
- **USER_IDENTITY.md**: Current user profile (user_identity.json), update_user_identity tool, and Settings UI.
- **CONTEXT_MANAGEMENT.md**: Dynamic system prompt and token optimization strategies.
- **GATEWAY.md**: Persistent gateway server and multi-channel access.
- **WEB_UI.md**: Browser-based dashboard and WebSocket API.

### Sub-Agents
- **CODER_ARCHITECTURE.md**: Deep dive into the Coder sub-agent's design and logic.
- **SUBAGENT_IPC.md**: Inter-process communication between agents.
- **SUBAGENT_FILE_EXTRACTION.md**: File handling in sub-agents.

### Integration & APIs
- **API_INTEGRATION.md**: Integration with various LLM providers and APIs.
- **WEBUI_WEBSOCKET_FLOW.md**: WebSocket flow, session scoping, and debugging.
- **MODELL_UND_PROVIDER_WECHSEL.md**: Wechsel zwischen lokalem Modell und API (Overlay, VRAM-Entladen/-Laden).

### Security & Execution
- **SANDBOXING.md**: Secure code execution using Docker.
- **SANDBOX_MODULES.md**: Sandboxed module system.

### Platform & Setup
- **WINDOWS_SETUP.md**: Windows-specific setup and troubleshooting.
- **DESIGN.md**: UI design tokens and component styles.

See individual files for detailed documentation.

## Usage for Developers

Developers should consult these documents when:
- Implementing new tools or sub-agents.
- Troubleshooting core system behavior (e.g., context compression).
- Setting up external bridges like Discord.
- Modifying the Web UI or Gateway architecture.

## Writing Style

When adding new documentation:
- Use clear, technical English.
- Include architecture diagrams or flowcharts where applicable (using Mermaid or code blocks).
- Provide practical examples for configuration and usage.
- Ensure all links between documents are maintained and valid.
