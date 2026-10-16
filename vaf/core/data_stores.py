# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where VAF keeps what it keeps: one list, read by everything that has to know.

VAF writes into five places on disk, and which of them holds what grew file by file:

- ``app``: ``Config.APP_DIR`` (``~/.vaf``, or ``$VAF_CONFIG_DIR`` in Docker mode) - the
  config, the machine key, the per-user identity folders.
- ``vaf``: ``Platform.vaf_dir()`` (``~/.vaf`` always) - chats, automations, skills, rooms,
  the SSH and FTP folders. On a normal install the same directory as ``app``.
- ``data``: ``Platform.data_dir()`` - the keyring and the other encrypted stores, the
  message, mail, calendar and contact databases.
- ``config``: ``Platform.config_dir()`` - the tool grants per account.
- ``projects``: ``Documents/VAF_Projects`` - the people's own working folders.

Before this list, the answer to "what has to be backed up?" lived as half-copies in three
places (the at-rest migration's tree list, ``vaf secure status``, a cleanup script), and a
measured inventory found 19 statements in the docs that no longer matched the code. Now
each top-level entry of each root is classified once, here:

- ``KEY``: key material. Without it the ciphertext beside it is noise.
- ``DATA``: what people made or decided; irreplaceable.
- ``RECREATABLE``: comes back by signing in or pairing again, at a cost worth avoiding
  (the WhatsApp link, the TLS authority browsers trust).
- ``AUDIT``: the security event log - a record, not a cache.
- ``DERIVED``: caches and indexes VAF rebuilds on its own.
- ``RUNTIME``: locks, pids, queues and markers of a running process. Restoring them would
  replay or confuse work, not recover it.
- ``LOG``: diagnostics.
- ``NEVER``: must not leave the machine in a backup (the recovery note: a plaintext key).

A backup takes ``KEY``, ``DATA``, ``RECREATABLE`` and ``AUDIT`` (``BACKED_UP``). An entry
on disk that this list does not know is reported, never silently taken or dropped: the
owner sees it, and a test fails when the code joins a new literal name onto a root
without a row here (tests/test_data_stores.py).

What is not a file: Postgres, the Docker volumes of the sandbox environments and the
personal browsers, and OS keyring entries. ``EXTERNAL`` names them; the backup engine
reads each with its own tool.

NAMED BOUNDARY: the working memory under the launch directory (``<cwd>/.vaf/main``) is
per-session scratch state that the garbage collector removes by age; it is not a root
here. Not on the facade: the stores are VAF's own; an embedder's application keeps its
own, and no embedder has asked for a shared registry.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

KEY = "key"
DATA = "data"
RECREATABLE = "recreatable"
AUDIT = "audit"
DERIVED = "derived"
RUNTIME = "runtime"
LOG = "log"
NEVER = "never"

BACKED_UP = frozenset({KEY, DATA, RECREATABLE, AUDIT})
KINDS = frozenset({KEY, DATA, RECREATABLE, AUDIT, DERIVED, RUNTIME, LOG, NEVER})


@dataclass(frozen=True)
class Store:
    root: str
    pattern: str          # fnmatch on the entry's path relative to the root, "/"-separated
    kind: str
    owner: str
    note: str = ""


def _s(root, pattern, kind, owner, note=""):
    return Store(root, pattern, kind, owner, note)


STORES: Tuple[Store, ...] = (
    # ── app: Config.APP_DIR ──────────────────────────────────────────────────────
    _s("app", "config.json", DATA, "vaf/core/config.py", "includes the database connection strings"),
    _s("app", "secure_store.kek", KEY, "vaf/core/secure_store.py", "the machine key (file backend)"),
    _s("app", "secure_store.kek.where", KEY, "vaf/core/secure_store.py"),
    _s("app", "secure_store.kek.lock", RUNTIME, "vaf/core/secure_store.py"),
    _s("app", "data_keys.established", KEY, "vaf/core/data_keyring.py"),
    _s("app", "service.env", KEY, "install.sh", "VAF_MASTER_PASSPHRASE for a server install"),
    _s("app", "config.json.pre-keyring.bak", NEVER, "vaf/core/secure_store.py",
       "a plaintext copy of legacy keys, deleted after the migration"),
    _s("app", "config.json.bak*", NEVER, "vaf/core/migrations.py", "plaintext legacy copies"),
    _s("app", "compose.env", DERIVED, "vaf/core/service_stack.py", "rewritten from the keyring"),
    _s("app", "users", DATA, "vaf/auth/user_workspace.py",
       "identity, soul, front office profile; WhatsApp logins inside"),
    _s("app", "users/*/whatsapp", RECREATABLE, "vaf/core/whatsapp_auth.py"),
    _s("app", "users/*/whatsapp_avatars", DERIVED, "vaf/api/whatsapp_bridge.py"),
    _s("app", "user_profile_cache", DERIVED, "vaf/memory/rag.py"),
    _s("app", "tag_links.json", DATA, "vaf/memory/tag_links.py"),
    _s("app", "compaction_state.json", DERIVED, "vaf/memory/rag.py"),
    _s("app", "maintenance", RUNTIME, "vaf/memory/reembed.py"),
    _s("app", "firewalld_lan.json", DERIVED, "vaf/network/firewall.py"),
    _s("app", "ufw_sources.json", DERIVED, "vaf/network/firewall.py"),
    _s("app", "ssl", RECREATABLE, "vaf/network/ssl_utils.py", "browsers trust this authority"),
    _s("app", "tmp", DERIVED, "vaf/tools/search.py"),
    # ── vaf: Platform.vaf_dir() ──────────────────────────────────────────────────
    _s("vaf", "sessions", DATA, "vaf/core/session.py", "the chats, encrypted"),
    _s("vaf", "context_archive", DERIVED, "vaf/core/context.py", "removed after 14 days"),
    _s("vaf", "handoff_bundles", RUNTIME, "vaf/core/handoff_bundle.py", "kept 7 days"),
    _s("vaf", "subagent_queue", RUNTIME, "vaf/core/subagent_ipc.py"),
    _s("vaf", "automations", DATA, "vaf/core/automation.py"),
    _s("vaf", "last_automation_run.json", RUNTIME, "vaf/core/automation.py"),
    _s("vaf", "automation_planner", DATA, "vaf/core/automation_planner.py"),
    _s("vaf", "reminders", DATA, "vaf/core/reminders.py"),
    _s("vaf", "thinking_requests", DATA, "vaf/core/thinking_requests.py"),
    _s("vaf", "thinking_suggestions", DATA, "vaf/core/thinking_suggestions.py"),
    _s("vaf", "thinking_mode_logs", LOG, "vaf/core/thinking_mode.py"),
    _s("vaf", "thinking_notes.db", DERIVED, "vaf/core/thinking_notes.py"),
    _s("vaf", "security", DATA, "vaf/core/threat_db.py", "the known-bad hash list"),
    _s("vaf", "whare_wananga", DATA, "vaf/whare_wananga/store.py", "learned, expensive to relearn"),
    _s("vaf", "whare_wananga_retrain.json", DATA, "vaf/whare_wananga/retrain.py"),
    _s("vaf", "a2a", DATA, "vaf/core/a2a/store.py", "rooms, pinned peers, seats"),
    _s("vaf", "a2a/session", RUNTIME, "vaf/core/a2a/session.py"),
    _s("vaf", "ssh", DATA, "vaf/core/ssh.py", "each account's key and confirmed servers"),
    _s("vaf", "ftp", DATA, "vaf/core/ftp.py"),
    _s("vaf", "environments", DATA, "vaf/core/environments.py", "ties volumes to their owners"),
    _s("vaf", "browser_sessions", RECREATABLE, "vaf/core/browser_interactive.py"),
    _s("vaf", "speaker_profiles", DATA, "vaf/core/speaker_id.py"),
    _s("vaf", "speaker_confirm", RUNTIME, "vaf/core/speaker_confirm.py"),
    _s("vaf", "skills", DATA, "vaf/core/skills_registry.py"),
    _s("vaf", "workflows", DATA, "vaf/workflows/templates.py"),
    _s("vaf", "autosuggest.json", DERIVED, "vaf/cli/autosuggest.py"),
    _s("vaf", "autosuggest", DERIVED, "vaf/cli/autosuggest.py"),
    _s("vaf", "history", DATA, "vaf/cli/history.py", "the terminal's prompt history"),
    _s("vaf", "output", DATA, "vaf/core/platform.py"),
    # A project's own memory, here when the project is the home folder (vaf/core/persistence.py).
    _s("vaf", "tasks.json", DATA, "vaf/core/persistence.py"),
    _s("vaf", "codex.md", DATA, "vaf/core/persistence.py"),
    _s("vaf", "memory.md", DATA, "vaf/core/persistence.py"),
    _s("vaf", "snapshots", DATA, "vaf/core/snapshot.py"),
    _s("vaf", "icons", DERIVED, "vaf/tray.py"),
    _s("vaf", "logs", LOG, "vaf/main.py"),
    _s("vaf", "main", RUNTIME, "vaf/core/main_persistence.py", "working memory of a launch from HOME"),
    _s("vaf", "update_cache.json", RUNTIME, "vaf/core/update_check.py"),
    _s("vaf", "last_update.json", RUNTIME, "vaf/core/update_check.py"),
    _s("vaf", "update_result.json", RUNTIME, "vaf/core/update_check.py"),
    _s("vaf", "instance.json", RUNTIME, "vaf/core/instance.py"),
    _s("vaf", "active_sessions.txt", RUNTIME, "vaf/core/agent.py"),
    _s("vaf", "shutdown.lock", RUNTIME, "vaf/core/agent.py"),
    _s("vaf", "locks", RUNTIME, "vaf/core/lock_manager.py"),
    _s("vaf", "service.pid", RUNTIME, "vaf/cli/cmd/service.py"),
    _s("vaf", "server.pid", RUNTIME, "vaf/core/backend.py"),
    _s("vaf", "web_port", RUNTIME, "vaf/core/frontend_manager.py"),
    _s("vaf", "sandbox_env_image.lock", RUNTIME, "vaf/core/environment_image.py"),
    # ── data: Platform.data_dir() ────────────────────────────────────────────────
    _s("data", "data_keys.enc", KEY, "vaf/core/data_keyring.py"),
    _s("data", "data_keys.key.json", KEY, "vaf/core/data_keyring.py"),
    _s("data", "data_keys.recovery.json", KEY, "vaf/core/recovery_kit.py"),
    _s("data", "*.enc", KEY, "vaf/core/secure_store.py", "the credential stores"),
    _s("data", "*.key.json", KEY, "vaf/core/secure_store.py"),
    _s("data", "*.enc.lock", RUNTIME, "vaf/core/secure_store.py"),
    _s("data", "VAF-BackThisUp.md", NEVER, "vaf/core/recovery_kit.py",
       "the recovery note where there is no Desktop: a plaintext key"),
    _s("data", "*_oauth_state.json", RUNTIME, "vaf/core/oauth_pkce.py"),
    _s("data", "channel_messages.db*", DATA, "vaf/core/channel_message_store.py"),
    _s("data", "whatsapp_messages.db*", DATA, "vaf/core/channel_message_store.py", "legacy"),
    _s("data", "email_sync.db*", DATA, "vaf/core/email_sync_store.py"),
    _s("data", "scopes", DATA, "vaf/mail/store.py", "per account: mail, calendar, messages, contacts"),
    _s("data", "contacts.json", DATA, "vaf/core/contacts_store.py"),
    _s("data", "users", DATA, "vaf/core/contacts_store.py", "legacy per-username stores"),
    _s("data", "users/*/cloud_sync", DERIVED, "vaf/cloud/sync_manifest.py"),
    _s("data", "users/*/github_activity.json", DERIVED, "vaf/github/activity.py"),
    _s("data", "workspaces", DATA, "vaf/core/thinking_workspace.py"),
    _s("data", "spend", DATA, "vaf/core/cost.py"),
    _s("data", "fx_rates.json", DERIVED, "vaf/core/cost.py"),
    _s("data", "notifications", RUNTIME, "vaf/core/user_notifications.py"),
    _s("data", "thinking_notes.db*", DERIVED, "vaf/core/thinking_notes.py"),
    _s("data", "thinking_*.json", RUNTIME, "vaf/core/thinking_mode.py"),
    _s("data", "last_interaction.json", RUNTIME, "vaf/core/last_interaction.py"),
    _s("data", "messaging_endpoints.json", DATA, "vaf/core/messaging_connections.py"),
    _s("data", "mcp_servers.json", DATA, "vaf/core/mcp_registry.py"),
    _s("data", "custom_tools", DATA, "vaf/core/custom_tools_registry.py"),
    _s("data", "cloud_sync_*.db*", DERIVED, "vaf/cloud/sync_worker.py"),
    _s("data", "github_activity.json", DERIVED, "vaf/github/activity.py"),
    _s("data", "logs", LOG, "vaf/core/log_helper.py"),
    _s("data", "logs/security_events_*.jsonl", AUDIT, "vaf/core/security_events.py"),
    _s("data", "logs/security_*.log", AUDIT, "vaf/core/security_events.py"),
    _s("data", "drafts", DATA, "vaf/core/headless_runner.py", "legacy drafts"),
    _s("data", "whatsapp_bridge_ipc", RUNTIME, "vaf/api/whatsapp_bridge.py"),
    _s("data", "debug", LOG, "vaf/tools/search.py"),
    _s("data", "QtWebEngine", DERIVED, "the desktop window"),
    _s("data", ".telegram_backfill_done", RUNTIME, "vaf/api/telegram_bridge.py"),
    _s("data", "backups", NEVER, "vaf/core/backup.py", "backups do not carry backups"),
    # ── config: Platform.config_dir() ────────────────────────────────────────────
    _s("config", "trust", DATA, "vaf/core/trust.py", "the tool grants per account"),
    _s("config", "trust.json*", DATA, "vaf/core/trust.py", "legacy"),
    _s("config", "processes", LOG, "vaf/core/processes.py"),
    # ── projects: Documents/VAF_Projects ─────────────────────────────────────────
    _s("projects", "*", DATA, "vaf/core/session.py", "the people's own working folders"),
)

EXTERNAL = (
    ("postgres", "accounts, sessions and memories (Docker volume vaf_memory_pgdata)"),
    ("environment volumes", "vaf-env-vol-* of the project sandbox environments"),
    ("browser profiles", "vaf-browser-profile-* of the personal browsers"),
    ("os keyring", "credentials kept in the operating system's keyring"),
)


def roots() -> Dict[str, Path]:
    """Each root by name, resolved for this machine."""
    from vaf.core.config import Config
    from vaf.core.platform import Platform
    return {
        "app": Path(Config.APP_DIR),
        "vaf": Platform.vaf_dir(),
        "data": Platform.data_dir(),
        "config": Platform.config_dir(),
        "projects": projects_dir(),
    }


def projects_dir() -> Path:
    from vaf.core.platform import Platform
    return Platform.documents_dir() / "VAF_Projects"


def _rows_for(root_path: Path, named: Dict[str, Path]) -> List[Store]:
    """Every row whose root resolves to this directory: on a normal install ``app`` and
    ``vaf`` are one folder and both halves of the list describe it."""
    same = {name for name, path in named.items() if _same(path, root_path)}
    return [s for s in STORES if s.root in same]


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def classify(rel: str, rows: List[Store]) -> Optional[Store]:
    """The most specific row for a path relative to its root ("a/b/c"): a nested pattern
    wins over its parent's row."""
    best: Optional[Store] = None
    for row in rows:
        depth = row.pattern.count("/")
        parts = rel.split("/")
        if len(parts) < depth + 1:
            continue
        if fnmatch.fnmatchcase("/".join(parts[:depth + 1]), row.pattern):
            if best is None or depth > best.pattern.count("/"):
                best = row
    return best


def distinct_roots() -> List[Tuple[Path, List[Store]]]:
    """The directories to look at, each once, with every row that describes it."""
    named = roots()
    seen: List[Path] = []
    out: List[Tuple[Path, List[Store]]] = []
    for path in named.values():
        if any(_same(path, p) for p in seen):
            continue
        seen.append(path)
        out.append((path, _rows_for(path, named)))
    return out


def walk(include_projects: bool = True) -> Iterator[Tuple[Path, Path, Optional[Store]]]:
    """(root, file, row) for every file under every root; row None = not in the list.

    Descends into a directory unless its row says it is not backed up AND no more specific
    row below it is (``logs`` is a LOG, ``logs/security_*`` is AUDIT). A directory that
    cannot be read raises (OSError): skipped, it would be missing from a backup without a
    word, while an unreadable FILE already fails the backup."""
    proj = projects_dir()
    for root, rows in distinct_roots():
        if not include_projects and _same(root, proj):
            continue
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            for entry in sorted(current.iterdir()):
                rel = entry.relative_to(root).as_posix()
                row = classify(rel, rows)
                if entry.is_symlink():
                    yield root, entry, row
                    continue
                if entry.is_dir():
                    # An unknown directory is reported as it is, not entered: what is in
                    # it is not VAF's to judge. A known one is entered when it is backed
                    # up, or when a more specific row below it is (``logs/security_*``).
                    deeper = any(r.pattern.count("/") > rel.count("/") and
                                 fnmatch.fnmatchcase(rel, "/".join(r.pattern.split("/")[:rel.count("/") + 1]))
                                 for r in rows)
                    if row is not None and (row.kind in BACKED_UP or deeper):
                        stack.append(entry)
                    else:
                        yield root, entry, row
                    continue
                yield root, entry, row


def backed_up(row: Optional[Store]) -> bool:
    return row is not None and row.kind in BACKED_UP
