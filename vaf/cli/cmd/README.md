# CLI Command Implementations

This directory contains the logic for individual `vaf` CLI commands. Each file typically corresponds to a sub-command (e.g., `vaf run`, `vaf session`).

## Commands

- **run.py**: Handles the `run` command, initializing the agent and starting the TUI or one-shot prompt.
- **settings.py**: Provides the interactive settings menu to modify `config.json`.
- **scaffold.py**: Logic for creating new project templates from pre-defined structures.
- **automate.py**: Test/build/lint automation commands.
- **git.py**: AI-enhanced Git operations (auto-commits, status summaries).
- **audit.py**: Code Audit (`audit run` reviews a change in any git repository with the configured model and prints verified findings with a fix prompt each; exit 0 clean, 1 findings, 2 incomplete; `--verify-steps N` bounds the verifier's searches and reads per confirmed finding, 0 keeps the first check only; `audit show` prints the last audit, `audit dismiss` records a finding as not a problem). Engine: `vaf.core.code_audit`.
- **models.py**: Model management commands (list, download, select).
- **subagent.py**: Allows running specialized sub-agents (Coder, Researcher) independently.
- **workflow.py**: Workflow execution and inspection commands.
- **service.py**: Manages the VAF background process (`start`/`stop`/`restart`/`status`), via PID file in desktop mode or systemd in server mode.
- **server.py**: Toggles local network hosting with mandatory TLS (`server on`/`server off`), provisions a server (`server provision`), shows the access URLs per LAN and VPN interface (`server status`), and manages who is admitted besides the local networks (`server networks list|allow|remove|tailscale`, `server vpn-only on|off`).
- **update.py**: Self-update to the latest published GitHub Release, with dependency reinstall, migrations, and rollback on failure.
- **info.py**: Displays system and diagnostic information (Python, platform, key dependency versions).
- **security.py**: Security diagnostics and hardening checks (`security doctor`).
- **debug.py**: AI-powered error analysis (`debug explain`) that parses stack traces and suggests fixes.
- **generate.py**: AI code generation for snippets, API endpoints, and functions.
- **ww.py**: Whare Wananga tool self-learning commands (train and inspect per-tool know-how).
- **secure.py**: Encryption and key management (`secure status` reports where every at-rest key lives and what is still unprotected, `secure recover` restores the data key from the recovery key after a reinstall, `secure rotate-db` replaces the shipped default Postgres password).
- **memory.py**: Memory store maintenance (`memory rekey` re-encrypts rows after a key rotation, `memory cross-chat` dry-runs the Cross Chat Hint lane for one question).
- **setup.py**: Creates the admin account for this machine (`vaf setup`).
- **repair.py**: Checks the Docker services and puts a broken one back (`vaf repair`); the work is `vaf.core.service_health`.
- **top.py**: Live server dashboard in the terminal (`vaf top`): uptime, configuration, utilization, services.
- **usage.py**: Token usage and spend records (`usage show`, `usage set-currency`).
- **inbox.py**: Your conversations across every channel (`inbox list`).
- **outbox.py**: Messages your agent prepared and has not sent (`outbox list|send|discard|edit`).
- **secrets.py**: Credentials your agent's commands may use, never shown to the model (`secrets list|set|rm`).
- **ssh.py**: The SSH key and servers your agent uses, never your `~/.ssh` (`ssh key|hosts|forget`).
- **a2a.py**: Agent-to-agent rooms: join, talk, read.

## Development Guide

When adding a new command:
1.  **File Naming**: Use a descriptive name (e.g., `my_command.py`).
2.  **Interface**: Ensure the command accepts standard arguments and provides a `--help` description.
3.  **Integration**: Import and register the command's entry point in the main CLI router.
4.  **Consistency**: Use the UI utilities from `vaf.cli.ui` to ensure output matches the project's visual style.

## Dependencies

- Relies on `vaf.core` for agent logic and `vaf.cli.ui` for presentation.
- May use specialized libraries relevant to the command.
