# Built-in Tools Catalog

The tools the **main agent** loads by default, grouped by area. Generated from the live
tool registry (`Agent.tools`, populated by `_load_tools()` in
[vaf/core/agent.py](../../vaf/core/agent.py)); 111 tools. The **Coder sub-agent**
additionally loads `coder_only` file/shell tools (e.g. `bash`, `move_file`,
`codesearch`) that are not in this list. Some tools only do anything once their
integration is connected (GitHub, email, calendar, WhatsApp, …).

`Perm` is the tool's `permission_level`: **read** (safe), **write** (changes state, no
prompt by default), **dangerous** (always confirmed), **system** (internal plumbing). See
the contract in [vaf/tools/base.py](../../vaf/tools/base.py) and
[TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md). To regenerate this
list, enumerate `Agent.tools` after constructing a `CoreAgent`.

## Web & research

| Tool | Perm | What it does |
|------|------|--------------|
| `web_search` | read | Search the web; auto-fetches page content for extraction. |
| `webfetch` | read | Fetch a URL and convert it to readable Markdown. |
| `research_agent` | read | Deep multi-section research sub-agent (10+ sources). |
| `browser_agent` | write | Drive a real Chromium browser for multi-step web tasks. |
| `repair_report` | read | Regenerate empty/too-short sections of an HTML report. |

## Files & filesystem

| Tool | Perm | What it does |
|------|------|--------------|
| `read_file` | read | Read a file (text, PDF, Word, Excel, PowerPoint, …). |
| `write_file` | write | Write a single file (create/overwrite). Relative paths land in the chat workspace; non-admin users are jailed to their own `VAF_Projects/<uid8>`. |
| `find_files` | read | Find files by glob pattern, recursively. |
| `list_files` | read | List files in a directory. |
| `tree` | read | ASCII tree of a directory structure. |
| `librarian_agent` | write | Sub-agent for file system / storage / retrieval. |

## Documents (create, edit, view)

| Tool | Perm | What it does |
|------|------|--------------|
| `document_agent` | write | Sub-agent for large structured documents (contracts, reports). |
| `document_writer` | write | Create simple structured documents (letters, templates). |
| `document_editor` | read | Open a document in the editor panel for the user. |
| `document_viewer` | read | Open a document in the viewer panel. |
| `analyze_image` | read | Take a closer, targeted look at an image the user attached (exact colours, positions, small text, finding an object). Re-runs the vision backend on demand — see the vision section in [API_INTEGRATION.md](../llm/API_INTEGRATION.md). |
| `replace_editor_selection` | write | Replace the marked selection in the open editor. |
| `replace_editor_text` | write | Replace an exact snippet in the open editor. |
| `report_filename` | read | Generate a report filename in the Documents folder. |

## Memory & knowledge

| Tool | Perm | What it does |
|------|------|--------------|
| `memory_save` | write | Save information to long-term RAG memory. |
| `memory_search` | read | Search long-term RAG memory. |
| `add_memory` | write | Add a note to short-term session memory. |
| `learn_document` | write | Learn a document into long-term memory. |
| `learn_attached_knowledge` | write | Persist attached Web UI documents into memory. |
| `update_codex` | write | Save a pattern/convention to the project Codex. |
| `checkpoint_context` | system | Archive history and reset context after a major step. |

## Working memory, intent & identity

| Tool | Perm | What it does |
|------|------|--------------|
| `update_working_memory` | system | Update notes / plan / tasks that persist across turns. |
| `update_intent` | system | Update the primary session goal/task. |
| `add_task` | system | Add one pending task (prefer `update_working_memory`). |
| `update_user_identity` | write | Update who the current user is and their preferences. |

## Code & execution

| Tool | Perm | What it does |
|------|------|--------------|
| `coding_agent` | write | Autonomous code-generation sub-agent. |
| `create_agent_tool` | system | Create/update a Python tool the agent can use immediately. |
| `python_sandbox` | write | Run Python in a Docker-isolated sandbox. |
| `python_exec` | dangerous | Run Python on the host (no sandbox) — confirmed. |
| `run_tests` | read | *(coder-only)* Run the project's tests in the isolated sandbox and return the real pass/fail. |
| `host_bash` | dangerous | *(main agent)* Run a shell command on the HOST for host/docker tasks. Requires the user's confirmation each time; hard-blocked on remote channels (Telegram/WhatsApp/Discord), local app only. |

> **Two different shells.** The coder's `bash` (`coder_only`) runs inside a kernel jail
> (bubblewrap): full access to its project workspace, but VAF's source, `~/.vaf`, secrets and
> the host docker socket are structurally out of reach, and network is unshared. Host/docker
> work is the *main agent's* `host_bash`, which runs unsandboxed on the host under an explicit
> per-command confirmation gate and is never exposed over remote channels.
> See `docs/security/SANDBOXING.md` § "Shell execution surfaces" for the confinement details.

## Workflows & skills

