# 文 Veyllo Agentic Framework (VAF)
```
O))         O))       O))))))))
 O))       O))))      O))      
  O))     O))  O))    O))      
   O))   O))    O))   O))))))  
    O)) O)) )))) O))  O))      
     O))))        O)) O))      
      O))          O))O))     (OO ) 
```
VAF is a comprehensive agent suite designed to transform LLMs into autonomous powerhouses. It features a modular plug-and-play architecture, allowing you to extend agent capabilities with custom Python workflows. Built for Python 3.10+, VAF offers a terminal UI, cross-platform support (Windows, Linux, macOS), session management, and powerful automation tools.

**Note on Models:** VAF works with any GGUF model (Llama 3, Mistral, Qwen, etc.).

**New in VAF 2.5:**
*   **Memory System (RAG):** Encrypted memory storage with semantic search, graph visualization, and AI-powered retrieval.
*   **Auto-Installer:** Cross-platform installation scripts with Docker detection (`install.sh`, `install.ps1`).
*   **System Tray:** Persistent background server for instant agent availability.
*   **Gateway Architecture:** A persistent control plane for concurrent multi-channel access.
*   **Docker Sandboxing:** Secure code execution in isolated containers.
*   **Discord Bridge:** Connect your agent to external platforms seamlessly.

---

## 📥 Installation & Setup

### Prerequisites
- **Python 3.10+** (3.12 recommended)
- **Git** (for cloning the repository)
- **Docker** (Optional - required for Memory System and Sandboxing)
- **Node.js 18+** (Optional - for Web UI development)

### Quick Install (Recommended)

**🪟 Windows:**
```powershell
# Clone the repository
git clone https://github.com/Veyllo-Labs/VAF.git
cd VAF

# Run the installer (double-click install.bat or run in PowerShell)
.\install.ps1
```

**🍎 macOS / 🐧 Linux:**
```bash
# Clone the repository
git clone https://github.com/Veyllo-Labs/VAF.git
cd VAF

# Run the installer
chmod +x install.sh
./install.sh
```

### What the Installer Does

The auto-installer handles everything:

1. ✅ **System Detection** - OS, GPU (NVIDIA/AMD/Apple Silicon), package manager
2. ✅ **Python Setup** - Virtual environment, all dependencies
3. ✅ **Docker Check** - Detection for Memory System database
4. ✅ **Node.js Check** - For Web UI
5. ✅ **Shortcuts** - Desktop/Start Menu (Windows), App Bundle (macOS), Desktop Entry (Linux)
6. ✅ **Shell Alias** - `vaf` command added to your shell

### Installation Options

```bash
# Skip Docker setup
./install.sh --skip-docker        # macOS/Linux
.\install.ps1 -SkipDocker         # Windows

# Force recreate virtual environment
.\install.ps1 -Force              # Windows

# Show help
./install.sh --help
```

### Alternative: Manual Install

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -e .
pip install -r requirements.txt

