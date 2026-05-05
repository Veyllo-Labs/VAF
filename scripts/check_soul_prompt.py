#!/usr/bin/env python3
"""
Check that Soul/identity loading and system prompt building work and write to logs.

Run from repo root: python scripts/check_soul_prompt.py

This forces log output to repo logs/ (unless VAF_LOG_DIR is already set),
builds the system prompt (which triggers Soul load + soul_prompt.log and system_prompt_full.log),
then prints where to look. Use this to verify Soul is loaded and the full prompt is logged.
"""
from pathlib import Path
import os
import sys

# Ensure repo root is on path when run as scripts/check_soul_prompt.py
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Force logs to repo logs/ so we can inspect soul_prompt.log and system_prompt_full.log
_log_dir = _repo_root / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("VAF_LOG_DIR", str(_log_dir))

def main():
    from vaf.core.system_prompt import SystemPromptManager
    pm = SystemPromptManager(tools=[], model_name="VQ-1", username="admin")
    prompt = pm.build_prompt(filename="vaf")
    print(f"Log dir: {_log_dir}")
    print(f"Prompt length: {len(prompt)} chars")
    print("Check these files:")
    print(f"  - {_log_dir / 'soul_prompt.log'}")
    print(f"  - {_log_dir / 'system_prompt_full.log'}")
    soul_log = _log_dir / "soul_prompt.log"
    if soul_log.exists():
        lines = soul_log.read_text(encoding="utf-8").strip().split("\n")
        print("\nSoul log (last 5 lines):")
        print("\n".join(lines[-5:]) if lines else "(empty)")
    else:
        print("\nSoul log not found (build_prompt may not have written to this dir).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
