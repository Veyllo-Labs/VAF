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
- ``INSTALL``: part of the installation that an installer put into a root (the portable
  Node.js). Not backed up, and a restore leaves it where it is: the backup's machine may not
  have had it, and without it the web UI does not start.
- ``NEVER``: must not leave the machine in a backup (the recovery note: a plaintext key).

A backup takes ``KEY``, ``DATA``, ``RECREATABLE`` and ``AUDIT`` (``BACKED_UP``). An entry
on disk that this list does not know is reported, never silently taken or dropped: the
owner sees it, and a test fails when the code joins a new literal name onto a root
without a row here (tests/test_data_stores.py).

Two more columns say how a store is protected at rest, and the at-rest sweep
(vaf/core/at_rest_migration.py) reads them on every start:

- ``sealed``: the files its writer puts through the encrypted seam (vaf/core/data_files.py),
  as globs relative to the root. The sweep encrypts what is still plaintext there and turns
  plaintext reads off once none is left; the key probe in data_files looks there for
  existing ciphertext before it would mint a key. These used to be two hand lists that had
  drifted apart: archived chats, word suggestions and the rooms were in neither or one.
- ``harden``: made owner-only on every start (directories 0700, files 0600). For the stores
  whose writers do not read through the encrypted seam (plain ``json.load``/``json.dump``),
  so encrypting them would lock their own loaders out; modes are the half that applies to
  them today. The list once hardened a folder no code writes while the real one sat open.

A last column says whether the agent's file tools may READ a store: ``agent_readable``.
Every state root is refused to them for reads as well as writes (`is_vaf_state` with
``mode="read"``); the rows set here are the exceptions, the only folders a tool hands the
model a path into (see `is_vaf_state`).

What is not a file: Postgres, the Docker volumes of the sandbox environments and the
personal browsers, and OS keyring entries. ``EXTERNAL`` names them; the backup engine
reads each with its own tool.

NAMED BOUNDARY: the working memory under the launch directory (``<cwd>/.vaf/main``) is
per-session scratch state that the garbage collector removes by age; it is not a root
here. Neither is the desktop window's browser storage (``.vaf_webview`` beside the code,
vaf/core/desktop_window.py): the window's size and the web login, which signing in again
recreates. The list itself is not on the facade: the stores are VAF's own; an embedder's
application keeps its own, and no embedder has asked for a shared registry. One question
over it is: `is_vaf_state` (``vaf.is_vaf_state``), which every surface that writes a file on
someone else's behalf asks before it writes, VAF's file tools first among them. A
third-party write tool needs the same answer and had nothing to ask.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

KEY = "key"
DATA = "data"
RECREATABLE = "recreatable"
AUDIT = "audit"
DERIVED = "derived"
RUNTIME = "runtime"
LOG = "log"
INSTALL = "install"
NEVER = "never"

BACKED_UP = frozenset({KEY, DATA, RECREATABLE, AUDIT})
KINDS = frozenset({KEY, DATA, RECREATABLE, AUDIT, DERIVED, RUNTIME, LOG, INSTALL, NEVER})


@dataclass(frozen=True)
class Store:
    root: str
    pattern: str          # fnmatch on the entry's path relative to the root, "/"-separated
    kind: str
    owner: str
    note: str = ""
    sealed: Tuple[str, ...] = ()   # globs relative to the root, written through data_files
    harden: bool = False           # owner-only modes on every start
    agent_readable: bool = False   # the file tools may read it (is_vaf_state mode="read")