# (Optional) Start Memory System database (Postgres + Redis + Sandbox)
docker compose -f docker-compose.memory.yml up -d
```

If you don't see DB/Redis/Sandbox containers in Docker, use the same file—see [docs/DOCKER_SERVICES.md](docs/DOCKER_SERVICES.md).

For platform-specific troubleshooting, see [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).

### 3. Run VAF

VAF offers two distinct modes tailored to your workflow:

| Mode | Entry Point | Best For |
|------|-------------|----------|
| **Desktop Mode** | **Desktop Shortcut** or `vaf tray` | Silent background service, Web Dashboard, persistent agent access. |
| **CLI Mode** | `vaf run` | Professional TUI for fast, keyboard-centric coding and research. |

---

## 🚀 Advanced Features (Gateway & Bridges)

VAF runs a persistent background service (Gateway) to handle WebUI and multi-channel access. In **Desktop Mode**, this service starts silently without a console window.

### 1. View Logs
For troubleshooting the background service, check the startup trace:
`logs/startup_trace.txt`

WebUI debug logs are written to the first available location:
1. `VAF_LOG_DIR` (if set)
2. `Platform.data_dir()/logs`
3. `Platform.vaf_dir()/logs`
4. Repo `logs/` (dev fallback)

### 2. Connect Discord (Bridge)
Once VAF is running (Tray or CLI), you can connect it to Discord.
```bash
vaf bridge discord --token "YOUR_DISCORD_BOT_TOKEN"
```

For more details, see [docs/GATEWAY.md](docs/GATEWAY.md).

### 3. Safe Execution (Sandboxing)
VAF uses Docker to run risky code. Make sure Docker Desktop is running before using the **Coder** or **Researcher** agents.

See [docs/SANDBOXING.md](docs/SANDBOXING.md).

---

## ✨ Highlights

- 🧠 **Memory System (RAG)** - Persistent encrypted memory with semantic search and graph visualization
- 🌐 **Web UI Dashboard** - Browser-based interface with real-time updates and session management
- 🎨 **Modern TUI** - Beautiful input box with smart autocomplete and themes
- ⌨️ **Smart AutoSuggest** - Inline word completion like Google Search
- 🎭 **12+ Themes** - Dracula, Nord, Tokyo Night, Catppuccin, and more
- 💾 **Session Management** - Save, load, and search conversations
- ⏪ **Undo/Snapshot** - Git-based code change tracking and rollback
- ⚡ **Persistent Mode** - Background service with system tray icon for instant access.
- ⚡ **Scheduled Automations** - Time-based tasks (daily news, weather reports)
- 🔄 **Workflows** - Plug-and-play multi-step pipelines (create website, research & code, etc.)
- 🛠️ **Powerful Tools** - Bash execution, web fetching, parallel operations
- 🤖 **Sub-Agents** - Specialized agents for coding, research, and file navigation
- 🚀 **Non-Interactive Mode** - Run one-shot prompts: `vaf prompt "..."` or `vaf run "..."
- 🔒 **Local-First & Privacy** - Zero data collection. All intelligence is processed locally on your hardware. Data only leaves your system if you explicitly use cloud APIs, external Workflows, or Tools (e.g., Web Search). Links open in incognito mode by default.
- 📊 **Live Progress** - Real-time TUI for research and coding tasks

---

## Features

### 🧠 Memory System (RAG)

Persistent, encrypted memory storage with RAG (Retrieval-Augmented Generation) for intelligent information retrieval.

**Key Features:**
- **Encrypted Storage**: AES-256-GCM encryption for all memory content at rest
- **Vector Search**: Semantic similarity search using pgvector (PostgreSQL)
- **RAG Pipeline**: Query memories with AI-powered answer generation and source citations
- **Graph Visualization**: Interactive ReactFlow-based memory graph
- **Auto-Connections**: Automatically links related memories based on semantic similarity

**Quick Start:**
```bash
# Start the Memory System database (requires Docker)
docker compose -f docker-compose.memory.yml up -d

# Access the Memory Graph UI
# Open: http://localhost:3000/memory
# Or via Settings → Advanced → System → Memory System
```

**API Example:**
```bash
# Create a memory
curl -X POST http://localhost:8000/api/memory \
  -H "Content-Type: application/json" \
  -d '{"content": "Important project notes...", "metadata": {"title": "Project Notes"}}'

# RAG Query
curl -X POST http://localhost:8000/api/memory/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the project notes?", "k": 5}'
```

**Configuration (Settings → Advanced → System):**
- Enable/Disable Memory System
- Chunk Size (tokens)
- Auto-Connect Threshold

**Documentation:** See [docs/MEMORY_SYSTEM.md](docs/MEMORY_SYSTEM.md) for full API reference and configuration.

### 🌐 Web UI Dashboard

Browser-based interface for interacting with VAF. In Desktop Mode (`vaf tray`) it starts automatically; in CLI mode it can be started with `--web`.

**Features:**
- Real-time chat with streaming responses
- Session management (create, load, delete)
- Collapsible thinking process display
- System workflow visualization
- Inline Tool Execution (tools appear in chat stream)
- WebSocket-based communication
- Auto-opens in browser on startup
- UI language selection (Settings → Interface); multiple locales supported (see [docs/I18N.md](docs/I18N.md))

**Access:**
- Frontend: `http://localhost:3000` (auto-detected port)
- Backend API: `http://localhost:8001`

**Control:**
```bash
# Enable Web UI (default)
vaf run --web

# Disable Web UI
vaf run --no-web

# Configure in settings
vaf settings  # Set web_ui_enabled
```

**Documentation:** See [docs/WEB_UI.md](docs/WEB_UI.md) for architecture details and API reference.

### 🎨 Python-based TUI

Beautiful, modern terminal interface with input box, themes, and rich formatting.

