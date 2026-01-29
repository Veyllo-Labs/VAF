# CLI Command Implementations

This directory contains the logic for individual `vaf` CLI commands. Each file typically corresponds to a sub-command (e.g., `vaf run`, `vaf session`).

## Commands

- **run.py**: Handles the `run` command, initializing the agent and starting the TUI or one-shot prompt.
- **settings.py**: Provides the interactive settings menu to modify `config.json`.
- **scaffold.py**: Logic for creating new project templates from pre-defined structures.
- **bridge.py**: Manages external platform connections like Discord.
- **bridge_discord.py**: Discord-specific bridge helper.
- **automate.py**: Test/build/lint automation commands.
- **git.py**: AI-enhanced Git operations (auto-commits, status summaries).
- **models.py**: Model management commands (list, download, select).
- **subagent.py**: Allows running specialized sub-agents (Coder, Researcher) independently.
- **workflow.py**: Workflow execution and inspection commands.

## Development Guide

When adding a new command:
1.  **File Naming**: Use a descriptive name (e.g., `my_command.py`).
2.  **Interface**: Ensure the command accepts standard arguments and provides a `--help` description.
3.  **Integration**: Import and register the command's entry point in the main CLI router.
4.  **Consistency**: Use the UI utilities from `vaf.cli.ui` to ensure output matches the project's visual style.

## Dependencies

- Relies on `vaf.core` for agent logic and `vaf.cli.ui` for presentation.
- May use specialized libraries relevant to the command (e.g., `discord.py` for bridges).
