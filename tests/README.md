# Tests

This directory contains the testing suite for VAF, including unit tests, integration tests, and system-level checks.

## Structure

The suite contains dozens of `test_*.py` files following the `test_*.py` naming convention. Rather than list each one, the tests are grouped by the area they cover. The list below is representative, not exhaustive:

- **LLM providers and failover**: provider integrations and the cross-provider/backend failover and routing paths (e.g. `test_anthropic_provider.py`, `test_provider_failover.py`, `test_router_provider_agnostic.py`).
- **Thinking and proactive runs**: the proactive/"thinking" loop, its delivery channels, dedup, and guards (e.g. `test_thinking_proactive.py`, `test_thinking_channel_delivery.py`).
- **Memory and RAG**: the memory store, working memory, and retrieval/embedding paths (e.g. `test_memory_store_tool.py`, `test_working_memory.py`).
- **Skills, automation, and workflows**: skill learning/tools, automation delivery, and workflow selection (e.g. `test_skills.py`, `test_automation_delivery.py`, `test_workflow_selector_extract.py`).
- **Tools and MCP**: the tool contract/registry, input repair, and MCP registration (e.g. `test_tool_contract.py`, `test_mcp_registry.py`).
- **Credentials and security**: secure/credential storage, auth middleware, and WebSocket auth (e.g. `test_secure_store.py`, `test_credential_store.py`, `test_ws_auth_failclosed.py`).
- **Core agent and integrity**: core agent capabilities and overall system health (e.g. `test_agent_features.py`, `test_integrity.py`).

Note: `list_tools_cli.py` is a helper script (it prints the CLI tool menu), not a test, so `pytest` does not collect it.

## Running Tests

VAF uses `pytest` as its primary testing framework.

```bash
# Run all tests
pytest

# Run tests with output
pytest -s

# Run specific test file
pytest tests/test_agent_features.py
```

## Writing New Tests

When adding features or fixing bugs:
- Create corresponding test files in this directory.
- Mock external API calls and heavy LLM operations where possible to keep tests fast.
- Ensure all new tests pass before submitting a pull request.
- Follow the naming convention `test_*.py` for test discovery.

## Dependencies

- `pytest`: Core testing framework.
- See `requirements.txt` for additional test-related dependencies.