```bash
# Start interactive chat with modern UI
vaf run

# Use specific theme
vaf run --theme dracula
vaf run --theme nord

# Resume a previous session
vaf run --session abc123

# Use minimal interface (troubleshooting/SSH fallback)
vaf run --classic

# Start persistent background service
vaf tray
```

**Keyboard Shortcuts (in chat):**
| Key | Action |
|-----|--------|
| `S` | Open Settings |
| `C` | Change Model |
| `T` | Change Theme |
| `H` | Session History |
| `?` | Show Help |

**Slash Commands:**
- `/help` - Show all commands
- `/theme <name>` - Change theme
- `/tools` - Show loaded tools
- `/undo` - Undo last code change
- `/clear` - Clear conversation
- `/exit` - Exit (prompts to save session)

### ⌨️ Smart AutoSuggest

Inline word completion as you type - like Google Search or GitHub Copilot.

```
❯ how_                     ← You type "how"
❯ how do I_                ← Gray suggestion appears
❯ how do I                 ← Press Tab to accept!

❯ wie_                     ← German support too
❯ wie viele dateien_       ← Tab to accept
```

**Accept suggestions:**
- `Tab` - Accept full suggestion
- `→` (Right Arrow) - Accept suggestion

**Built-in phrases:** English & German common phrases for coding tasks.

### 📁 Smart File Autocomplete (@)

Cross-platform file path completion with quick shortcuts.

```
@down        → 📁 Downloads → ~\/Downloads
@desk        → 📁 Desktop → ~\/Desktop  
@doc         → 📁 Documents → ~\/Documents
@C:\Users\   → Shows Windows paths
@.\/src       → Relative paths
```

**Quick Paths:**
| Shortcut | Path |
|----------|------|
| `~` | Home directory |
| `desktop` | ~\/Desktop |
| `downloads` | ~\/Downloads |
| `documents` | ~\/Documents |
| `.` | Current directory |
| `..` | Parent directory |
| `c:` | C:\ (Windows) |

### 🎭 Theme System

Choose from 12+ beautiful color themes.

```bash
# List all themes
vaf theme --list

# Set theme
vaf theme dracula
vaf theme nord
vaf theme synthwave
```

**Available Themes:**
| Theme | Description |
|-------|-------------|
| `vaf` | VAF Default (Cyan/Purple) |
| `dracula` | Classic Dracula |
| `nord` | Arctic, north-bluish |
| `tokyonight` | Tokyo Night |
| `catppuccin` | Catppuccin Mocha |
| `gruvbox` | Retro groove |
| `monokai` | Monokai Pro |
| `github` | GitHub Dark |
| `onedark` | Atom One Dark |
| `synthwave` | Synthwave '84 |
| `matrix` | Matrix green |
| `light` | Light theme |

### ⚡ Scheduled Automations

Create time-based tasks that run automatically.

```bash
# List all automations
vaf automation list

# Create new automation (interactive)
vaf automation create

# Run manually
vaf automation run <id>

# Start scheduler daemon
vaf automation start

# Enable/disable
vaf automation enable <id>
vaf automation disable <id>

# Delete
vaf automation delete <id>
```

**Example Conversation:**
```
❯ Create an automation: Every day at 6am, make a document 
  with news and weather on my Desktop

VAF: For which city should I get the weather?
❯ Berlin

VAF: Which news category? (tech, politics, sports, all)
❯ Tech

VAF: ✅ Automation created!
     Name: daily_news
     Schedule: daily at 06:00
     Next Run: 2025-12-19 06:00
```

**In the Web UI:** Open **Settings → Automations** to view and manage tasks, or use the **Automation** button in the footer to open the calendar: pick a month, then a day, then an hour slot to create a new automation (repeat: once/daily/weekly/monthly/hourly, time, and a detailed prompt). The agent can also create automations via chat using the `create_automation` tool.

### 💾 Session Management

Save and resume conversations.

```bash
# List saved sessions
vaf session list

# Load a session
vaf session load abc123

# Resume session on startup
vaf run --session abc123

# Delete a session
vaf session delete abc123

# Export session
vaf session export abc123 --format markdown --output chat.md

# Search sessions
vaf session search "authentication"
```

**On Exit:** VAF asks "Save session? (Y/n)" and shows how to restore it.

### ⏪ Snapshot & Undo

Track code changes and revert when needed.

