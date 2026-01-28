# VAF State Providers

This directory contains specialized modules that provide real-time state information to the VAF agent and the Web Dashboard. State providers are responsible for gathering, formatting, and presenting the "environmental context" of the framework.

## Key Modules

- **context_state.py**: Monitors and reports on token usage, active context modules, and history compression status.
- **sandbox_state.py**: Tracks the status of isolated code execution environments (Docker/Python Sandboxes).
- **tool_activity_state.py**: Provides live updates on which tools are currently running, their progress, and results.

## Usage

State providers are typically used by the `ContextManager` or the `Gateway` to enrich the agent's system prompt or to broadcast live updates to connected clients (like the Web UI).

## Development

When adding a new global state tracking feature:
1. Create a new provider file in this directory.
2. Implement a standard interface for state retrieval.
3. Register the provider in the core system's state registry.
