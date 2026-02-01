#!/usr/bin/env python3
"""
Quick test for memory_save tool (sync call from another thread/loop).
Run from VAF root: python tests/test_memory_store_tool.py
"""
import sys
import os

# Ensure vaf is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    from vaf.tools.context_tools import MemorySaveTool
    tool = MemorySaveTool()
    # user_scope_id=None => global scope (allowed for Web UI without login)
    result = tool.run(
        content="User's name is Mert",
        title="User Information",
        tags=["identity"],
        user_scope_id=None,
    )
    print("Result:", result)
    if "Error" in result:
        sys.exit(1)
    print("OK: memory_save succeeded.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