```bash
# Create a snapshot
vaf snapshot create -m "Before refactoring"

# List snapshots
vaf snapshot list

# Restore to snapshot
vaf snapshot restore abc123

# Quick undo
vaf snapshot undo

# Show diff
vaf snapshot diff

# Clean old snapshots
vaf snapshot clean --keep 50
```

### 🤖 AI Agent & Sub-Agents

- **Standalone Backend:** Auto-manages a `llama-server` process with true GPU acceleration.
- **Sub-Agent Architecture:** 
  - **Librarian:** Smart file navigation with descriptive status
  - **Coder:** Handles coding tasks with "Collaboration Mode" UI
  - **Researcher:** Topic-by-topic research with live TUI (similar to Coder)

**Sub-Agent Status:**
```
| Coder  🧠 Analyzing task...
| Coder  📝 Planning approach...
| Coder  💻 Writing code...
| Coder  📝 Writing: write_file
| Coder  ✅ Finalizing...

| Research  Section 3/10: Methods & Techniques
| Research  🔍 Searching... | ⏱ 0:45 | 🔄 1 | ● Summarizing...
```

### 🧠 Intelligent Context Management

VAF uses a **dynamic, modular context system** that intelligently manages tokens and adapts to your task.

#### Dynamic System Prompt ("The Prompt Router")

Unlike traditional agents with static system prompts, VAF **dynamically loads only the modules you need**:

```
┌─────────────────────────────────────────────────────────────┐
│              DYNAMIC SYSTEM PROMPT ARCHITECTURE             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  🔷 CORE MODULE (Always Active)                     │    │
│  │  ├─ Identity (VQ-1 / Model Name)                    │    │
│  │  ├─ Time, OS, CWD                                   │    │
│  │  ├─ Language Settings                               │    │
│  │  └─ Base Rules (~400 tokens)                        │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│           ┌──────────────┼──────────────┐                   │
│           ▼              ▼              ▼                   │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │
│  │🔍 RESEARCH  │ │ 💻 CODER   │ │📁 LIBRARIAN │            │
│  │   MODULE    │ │   MODULE    │ │   MODULE    │            │
│  │ (~400 tok)  │ │ (~500 tok)  │ │ (~300 tok)  │            │
│  │             │ │             │ │             │            │
│  │ web_search  │ │coding_agent │ │librarian    │            │
│  │ webfetch    │ │code rules   │ │file queries │            │
│  └─────────────┘ └─────────────┘ └─────────────┘            │
│           │              │              │                   │
│           └──────────────┼──────────────┘                   │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  ⚡ AUTOMATION MODULE (On-Demand)                   │    │
│  │  └─ Scheduling rules, create_automation (~300 tok)  │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Token Savings

| Scenario | Old (Static) | New (Dynamic) | Savings |
|----------|-------------|---------------|---------|
| Small Talk ("Hallo!") | ~4,000 tokens | ~800 tokens | **80%** |
| Web Search | ~4,000 tokens | ~1,200 tokens | **70%** |
| Coding Task | ~4,000 tokens | ~1,400 tokens | **65%** |
| File Analysis | ~4,000 tokens | ~1,100 tokens | **72%** |
| All Modules Active | ~4,000 tokens | ~2,400 tokens | **40%** |

**Result:** You start at **10-20%** context usage instead of **50%+**!

#### Sticky Context with Decay

Modules don't disappear immediately - they use a **decay mechanism**:

```
User: "Create a Python script"     → 💻 Coder activated (3 turns)
User: "Add error handling"         → 💻 Coder stays active (3 turns)
User: "What's the weather?"        → 💻 Coder (2), 🔍 Researcher (3)
User: "Thanks!"                    → 💻 Coder (1), 🔍 Researcher (2)
User: "Bye"                        → 💻 Coder gone, 🔍 Researcher (1)
```

**Benefits:**
- ✅ **No Context Flicker**: Modules stay active during related tasks
- ✅ **Automatic Cleanup**: Unused modules decay after 3 messages
- ✅ **Better Focus**: Only relevant rules are loaded

#### Hierarchical Context (Coder Agent)

The **Coder Agent** uses **isolated contexts per task**:

```
┌─────────────────────────────────────────────────────────────┐
│              CODER AGENT - HIERARCHICAL CONTEXT             │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────┐    │
│  │  MAIN CONTEXT (Planning Phase)                      │    │
│  │  └─ Task List from set_todos                        │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│        ┌─────────────────┼─────────────────┐                │
│        ▼                 ▼                 ▼                │
│  ┌───────────┐    ┌───────────┐    ┌───────────┐            │
│  │  TASK 1   │    │  TASK 2   │    │  TASK N   │            │
│  │ (Isolated)│    │ (Isolated)│    │ (Isolated)│            │
│  │ Fresh CTX │    │ Fresh CTX │    │ Fresh CTX │            │
│  └───────────┘    └───────────┘    └───────────┘            │
└─────────────────────────────────────────────────────────────┘
```

Each task gets a **fresh ContextManager** - no pollution from previous tasks!

#### Context Compression (Cursor-Style)

When context reaches **85%**, VAF compresses automatically:

```
BEFORE: 100 messages, 7,500 tokens
┌─────────────────────────────────────────────────┐
│ System Prompt + 95 messages                     │
└─────────────────────────────────────────────────┘
                    ⬇️ COMPRESS ⬇️
