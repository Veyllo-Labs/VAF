# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Hello Agent: the minimal VAF embedding.

Runs one prompt against your configured backend and shows that repeated
run() calls continue the same conversation. Uses whatever provider your
~/.vaf/config.json selects; pass config={...} to override per instance
(nothing is written to disk). Docs: docs/EMBEDDING.md.
"""
from vaf import Agent


def main() -> None:
    # config=None -> your on-disk VAF config decides provider and model.
    # Override per instance, e.g.:
    #   Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-..."})
    # Local mode note: the FIRST run may download a multi-GB model.
    agent = Agent()

    answer = agent.run("In one short sentence, what is Python?")
    print("Answer:", answer)

    # One Agent instance = one conversation: this follow-up sees the first turn.
    follow_up = agent.run("And in one sentence: who created it?")
    print("Follow-up:", follow_up)


if __name__ == "__main__":
    main()