| Tool | Perm | What it does |
|------|------|--------------|
| `create_agent_workflow` | system | Plan and run multi-step workflows. |
| `execute_workflow` | write | Run a specific workflow by ID. |
| `list_workflows` | read | List available workflows. |
| `use_skill` | read | Load the full instructions for a named Skill. |
| `list_skills` | read | List the Skills visible to the user; flags the ones they own. |
| `read_skill` | read | Show a visible Skill's raw SKILL.md source (inspect before editing). |
| `create_skill` | write | Create a new private Skill owned by the user (safety-scanned). |
| `update_skill` | write | Edit a Skill the user owns. |
| `delete_skill` | dangerous | Delete a Skill the user owns. |
| `list_tools` | read | List all tools available to the model. |
| `search_tools` | read | Search the tool catalogue by keyword. |

## Automations, timers & planner

| Tool | Perm | What it does |
|------|------|--------------|
| `create_automation` | write | Schedule a prompt to run at a clock time/frequency. |
| `update_automation` | write | Modify an existing automation. |
| `delete_automation` | write | Move an automation to trash (recoverable). |
| `restore_automation` | write | Restore an automation from trash. |
| `list_automations` | read | List scheduled automations (with today-status). |
| `read_automation` | read | Read one automation's full details. |
| `list_trash` | read | List automations in trash. |
| `add_automation_note` / `list_automation_notes` / `delete_automation_note` | write/read/write | Notes shown in the automation calendar. |
| `add_automation_todo` / `list_automation_todos` / `delete_automation_todo` | write/read/write | To-dos shown in the automation calendar. |
| `set_timer` | write | Schedule a short one-shot timer that fires in this chat. |
| `cancel_timer` | write | Cancel a pending timer. |
| `list_timers` | read | List pending timers. |

## Calendar & contacts

| Tool | Perm | What it does |
|------|------|--------------|
| `create_calendar_event` | write | Create an event (Google / Outlook). |
| `update_calendar_event` | write | Update an event. |
| `delete_calendar_event` | write | Delete an event (irreversible). |
| `list_calendar_events` | read | List events in a time range. |
| `create_contact` | write | Create a contact. |
| `update_contact` | write | Update a contact. |
| `delete_contact` | write | Delete a contact (irreversible). |
| `get_contact` | read | Get a contact by name (returns channel IDs). |
| `list_contacts` | read | List all contacts. |

## Email

| Tool | Perm | What it does |
|------|------|--------------|
| `mail_inbox` | read | Show the inbox. |
| `read_mail` | read | Read the full body of one email. |
| `find_mail` | read | Search the mailbox by subject/sender. |
| `send_mail` | write | Send an email (irreversible). |
| `label_mail` | write | Set an email's label/category. |
| `mark_mail_answered` | write | Mark an email as answered. |
| `list_email_accounts` | read | List connected email accounts. |

## Messaging

| Tool | Perm | What it does |
|------|------|--------------|
| `send_whatsapp` | write | Send WhatsApp text / voice / document (irreversible). |
| `whatsapp_inbox` | read | List WhatsApp chats. |
| `read_whatsapp_chat` | read | Read messages from a WhatsApp chat. |
| `find_whatsapp_messages` | read | Search WhatsApp messages. |
| `whatsapp_call` | write | Placeholder — WhatsApp call (not implemented). |
| `send_telegram` | write | Send a Telegram message (irreversible). |
| `telegram_inbox` | read | List Telegram chats with stored messages. |
| `read_telegram_chat` | read | Read messages from a Telegram chat. |
| `find_telegram_messages` | read | Search Telegram messages. |
| `send_discord` | write | Send a Discord message (irreversible). |
| `discord_inbox` | read | List Discord chats with stored messages. |
| `read_discord_chat` | read | Read messages from a Discord chat. |
| `find_discord_messages` | read | Search Discord messages. |
| `send_slack` | write | Send a Slack message (irreversible). |

## GitHub

| Tool | Perm | What it does |
|------|------|--------------|
| `github_list_repos` | read | List the user's repositories. |
| `github_list_directory` | read | List a directory in a repo. |
| `github_get_tree` | read | Recursive file list of a repo. |
| `github_get_file` | read | Get a file's content. |
| `github_get_file_structure` | read | Classes/functions/methods of a code file. |
| `github_search_files` | read | Search files by name/extension. |
| `github_list_issues` | read | List issues. |
| `github_list_pulls` | read | List pull requests. |
| `github_create_issue` | write | Create an issue (irreversible). |
| `github_update_file` | write | Create/update a file (commit, irreversible). |

## Git (local)

| Tool | Perm | What it does |
|------|------|--------------|
| `git_init` | write | Initialize a Git repo in the project dir. |
| `git_add_commit` | write | Stage files and commit (irreversible). |
| `git_status` | read | Show working-tree status. |
| `git_log` | read | Show commit history. |

## Other

| Tool | Perm | What it does |
|------|------|--------------|
| `cloud_storage` | write | Google Drive / OneDrive access (prefer `search_all`). |
| `mcp_call` | write | Call external tools via Model Context Protocol. |
| `thinking_note_add` | system | Save a note for the next background thinking run. |