AFTER: 12 messages, 2,000 tokens
┌─────────────────────────────────────────────────┐
│ System Prompt (kept)                            │
│ [COMPRESSED] 🎯 Intent + 📁 State + 📝 Summary │
│ Last 10 messages (raw)                          │
└─────────────────────────────────────────────────┘
```

**Strategy:**
1. **Archive**: Full history saved to `~/.vaf/context_archive/`
2. **Keep**: System prompt + last 10 messages
3. **Compress**: Old messages → Intent + State + Summary
4. **Restore**: Use `/restore` to recover full history

#### Context Tracking

**🎯 Intent Context**: Primary goal, sub-tasks, constraints, keywords

**📁 State Context**: Files created/modified, errors, tools used, decisions

#### Configuration

- **Default Limit**: 8,192 tokens (configurable via `vaf settings`)
- **Compression Trigger**: 85% usage
- **VAF.md**: Optional project context (max 12,000 chars)

**Example Context Usage (New):**
```
Tokens: ●○○○○○○○○○ 12% (1,000/8,192)
         ↑ Dynamic Prompt (~800) + First Message (~200)
```

> 📚 **Deep Dive**: See [docs/CONTEXT_MANAGEMENT.md](docs/CONTEXT_MANAGEMENT.md) for detailed documentation on sub-agents, web search context isolation, and compression strategies.

### 🔄 Workflows - Plug & Play Pipelines

VAF includes pre-built workflows that automatically execute multi-step tasks. Just describe what you want, and VAF handles the rest!

**Built-in Workflows:**
- **Create Website** - "Erstelle eine Website für ein Umzugsunternehmen in Berlin"
- **Research & Code** - "Recherchiere Python async/await und erstelle Beispielcode"
- **Deep Research** - "Tiefgehende Recherche über Machine Learning" (with live TUI, topic-by-topic)
- **Code Review** - "Review diesen Code und verbessere ihn"
- **Analyze Website** - "Analysiere diese URL und fasse zusammen"
- **Generate Docs** - "Erstelle Dokumentation für dieses Projekt"
- **Create Scheduled Task** - "Erstelle immer um 21:27 eine Website mit News auf meinem Desktop"

**How It Works:**
1. You describe your task in natural language (German or English)
2. VAF's "Brain" matches your input to the best workflow
3. Variables are automatically extracted (time, format, topic, etc.)
4. Workflow executes all steps automatically
5. Results are delivered with clickable file links

**Example:**
```
❯ Erstelle eine Website für ein Umzugsunternehmen in Berlin

VAF: 🧠 Matching workflow: create_website
     📝 Extracted: description = "Umzugsunternehmen in Berlin"
     🔄 Executing workflow...
     ✅ Website created: index.html, style.css, script.js
     📁 Files: file:///path/to/index.html
```

**Create Custom Workflows:**
Just create a `.py` file in `~/.vaf/workflows/` - VAF automatically discovers and loads it! See `vaf/workflows/README.md` for details.

### 🛠️ AI Tools

#### Bash Tool - Execute Shell Commands
```bash
# The agent can run shell commands
# Example: "Run the tests for my project"
# Agent uses: bash("pytest -v")
```

#### Web Search - Real-Time Information
```bash
# The agent searches the web for current info
# Example: "What's the weather in Berlin?"
# Agent uses: web_search("weather Berlin")
```

#### Web Fetch - Read Web Pages
```bash
# The agent can fetch and read web content
# Example: "Read the Python docs for asyncio"
# Agent uses: webfetch("https://docs.python.org/3/library/asyncio.html")
```

#### Code Search - Find Code Patterns
```bash
# The agent can search through your codebase
# Example: "Find all functions that handle authentication"
# Agent uses: codesearch("auth", search_type="symbol")
```

---

## CLI Tools

### 📦 Scaffold - Project Templates

Create project structures for various languages.

```bash
# Show available templates
vaf scaffold list

