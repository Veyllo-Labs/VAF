# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-instance tool registration: give ONE Agent instance an extra tool.

No pip package, no file drop-in: define a BaseTool subclass and register it
with add_tool() before the first run(). The tool exists only on this Agent
instance. For distribution, prefer the entry-point package lane
(vaf_example_tool/). Docs: docs/EMBEDDING.md "Writing a tool".
"""
import datetime

from vaf import Agent
from vaf.tools.base import BaseTool


class UtcTimeTool(BaseTool):
    name = "utc_time"
    description = "Return the current UTC date and time."
    permission_level = "read"
    side_effect_class = "none"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )


def main() -> None:
    agent = Agent()
    agent.add_tool(UtcTimeTool())  # must happen before the first run()

    answer = agent.run("What time is it in UTC right now? Use your tool.")
    print(answer)


if __name__ == "__main__":
    main()