def _s(root, pattern, kind, owner, note="", *, sealed=(), harden=False, agent_readable=False):
    return Store(root, pattern, kind, owner, note, tuple(sealed), harden, agent_readable)


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
    _s("app", "user_profile_cache", DERIVED, "vaf/memory/rag.py", harden=True),
    _s("app", "tag_links.json", DATA, "vaf/memory/tag_links.py"),
    _s("app", "compaction_state.json", DERIVED, "vaf/memory/rag.py"),
    _s("app", "maintenance", RUNTIME, "vaf/memory/reembed.py",
       "the re-embed status and lock; the journal of an unfinished context state repair "
       "(vaf/core/at_rest_migration.py)"),
    _s("app", "firewalld_lan.json", DERIVED, "vaf/network/firewall.py"),
    _s("app", "ufw_sources.json", DERIVED, "vaf/network/firewall.py"),
    _s("app", "ssl", RECREATABLE, "vaf/network/ssl_utils.py", "browsers trust this authority"),
    _s("app", "tmp", DERIVED, "vaf/tools/search.py"),
    # ── vaf: Platform.vaf_dir() ──────────────────────────────────────────────────
    _s("vaf", "sessions", DATA, "vaf/core/session.py", "the chats, encrypted",
       sealed=("sessions/*.json", "sessions/archive/*/*.json")),
    _s("vaf", "context_archive", DERIVED, "vaf/core/context.py", "removed after 14 days",
       sealed=("context_archive/context_*.json",)),
    _s("vaf", "handoff_bundles", RUNTIME, "vaf/core/handoff_bundle.py", "kept 7 days",
       sealed=("handoff_bundles/*/*.json",)),
    _s("vaf", "subagent_queue", RUNTIME, "vaf/core/subagent_ipc.py",
       sealed=("subagent_queue/*.json", "subagent_queue/task_payloads/*.txt")),
    _s("vaf", "automations", DATA, "vaf/core/automation.py", harden=True),
    _s("vaf", "last_automation_run.json", RUNTIME, "vaf/core/automation.py"),
    _s("vaf", "automation_planner", DATA, "vaf/core/automation_planner.py", harden=True),
    _s("vaf", "reminders", DATA, "vaf/core/reminders.py", harden=True),
    _s("vaf", "thinking_requests", DATA, "vaf/core/thinking_requests.py"),
    _s("vaf", "thinking_suggestions", DATA, "vaf/core/thinking_suggestions.py"),
    _s("vaf", "thinking_mode_logs", LOG, "vaf/core/thinking_mode.py"),
    _s("vaf", "thinking_notes.db", DERIVED, "vaf/core/thinking_notes.py"),
    _s("vaf", "security", DATA, "vaf/core/threat_db.py", "the known-bad hash list"),
    _s("vaf", "whare_wananga", DATA, "vaf/whare_wananga/store.py", "learned, expensive to relearn"),
    _s("vaf", "whare_wananga_retrain.json", DATA, "vaf/whare_wananga/retrain.py"),
    _s("vaf", "a2a", DATA, "vaf/core/a2a/store.py", "rooms, pinned peers, seats",
       sealed=tuple(f"a2a/rooms/*/{p}" for p in (
           "room.json", "log/*/*.json", "members/*.json", "cursors/*.json", "tickets/*.json",
           "tickets/spent/*.json"))),
    _s("vaf", "a2a/session", RUNTIME, "vaf/core/a2a/session.py"),
    _s("vaf", "ssh", DATA, "vaf/core/ssh.py", "each account's key and confirmed servers"),
    _s("vaf", "ftp", DATA, "vaf/core/ftp.py"),
    _s("vaf", "environments", DATA, "vaf/core/environments.py", "ties volumes to their owners"),
    _s("vaf", "app_link", DATA, "vaf/core/app_link/devices.py",
       "the paired Veyllo App devices and their outboxes", sealed=("app_link/*.json",)),
    _s("vaf", "app_link_spool", RUNTIME, "vaf/core/app_link/spool.py",
       "files waiting for an absent Veyllo App, kept 7 days", sealed=("app_link_spool/*",)),
    # [!.]: the agent lane's dot-prefixed .run-*.tmp.json staging files (pathlib's * DOES
    # match a leading dot) are short-lived plaintext by design and swept by their own lane;
    # encrypting one mid-run would crash the browser_use reader holding it.
    _s("vaf", "browser_sessions", RECREATABLE, "vaf/core/browser_interactive.py",
       sealed=("browser_sessions/*/[!.]*.json",)),
    _s("vaf", "speaker_profiles", DATA, "vaf/core/speaker_id.py"),
    _s("vaf", "speaker_confirm", RUNTIME, "vaf/core/speaker_confirm.py"),
    _s("vaf", "skills", DATA, "vaf/core/skills_registry.py", agent_readable=True),
    _s("vaf", "workflows", DATA, "vaf/workflows/templates.py", agent_readable=True),
    _s("vaf", "autosuggest.json", DERIVED, "vaf/cli/autosuggest.py",
       sealed=("autosuggest.json",)),
    _s("vaf", "autosuggest", DERIVED, "vaf/cli/autosuggest.py", sealed=("autosuggest/*.json",)),
    _s("vaf", "history", DATA, "vaf/cli/history.py", "the terminal's prompt history"),
    _s("vaf", "output", DATA, "vaf/core/platform.py"),
    # A project's own memory, here when the project is the home folder (vaf/core/persistence.py).
    _s("vaf", "tasks.json", DATA, "vaf/core/persistence.py"),
    _s("vaf", "codex.md", DATA, "vaf/core/persistence.py"),
    _s("vaf", "memory.md", DATA, "vaf/core/persistence.py"),
    _s("vaf", "snapshots", DATA, "vaf/core/snapshot.py"),
    _s("vaf", "icons", DERIVED, "vaf/tray.py"),
    _s("vaf", "node", INSTALL, "install.sh",
       "the portable Node.js when the system has none (vaf/core/node_runtime.py)"),
    _s("vaf", "logs", LOG, "vaf/main.py", harden=True),
    _s("vaf", "main", RUNTIME, "vaf/core/main_persistence.py", "working memory of a launch from HOME"),
    _s("vaf", "backup_schedule.json", RUNTIME, "vaf/core/backup_schedule.py",
       "when the scheduled backup last ran, and its last error"),
    _s("vaf", "update_cache.json", RUNTIME, "vaf/core/update_check.py"),
    _s("vaf", "last_update.json", RUNTIME, "vaf/core/update_check.py"),
    _s("vaf", "update_result.json", RUNTIME, "vaf/core/update_check.py"),
    _s("vaf", "instance.json", RUNTIME, "vaf/core/instance.py"),
    _s("vaf", "active_sessions.txt", RUNTIME, "vaf/core/agent.py"),
    _s("vaf", "shutdown.lock", RUNTIME, "vaf/core/agent.py"),
    _s("vaf", "locks", RUNTIME, "vaf/core/lock_manager.py"),
    _s("vaf", "service.pid", RUNTIME, "vaf/cli/cmd/service.py"),
    _s("vaf", "server.pid", RUNTIME, "vaf/core/backend.py"),
    _s("vaf", "vllm.pid", RUNTIME, "vaf/core/vllm_server.py"),
    _s("vaf", "web_port", RUNTIME, "vaf/core/frontend_manager.py"),
    _s("vaf", "sandbox_env_image.lock", RUNTIME, "vaf/core/environment_image.py"),
    _s("vaf", "browser_jar", RUNTIME, "vaf/core/browser_jar.py",
       "whose state the shared browser holds and who holds it, bound to the container's start"),
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
    _s("data", "workspaces", DATA, "vaf/core/thinking_workspace.py", harden=True),
    _s("data", "spend", DATA, "vaf/core/cost.py"),
    _s("data", "fx_rates.json", DERIVED, "vaf/core/cost.py"),
    _s("data", "notifications", RUNTIME, "vaf/core/user_notifications.py"),
    _s("data", "thinking_notes.db*", DERIVED, "vaf/core/thinking_notes.py"),
    _s("data", "thinking_*.json", RUNTIME, "vaf/core/thinking_mode.py"),
    _s("data", "last_interaction.json", RUNTIME, "vaf/core/last_interaction.py"),
    _s("data", "messaging_endpoints.json", DATA, "vaf/core/messaging_connections.py"),
    _s("data", "mcp_servers.json", DATA, "vaf/core/mcp_registry.py"),
    _s("data", "custom_tools", DATA, "vaf/core/custom_tools_registry.py",
       agent_readable=True),
    _s("data", "cloud_sync_*.db*", DERIVED, "vaf/cloud/sync_worker.py"),
    _s("data", "github_activity.json", DERIVED, "vaf/github/activity.py"),
    _s("data", "logs", LOG, "vaf/core/log_helper.py", harden=True),
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


