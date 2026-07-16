# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chatbot that survives restarts.

save_session() persists the conversation into VAF's standard session store
(~/.vaf/sessions/); Agent(session=<id>) resumes it. Run this script, chat,
quit, run it again - the agent remembers. Docs: docs/EMBEDDING.md
"Persistent conversations".
"""
from pathlib import Path

from vaf import Agent

ID_FILE = Path("chat_session_id.txt")  # where THIS app remembers its session id


def make_agent() -> Agent:
    if ID_FILE.exists():
        sid = ID_FILE.read_text().strip()
        try:
            return Agent(session=sid)
        except ValueError:
            print(f"(session {sid} not found, starting fresh)")
    return Agent()


def main() -> None:
    agent = make_agent()
    print("Chat - type 'quit' to exit. The conversation persists across restarts.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() in ("quit", "exit"):
            break
        print(agent.run(line))
        # Saving after every turn is safe: the same session is updated in place.
        ID_FILE.write_text(agent.save_session())


if __name__ == "__main__":
    main()
