# VAF Core Package

This is the main Python package for the Veyllo Agentic Framework (VAF). It contains all the core logic, CLI implementation, tools, and workflow definitions.

## Package Structure

### Subpackages

- **api/**: FastAPI route modules for the Web UI and messaging bridges (auth, calendar, email, GitHub, Telegram, WhatsApp, Discord, etc.).
- **auth/**: User authentication and authorization — crypto, database, middleware, models, and rate limiting.
- **cli/**: Implementation of the Terminal User Interface (TUI), themes, and command-line commands.
- **cloud/**: Cloud storage providers (Google Drive, Dropbox, iCloud) and the credential-cloud abstraction.
- **core/**: The heart of the framework, handling LLM management, context, persistence, and the gateway server.
- **github/**: GitHub integration — OAuth, activity, and credential handling.
- **media/**: Static assets (icons, logos, sounds) bundled with the app.
- **memory/**: Long-term memory and RAG — vector database, attachment RAG, crypto, and caching.
- **models/**: Local cache directory for downloaded machine-learning models.
- **network/**: Networking primitives — binding, firewall, HTTPS proxy, OAuth redirect, and SSL utilities.
- **skills/**: Anthropic Agent Skills (SKILL.md) support — scanning, parsing, and templates.
- **sources/**: Configuration and data for information sources (e.g., news, tech).
- **tools/**: A library of executable tools and sub-agents (Coder, Researcher, Librarian) that extend the agent's capabilities.
- **vendor/**: Vendored third-party code (e.g., langid) shipped in-tree.
- **whare_wananga/**: The per-tool self-learning system (knowledge delivery, jobs, and eager learning).
- **whatsapp_node/**: Node.js WhatsApp bridge process and its dependencies.
- **workflows/**: Plug-and-play multi-step pipelines and the execution engine.

### Top-level modules

- **main.py**: Primary entry point that bootstraps and launches the application.
- **tray.py**: Cross-platform system-tray background service.
- **framework.py**: The public, stable library façade (`from vaf import Agent`).
- **version.py**: Single source of truth for the package version.

## Development

Developers working on the backend or CLI should focus their efforts within this directory. The framework is designed to be modular:
- Add tools to `vaf/tools/`.
- Add commands to `vaf/cli/cmd/`.
- Add core logic to `vaf/core/`.

## Dependencies

- **Local LLM**: Managed by `vaf/core/backend.py`.
- **External APIs**: Handled via `vaf/core/api_backend.py`.
- **Rich/Prompt Toolkit**: Used for the CLI and TUI.

## Coding Conventions

- Strictly follow PEP 8 for Python code.
- Use type hints for all function signatures.
- Document classes and methods with clear docstrings.