# Create new project
vaf scaffold new python my-project
vaf scaffold new typescript my-app --path ~/Projects
vaf scaffold new rust my-cli
```

**Available Templates:**
- `python` - Standard Python project
- `python-flask` - Flask Web API
- `javascript` - Node.js project
- `typescript` - TypeScript project
- `rust` - Rust/Cargo project
- `go` - Go project
- `html` - Static website

### 🔧 Generate - AI Code Generation

Generate code with AI assistance.

```bash
# Generate API endpoint
vaf generate api /users --lang python --framework fastapi

# Generate function
vaf generate function "Parse CSV file and return dict"

# Generate class
vaf generate class "User with name, email, age"

# Generate tests
vaf generate test "User authentication flow"

# Generate UI component
vaf generate component "Login form" --framework react

# Free generation
vaf generate free "Create a Python decorator for caching"
```

### ⚙️ Automate - CI/CD Tasks

Automate tests, builds, and linting.

```bash
# Run tests
vaf automate test
vaf automate test --coverage

# Build project
vaf automate build
vaf automate build --release

# Linting & formatting
vaf automate lint --fix
vaf automate format

# Run all checks
vaf automate check
```

### 🐛 Debug - AI Error Analysis

Analyze error messages with AI.

```bash
# Explain error message
vaf debug explain "NameError: name 'x' is not defined"

# Analyze stack trace
vaf debug trace --file error.log

# Suggest fix
vaf debug fix main.py --line 42 --error "NameError"
```

### 🔀 Git - Git with AI

Git operations with AI-generated commit messages.

```bash
# Initialize repository
vaf git init

# Commit with AI-generated message
vaf git commit --auto

# Other commands
vaf git status
vaf git push
vaf git pull
vaf git log
```

---

## Quick Start

```bash
# Start modern chat interface
vaf run

# Create a new project
vaf scaffold new python my-project
cd my-project

# Initialize VAF config
vaf init

# Start coding with AI
vaf run
```

## Usage

### Start the Agent

**Interactive Mode (Chat):**
```bash
vaf run
```
*First run downloads the model (~4GB) and backend binary (~20MB).*

**Non-Interactive Mode (One-shot):**
```bash
# Quick one-shot prompt
vaf run "What is Python?"

# Or use the dedicated command
vaf prompt "Explain async/await in Python"

# With JSON output (for automation/scripts)
vaf prompt "List all files in current directory" --output-format json

