# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Streaming plus structured events: the two observability channels.

- on_token streams raw text deltas (reasoning models may include
  <think>...</think> blocks; run() still returns the cleaned final answer).
- The event sink receives structured dicts for tool execution:
  tool_start / tool_end / gate_required / gate_decision.

Full channel contract: docs/OBSERVABILITY.md.
"""
from vaf import Agent


def on_token(delta: str) -> None:
    print(delta, end="", flush=True)


def on_event(evt: dict) -> None:
    # Keep sink callbacks cheap and non-blocking; a raising sink is swallowed
    # by the engine, so you would lose events silently.
    kind = evt.get("type")
    if kind == "tool_start":
        print(f"\n[event] tool_start: {evt.get('tool')} args={evt.get('args')}")
    elif kind == "tool_end":
        print(f"\n[event] tool_end:   {evt.get('tool')}")
    else:
        # gate_required / gate_decision (gate_decision never fires in
        # non-interactive mode; gated tools return an error string instead).
        print(f"\n[event] {kind}: {evt}")


def main() -> None:
    agent = Agent()
    # Accessing .core builds the engine (and, in local mode, loads the model).
    agent.core.set_event_sink(on_event)

    print("--- streamed turn ---")
    answer = agent.run(
        "List the files in the current directory, then say how many there are.",
        on_token=on_token,
    )
    print("\n--- final (cleaned) answer ---")
    print(answer)


if __name__ == "__main__":
    main()
