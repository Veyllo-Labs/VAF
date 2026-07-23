# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Custom persona: give the embedded agent its own voice and instructions.

`system_prompt=` replaces VAF's built-in on-disk persona (the desktop
product's "Soul") for THIS agent only - nothing is written to disk. The
engine's technical instructions (thinking format, action verification) are
kept, so tools and streaming still work; you are setting the personality and
task framing. Docs: docs/EMBEDDING.md "Setting the persona".
"""
from vaf import Agent


def main() -> None:
    agent = Agent(
        # config=None -> your on-disk VAF config decides provider and model.
        system_prompt=(
            "You are Aria, a terse code-review assistant. "
            "Answer in short, direct bullet points. Never apologise. "
            "If code is correct, say so in one line."
        ),
    )

    print(agent.run("Review this: def add(a, b): return a - b"))

    # The persona persists across turns of the same Agent.
    print(agent.run("Now suggest a one-line fix."))


if __name__ == "__main__":
    main()
