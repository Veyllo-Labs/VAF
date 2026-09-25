# Built-in Tools Catalog

The tools the **main agent** loads by default, grouped by area. Generated from the live
tool registry (`Agent.tools`, populated by `_load_tools()` in
[vaf/core/agent.py](../../vaf/core/agent.py)); 119 tools, counted from a freshly
constructed agent rather than from this list's own history. The **Coder sub-agent**
additionally loads `coder_only` file/shell tools (e.g. `bash`, `move_file`,
`codesearch`) that are not in this list. Some tools only do anything once their
integration is connected (GitHub, email, calendar, WhatsApp, …).

The coder does **not** get this whole catalogue. It is offered the whitelist in
[vaf/core/coder_tools.py](../../vaf/core/coder_tools.py) (`CODER_ALLOWED_TOOLS`): files,
code, git, shell, tests and lookups, with mail, messengers, calendars and contacts
deliberately absent. Adding a tool here therefore does not put it in front of the coder;
that takes an entry in the whitelist, or the `coder_tool_allowlist_extra` config key. See
[CODER_ARCHITECTURE.md](CODER_ARCHITECTURE.md) for why it is a whitelist and not the
exclusion list it used to be.

`Perm` is the tool's `permission_level`: **read** (safe), **write** (changes state, no
prompt by default), **dangerous** (always confirmed), **system** (internal plumbing). See
the contract in [vaf/tools/base.py](../../vaf/tools/base.py) and
[TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md). To regenerate this
list, enumerate `Agent.tools` after constructing a `CoreAgent`.

## Web & research

| Tool | Perm | What it does |
|------|------|--------------|
| `web_search` | read | Search the web; auto-fetches page content for extraction. |
| `webfetch` | read | Fetch a URL and convert it to readable Markdown. `prompt` reads the page for a question in a model call of its own (`search.answer_from_page`, the reader `web_search` uses per result page) and returns only the answer, so a long page stays out of the conversation. |
| `download_file` | write | Save a file from a URL as-is: relative `save_to` = the chat's workspace, a folder keeps the file's name. At most 500 MB, never a partial file. `file_access = "write"`: nothing lands outside the caller's own tree. Its own tool so that reading a page stays a read; not offered to a thinking run, which must not create files. |
| `research_agent` | read | Deep multi-section research sub-agent (10+ sources). |
| `browser_agent` | write | Drive a real Chromium browser for multi-step web tasks. |
| `render_check` | write | Open a URL or workspace HTML file in the sandbox browser once and report page errors, console output, failed requests, rendered text and a screenshot. |
| `repair_report` | read | Regenerate empty/too-short sections of an HTML report. |

## Files & filesystem

| Tool | Perm | What it does |
|------|------|--------------|
| `read_file` | read | Read a file (text, PDF, Word, Excel, PowerPoint, …). Budgets its own result (deliverable exemption): large text returns a first window plus a heading index with line numbers, continued via `start_line`/`end_line`; PDFs via `first_page`/`last_page`. |
| `write_file` | write | Write a single file (create/overwrite); binary files via `content_base64` (e.g. sandbox-rendered images). Relative paths land in the chat workspace; non-admin users are jailed to their own `VAF_Projects/<uid8>`. |
| `find_files` | read | Find files by glob pattern, recursively. |
| `list_files` | read | List files in a directory. |
| `tree` | read | ASCII tree of a directory structure. |
| `librarian_agent` | write | Sub-agent for file system / storage / retrieval; no delete capability - deletion tasks are refused with an explicit capability statement. |

## Documents (create, edit, view)