# Stream JSON (real-time updates)
vaf prompt "Research machine learning" --output-format stream-json
```

**Aliases:**
- `vaf run prompt "..."` → same as `vaf prompt "..."`
- Both support `--output-format text|json|stream-json`

### Interactive Shortcuts
Inside chat session:
- `S` - Open Settings
- `C` - Change Model
- `T` - Change Theme
- `H` - Session History
- `?` - Help
- `@filename` - Attach file

### GPU Acceleration
VAF auto-detects and uses your GPU:
- **Windows:** CUDA-enabled binaries
- **Mac:** Metal (ARM64) or Intel
- **Linux:** Generic x64

Force re-install:
```bash
vaf install-gpu
```

---

## Configuration

Settings are stored in `~/.vaf/`:
```
~/.vaf/
├── config.json       # Global settings
├── sessions/         # Saved conversations
├── snapshots/        # Code snapshots
├── automations/      # Scheduled tasks
│   └── README.md     # How automations work
├── workflows/        # Custom workflows (plug & play!)
│   └── *.py          # Your custom workflow files
├── history           # Command history
└── autosuggest.json  # Learned word suggestions
```

**Custom Workflows:**
Place your custom workflow files in `~/.vaf/workflows/*.py` - they're automatically discovered and loaded at startup!

## Project Structure

```
vaf/
├── core/
│   ├── gateway.py        # Gateway Server (FastAPI)
│   ├── protocol.py       # Pydantic Protocol
│   ├── agent.py          # Main Agent logic
│   ├── api_backend.py    # Cloud API Provider Integration
│   ├── backend.py        # Local LLM Server Manager
│   ├── config.py         # Global Configuration
│   ├── context.py        # Context Management (Cursor-style)
│   ├── session.py        # Session Management
│   ├── snapshot.py       # Undo/Snapshot System
│   ├── automation.py     # Scheduled Tasks
│   ├── subagent_ipc.py   # Sub-Agent Inter-Process Communication
│   ├── system_prompt.py  # Dynamic Prompt Router
│   ├── speech.py         # TTS/STT Speech Engine
│   ├── trust.py          # Security & Permission Gating
│   └── platform.py       # Cross-Platform Utils
├── cli/
│   ├── tui.py            # Modern Terminal UI
│   ├── ui.py             # Rich UI Components
│   ├── themes.py         # Color Themes
│   ├── autosuggest.py    # Smart AutoComplete
│   └── cmd/
│       ├── run.py        # Agent Runner
│       ├── bridge.py     # Bridge Runner (Discord/Slack)
│       ├── scaffold.py   # Project Templates
│       ├── generate.py   # Code Generation
│       ├── automate.py   # Test/Build Automation
│       ├── debug.py      # AI Error Analysis
│       ├── git.py        # Git Integration
│       ├── info.py       # System & GPU Info
│       ├── subagent.py   # Sub-Agent Runner
│       └── settings.py   # Settings UI
├── tools/
│   ├── sandbox.py        # Docker Sandbox
│   ├── base.py           # Plugin Base Class
│   ├── coder.py          # Coding Sub-Agent
│   ├── librarian.py      # Librarian Sub-Agent
│   ├── research_agent.py # Research Sub-Agent
│   ├── document_agent.py # Document Sub-Agent
│   ├── bash.py           # Shell Execution
│   ├── python_exec.py    # Python Execution
│   ├── filesystem.py     # File Operations
│   ├── webfetch.py       # Web Content Fetching
│   ├── search.py         # Web Search
│   ├── codesearch.py     # Code Search
│   ├── mcp_client.py     # Model Context Protocol Client
│   ├── linter.py         # Code Linting Tool
│   ├── batch.py          # Parallel Tool Execution
│   └── automation.py     # Automation Tool
├── workflows/
│   ├── engine.py         # Workflow Execution Engine
│   ├── selector.py       # Workflow Matching (Brain + Patterns)
│   ├── templates.py      # Auto-loading Workflow System
│   ├── README.md         # Workflow Documentation
│   └── workflows/        # Individual Workflow Files
│       ├── create_website.py
│       ├── research_and_code.py
│       ├── deep_research.py
│       ├── create_scheduled_task.py
│       ├── analyze_website.py
│       ├── code_review.py
│       ├── generate_docs.py
│       ├── create_file.py
│       ├── create_document.py
│       ├── legal_contract_research.py
│       └── technical_doc_research.py
└── main.py               # CLI Entry Point
```

## Extending VAF

### 1. Adding New Tools (Plugin System)

VAF supports a "Drop-in" plugin system. To add a new tool:

1. Create a python file in `vaf/tools/` (e.g., `my_tool.py`)
2. Create a class inheriting from `BaseTool`
3. Define `name`, `description`, `parameters`, and `run()` method

**Example:**
```python
from vaf.tools.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does cool things."
    parameters = {
        "type": "object",
        "properties": {
            "arg": {"type": "string", "description": "Input argument"}
        },
        "required": ["arg"]
    }
    
    def run(self, **kwargs) -> str:
        arg = kwargs.get("arg", "")
        return f"Processed: {arg}"
```

The agent automatically discovers and registers your tool at startup!

### 2. Creating Custom Workflows (Plug & Play)

Workflows are even easier - just create a `.py` file with a `WORKFLOW` dictionary!

**Create `~/.vaf/workflows/my_workflow.py`:**
```python
"""
My Custom Workflow
"""

WORKFLOW = {
    "name": "My Workflow",
    "description": "What this workflow does",
    "triggers": [
        "keyword1", "keyword2",
        "phrase to match",
    ],
    "trigger_patterns": [
        r"pattern.*match",
    ],
    "variables": {
        "topic": "What to process",
    },
    "steps": [
        {
            "tool": "web_search",
            "input": "{topic}",
            "output": "results",
            "description": "Search the web",
        },
        {
            "tool": "coding_agent",
            "input": "Based on: {results}\nCreate code for: {topic}",
            "output": "code",
            "description": "Generate code",
        },
    ],
}
```

**That's it!** VAF automatically:
- Discovers your workflow at startup
- Matches user input to your triggers
- Extracts variables automatically
- Executes all steps in sequence

See `vaf/workflows/README.md` for complete documentation and examples.


## System Architecture & Resource Management

VAF uses an intelligent resource management system that adapts to your hardware capabilities:

### 🎮 VRAM Detection & Parallelism
The system automatically detects your available VRAM and calculates the memory cost for running agents.

- **High VRAM Mode (Parallel):**
  If your GPU has enough memory (`Model Size + 2x Context`), VAF runs the Main Agent and Sub-Agents in **parallel slots**. This is the fastest mode, allowing seamless background execution.

- **Low VRAM Mode (Sequential):**
  If VRAM is limited, VAF switches to **sequential execution**. The Main Agent pauses to free up resources, the Sub-Agent runs, and then the Main Agent resumes. This prevents Out-Of-Memory (OOM) crashes while still allowing full functionality on weaker hardware.

### 🛡️ Context Isolation vs. Separation
It is important to distinguish between **execution** (Parallel/Sequential) and **context** (Isolated):

1.  **Execution (Hardware-dependent):** Decides *when* agents run (simultaneously or one after another) based on VRAM.
2.  **Context (Architecture-guaranteed):** Regardless of VRAM, Sub-Agents **ALWAYS** run with their own isolated context.
    - **Main Agent:** Keeps high-level plan and conversation history.
    - **Sub-Agent:** Gets a fresh, task-specific context window.
    - **Benefit:** Massive tasks (like reading 50 files) never pollute or overflow the Main Agent's context, ensuring long-term stability.

## 🌐 API & Model Integration

VAF is model-agnostic and supports both local privacy-first models and powerful cloud APIs.

### 🤗 Hugging Face Integration
VAF has built-in integration with Hugging Face for local GGUF models.
- **Search & Download:** Directly from the CLI settings menu.
- **Auto-GGUF:** Automatically filters for compatible GGUF quantized models.
- **Zero Config:** Downloaded models are instantly available for selection.

### ☁️ Cloud Providers
Connect to leading AI providers for maximum performance:
- **OpenAI** (GPT-4o, o1)
- **Anthropic** (Claude 3.5 Sonnet)
- **Google** (Gemini 1.5 Pro/Flash)
- **DeepSeek** (DeepSeek-V3/R1)
- **OpenRouter** (Access to Llama 3, Mistral, and 100+ others)

### 🔄 Dynamic Switching
Switch models instantly without restarting:
- Press **`C`** in the main chat interface.
- Use the slash command: `/model gpt-4o`
- Configure defaults in **Settings (`S`)**.

### ⚡ Sub-Agent Optimization
You can configure **Sub-Agents** to use a different provider than the Main Agent.
- **Example:** Use **Claude 3.5 Sonnet** for the Main Agent (high reasoning) and a fast **Local Model** for the Librarian (file reading) to save costs and latency.

## Version

```bash
vaf --version
```

## License & Usage

**VAF (Veyllo Agentic Framework)** is distributed under the **MIT License, modified with the Commons Clause v1.0**. See [LICENSE](LICENSE) for full terms.

This means the software is source-available but carries specific restrictions on **selling VAF itself** or offering paid services whose value derives substantially from VAF.

### ✅ You CAN:
- Use VAF for any purpose (personal, commercial, academic).
- Modify source code for your own use.
- Distribute copies.
- **Create and sell custom Workflows, Tools, or Plugins** built *on top of* VAF.
- Build internal business solutions.

### ❌ You CANNOT:
- **Sell VAF as a standalone product or service** (including hardware embedding).
- **Offer VAF as a hosted SaaS or cloud service**.
- Create direct competing products based substantially on VAF's codebase.

### 💼 Business & OEM Licensing
For commercial use including hardware embedding, SaaS, or proprietary distribution:

**Contact Veyllo GmbH** for Business/OEM licensing options.

---

# **Support**

> ## **Community Support only via GitHub Issues.**  
> ## **Guaranteed Support only for Enterprise Customers.**

---

<p align="center">
  <b>VAF</b> - Veyllo Agentic Framework<br>
  Built with ❤️ in Berlin by Veyllo Labs.
</p>