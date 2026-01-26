# VAF Command Line Interface (CLI)

The `vaf.cli` module implements the user-facing terminal interface, including the modern TUI and various management commands.

## Key Components

- **tui.py**: The primary interactive chat interface using `prompt_toolkit`.
- **ui.py**: Reusable Rich-based UI components (tables, panels, status indicators).
- **themes.py**: Definition of the color themes (Dracula, Nord, etc.).
- **autosuggest.py**: Logic for smart inline completions.
- **cmd/**: Directory containing individual CLI command implementations.

## Usage

The CLI is the main entry point for most users. Commands are structured using `argparse` or similar, and are routed through `vaf/main.py`.

### Key CLI Actions:
- `vaf run`: Starts the interactive TUI.
- `vaf settings`: Opens the configuration menu.
- `vaf session list`: Manages saved conversations.

## Extending the CLI

To add a new command:
1. Create a new file in `vaf/cli/cmd/`.
2. Register the command in `vaf/main.py`.
3. Use `vaf.cli.ui` for consistent formatting.

## Dependencies

- **prompt_toolkit**: For the interactive TUI and autosuggestions.
- **rich**: For beautiful terminal output and formatting.
- **pygments**: For syntax highlighting in code blocks.
