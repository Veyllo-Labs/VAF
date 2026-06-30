# VAF State Providers

This directory contains `StateProvider` implementations used for VAF session-state persistence. Each provider captures the runtime state of a particular subsystem so that it can be restored when a session is resumed.

## Key Modules

- **context_state.py**: Persists and restores the `ContextManager`'s intent (goals, constraints, keywords) and state tracking (files, errors, decisions), along with its token limits and trigger threshold.
- **sandbox_state.py**: Persists the Python sandbox state, including serializable global variables, import statements, execution history, and the working directory.
- **tool_activity_state.py**: Tracks active and recent tool executions (currently running tools, recent history, and usage statistics) for session resumption.

## Usage

State providers are registered into the agent's own `StateRegistry` (created once in `Agent.__init__`), for example:

```python
self.state_registry.register('context', ContextStateProvider(self.context_manager))
```

The `StateRegistry` is wired into the `SessionManager`, which captures a snapshot of every registered provider when a session is saved and restores it when the session is resumed.

## Development

When adding a new state-persistence provider:
1. Create a new provider file in this directory.
2. Implement the `StateProvider` interface (`get_state`, `restore_state`, `state_version`).
3. Register the provider into the agent's `StateRegistry`.
