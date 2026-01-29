# Tests

This directory contains the testing suite for VAF, including unit tests, integration tests, and system-level checks.

## Structure

- **test_agent_features.py**: Tests for core agent capabilities, tool usage, and response generation.
- **test_integrity.py**: Verifies the overall system health and critical pathways.

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