| Tool | Perm | What it does |
|------|------|--------------|
| `document_agent` | write | Sub-agent for large structured documents (contracts, reports). |
| `document_writer` | write | Create simple structured documents (letters, templates) as `.txt`/`.md`/`.docx` only; other extensions are rejected with a redirect to `write_file`/`coding_agent`. |
| `document_editor` | read | Open a document in the editor panel for the user. |
| `document_viewer` | read | Open a document in the viewer panel. |
| `analyze_image` | read | Take a closer, targeted look at an image the user attached OR an image file of the person's own (`image_path`: a path relative to the chat workspace, e.g. a sandbox-exported chart, or an absolute one, e.g. a saved screenshot). `file_access = "read"`: exactly the files `read_file` may read, and `read_file` itself points an image at this tool instead of returning its bytes. Re-runs the vision backend on demand - see the vision section in [API_INTEGRATION.md](../llm/API_INTEGRATION.md). |
| `replace_editor_selection` | write | Replace the marked selection in the open editor. |
| `replace_editor_text` | write | Replace an exact snippet in the open editor. |
| `report_filename` | read | Generate a report filename in the Documents folder. |

## Memory & knowledge

| Tool | Perm | What it does |
|------|------|--------------|
| `memory_save` | write | Save information to long-term RAG memory. Refuses a near-duplicate once, naming the existing memory: the model then updates it via `memory_update` or insists with `confirm_new=true`. |
| `memory_update` | write | Update an existing long-term memory in place (full new text, re-embedded); the id comes from `memory_search` or `memory_save`'s duplicate notice. |
| `memory_search` | read | Search long-term RAG memory, and the caller's other chats for where a topic was discussed (two labelled sections; the chat half needs no database and still answers during an outage). Each memory snippet names its `memory_id` for `memory_update`. |
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
| `ask_user` | system | Ask the user ONE question, optionally with `options` to pick. In a chat it ends the turn (`ends_turn`): the question is the answer, the options become buttons in the web chat and a numbered list elsewhere, and the reply is the next message. In a thinking run or a scheduled automation it is delivered and tracked as a request instead ([Thinking-Mode.md](Thinking-Mode.md)). Offered on every chat turn. |

## Code & execution

| Tool | Perm | What it does |
|------|------|--------------|
| `coding_agent` | write | Autonomous code-generation sub-agent. |
| `create_agent_tool` | system | Create/update a Python tool the agent can use immediately. |
| `python_sandbox` | write | Run Python in a Docker-isolated sandbox; `export_files` copies produced artifacts (images, PDFs) into the chat workspace after the run. |
| `python_exec` | dangerous | Run Python on the host (no sandbox) - confirmed; the tool re-checks on its own (a workflow step runs without the gate) that the person allowed it beyond one call: a persisted `set_tool_policy("python_exec", "allow")` or their "for this chat" grant for this chat. "Only this time" is not enough. Stored credentials as `os.environ["VAF_SECRET_<NAME>"]`, like `host_bash`. See the host-execution line in [EMBEDDING.md](../EMBEDDING.md). |
| `run_tests` | read | *(coder-only)* Run the project's tests in the isolated sandbox and return the real pass/fail. |
| `host_bash` | dangerous | Run a shell command on the HOST for host/docker tasks. An account permission, outside the file jail. In the chat the person answers once / for this chat / always; the coder and workflow steps run it without asking. The main agent's direct call is hard-blocked on remote channels (Telegram/WhatsApp/Discord). `background=true` starts it detached and returns an id; the chat is woken when it ends. A credential the person stored is written as `$VAF_SECRET_<NAME>`: the command gets exactly the ones it names, and their values are removed from the output ([CONNECTIONS.md](../integrations/CONNECTIONS.md)). |
| `host_process` | write | The handle on this chat's background commands: `list`, `log` (end of the output), `write` (a line to its input), `stop` (the whole tree). Visible only to the chat and person that started them. |

> **Two different shells.** The coder's `bash` (`coder_only`) runs inside a kernel jail
> (bubblewrap): full access to its project workspace, but VAF's source, `~/.vaf`, secrets and
> the host docker socket are structurally out of reach, and network is unshared. Host/docker
> work is `host_bash`, which runs unsandboxed on the host: asked in the chat, unattended in
> the coder and in workflow steps, and never called directly by the main agent from a remote
> channel.
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
| `search_tools` | read | Search the tool catalogue by keyword; top matches include the call signature. |

## Automations, timers & planner