# The working memory of a launch (vaf/core/main_persistence.py): under the launch directory,
# so not a root (the named boundary above), and written through the encrypted seam all the same.
LAUNCH_MEMORY_GLOBS = ("*.json", "*.md", "sessions/*/*", "conversations/*/*")


def sealed_trees(*, with_launch_dir: bool = False) -> Dict[str, Tuple[Path, Tuple[str, ...]]]:
    """Every store written through the encrypted seam: its entry name -> (its root, the
    globs relative to that root). The at-rest sweep and the key probe read this one list;
    ``with_launch_dir`` adds the working memory under the current directory as
    ``main_context``, which the sweep encrypts and the key probe leaves out (its folder may
    hold another installation's ciphertext; vaf/core/data_files.py says why)."""
    named = roots()
    trees = {s.pattern: (named[s.root], s.sealed) for s in STORES if s.sealed}
    if with_launch_dir:
        trees["main_context"] = (Path.cwd() / ".vaf" / "main", LAUNCH_MEMORY_GLOBS)
    return trees


def hardened_dirs() -> Dict[str, Path]:
    """Every store made owner-only on every start: "<root>/<entry>" -> its directory."""
    named = roots()
    return {f"{s.root}/{s.pattern}": named[s.root] / s.pattern for s in STORES if s.harden}


