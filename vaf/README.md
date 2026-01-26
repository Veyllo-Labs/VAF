# VAF Core Package

This is the main Python package for the Veyllo Agentic Framework (VAF). It contains all the core logic, CLI implementation, tools, and workflow definitions.

## Package Structure

- **core/**: The heart of the framework, handling LLM management, context, persistence, and the gateway server.
- **cli/**: Implementation of the Terminal User Interface (TUI), themes, and command-line commands.
- **tools/**: A library of executable tools and sub-agents (Coder, Researcher, Librarian) that extend the agent's capabilities.
- **workflows/**: Plug-and-play multi-step pipelines and the execution engine.
- **sources/**: Configuration and data for information sources (e.g., news, tech).

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
