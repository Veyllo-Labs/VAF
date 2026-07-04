"""Tests for {{CLI_TOOL_NAME}}.

These pass out of the box against the example logic in cli.py. When you replace
`_execute` with your real logic, UPDATE these assertions and re-run the tests.
"""
import argparse

from cli import CLITool


def test_execute_example():
    tool = CLITool()
    ns = argparse.Namespace(input="hello")
    # REPLACE: assert your real logic's output.
    assert "5 characters" in tool._execute(ns)


def test_run_returns_success():
    tool = CLITool()
    assert tool.run(["--input", "data"]) == 0