| Tool | Perm | What it does |
|------|------|--------------|
| `create_automation` | write | Schedule a prompt to run at a clock time/frequency, or with `frequency: on_event` when something happens in an agent room (`trigger_room` plus `trigger_match` or `trigger_emoji`). |
| `update_automation` | write | Modify an existing automation. |
| `delete_automation` | write | Move an automation to trash (recoverable). |
| `restore_automation` | write | Restore an automation from trash. |
| `list_automations` | read | List scheduled automations (with today-status). |
| `read_automation` | read | Read one automation's full details. |
| `list_trash` | read | List automations in trash. |
| `add_automation_note` / `list_automation_notes` / `delete_automation_note` | write/read/write | Notes shown in the calendar window. |
| `add_automation_todo` / `list_automation_todos` / `delete_automation_todo` | write/read/write | To-dos shown in the calendar window. |
| `set_timer` | write | Schedule a short one-shot timer that fires in this chat. |
| `schedule_reminder` | write | Persistent one-shot reminder: stored as data, delivered verbatim at fire_at on the user's main messenger by the scheduler (no agent run). |
| `cancel_timer` | write | Cancel a pending timer. |
| `list_timers` | read | List pending timers. |

## Calendar & contacts

| Tool | Perm | What it does |
|------|------|--------------|
| `create_calendar_event` | write | Create an event in the VAF calendar; mirrored into the connected Google or Outlook account unless `internal_only`. Links a contact, sets a reminder. |
| `update_calendar_event` | write | Update an event (title, time, location, reminder); a moved start keeps the duration. |
| `delete_calendar_event` | write | Delete an event (irreversible; a mirrored one is removed at the provider by the next sync). |
| `list_calendar_events` | read | List the calendar's events in a time range, offline, from the store. |
| `create_contact` | write | Create a contact: name, channels, personal file, company, role, comma-separated tags, and `assistant_access` (`allowed` or `denied`) only when the user said so (recorded with source `agent`). |
| `update_contact` | write | Update a contact: fields (company, role and tags included; tags replace), status, `assistant_access` (`allowed` / `denied` / `undecided`, the three states of "may the agent answer this person"), a dated note (`add_note`), a dated event (`add_event_title` + `add_event_when`). |
| `delete_contact` | write | Delete a contact (irreversible). |
| `get_contact` | read | Get a contact by name: channel IDs, personal file, company, role, tags, since when and from where, status, last contact, upcoming events, newest notes, and the newest stored messages and mails with the person. |
| `list_contacts` | read | List all contacts, optionally filtered by status or tag. |

## Email

| Tool | Perm | What it does |
|------|------|--------------|
| `read_mail` | read | Read the full body of one email. |
| `find_mail` | read | Search the mailbox by subject/sender. |
| `send_mail` | write | Send an email (irreversible). Ordered in the web chat, it is parked as a draft for the person (`outward_send_hold`). |
| `reply_mail` | write | Reply (quoted, correctly threaded) to an email (irreversible). Parked as a draft when ordered in the web chat. |
| `forward_mail` | write | Forward an email to new recipients (irreversible). Parked as a draft when ordered in the web chat. |
| `archive_mail` | write | Move an email out of the inbox into Archive. |
| `delete_mail` | write | Move an email to Trash (trash-only, never expunged). |
| `label_mail` | write | Set an email's label/category. |
| `mark_mail_answered` | write | Mark an email as answered. |
| `list_email_accounts` | read | List connected email accounts. |

`reply_mail`, `forward_mail`, `archive_mail` and `delete_mail` are live agent
verbs. Their mailbox writes apply locally first and replay to the server through
the durable op queue only once `mail_engine_write_enabled` is on (still off by
default). SENDING is deliberately not gated by that flag - a queued mail must be
able to leave - so `reply_mail` and `forward_mail` send for real, each still
passing the high-risk send gate. All four are excluded from the front-office
contact lane by design.