def projects_dir() -> Path:
    from vaf.core.platform import Platform
    return Platform.documents_dir() / "VAF_Projects"


# The roots whose files VAF itself writes and later obeys: every root except ``projects``,
# which holds the people's own folders.
STATE_ROOTS = ("app", "vaf", "data", "config")

# The path module the state check spells paths with. A seam, so the Windows spelling rules
# can be proven from another host (tests/test_at_rest_cross_platform.py hands in ntpath).
_pathmod = os.path


def _norm_parts(spelled: str) -> Tuple[str, ...]:
    """A path as comparable components, the same way on every host: separators unified,
    every component case-folded, an NTFS stream suffix (``name:stream``, ``name::$DATA``)
    cut, trailing dots and spaces stripped, the Windows device prefixes (``//?/`` and
    ``//./`` once the separators are unified) dropped. Lexical on purpose: macOS APFS and NTFS are case-insensitive by default, and
    Windows opens ``vaf.`` and ``vaf::$INDEX_ALLOCATION`` as the folder ``vaf``, so a
    comparison that only matched the spelling a root was created with would miss the same
    folder written another way. On a case-sensitive host it refuses a differently cased
    sibling as well, which only ever errs towards refusing."""
    norm = spelled.replace("\\", "/")
    if norm.startswith(("//?/", "//./")):
        norm = norm[4:]
        if norm[:4].casefold() == "unc/":
            norm = "//" + norm[4:]
    parts = []
    for i, part in enumerate(norm.split("/")):
        drive = i == 0 and len(part) == 2 and part[1] == ":" and part[0].isalpha()
        if not drive:
            part = part.split(":", 1)[0]
        part = part.rstrip(". ").casefold()
        if part:
            parts.append(part)
    return tuple(parts)


def _spellings(path: Any) -> List[Tuple[str, ...]]:
    """The path as given (made absolute) and as resolved (links followed, non-strict). Both,
    because a root reached through a symlink (a home on another disk, macOS /var ->
    /private/var) is spelled one way by the settings that name it and the other way by a
    path the links were followed for."""
    raw = _pathmod.expanduser(os.fspath(path))
    out: List[Tuple[str, ...]] = []
    for spelled in (_pathmod.abspath(raw), _pathmod.realpath(raw)):
        parts = _norm_parts(spelled)
        if parts not in out:
            out.append(parts)
    return out


def _state_root_spellings() -> List[Tuple[str, ...]]:
    named = roots()
    out: List[Tuple[str, ...]] = []
    for name in STATE_ROOTS:
        for parts in _spellings(named[name]):
            if parts and parts not in out:
                out.append(parts)
    return out


