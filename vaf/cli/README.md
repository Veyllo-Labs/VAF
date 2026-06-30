# VAF Command Line Interface (CLI)

The `vaf.cli` module implements the user-facing terminal interface, including the modern TUI and various management commands.

## Key Components

- **tui.py**: The primary interactive chat interface using `prompt_toolkit`, and the home of the Rich-based UI components (tables, panels, status indicators).
- **ui.py**: A thin backward-compatibility shim that re-exports the UI components from `tui.py`.
- **themes.py**: Definition of the color themes (Dracula, Nord, etc.).
- **autosuggest.py**: Logic for smart inline completions.
- **cmd/**: Directory containing individual CLI command implementations.

## Usage

The CLI is the main entry point for most users. Commands are structured using Typer and are routed through `vaf/main.py`.

### Key CLI Actions:
- `vaf run`: Starts the interactive TUI.
- `vaf session list`: Manages saved conversations.

The configuration menu is not a top-level subcommand; it is reached by typing `settings` (or `s`) inside the interactive TUI started by `vaf run`.

## Extending the CLI

To add a new command:
1. Create a new file in `vaf/cli/cmd/`.
2. Register the command in `vaf/main.py`.
3. Use `vaf.cli.ui` for consistent formatting.

## Dependencies

- **prompt_toolkit**: For the interactive TUI and autosuggestions.
- **rich**: For beautiful terminal output and formatting.
