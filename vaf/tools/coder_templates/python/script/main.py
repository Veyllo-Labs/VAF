#!/usr/bin/env python3
"""
{{SCRIPT_NAME}} - {{SCRIPT_DESCRIPTION}}

{{SCRIPT_DETAILS}}
"""

import sys
import argparse
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# CORE LOGIC  <-- REPLACE THIS with the real logic for your task.
# This starter is a WORKING example so the scaffold runs and test_main.py passes
# out of the box. Adapt this function (and its test) to what the task actually needs.
# ─────────────────────────────────────────────────────────────────────────────
def process(text: str) -> str:
    """Transform the input and return the result.

    Example behaviour: return the input reversed. Replace the body with your task's
    real logic, and update test_main.py to assert the new behaviour.
    """
    return text[::-1]


def main(args: Optional[list] = None) -> int:
    """Entry point. Parses arguments, runs ``process``, prints the result.

    Returns an exit code (0 = success, non-zero = error).
    """
    parser = argparse.ArgumentParser(
        description="{{SCRIPT_DESCRIPTION}}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", default="hello", help="Input value to process")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parsed_args = parser.parse_args(args)

    if parsed_args.verbose:
        print(f"Running {{SCRIPT_NAME}} on: {parsed_args.input!r}")

    result = process(parsed_args.input)
    print(result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
