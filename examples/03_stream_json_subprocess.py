# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Drive VAF as a subprocess and parse its NDJSON event stream.

This is the integration pattern for non-Python applications: spawn
`vaf prompt --output-format stream-json` and read one JSON object per
stdout line. Event schema: docs/OBSERVABILITY.md. The same parsing works
from Node, Go, Rust, ... - Python here is just the demonstration language.
"""
import json
import re
import shutil
import subprocess
import sys

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def main() -> None:
    if shutil.which("vaf") is None:
        sys.exit("The 'vaf' CLI is not on PATH. Install VAF first (pip install -e .).")

    proc = subprocess.Popen(
        ["vaf", "prompt", "-p", "In one sentence, what is an agent framework?",
         "--output-format", "stream-json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    text_parts: list[str] = []
    tools_used: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        # Be defensive: ignore any stdout line that is not JSON (the machine
        # formats silence the common printers, but not every residual line).
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = evt.get("type")
        if kind == "text_delta":
            text_parts.append(evt.get("text", ""))
        elif kind == "tool_start":
            tools_used.append(evt.get("tool", "?"))
        elif kind == "gate_required":
            # Non-interactive runs never get a gate_decision; the tool comes
            # back as an "[ERROR] ... requires confirmation" result instead.
            print(f"[gated] {evt.get('tool')}: {evt.get('reason')}")

    proc.wait()

    # There is no aggregated-result event: reconstruct the answer from the
    # deltas and strip reasoning blocks.
    answer = THINK_BLOCK.sub("", "".join(text_parts)).strip()
    print("Tools used:", tools_used or "none")
    print("Answer:", answer)
    print("Exit code:", proc.returncode)


if __name__ == "__main__":
    main()
