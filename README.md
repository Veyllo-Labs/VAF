# VAF — Veyllo Agentic Framework

```
O))         O))       O))))))))
 O))       O))))      O))
  O))     O))  O))    O))
   O))   O))    O))   O))))))
    O)) O)) )))) O))  O))
     O))))        O)) O))
      O))          O))O))     (OO )
```

An autonomous agent framework built on top of local and cloud LLMs. VAF runs as a desktop application, a headless server, or a terminal interface — on Windows, macOS, and Linux.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE) [![Commercial license](https://img.shields.io/badge/Commercial%20license-available-success.svg)](COMMERCIAL.md)

**Dual-licensed:** free under the [GNU AGPL-3.0](LICENSE), or a [commercial license](COMMERCIAL.md) for proprietary/SaaS use without copyleft — `legal@veyllo.io`.

**Requires:** Git to clone, and a container runtime — VAF keeps users, auth, setup and memory in a PostgreSQL/pgvector container, so one is required to finish setup and sign in. The Windows installer sets up the free Rancher Desktop for you, and macOS auto-installs the free Colima (or uses Docker Desktop if present); on Linux install your distro's Docker package. The installer provisions Python and Node itself (via uv / a portable Node).

---

## Start here

Pick your path:

| You want to… | Go to |
|---|---|
| **Run VAF** as a desktop app, server, or terminal | [Installation](#installation) below |
| **Build on VAF** as a library (embed the agent in your app) | [docs/EMBEDDING.md](docs/EMBEDDING.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| **Write a tool** (extend the agent, ship it as a pip package) | [vaf/tools/README.md](vaf/tools/README.md) · [docs/EMBEDDING.md](docs/EMBEDDING.md) |
| **Contribute to the engine** (the core agent loop) | [vaf/core/README.md](vaf/core/README.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |

---

## Installation

**Windows:**
```powershell
git clone https://github.com/Veyllo-Labs/VAF.git && cd VAF
.\install.ps1
```

**macOS / Linux:**
```bash
git clone https://github.com/Veyllo-Labs/VAF.git && cd VAF
chmod +x install.sh && ./install.sh
```

The installer sets up a Python venv, installs all dependencies, prepares the web UI (built on first launch), detects an existing Docker runtime (it doesn't install one), and adds the `vaf` command to your shell.

**Installation mode** — the installer asks once:
```
[1] Desktop  — personal use, local only, system tray (default)
[2] Server   — always-on service, LAN accessible via HTTPS, starts at boot
```
Choose **[2] Server** for home servers, NAS devices, or any headless machine that should be reachable from other devices. See [docs/setup/SERVER_MODE.md](docs/setup/SERVER_MODE.md) for details.

---

## Updating

VAF updates in place to the latest published release:

```bash
vaf update           # Stop, fetch the latest release, reinstall, migrate, restart
vaf update check     # Check whether a newer version is available
```

`vaf update` only proceeds from a clean checkout, records a rollback point, and reverts automatically if any step fails. A one-line "update available" hint appears at startup (disable via `update_check_on_start` in `config.json`). Maintainers cutting releases: see [docs/setup/RELEASING.md](docs/setup/RELEASING.md).

---

## Modes

VAF runs in three modes depending on your use case.

### Desktop (recommended for personal use)

The desktop app — system-tray icon + agent window, web UI at `http://localhost:3000`. Easiest start: double-click the **VAF Agent** shortcut the installer created, or run:

```bash
vaf tray       # Start the desktop app (tray + web UI)
```

Or manage it as a background service:

```bash
vaf start      # Start in background
vaf stop       # Stop cleanly
vaf restart    # Restart
vaf status     # Show status
```

**LAN access** (other devices on your network):
```bash
vaf server on      # Bind to 0.0.0.0 with HTTPS
vaf server off     # Back to localhost only
vaf server status  # Show active URLs
```

### Server (always-on, starts at boot)

Installed via the **[2] Server** option. VAF runs as a systemd user service — use the same commands:

```bash
vaf start      # systemctl --user start vaf
vaf stop       # systemctl --user stop vaf
vaf restart    # systemctl --user restart vaf
vaf status     # systemctl --user status vaf
```

LAN access (HTTPS on port 8443) is always enabled and locked in server mode. See [docs/setup/SERVER_MODE.md](docs/setup/SERVER_MODE.md).

### Terminal (CLI)

Interactive chat in the terminal with themes, session management, and streaming output.

```bash
vaf run                        # Start interactive chat
vaf run --session <id>         # Resume a saved session
vaf run --classic              # Minimal interface (SSH / low-bandwidth)
vaf run --no-web               # Skip starting the web UI
```

One-shot (non-interactive):
```bash
vaf prompt "Explain async/await in Python"
vaf prompt "List modified files" --output-format json
```

### Web UI only

If you want the web interface without the tray:
```bash
vaf run --web
```

---

## Models

VAF works with **local GGUF models** and **cloud APIs**. On first start it downloads the default model and the llama-server backend automatically.

**Local (privacy-first):**
- Any GGUF model (Llama 3, Mistral, Qwen, DeepSeek, etc.)
- GPU acceleration: CUDA on Windows, Vulkan on Linux, Metal on macOS
- Minimum context: 32 768 tokens (enforced; required for tool use)

**Cloud APIs:**
- Veyllo
- OpenAI
- Anthropic 
- Google 
- DeepSeek 
- OpenRouter (100+ models)

Switch provider or model at runtime: press `C` in the chat, or use `/model <name>`.

---

## Features

**Agent & Tools**
- 100+ built-in tools: web search, file system, bash, Python execution, calendar, email, GitHub, and more
- Sub-agents for long-running tasks: Coder, Researcher, Librarian
- Docker sandboxing for untrusted code execution
- Scheduled automations (daily news, reminders, reports)

**Memory**
- Persistent long-term memory with semantic search (pgvector + Redis)
- Auto-compression when context fills up; full history archived and restorable
- Memory graph visualization in the web UI

**Sessions & History**
- Named sessions, saved and searchable
- Git-based snapshot/undo for code changes
- Export sessions to Markdown

**Integrations**
- Telegram, Discord, WhatsApp (bridge mode)
- Google Calendar, Gmail, Google Drive
- GitHub

**Developer**
- Plugin system: drop a `BaseTool` subclass into `vaf/tools/` — auto-discovered at startup
- Custom workflows: place a `WORKFLOW` dict in `~/.vaf/workflows/*.py` — auto-loaded
- MCP (Model Context Protocol) client support

---

## CLI Reference

```bash
vaf start            # Start background service (or: systemctl in server mode)
vaf stop             # Stop background service
vaf restart          # Restart background service
vaf status           # Show service status

vaf run              # Interactive chat (TUI)
vaf prompt "..."     # One-shot prompt
vaf tray             # Background service + web UI (foreground)
vaf server on|off|status

vaf update           # Update to the latest release
vaf update check     # Check if a newer release is available

vaf session list|load|delete|export|search
vaf snapshot create|list|restore|undo
vaf automation list|create|run|enable|disable|delete

vaf scaffold new <template> <name>   # Project templates
vaf generate api|function|class|test|component
vaf automate test|build|lint|format|check
vaf debug explain|trace|fix
vaf git commit --auto

vaf models list|set
vaf theme <name>
vaf install-gpu      # Re-detect and install GPU backend
vaf info             # System info
vaf --version
```

---

## Configuration

All user data lives in `~/.vaf/`:

```
~/.vaf/
├── config.json        # Settings (provider, model, n_ctx, ...)
├── sessions/          # Saved conversations
├── context_archive/   # Compressed conversation history
├── snapshots/         # Code undo history
├── automations/       # Scheduled tasks
└── workflows/         # Custom workflow plugins (*.py)
```

Key config options (`config.json`):

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | `local` | `local`, `veyllo`, `openai`, `anthropic`, `google`, `deepseek`, `openrouter` |
| `n_ctx` | `32768` | Context window (minimum 32 768 for tool use) |
| `gpu_layers` | `99` | Layers to offload to GPU (`0` = CPU only) |
| `web_ui_enabled` | `true` | Start web UI alongside the agent |
| `persist_server` | `false` | Keep llama-server running after VAF exits |

---

## Extending VAF

**Custom tool** — create `vaf/tools/my_tool.py`:
```python
from vaf.tools.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "One-line description the agent uses to decide when to call this."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"}
        },
        "required": ["query"]
    }

    def run(self, **kwargs) -> str:
        return f"result for {kwargs['query']}"
```

**Custom workflow** — create `~/.vaf/workflows/my_workflow.py`:
```python
WORKFLOW = {
    "name": "My Workflow",
    "triggers": ["my keyword"],
    "variables": {"topic": "What to process"},
    "steps": [
        {"tool": "web_search", "input": "{topic}", "output": "results"},
        {"tool": "coding_agent", "input": "Based on {results}, write code for {topic}", "output": "code"},
    ],
}
```

Both are auto-discovered at startup — no registration needed.

---

## Platform Notes

| | Windows | macOS | Linux |
|---|---|---|---|
| Desktop mode | Tray icon | Menu bar icon | Headless (no icon) |
| GPU backend | CUDA | Metal | Vulkan |
| Web UI | `localhost:3000` | `localhost:3000` | `localhost:3000` |
| Install | `install.ps1` | `install.sh` | `install.sh` |

Linux headless mode requires no display server. See [docs/setup/LINUX_SETUP.md](docs/setup/LINUX_SETUP.md) for details.

---

## Documentation

| Topic | Doc |
|---|---|
| Build on VAF (embed as a library) | [docs/EMBEDDING.md](docs/EMBEDDING.md) |
| Architecture (framework vs. harness) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Configuration reference (all config keys) | [docs/setup/CONFIG_SCHEMA.md](docs/setup/CONFIG_SCHEMA.md) |
| Write a tool (BaseTool, pip packaging) | [vaf/tools/README.md](vaf/tools/README.md) |
| Server mode (LAN, autostart) | [docs/setup/SERVER_MODE.md](docs/setup/SERVER_MODE.md) |
| Linux setup & GPU | [docs/setup/LINUX_SETUP.md](docs/setup/LINUX_SETUP.md) |
| LLM backend (local vs API) | [docs/llm/LLM_BACKEND_FACTS.md](docs/llm/LLM_BACKEND_FACTS.md) |
| Memory system | [docs/memory/MEMORY_SYSTEM.md](docs/memory/MEMORY_SYSTEM.md) |
| Web UI & API reference | [docs/web-ui/WEB_UI.md](docs/web-ui/WEB_UI.md) |
| Context management | [docs/memory/CONTEXT_MANAGEMENT.md](docs/memory/CONTEXT_MANAGEMENT.md) |
| Automations | [docs/platform/AUTOMATIONS.md](docs/platform/AUTOMATIONS.md) |
| Docker services | [docs/setup/DOCKER_SERVICES.md](docs/setup/DOCKER_SERVICES.md) |
| Telegram / Discord / WhatsApp | [docs/integrations/TELEGRAM_INTEGRATION.md](docs/integrations/TELEGRAM_INTEGRATION.md) |
| Workflows | [vaf/workflows/README.md](vaf/workflows/README.md) |

---

## Star History

<a href="https://www.star-history.com/#Veyllo-Labs/VAF&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Veyllo-Labs/VAF&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Veyllo-Labs/VAF&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Veyllo-Labs/VAF&type=Date" />
 </picture>
</a>

---

## License

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE) [![Commercial license](https://img.shields.io/badge/Commercial%20license-available-success.svg)](COMMERCIAL.md)

VAF is **dual-licensed**:

- **Open source — [GNU AGPL-3.0-or-later](LICENSE)**: free to use, modify, and distribute. If you distribute VAF or run a **modified** version as a network service (SaaS), you must make your source available under the AGPL. Unmodified internal use — even commercial — triggers no disclosure. Building Plugins, Tools, and Workflows on top of VAF is explicitly permitted (Section 7 additional permission).
- **Commercial license**: for embedding VAF in a closed-source product or running a proprietary SaaS without AGPL copyleft. See **[COMMERCIAL.md](COMMERCIAL.md)**.

See **[LICENSING.md](LICENSING.md)** for the full dual-licensing explanation (English & German) and how to choose.

**Trademarks & brand:** "VAF", "Veyllo", the VAF logo, and the agent avatar (the living-dot visual identity and its animated states) are trademarks and brand assets of Veyllo GmbH and are **not** covered by the code license. See the "Trademarks and Brand Assets" section in [LICENSING.md](LICENSING.md).

For commercial and OEM licensing, contact **legal@veyllo.io** · **https://veyllo.io**.
