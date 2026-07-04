"""Tests for {{SCRIPT_NAME}}.

These tests pass out of the box against the example logic in main.py. When you replace
`process` with your real logic, UPDATE these assertions to describe the behaviour your
task requires, then run the tests (they are your safety net).
"""
from main import process, main


def test_process_example():
    # REPLACE: assert the behaviour of your real `process` logic.
    assert process("abc") == "cba"


def test_process_is_reversible():
    assert process(process("hello")) == "hello"


def test_main_runs_without_error():
    # `main` should return 0 (success) for valid input.
    assert main(["hello"]) == 0