Note that the **Mail Composer** (the draft assistant in the mail window's compose
box) is NOT a tool and is not in this list. It is a route, it never sends, and it
runs its model call with no tools at all - see
[EMAIL_CLIENT.md](../integrations/EMAIL_CLIENT.md).

## Messaging

| Tool | Perm | What it does |
|------|------|--------------|
| `inbox` | read | Every conversation across WhatsApp, Telegram, Discord, mail and rooms, newest first, with unread and who waits for an answer; `channel` and `view` narrow it. The rows the inbox window shows. |
| `contact_history` | read | Front Office only: what the contact being answered wrote to the owner before, and what went to them, across WhatsApp, Telegram, Discord and mail (the contact book's timeline for that one person, pinned by the runner, no argument names anyone); `channel` and `query` narrow it. See [FRONT_OFFICE.md](FRONT_OFFICE.md#tool-restriction). |
| `send_to_user` | write | Channel-agnostic delivery: resolves the user's `main_messenger` at run time and sends text plus optional file via the canonical router; Web UI notification fallback (irreversible). |
| `send_whatsapp` | write | Send WhatsApp text / voice / document (irreversible). With `to_phone` (a third party) and ordered in the web chat, it is parked for the person; without it the message goes to the account owner and is sent. |
| `read_whatsapp_chat` | read | Read messages from a WhatsApp chat. |
| `find_whatsapp_messages` | read | Search WhatsApp messages. |
| `whatsapp_call` | write | Placeholder - WhatsApp call (not implemented). |
| `send_telegram` | write | Send a Telegram message (irreversible). |
| `read_telegram_chat` | read | Read messages from a Telegram chat. |
| `find_telegram_messages` | read | Search Telegram messages. |
| `send_discord` | write | Send a Discord message, optionally with a document attachment (irreversible). |
| `read_discord_chat` | read | Read messages from a Discord chat. |
| `find_discord_messages` | read | Search Discord messages. |
| `send_slack` | write | Placeholder: Slack is a known channel without a bridge, so the tool answers that it cannot send yet. |

## Agent rooms

A room is a group chat shared with other agents, which may be VAF agents or foreign
ones. It is **not** a messaging channel: `room` is deliberately absent from
`KNOWN_CHANNELS`, so a room is never offered as a place to send proactive messages,
and a room grants no capability of any kind: it assigns a role, which decides what a
peer may say, never what it may do to the machine.

| Tool | Perm | What it does |
|------|------|--------------|
| `room_open` | write | Open a new room and join it: `round` for equals, `chain` when the agent leads the agents it invites. |
| `room_invite` | write | Mint a single-use invitation for one more agent and return the briefing to hand over verbatim. Called again for each further agent. With `account`, invite a VAF account on this server by user name instead: the person answers in their own sidebar, the room is opened to other accounts if it was not, and the result says so. |
| `room_join` | write | Join a room by id and set how far the agent may act on what arrives there (`observe` / `assist` / `autonomous`). |
| `room_send` | write | Write into a room: `say`, `ask`, `answer`, `report` (with a status) or `directive`. What the role may emit is decided by the room, not by the tool. A leading `@Name` in the text addresses that one member; the room resolves it. `files` names files in the room's shared folder the message is about. |
| `room_react` | write | An emoji on ONE message (`reply_to` its id): the way to say seen, agreed, done or no. Shown to everybody, wakes nobody, and the only acknowledgement the conduct rules allow. |
| `room_read` | read | Read what is new in a room, or list the agent's rooms with their unread counts. Reading takes nothing away from other readers. |
| `room_verify` | read | One verdict per message: who can be PROVEN to have written it, rather than whose name the host wrote on it. `problems_only` narrows it to what is not plainly in order. |

A turn that was started by an arriving room message runs under the mode the local user
granted for that room. In `observe` no write-level tool runs at all, in `assist` the
agent may talk in the room but anything touching the machine waits for the user's word
in the ordinary chat, and only `autonomous` acts unprompted. The gate is
`_room_mode_gate_decision` in [vaf/core/agent.py](../../vaf/core/agent.py), and it fails
closed.

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