def is_vaf_state(path: Any, *, mode: str = "write") -> bool:
    """Whether ``path`` is one of VAF's own state roots or lies inside one: ``~/.vaf`` (or
    ``$VAF_CONFIG_DIR`` in Docker mode), ``Platform.data_dir()`` and ``Platform.config_dir()``,
    taken from `roots()` at call time. ``mode="write"`` (the default) asks whether a write
    there must be refused, ``mode="read"`` whether a read must be; any other mode answers
    True.

    Those folders hold what VAF executes or obeys later: the custom tools, whose Python runs
    at every agent load; the MCP server list, whose commands VAF starts; the standing tool
    grants; the messaging endpoints; the skills and workflows, matched against later
    messages. Each is written by its own registry, which checks what it saves, and never by
    a generic file write. So a surface that writes a file on a model's, a request's or a
    peer's behalf asks this first and refuses on True.

    The read half: the same roots also hold every account's messages, mail and contacts,
    the key files, the MCP commands and the tool grants, and a credential read out through a
    chat prompt or sent on as an attachment outlives anything this instance can revoke. The
    owner reads settings through VAF's own screens, never through a file tool. So a surface
    that reads a file on a model's behalf asks ``mode="read"`` and refuses on True, the
    machine owner included. The exceptions are the rows marked ``agent_readable``, each one
    a folder a tool hands the model a path into: ``skills`` (use_skill names a skill's
    bundled files for read_file), ``workflows`` (the agent-created workflows, the same
    category) and the data directory's ``custom_tools`` (create_agent_tool's edit replaces a
    tool's whole source, so the model reads the source it edits). Those are compared
    case-exact (`is_agent_readable_state`), so another spelling of them errs towards
    refusing.

    Compared component by component, never as a string prefix, in every spelling: as given
    and resolved, for the path and for each root, normalised by `_norm_parts`. Fail-closed:
    any error answers True.

    NAMED BOUNDARIES. This answers for a path, and a path can change after it is asked.
    Code in a sandbox environment (``sandbox_exec``, the coder's jailed ``bash``) creates
    links in its mounted folder, which the host sees as ordinary entries: a link at the
    name a writer is about to open, or a folder swapped for a link between this check and
    the open. The mitigation is `vaf.core.path_jail.open_for_write`, which opens the folder
    first, refuses a link as the last component and asks this question again on the real
    path of the folder it opened; every writer of a name a model chose goes through it. Two
    gaps stay. On Windows there is no open relative to a folder handle and no
    ``O_NOFOLLOW``, so a link or junction swapped in between the check and the open is
    followed for that one open (a file created in VAF's state that way is removed again).
    And ``write_file``/``edit_file`` write a temporary file beside the target and rename it
    into place, which resolves the folder again at the rename; a folder swapped for a link
    in that window carries the rename with it. A hard link is not seen at all, but a sandbox
    environment cannot make one to a file outside its mount, and a mount that holds VAF's
    state is refused (`holds_vaf_state`). Windows 8.3 short names are expanded by the resolved
    half (``realpath`` asks for the final path); a short name of a folder that does not exist
    yet has nothing to expand and stays unseen. The web UI's own save routes let a signed-in
    admin save into the data directory on purpose (``_allowed_file_path`` in web_server.py):
    a person's authenticated save, not a lane a model drives, and not decided here. The
    same routes serve the data directory to that admin for reading, for the same reason."""
    try:
        if mode not in ("write", "read"):
            return True
        mine = _spellings(path)
        inside = False
        for root in _state_root_spellings():
            for parts in mine:
                if parts[:len(root)] == root:
                    inside = True
        if not inside:
            return False
        if mode == "read" and is_agent_readable_state(path):
            return False
        return True
    except Exception:
        return True


def _exact_parts(spelled: str) -> Tuple[str, ...]:
    """Components with the separators unified and nothing else changed: no case-folding, no
    stream or trailing-dot strip. The readable-row comparison is deliberately stricter than
    `_norm_parts`, so a spelling the state check folds onto a root is still not let through
    as readable."""
    return tuple(c for c in spelled.replace("\\", "/").split("/") if c)


def is_agent_readable_state(path: Any) -> bool:
    """Whether ``path`` equals or lies inside the folder of a row marked ``agent_readable``
    (``roots()[row.root] / row.pattern``). Both spellings, as given and resolved, of each row
    folder, compared case-exact (`_exact_parts`). Of the path's two spellings, as given and
    resolved, every one that lies inside a state root (compared like `is_vaf_state`) must lie
    in one of those folders, and at least one must lie inside a state root. A link inside a
    readable folder that points at a key file is readable by its given spelling only, and its
    target is state that is not, so it is refused. A skill folder linked to a folder outside
    VAF (``~/.vaf/skills/foo -> /ext/foo``) stays readable: its resolved spelling is no state
    at all, and a read of it is judged by the caller's own rules for that path. Any error
    answers False, which makes `is_vaf_state` refuse the read."""
    try:
        named = roots()
        raw = _pathmod.expanduser(os.fspath(path))
        state = _state_root_spellings()
        mine = set()
        for spelled in (_pathmod.abspath(raw), _pathmod.realpath(raw)):
            folded = _norm_parts(spelled)
            if any(folded[:len(root)] == root for root in state):
                mine.add(_exact_parts(spelled))
        wants = []
        for row in STORES:
            if not row.agent_readable:
                continue
            folder = os.fspath(named[row.root] / row.pattern)
            for spelled in (_pathmod.abspath(folder), _pathmod.realpath(folder)):
                want = _exact_parts(spelled)
                if want:
                    wants.append(want)
        return bool(mine) and all(
            any(parts[:len(want)] == want for want in wants) for parts in mine)
    except Exception:
        return False


def holds_vaf_state(path: Any) -> bool:
    """Whether a state root (see `is_vaf_state`) lies strictly inside ``path``: a folder
    that contains VAF's own data, such as the parent of the data directory. A project, a
    mount or a copy target there would reach the state below it. Fail-closed: any error
    answers True."""
    try:
        mine = _spellings(path)
        for root in _state_root_spellings():
            for parts in mine:
                if len(parts) < len(root) and root[:len(parts)] == parts:
                    return True
        return False
    except Exception:
        return True


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
