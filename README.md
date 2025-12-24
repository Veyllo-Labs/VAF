# 文 Veyllo Agentic Framework (VAF)

VAF is a premium, local-first AI agent framework designed for high efficiency and aesthetic interaction. It features a **modern terminal UI**, multiple themes, session management, scheduled automations, and powerful developer tools that work on **Python 3.13** across platforms (Windows, Linux, macOS).

```
O))         O))       O))))))))
 O))       O))))      O))      
  O))     O))  O))    O))      
   O))   O))    O))   O))))))  
    O)) O)) )))) O))  O))      
     O))))        O)) O))      .-.
      O))          O))O))     (OO ) 
      
```

## ✨ Highlights

- 🎨 **Modern TUI** - Beautiful input box with smart autocomplete and themes
- ⌨️ **Smart AutoSuggest** - Inline word completion like Google Search
- 🎭 **12+ Themes** - Dracula, Nord, Tokyo Night, Catppuccin, and more
- 💾 **Session Management** - Save, load, and search conversations
- ⏪ **Undo/Snapshot** - Git-based code change tracking and rollback
- ⚡ **Scheduled Automations** - Time-based tasks (daily news, weather reports)
- 🛠️ **Powerful Tools** - Bash execution, web fetching, parallel operations
- 🤖 **Sub-Agents** - Specialized agents for coding and file navigation

---

## Features

### 🎨 Modern Terminal UI

Beautiful, modern terminal interface with input box, themes, and rich formatting.

```bash
# Start interactive chat with modern UI
vaf run

# Use specific theme
vaf run --theme dracula
vaf run --theme nord

# Resume a previous session
vaf run --session abc123

# Use classic simple interface
vaf run --classic
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
@down        → 📁 Downloads → ~/Downloads
@desk        → 📁 Desktop → ~/Desktop  
@doc         → 📁 Documents → ~/Documents
@C:\Users\   → Shows Windows paths
@./src       → Relative paths
```

**Quick Paths:**
| Shortcut | Path |
|----------|------|
| `~` | Home directory |
| `desktop` | ~/Desktop |
| `downloads` | ~/Downloads |
| `documents` | ~/Documents |
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

**In Settings:** `S` → `⚡ Automations` to view/manage

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

**Sub-Agent Status (new):**
```
| Coder  🧠 Analyzing task...
| Coder  📝 Planning approach...
| Coder  💻 Writing code...
| Coder  📝 Writing: write_file
| Coder  ✅ Finalizing...
```

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

## Installation

1. **Clone the repo:**
   ```bash
   git clone https://github.com/Veyllo-Labs/VAF.git
   cd VAF
   ```

2. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Install Package (Editable Mode):**
   ```bash
   pip install -e .
   ```

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
```bash
vaf run
```
*First run downloads the model (~4GB) and backend binary (~20MB).*

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
├── history           # Command history
└── autosuggest.json  # Learned word suggestions
```

## Project Structure

```
vaf/
├── core/
│   ├── agent.py          # Main Agent logic
│   ├── backend.py        # llama-server Manager
│   ├── config.py         # Global Configuration
│   ├── session.py        # Session Management
│   ├── snapshot.py       # Undo/Snapshot System
│   ├── automation.py     # Scheduled Tasks
│   └── platform.py       # Cross-Platform Utils
├── cli/
│   ├── tui.py            # Modern Terminal UI
│   ├── ui.py             # Rich UI Components
│   ├── themes.py         # Color Themes
│   ├── autosuggest.py    # Smart AutoComplete
│   └── cmd/
│       ├── run.py        # Agent Runner
│       ├── scaffold.py   # Project Templates
│       ├── generate.py   # Code Generation
│       ├── automate.py   # Test/Build Automation
│       ├── debug.py      # AI Error Analysis
│       ├── git.py        # Git Integration
│       └── settings.py   # Settings UI
├── tools/
│   ├── base.py           # Plugin Base Class
│   ├── coder.py          # Coding Sub-Agent
│   ├── librarian.py      # Search Sub-Agent
│   ├── bash.py           # Shell Commands
│   ├── webfetch.py       # Web Content Fetching
│   ├── codesearch.py     # Code Search
│   ├── automation.py     # Automation Tool
│   └── search.py         # Web Search
└── main.py               # CLI Entry Point
```

## Extending VAF (Plugin System)

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

## Version

```bash
vaf --version
```

## License

MIT

---

<p align="center">
  <b>VAF</b> - Veyllo Agentic Framework<br>
  Built with ❤️ by Veyllo Labs
</p>
