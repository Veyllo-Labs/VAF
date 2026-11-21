# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Long-lived WhatsApp bridge: one Node (Baileys) subprocess per user with linked auth.
Receives messages via Node stdout, enqueues tasks, sends replies via stdin to Node.
User isolation: each user's credentials and session are strictly separate.
Voice messages from WhatsApp are downloaded by Node, transcribed via Whisper STT, and passed as text.

Roles. The linked account is the AGENT's own number: the agent writes to contacts and
third parties from it, and nobody chats with the agent from that phone (its "message
yourself" chat is dropped). Who may write IN is decided per message, by the one rule both
directions ask (`channel_ingress_policy.evaluate_ingress`): the registered main-user number
(whitelist entry: full chat as the owner), a contact the owner ALLOWED (Front Office, on
every channel they have), and, while this channel's Inbound is open, everybody the owner has
not decided about (Front Office, enrolled as a contact when they write). A contact the owner
blocked is answered nowhere, and everyone else is rejected. A message the agent sent opens
nothing: the 72 hour reply window that used to admit its recipient is gone.

Starting and stopping. The whole bridge (`start_bridge`, `stop_bridge`, `restart_bridge`) is
an admin's: it starts or ends every account's process at once. Each account's own process is
that account's (`start_account`, `stop_account`, `restart_account`): it starts into a running
bridge, or brings the bridge up with that process alone, and stops without touching anybody
else's. Measured before: an ordinary account's Stop and Restart ended every account's process,
and its Start answered "already running" while the bridge ran for somebody else, so its own
process never came up. `start_missing_accounts` is an admin's start into a running bridge: it
starts every switched-on account that has no process yet, a newly linked one included. A
session that expired stops its own account's process only and changes no switch.

Each account's own switch decides its own process, and nobody else's: the global
`whatsapp_config.enabled` is the local admin's own switch, every other account has one under
`connection_enabled_by_scope` (`channel_enabled_for_scope` reads them, `Config.
set_own_channel_switch` writes them, `PUT /api/whatsapp/switch` is an account's own toggle).
The spawn gate asks the switch under `_spawn_lock` (`_account_may_start`), so a start that
races a switch-off spawns nothing that outlives it. Measured before: the global switch was
also the master gate of every start, so an ordinary account could not run while the local
admin's WhatsApp was off, and an admin's switch-off (the local admin's or a second admin's)
ended every account's process. The reconnect worker reconnects accounts, not the bridge
(`Reconnector`): it restarts a registered account whose connection stays down, leaves an
account its owner stopped alone, and brings a crashed bridge back a bounded number of times.

The account's end. A deactivated or deleted account's process stops and its pending start is
cancelled (`stop_scope`, called by this module's revocation listener only when the account is
revoked or no longer stands; a demotion or a narrowed tool list stops nothing). Deleting an
account also removes its login and the name's cached chat list, LID mappings, names, own
number and connection state (`forget_account_login`, called by the delete route), once no
Node runs on the folder any more. Measured before: a revoked account's process stayed
registered and alive, and a new account created under a deleted account's name ran its
login. NAMED BOUNDARY: the revocation does not tell a deletion from a deactivation, so the
delete route calls `forget_account_login` itself; one consumer (this login) needs the
difference, and a second store that must forget at deletion makes it a flag on the listener.
Six such stores are measured and filed (the account workspace, contacts, mail sync, channel
messages, the Front Office profile and the cloud folders, all keyed by the name under
`users/<name>/` and kept at deletion): building their removal is the trigger for that flag.

One Node per login folder. `spawn_node_bridge`, the one place Node starts (the account's
process and the QR link alike), refuses a second live Node on a folder one already runs on,
reached by the same path or through a link (`LoginInUse`, `login_in_use`): two Baileys sockets
on one login evict each other. Measured before: two spawns on one folder ran two Nodes.
"""
import hashlib
import json
import logging
import os
import time
import uuid
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vaf.core.config import Config, messenger_lane_identity
from vaf.core.contacts_store import whatsapp_send_jid
from vaf.core.channel_ingress_policy import evaluate_ingress, should_log_unauthorized
from vaf.core.messaging_connections import front_office_open
from vaf.core.messaging_connections import (
    channel_enabled_for_scope, save_whatsapp_chat_jid, whatsapp_session_id,
)
from vaf.core.platform import Platform
from vaf.core.task_queue import TaskQueue
from vaf.core.whatsapp_auth import get_linked_phone, get_whatsapp_auth_dir, linked_usernames
from vaf.core.whatsapp_reply import set_whatsapp_reply_callback
from vaf.core.whatsapp_send import chunk_whatsapp_text

logger = logging.getLogger("vaf.api.whatsapp_bridge")

_bridge_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_chat_sync_thread: Optional[threading.Thread] = None
# The stop of the current (or last) bridge run. `start_bridge` makes a new one per run and never
# clears an old one; the run's threads are handed their own and watch only that.
_bridge_stop = threading.Event()
_processes: Dict[str, subprocess.Popen] = {}
_process_lock = threading.Lock()
# Serialises every spawn and every stop of a Node process and the bridge's shutdown, so a
# check-then-spawn cannot run twice for one account and no spawn slips in between the
# shutdown's terminate and its clear (which would leave a Node nobody stops). Lock order:
# `_spawn_lock` outer, `_process_lock` inner; `_process_for` takes only the inner one.
_spawn_lock = threading.Lock()
# Whether the running bridge still takes new processes: set when the bridge starts, cleared
# under `_spawn_lock` by the bridge thread as it ends (its shutdown, or its exit with nobody
# to run), so a spawn that raced the end is refused instead of outliving the bridge.
_spawns_open = False
# Serialises the bridge's own start and restart: the routes run them off the event loop, and
# two starts at once would each spawn a bridge thread and a sender loop.
_lifecycle_lock = threading.RLock()
# What an account's own start answers when it cannot start (the routes turn START_FAILED and
# STOP_FAILED into 500, every other refusal into 400).
NOT_STARTABLE = ("WhatsApp is not linked or not switched on for your account. "
                 "Link it and turn it on in Settings, Connections.")
BRIDGE_STOPPING = "WhatsApp is stopping; try again in a few seconds."
START_FAILED = ("Failed to start WhatsApp. Ensure Node.js is installed and npm install was run "
                "in vaf/whatsapp_node.")
START_CANCELLED = "WhatsApp was stopped for this account before it had started."
STOP_FAILED = "WhatsApp did not stop for this account; try again."
PREVIOUS_NOT_ENDED = "The previous WhatsApp connection of this account has not ended yet. Try again shortly."
SHUTDOWN_OVERRAN = "The old WhatsApp process did not stop in time; try again."
ACCOUNT_STARTED = "WhatsApp started for this account."
ACCOUNT_ALREADY_RUNNING = "WhatsApp already runs for this account."
# What a start of the whole bridge answers when no account is linked and switched on: nothing
# to run is no failure of the installation (the routes answer it with 400, not 500).
NOBODY_TO_RUN = "No account has WhatsApp linked and switched on. Link WhatsApp and switch it on first."


class _PendingStart:
    """An account start that brought the bridge up and waits for the bridge thread to spawn
    its process. The bridge thread answers it once (`resolve`); a stop of the same account
    before the spawn cancels it, so the bridge thread skips that spawn."""

    def __init__(self, user_scope_id: str) -> None:
        self.user_scope_id = str(user_scope_id or "")
        self.cancelled = False
        self.ok = False
        self.message = START_FAILED
        self.done = threading.Event()

    def resolve(self, ok: bool, message: str) -> None:
        if not self.done.is_set():
            self.ok, self.message = bool(ok), message
            self.done.set()


# username -> the pending start of that account, guarded by `_spawn_lock`.
_pending_starts: Dict[str, _PendingStart] = {}


class _RunOutcome:
    """What a bridge run's spawn phase decided: the accounts it skipped because their previous
    process has not ended (or belongs to another scope), and `settled` once that phase is over
    (or the run ended without one). An admin's restart reports these decisions, never a guess
    taken before the start."""

    def __init__(self) -> None:
        self.skipped: List[str] = []
        self.settled = threading.Event()
        # The bridge thread raised: the one way a run ends that the reconnect worker brings
        # back (`Reconnector`). A stop on request or a run with nobody to run stays down.
        self.failed = False
        # (name, scope key) of every account this run was started for or spawned, minus the ones
        # stopped on request since (`stop_account`, an expired session): whom its own spawn
        # phase starts, whom a crash recovery brings back, and whose automatic reconnect may go
        # on. Seeded when the run is published and only removed from afterwards, apart from an
        # explicit start into the running bridge (`_spawn_account`). Guarded by `_spawn_lock`.
        self.members: set = set()
        # `_whole_stops` when the run was published: a whole-bridge Stop since then cancels any
        # recovery of this run (`_start_bridge(recover=...)`).
        self.started_after = 0


# The current (or last) run's outcome, replaced by `start_bridge` with each run.
_run_outcome = _RunOutcome()
# How many whole-bridge Stops there were (`stop_bridge`), and the leaf lock that makes counting a
# Stop and setting the current run's stop one step, and checking that count and publishing a
# new run another: a crash recovery's start and an admin's Stop then cannot pass each other (the
# Stop either cancels the start or ends the run it published). Nothing is taken under it.
_whole_stops = 0
_marks_lock = threading.Lock()
# How long an admin's restart waits for the new run's spawn decisions before it answers
# without naming any skipped account.
RESTART_REPORT_WAIT = 15.0


def _retire(proc: Any) -> None:
    """Mark a process VAF is ending (an account's stop or restart, the bridge's shutdown, a
    replaced registration) before the terminate: from here on nothing it writes is acted on
    (its readers drop the next line they see) and no lane writes to it (`_process_for`). One
    that survives the kill stays registered, so nothing starts a second Node on its login, and
    a start of its account answers PREVIOUS_NOT_ENDED until it has ended. The mark lives on
    the process object, so a later process under the same name can neither inherit it nor
    clear it (an `id()` set could, once the old object is freed and its id reused).

    Named boundary: a line whose dispatch began before the mark is not called back; the check
    cannot be atomic with a dispatch, which may itself stop the account. The one event that
    ends a process (`qr`) closes that gap itself: it acts only while its process is still the
    account's registration (`_is_registration`). Never raises: one
    object that refuses the attribute must not abort the end of every other process."""
    try:
        proc._vaf_retired = True
    except Exception:
        logger.warning("WhatsApp: could not mark a process %r as ended", proc, exc_info=True)


def _is_retired(proc: Any) -> bool:
    # `is True`: a MagicMock double answers any attribute with a truthy Mock.
    return proc is not None and getattr(proc, "_vaf_retired", None) is True
# Terminations a bridge's shutdown still owes, so a restart's deadline counts processes the
# shutdown has already taken out of `_processes`. Guarded by `_process_lock`.
_terminating = 0
# How long an account start that brought the bridge up waits for its own spawn.
COLD_START_TIMEOUT = 60.0
# What a send answers when the account has no Node process of its own (see _process_for).
NO_PROCESS_FOR_ACCOUNT = "No WhatsApp process runs for this account. Link WhatsApp in Settings, Connections."
# What a send answers when it carries no scope and names somebody other than the local admin
# (`config.messenger_lane_identity`): no account can be told, so nothing is sent.
NO_ACCOUNT_FOR_REQUEST = "WhatsApp: Cannot send - a request without an account acts only for the local admin."
_outgoing_queue: Optional[queue.Queue] = None
# username -> user_scope_id of every running process, so the sender loop stores outbound
# rows in the same per-scope store the inbound lane writes to.
_scope_by_user: Dict[str, str] = {}
# username -> E.164 of the linked account (the agent's own number), refreshed on `connected`.
_self_phone: Dict[str, str] = {}
_chat_lists: Dict[str, List[Dict[str, Any]]] = {}
_chat_list_events: Dict[str, threading.Event] = {}
_chat_lists_lock = threading.Lock()
# The scope of the process that wrote each name's chat list, LID mappings and contact names
# (guarded by that cache's lock). The caches are keyed by the process's NAME, and a name can
# pass to a new account: a cached answer goes only to the scope that wrote it (`_owned`).
_chat_lists_owner: Dict[str, str] = {}
_lid_mappings_owner: Dict[str, str] = {}
_contact_names_owner: Dict[str, str] = {}

# LID→E.164 from Node (Baileys lidMapping), so UI can show "this LID = this number" when WhatsApp resolved it
_lid_mappings: Dict[str, List[Dict[str, str]]] = {}
_lid_mappings_events: Dict[str, threading.Event] = {}
_lid_mappings_lock = threading.Lock()

# Named numbers from the Node's contact store (phone digits -> {name, source, e164}), the
# names WhatsApp shows; asked for like the LID mappings.
_contact_names: Dict[str, Dict[str, Dict[str, str]]] = {}
_contact_names_events: Dict[str, threading.Event] = {}
_contact_names_lock = threading.Lock()

# Connection check: ping/pong from Node to verify socket is connected
_connection_status: Dict[str, bool] = {}
_connection_events: Dict[str, threading.Event] = {}
_connection_lock = threading.Lock()

# Reply with voice when user sent voice (like Telegram)
_voice_reply_pending: Dict[str, str] = {}  # "username|chat_jid" -> voice_lang
_voice_reply_lock = threading.Lock()

# Send confirmation: req_id -> queue that receives (success, error)
_pending_sends: Dict[str, queue.Queue] = {}
_pending_sends_lock = threading.Lock()
# Profile pictures: req_id -> queue that receives the Node's `avatar` event; one Node query
# at a time so a dashboard with a hundred rows does not fire a hundred profile lookups at once.
_avatar_results: Dict[str, queue.Queue] = {}
_avatar_results_lock = threading.Lock()
_avatar_query_lock = threading.Lock()
AVATAR_TTL_SECONDS = 24 * 3600
_external_pending_results: Dict[str, Path] = {}
_external_pending_results_lock = threading.Lock()

# Per-chat inbound debounce (like Telegram 5s rule but 7s for WhatsApp).
# Key: "username|from_jid" → accumulated state dict.
# When a new message arrives we reset the timer; when it fires we flush to TaskQueue.
_wa_pending: Dict[str, Dict[str, Any]] = {}
_wa_pending_lock = threading.Lock()
WA_DEBOUNCE_SECONDS = 7

# The reply window (`reply_window_hours`) lives with the other channel rules in
# messaging_connections, and it decides NOTHING about who may write in: the bridge's own copy
# of the rule (`conversation_open_until`, `_conversation_is_open`) is gone with the 72 hour
# door it served, and the one implementation left is `vaf.core.inbox.reply_window_until`,
# which the WhatsApp window reads to show how long a conversation the owner started counts as
# live. NAMED BOUNDARY: the setting is display-only now. Deleting it outright touches 93 sites
# in 26 files (the inbox rows, the inbox tool, the CLI, the store's overview, the settings
# card), so it is its own round; what must not happen is a new caller treating it as a door.


def _ipc_base_dir() -> Path:
    return Platform.data_dir() / "whatsapp_bridge_ipc"


def _ipc_requests_dir() -> Path:
    return _ipc_base_dir() / "requests"


def _ipc_results_dir() -> Path:
    return _ipc_base_dir() / "results"


def _ipc_state_path() -> Path:
    return _ipc_base_dir() / "bridge_state.json"


def _ensure_ipc_dirs() -> None:
    _ipc_requests_dir().mkdir(parents=True, exist_ok=True)
    _ipc_results_dir().mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """The IPC files (bridge state, requests, results) through the store writer: temp file,
    fsync, and a replace that retries a transient Windows sharing violation. A bare replace of
    `bridge_state.json` failed there whenever a process without the bridge was reading it, and
    `_write_bridge_state` swallows the error, so the file could keep saying `running: true`
    after the run ended. Created 0600: request files carry message text. The temp name
    (`.tmp-*.part`) matches neither `*.json` glob of a consumer."""
    from vaf.core.secure_store import atomic_write_bytes
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _write_bridge_state(running: bool) -> None:
    """What processes without the bridge (CLI, sub-agents) read before they queue a send.

    Write order: every write that can say True happens under `_spawn_lock` while spawns are
    open (`start_bridge`, `_spawn_account`, `stop_account`), and the end of a run (`_end_run`)
    writes False under that lock after it has closed them, so the end of a run is the file's
    last word. `stop_bridge`'s early write stays outside the lock; it only ever writes False.
    Measured before: with the start's True written after the bridge thread started, 39 of 200
    starts with nobody to run left the file at `running: true`, and every send from outside
    then waited its full timeout."""
    try:
        _ensure_ipc_dirs()
        with _process_lock:
            usernames = sorted(_processes.keys())
        _write_json_atomic(
            _ipc_state_path(),
            {
                "running": bool(running),
                "usernames": usernames,
                "updated_at": time.time(),
            },
        )
    except Exception:
        pass


def _read_bridge_state() -> Dict[str, Any]:
    try:
        path = _ipc_state_path()
        if not path.exists():
            return {"running": False, "usernames": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"running": False, "usernames": []}
        usernames = data.get("usernames") or []
        if not isinstance(usernames, list):
            usernames = []
        return {
            "running": bool(data.get("running")),
            "usernames": [str(u).strip() for u in usernames if str(u).strip()],
            "updated_at": float(data.get("updated_at") or 0.0),
        }
    except Exception:
        return {"running": False, "usernames": []}


def _write_external_send_result(response_path: Optional[Path], success: bool, error: Optional[str] = None) -> None:
    if response_path is None:
        return
    try:
        _write_json_atomic(
            response_path,
            {
                "success": bool(success),
                "error": str(error or ""),
                "updated_at": time.time(),
            },
        )
    except Exception:
        pass


def _register_external_response_path(req_id: Optional[str], response_path: Optional[Path]) -> None:
    if not req_id or response_path is None:
        return
    with _external_pending_results_lock:
        _external_pending_results[req_id] = response_path


def _pop_external_response_path(req_id: Optional[str]) -> Optional[Path]:
    if not req_id:
        return None
    with _external_pending_results_lock:
        return _external_pending_results.pop(req_id, None)


def _complete_send_request(
    req_id: Optional[str],
    success: bool,
    error: Optional[str] = None,
    *,
    response_path: Optional[Path] = None,
) -> None:
    external_response_path = response_path or _pop_external_response_path(req_id)
    if req_id:
        _deliver_send_result(req_id, success, error)
    _write_external_send_result(external_response_path, success, error)


def _claim_request_file(path: Path) -> Optional[Path]:
    """Take a request file for this consumer alone: rename it to `<name>.<uuid>.claimed` in the
    same folder (atomic on POSIX and NTFS) and return the new path, or None when it is not
    ours. `FileNotFoundError` means another consumer renamed it first, `PermissionError` that a
    Windows process still holds it. Neither `*.json` glob of a consumer matches the claimed
    name, nor the writer's `.tmp` name."""
    claimed = path.with_name(f"{path.name}.{uuid.uuid4().hex}.claimed")
    try:
        path.rename(claimed)
    except OSError:
        return None
    return claimed


def _dequeue_external_send_request(stop: Optional[threading.Event] = None) -> Optional[Tuple[Any, ...]]:
    """The oldest request file a process without the bridge wrote, as a queue item, or None.

    Claimed by a rename before it is read: two consumers that read a file before either
    unlinked it both sent it (measured: 410 of 600 files handed out twice by two threads).
    `stop` is the consumer's run: once it is set after the claim, the file is renamed back for
    the next run's loop, since a claim is exclusive and an ended loop would otherwise take a
    request meant for its successor with it. Checked after the rename, so a stop that was not
    set then means the run was still active when the file became ours (a stop is never
    cleared, and a new run starts only after the old one's stop).

    Named boundary: a consumer that dies between the claim and the unlink leaves a `.claimed`
    file that nobody delivers; its sender's wait then answers SEND_UNCONFIRMED, which is true,
    since nobody knows whether it left."""
    try:
        _ensure_ipc_dirs()
        for found in sorted(_ipc_requests_dir().glob("*.json")):
            path = _claim_request_file(found)
            if path is None:
                continue
            if stop is not None and stop.is_set():
                try:
                    path.rename(found)
                    return None
                except OSError:
                    # Not handed back: read below, and the ended loop answers it as stopping.
                    pass
            response_path: Optional[Path] = None
            req_id: Optional[str] = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                path.unlink(missing_ok=True)
                if not isinstance(data, dict):
                    continue
                req_id = str(data.get("req_id") or "").strip() or None
                response_path_str = str(data.get("response_path") or "").strip()
                response_path = Path(response_path_str) if response_path_str else None
                return (
                    str(data.get("username") or "").strip(),
                    str(data.get("chat_jid") or "").strip(),
                    str(data.get("text") or ""),
                    str(data.get("voice_path") or "").strip() or None,
                    req_id,
                    str(data.get("document_path") or "").strip() or None,
                    response_path,
                    str(data.get("origin") or "").strip() or None,
                    str(data.get("user_scope_id") or "").strip() or None,
                )
            except Exception as e:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                _write_external_send_result(response_path, False, f"Invalid IPC request: {e}")
    except Exception:
        return None
    return None


# What a send answers, in one place for both send paths below. The outward hold reads the
# outcome from these words (`SendWhatsAppTool.delivery_markers`, pinned to them by
# tests/test_outbox_send_outcome.py): SENT consumes a draft, UNCONFIRMED makes it ambiguous,
# because the message may or may not have left - a second click could deliver it twice.
SENT_TEXTS = ("Message sent via WhatsApp.", "Voice message sent via WhatsApp.",
              "Document sent via WhatsApp.")
SEND_UNCONFIRMED = (
    "No delivery confirmation from the WhatsApp bridge within the time limit. "
    "If the message appeared in WhatsApp, it was delivered; otherwise check Settings → "
    "Connections → WhatsApp (bridge running, linked)."
)


def _sent_text(voice_path: Optional[str], document_path: Optional[str]) -> str:
    if document_path:
        return SENT_TEXTS[2]
    if voice_path:
        return SENT_TEXTS[1]
    return SENT_TEXTS[0]


def _wait_for_external_send_result(
    response_path: Path,
    *,
    timeout: float,
    voice_path: Optional[str] = None,
    document_path: Optional[str] = None,
) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if response_path.exists():
            try:
                data = json.loads(response_path.read_text(encoding="utf-8"))
            except Exception as e:
                try:
                    response_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return f"WhatsApp could not deliver the message: Invalid IPC result: {e}"
            try:
                response_path.unlink(missing_ok=True)
            except Exception:
                pass
            if bool(data.get("success")):
                return _sent_text(voice_path, document_path)
            return f"WhatsApp could not deliver the message: {data.get('error', '')}"
        time.sleep(0.1)
    return SEND_UNCONFIRMED


def _wa_bridge_path() -> Path:
    """Path to wa-bridge.js."""
    return Path(__file__).resolve().parents[1] / "whatsapp_node" / "wa-bridge.js"


def _node_path() -> Optional[str]:
    """Resolve Node executable: PATH, else the installer's portable Node."""
    from vaf.core import node_runtime
    return node_runtime.find("node")


_deps_lock = threading.Lock()

# The staged install's working folders, next to node_modules so the swap is a rename on one
# file system. Each install stages into a fresh `.deps-staging-<random>` (tempfile.mkdtemp:
# owner-only on POSIX, because a project .npmrc copied in may carry a registry token, and never
# a name a leftover still holds, which a Windows scanner can keep from being removed). A swap
# that is interrupted between its two renames leaves the old tree in .deps-previous; the next
# call puts it back.
_DEPS_STAGING = ".deps-staging"
_DEPS_PREVIOUS = ".deps-previous"

# sha256 of the package-lock.json whose reinstall failed in this process while a usable tree
# was kept. The reconnect worker restarts the bridge every few minutes while it is
# disconnected; without this, every restart would run npm again for the same lock.
_failed_reinstall_lock = ""

# (reason, lockfile sha256) pairs already written to whatsapp_qr.log for a call that kept the
# old tree: once per lockfile, not once per reconnect-worker restart.
_deps_notes: set = set()

# Every Node spawn_node_bridge started, the QR-link ones included: while one of them runs from
# node_modules, the tree is not swapped under it, and no second Node starts on the login folder
# one of them runs on (`proc._vaf_login`, see `login_in_use`). Lock order: `_spawn_lock` and the
# routes' `_qr_lock` before this one; nothing takes another lock while holding it.
_spawned_nodes: List[subprocess.Popen] = []
_spawned_nodes_lock = threading.Lock()


class LoginInUse(RuntimeError):
    """`spawn_node_bridge` refused: a live Node already runs on this login folder."""


def _installed_baileys_version(node_dir: Path) -> str:
    try:
        pkg = json.loads((node_dir / "node_modules" / "@whiskeysockets" / "baileys" / "package.json").read_text(encoding="utf-8"))
        return str(pkg.get("version") or "")
    except (OSError, ValueError):
        return ""


def bridge_deps_drift() -> Optional[list]:
    """Where the bridge's node_modules differ from its lockfile: [] when they match, None when
    the lockfile cannot be read (node_runtime.lock_drift). The bridge installs with
    --omit=dev, so dev entries do not count."""
    from vaf.core import node_runtime
    return node_runtime.lock_drift(_wa_bridge_path().parent, include_dev=False)


def _drift_summary(drift) -> str:
    """The drift as one ASCII line: the count, then the first three packages, as in
    "2 packages differ from the lockfile: follow-redirects 1.16.0 -> 1.16.2, ..."."""
    def one(d) -> str:
        name = d.name[len("node_modules/"):] if d.name.startswith("node_modules/") else d.name
        if not d.installed:
            return f"{name} missing -> {d.locked}"
        if not d.locked:
            return f"{name} {d.installed} not in the lockfile"
        return f"{name} {d.installed} -> {d.locked}"
    count = len(drift)
    head = ", ".join(one(d) for d in drift[:3])
    more = f", and {count - 3} more" if count > 3 else ""
    return f"{count} package{'s' if count != 1 else ''} differ{'s' if count == 1 else ''} from the lockfile: {head}{more}"


def _lock_digest(node_dir: Path) -> str:
    try:
        return hashlib.sha256((node_dir / "package-lock.json").read_bytes()).hexdigest()
    except OSError:
        return ""


def _remove_tree(path: Path) -> None:
    """Remove a folder (or a link in its place) that may or may not exist."""
    try:
        if path.is_symlink():
            path.unlink()
            return
    except OSError:
        return
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _recover_interrupted_swap(node_dir: Path) -> None:
    """Put back the tree an interrupted swap moved aside, then drop the leftovers."""
    live, previous = node_dir / "node_modules", node_dir / _DEPS_PREVIOUS
    if not live.exists() and previous.is_dir():
        try:
            os.replace(previous, live)
            logger.warning("WhatsApp: restored the bridge packages an interrupted install had moved aside")
        except OSError as e:
            logger.warning("WhatsApp: could not restore %s: %s", previous, e)
            return
    for entry in _staging_leftovers(node_dir):
        _remove_tree(entry)
    if live.exists():
        _remove_tree(previous)


def _staging_leftovers(node_dir: Path) -> List[Path]:
    try:
        return [p for p in node_dir.iterdir()
                if p.name == _DEPS_STAGING or p.name.startswith(_DEPS_STAGING + "-")]
    except OSError:
        return []


def _swap_in(node_dir: Path, staging: Path) -> str:
    """Replace node_modules with the staged tree. Returns "" on success, else the error, and
    then the old tree is where it was."""
    live, previous = node_dir / "node_modules", node_dir / _DEPS_PREVIOUS
    moved = False
    try:
        if live.exists() or live.is_symlink():
            os.replace(live, previous)
            moved = True
        os.replace(staging / "node_modules", live)
    except OSError as e:
        if moved and not live.exists():
            try:
                os.replace(previous, live)
            except OSError:
                pass  # _recover_interrupted_swap puts it back on the next call
        return str(e) or type(e).__name__
    _remove_tree(previous)
    return ""


def _bridge_node_running() -> bool:
    if _bridge_thread is not None and _bridge_thread.is_alive():
        return True
    with _spawned_nodes_lock:
        _spawned_nodes[:] = [p for p in _spawned_nodes if _still_running(p)]
        return bool(_spawned_nodes)


def _still_running(proc: subprocess.Popen) -> bool:
    try:
        return proc.poll() is None
    except Exception:
        return False


def _log_deps(line: str) -> None:
    try:
        from vaf.core.log_helper import log_whatsapp_qr
        log_whatsapp_qr(f"[deps] {line}")
    except Exception:
        pass


def _kept(reason: str, digest: str, msg: str) -> Tuple[bool, str]:
    """(True, msg) for a call that keeps the installed tree, with msg in whatsapp_qr.log the
    first time for this reason and lockfile."""
    if (reason, digest) not in _deps_notes:
        _deps_notes.add((reason, digest))
        _log_deps(msg)
    return True, msg


def ensure_bridge_deps(timeout: float = 600.0) -> Tuple[bool, str]:
    """Install the Node bridge's dependencies when they are missing or differ from the lockfile.

    `vaf/whatsapp_node` ships `package.json`, `package-lock.json` and `wa-bridge.js`; the
    `node_modules` behind them were a manual `npm install` nobody ran (no installer, no
    `vaf update` step, no start path did it). This is the one place that does: every
    path that spawns the Node (bridge start, QR login, `vaf update`) calls it first.

    The whole lock is compared (node_runtime.lock_drift), not Baileys alone: a patched
    package further down the tree is the usual security bump. In sync costs no npm call.
    Otherwise `npm ci` (exactly the lockfile; `npm install` as the fallback for a lockfile npm
    refuses) runs in a fresh `.deps-staging-*/` (owner-only on POSIX), and the result replaces
    node_modules only once Baileys is in it: a failed `npm ci` empties the node_modules it runs
    in, so an install in place that fails offline would stop a bridge that works. A tree that
    still has Baileys stays in use, and the call succeeds with a message that says what
    differs, when the reinstall fails, when npm is missing or the folder is not writable, while
    a bridge Node of this process runs from it, and once a reinstall for these lockfile bytes
    has already failed in this process.

    The lock below serialises callers inside one process only. The other process that calls
    this is `vaf update`, which stops the service before its dependency steps; a caller that
    could overlap from another process would need vaf.core.lock_manager. Returns (ok, message);
    never raises."""
    global _failed_reinstall_lock
    node_dir = _wa_bridge_path().parent
    if not (node_dir / "package.json").is_file():
        return False, f"WhatsApp bridge sources not found at {node_dir}"
    with _deps_lock:
        _recover_interrupted_swap(node_dir)
        have = _installed_baileys_version(node_dir)
        drift = bridge_deps_drift()
        if have and not drift:
            return True, f"WhatsApp bridge dependencies present (Baileys {have})"
        summary = _drift_summary(drift) if drift else "Baileys is not installed"
        behind = f"WhatsApp bridge dependencies differ from the lockfile ({summary})"
        digest = _lock_digest(node_dir)
        if have and _bridge_node_running():
            return _kept("running", digest, (
                f"{behind}. Baileys {have} stays in use while the bridge runs; the "
                "lockfile's packages are installed at the next bridge start."))
        from vaf.core import node_runtime
        npm = node_runtime.find("npm")
        if not npm:
            if have:
                return _kept("no-npm", digest, (
                    f"{behind}; npm not found, so Baileys {have} stays in use. Install "
                    f"Node.js 18+ (with npm), or run `npm ci --omit=dev` in {node_dir} with VAF stopped."))
            return False, (
                "npm not found. Install Node.js 18+ (with npm), then start WhatsApp again; "
                f"or run `npm install` in {node_dir} by hand."
            )
        if not os.access(node_dir, os.W_OK):
            if have:
                return _kept("read-only", digest, (
                    f"{behind}; {node_dir} is not writable, so Baileys {have} stays in use. "
                    f"Run `npm ci --omit=dev` in {node_dir} with VAF stopped."))
            return False, f"{node_dir} is not writable; run `npm install` there with the right permissions."
        if have and digest and digest == _failed_reinstall_lock:
            return _kept("failed-before", digest, (
                f"{behind}; the reinstall for this lockfile already failed, so Baileys {have} "
                "stays in use until the next VAF start or `vaf update`."))
        _log_deps(f"installing bridge dependencies (Baileys {have or 'none'}; {summary})")
        logger.info("WhatsApp: installing bridge dependencies in %s (Baileys %s; %s)", node_dir, have or "none", summary)
        staging = node_dir / _DEPS_STAGING
        last_err = ""
        try:
            staging = Path(tempfile.mkdtemp(prefix=_DEPS_STAGING + "-", dir=node_dir))
            for name in ("package.json", "package-lock.json", ".npmrc"):
                if (node_dir / name).is_file():
                    shutil.copy2(node_dir / name, staging / name)   # keeps a 0600 .npmrc 0600
        except OSError as e:
            last_err = f"could not prepare a staging folder in {node_dir}: {e}"
        installed = ""
        if not last_err:
            kwargs: dict = {"cwd": str(staging), "capture_output": True, "text": True, "timeout": timeout,
                            "env": node_runtime.child_env(npm)}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            use_ci = (staging / "package-lock.json").is_file()
            cmds = [[npm, "ci", "--omit=dev", "--no-audit", "--no-fund"]] if use_ci else []
            cmds.append([npm, "install", "--omit=dev", "--no-audit", "--no-fund"])
            for cmd in cmds:
                try:
                    r = subprocess.run(cmd, **kwargs)
                except (OSError, subprocess.TimeoutExpired) as e:
                    last_err = str(e)
                    continue
                installed = _installed_baileys_version(staging) if r.returncode == 0 else ""
                if installed:
                    break
                last_err = (r.stderr or r.stdout or "").strip()[-600:] or f"exit {r.returncode}"
        if installed:
            swap_err = _swap_in(node_dir, staging)
            if swap_err:
                installed, last_err = "", f"could not replace node_modules: {swap_err}"
        _remove_tree(staging)
        if installed:
            _failed_reinstall_lock = ""
            msg = f"WhatsApp bridge dependencies installed (Baileys {installed})"
            logger.info(msg)
            _log_deps(msg)
            still = bridge_deps_drift()
            if still:
                # A drift npm cannot close would cost an install at every start; say so.
                logger.warning("WhatsApp: the bridge's packages still differ after the install: %s",
                               _drift_summary(still))
            return True, msg
        logger.error("WhatsApp bridge dependency install failed: %s", last_err)
        _log_deps(f"FAILED {last_err}")
        kept = _installed_baileys_version(node_dir)
        if kept:
            _failed_reinstall_lock = digest
            return True, (f"{behind}; reinstall failed: {last_err}. The installed packages stay in use "
                          f"(Baileys {kept}); the next VAF start or `vaf update` tries again, or run "
                          f"`npm ci --omit=dev` in {node_dir} with VAF stopped.")
        return False, f"WhatsApp bridge dependency install failed: {last_err}. Run `npm install` in {node_dir} by hand."


def _jid_to_e164(jid: str) -> str:
    """Convert WhatsApp JID to E.164-like string (e.g. 491234567890@s.whatsapp.net -> 491234567890).
    Strips :device suffix, skips @lid/@broadcast/@status."""
    if not jid or not isinstance(jid, str):
        return ""
    jid = jid.strip()
    if "@lid" in jid or jid.endswith("@broadcast") or jid.endswith("@status"):
        return ""
    if "@" not in jid:
        return jid
    part = jid.split("@")[0].split(":")[0].strip()
    if not part or not part.isdigit() or len(part) < 7 or len(part) > 15:
        return ""
    return part


def _normalize_phone(phone: str) -> str:
    """Digits only, for the inbound session key of a resolved sender. Deliberately not the
    send path (that is `contacts_store.whatsapp_send_jid`): it reads JIDs WhatsApp sends,
    ASCII digits by protocol, and a different rule here would re-key every existing chat
    session."""
    return "".join(c for c in (phone or "") if c.isdigit())


def _chat_label(chat_id: str, push_name: str, username: str, user_scope_id: Optional[str]) -> str:
    """The person's name for a chat's memory namespace: the contact book first, then the
    push name WhatsApp carries, else the number itself. Best-effort, never raises."""
    try:
        if (chat_id or "").startswith("+"):
            from vaf.core.contacts_store import get_contact_name_by_phone
            name = get_contact_name_by_phone(chat_id, username, user_scope_id=user_scope_id)
            if name and name.strip():
                return name.strip()
    except Exception:
        pass
    return (push_name or "").strip() or (chat_id or "")


def _phone_digits_canonical(phone_or_jid: str) -> str:
    """Digits for matching, from the one canonicaliser the contact book and the dashboard use
    (contacts_store.phone_digits_canonical): a JID's user part, 0-prefixed German numbers as
    49..., a LID or group as no number at all."""
    from vaf.core.contacts_store import phone_digits_canonical
    return phone_digits_canonical(phone_or_jid if isinstance(phone_or_jid, str) else "")


def _to_e164_display(phone_or_jid: str) -> str:
    """The message-store key for a number (contacts_store.whatsapp_store_key: one leading +,
    canonical digits), or "" when the value is not a phone number."""
    if not phone_or_jid or not isinstance(phone_or_jid, str):
        return ""
    from vaf.core.contacts_store import whatsapp_store_key
    return whatsapp_store_key(phone_or_jid) or ""


def _append_chat_activity(chat_id: str, user_scope_id: Any, direction: str = "in") -> None:
    """Append one activity entry for the dashboard timeline (keeps last 100)."""
    from vaf.core.messaging_connections import append_channel_activity
    append_channel_activity("whatsapp", {"chat_id": str(chat_id), "user_scope_id": str(user_scope_id) if user_scope_id else None,
                                    "ts": time.time(), "direction": direction}, keep=100)


def _synthesize_voice_for_reply(text: str, lang: str,
                                user_scope_id: Optional[str] = None,
                                busy_raises: bool = False) -> Optional[str]:
    """Synthesize TTS to temp file. Returns path or None (also when the TTS queue is busy:
    the reply then goes out as text; with `busy_raises` that case raises
    fair_queue.QueueBusy instead). `user_scope_id` is the person it waits for."""
    from vaf.core.fair_queue import QueueBusy
    try:
        import tempfile
        from vaf.core import speech_client
        logger.info("WhatsApp TTS: synthesizing lang=%s text_len=%d", lang, len(text))
        data = speech_client.synthesize(text[:4000], lang, want_format="ogg",
                                        user_key=user_scope_id, busy_raises=busy_raises)
        if not data:
            logger.warning("WhatsApp TTS: no audio returned")
            return None
        if data[:4] not in (b"OggS", b"RIFF"):
            logger.warning("WhatsApp TTS: unknown audio format (magic: %s)", data[:4].hex())
            return None
        suffix = ".ogg" if data[:4] == b"OggS" else ".wav"
        with tempfile.NamedTemporaryFile(prefix="vaf_wa_", suffix=suffix, delete=False) as f:
            f.write(data)
            logger.info("WhatsApp TTS: wrote %d bytes to %s", len(data), f.name)
            return f.name
    except QueueBusy:
        raise
    except Exception as e:
        logger.warning("WhatsApp TTS synthesis error: %s", e)
        return None


def _transcribe_voice_file(voice_path: str,
                           user_scope_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """
    Transcribe a voice file through the shared speech client: the STT provider first,
    the Whisper container only on the Full profile.
    Returns (transcribed_text, detected_language) or (None, None) on error. A note the
    STT queue turned away is (None, None) too, logged as busy: the caller's media marker
    reaches the agent, which answers it. `user_scope_id` is the person it waits for.
    """
    from vaf.core.fair_queue import QueueBusy
    try:
        path_obj = Path(voice_path)
        if not path_obj.is_file():
            logger.warning("WhatsApp STT: voice file not found: %s", voice_path)
            return None, None
        file_size = path_obj.stat().st_size
        # Known-bad gate. This is WhatsApp's only inbound FILE lane: Node has already
        # written the media to disk, so the gate sits here, at the one door through
        # which those bytes reach anything that reads them. Refused audio is simply
        # not transcribed, and the caller's existing "no transcript" branch takes over.
        from vaf.core.threat_db import inspect_upload_file
        if inspect_upload_file(path_obj, origin="whatsapp").blocked:
            return None, None
        # Use MIME type from extension (Node sends .ogg for PTT, .opus for other audio)
        ext = (path_obj.suffix or "").lower()
        mime = "audio/ogg" if ext == ".ogg" else ("audio/opus" if ext == ".opus" else "audio/ogg")
        filename = f"voice{ext}" if ext else "voice.ogg"
        logger.info("WhatsApp STT: transcribing %s (%d bytes)", voice_path, file_size)
        from vaf.core import speech_client
        text, language = speech_client.transcribe(
            voice_path, mime=mime, filename=filename, user_key=user_scope_id,
            deadline_s=speech_client.WAIT_BACKGROUND_S, busy_raises=True)
        if not text:
            return None, None
        logger.info("WhatsApp voice transcribed: lang=%s, text=%s...", language, (text or "")[:50])
        return text, language or "en"
    except QueueBusy as busy:
        logger.warning("WhatsApp STT busy: %s", busy)
        return None, None
    except Exception as e:
        logger.warning("WhatsApp voice transcription error: %s", e)
        return None, None
    finally:
        try:
            os.unlink(voice_path)
        except Exception:
            pass


def _deliver_send_result(req_id: str, success: bool, error: Optional[str] = None) -> None:
    """Deliver send result to waiting caller."""
    with _pending_sends_lock:
        q = _pending_sends.pop(req_id, None)
    if q is not None:
        try:
            q.put((success, error or ""))
        except Exception:
            pass


def _allow_from_match(sender_jid: str, allowed_phones: List[str]) -> bool:
    """Check if sender JID matches any allowed phone number. Normalizes 0176... and +49176... to same form."""
    sender_canonical = _phone_digits_canonical(sender_jid)
    if not sender_canonical:
        return False
    for p in allowed_phones:
        if _phone_digits_canonical(p) == sender_canonical:
            return True
    return False


def _get_allowed_phones_for_user(username: str, user_scope_id: str) -> Tuple[List[str], List[str]]:
    """(config_phones, allowed_phones) for this account, read per message so a change in the
    book needs no bridge restart.

    `config_phones` is the whitelist, and it is the one that DECIDES: a match is the owner's
    own registered number (`explicit_pair`). `allowed_phones` adds the endpoints of the
    contacts the owner allowed, and it decides nothing any more - the person's own record is
    read by `contacts_store.contact_access` at the decision site, because a set built from a
    flag cannot tell "allowed" from "denied" and was consulted first, so a denial was never
    seen. What is left of it is one debug line for a user whose book and whitelist are both
    empty. A caller that wants the answer, not the list, asks `evaluate_ingress`.

    Named boundaries:
    - An entry without a scope filed under "admin" or no name is not the local admin's
      registered number here on an install whose admin has another name (it matches neither
      the admin's name nor scope); where an ordinary account is named "admin", the name match
      makes it that account's. Such an entry is written by hand, or by a settings save of a
      version that stored the Connections page's copy of a number it had just added. The
      repair is the admin's scope AND the admin's own username on the entry: with the scope
      alone, the name "admin" still matches that ordinary account.
    - A scoped entry still matches by name too, not by its scope alone, for an entry whose
      scope and name disagree (written by hand). The route files a number an admin registers
      for another account under that account's scope, and entries older versions filed under
      the admin's scope are moved at the bridge start
      (`channel_pairing.refile_whatsapp_owner_numbers`), so a route-written entry no longer
      needs the name to reach its account."""
    config_phones: List[str] = []
    allowed_phones: List[str] = []
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        return config_phones, allowed_phones
    for entry in (whatsapp_config.get("whitelist") or []):
        if not isinstance(entry, dict) or not entry.get("phone_number"):
            continue
        p = str(entry.get("phone_number", "")).strip()
        if not p:
            continue
        if entry.get("vaf_username") == username or str(entry.get("user_scope_id")) == user_scope_id:
            if p not in config_phones:
                config_phones.append(p)
            if p not in allowed_phones:
                allowed_phones.append(p)
    try:
        from vaf.core.contacts_store import front_office_endpoints
        seen_phones: set = set()
        # This user's book by scope, then their legacy per-username file. Never the
        # local admin's scope on top: for the admin it is user_scope_id already, and
        # for anyone else it would admit the admin's Front Office contacts to a
        # tenant's agent number (the contact book keeps that door shut on its side
        # as well, see contacts_store._contacts_path_candidates).
        for scope_arg in (user_scope_id, None):
            for key in sorted(front_office_endpoints(username, scope_arg or None, "whatsapp")):
                if key not in seen_phones:
                    seen_phones.add(key)
                    allowed_phones.append(key)
    except Exception:
        pass
    return config_phones, allowed_phones


def _get_users_to_run() -> List[Tuple[str, str, Path]]:
    """(user_scope_id, username, auth_dir) for every account that linked a WhatsApp number
    and has the connection switched on. The linked account is that user's agent number, so
    no whitelist entry is needed to run it (a registered main-user number only decides
    whether the OWNER is reachable), and there is no fallback to another user's credentials:
    two Baileys sockets on one credential set evict each other."""
    from vaf.core.config import scope_id_for_username
    result: List[Tuple[str, str, Path]] = []
    for username in linked_usernames():
        scope = scope_id_for_username(username)
        if not scope:
            logger.warning("WhatsApp: linked credentials for unknown account %r ignored", username)
            continue
        if not channel_enabled_for_scope("whatsapp", scope):
            continue
        result.append((str(scope), username, get_whatsapp_auth_dir(username)))
    return result


def _run_user_process(username: str, auth_dir: Path) -> Optional[subprocess.Popen]:
    """Spawn Node wa-bridge for one user. Returns process or None on failure."""
    node = _node_path()
    if not node:
        logger.error("Node.js not found. Install Node >= 18 for WhatsApp support.")
        return None
    wa_js = _wa_bridge_path()
    if not wa_js.exists():
        logger.error("wa-bridge.js not found at %s", wa_js)
        return None
    try:
        return spawn_node_bridge(node, wa_js, auth_dir)
    except LoginInUse:
        logger.error("WhatsApp: %s not started, another process runs on its login", username)
        return None
    except Exception as e:
        logger.exception("Failed to start WhatsApp bridge for %s: %s", username, e)
        return None


def spawn_node_bridge(node: str, wa_js: Path, auth_dir: Path) -> subprocess.Popen:
    """Start wa-bridge.js on one account's session directory. The one place Node is spawned:
    the running bridge and the QR link both come through here, so both get the same pipes
    and the same owner-only files.

    The session directory is the WhatsApp login (whatsapp_auth.harden_auth_dir has the
    measurement), so Node runs with umask 077 and every key file Baileys writes is born
    0600, and whatever an earlier start wrote is fixed first. `umask=` rather than a
    preexec_fn, which is not safe in a threaded process; POSIX only, Windows ignores both.

    Raises `LoginInUse` and spawns nothing while a Node it started still runs on the same
    folder (`login_in_use`): the check, the spawn and the record happen under one lock, so two
    threads cannot both pass it.
    """
    from vaf.core import node_runtime
    from vaf.core.whatsapp_auth import harden_auth_dir
    auth_dir.mkdir(parents=True, exist_ok=True)
    harden_auth_dir(auth_dir)
    login = _login_identity(auth_dir)
    kwargs: dict = {
        "env": node_runtime.child_env(node),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["umask"] = 0o077
    with _spawned_nodes_lock:
        if _login_taken(login):
            raise LoginInUse(f"a WhatsApp process already runs on {auth_dir}")
        proc = subprocess.Popen([node, str(wa_js), "--auth-dir", str(auth_dir.resolve())], **kwargs)
        try:
            proc._vaf_login = login
        except Exception:
            logger.warning("WhatsApp: could not record the login folder of %r", proc, exc_info=True)
        _spawned_nodes.append(proc)
    return proc


def _login_identity(auth_dir: Path) -> Tuple[str, Optional[Tuple[int, int]]]:
    """(absolute path, (st_dev, st_ino)) of a login folder; the second is None while the folder
    does not exist or the file system reports no inode. Two spellings of one folder (a link, a
    case-insensitive file system) share the second."""
    path = os.path.abspath(os.fspath(auth_dir))
    try:
        st = os.stat(path)
        ino = (st.st_dev, st.st_ino) if st.st_ino else None
    except OSError:
        ino = None
    return path, ino


def _same_login(a: Any, b: Any) -> bool:
    if not (isinstance(a, tuple) and isinstance(b, tuple) and len(a) == 2 and len(b) == 2):
        return False
    return a[0] == b[0] or (a[1] is not None and a[1] == b[1])


def _login_taken(login: Tuple[str, Optional[Tuple[int, int]]]) -> bool:
    """Whether a live Node of `_spawned_nodes` runs on this login; prunes the ended ones.
    Caller holds `_spawned_nodes_lock`."""
    _spawned_nodes[:] = [p for p in _spawned_nodes if _still_running(p)]
    return any(_same_login(getattr(p, "_vaf_login", None), login) for p in _spawned_nodes)


def login_in_use(auth_dir: Path) -> bool:
    """Whether a live Node VAF started (an account's process or a QR link) runs on this login
    folder, reached by the same path or as the same folder through another one. A deletion
    and a reset remove nothing while it does, and no second Node starts on it."""
    login = _login_identity(Path(auth_dir))
    with _spawned_nodes_lock:
        return _login_taken(login)


def _account_scope(username: str) -> str:
    """The scope an account name stands for here: the one its running process was started
    with, else the account directory's answer, else "" (no account by that name)."""
    name = (username or "").strip()
    if not name:
        return ""
    known = _scope_by_user.get(name)
    if known:
        return str(known).strip()
    from vaf.core.config import scope_id_for_username
    return str(scope_id_for_username(name) or "").strip()


def _process_for(username: str, user_scope_id: Optional[str]
                 ) -> Tuple[Optional[str], Optional[subprocess.Popen]]:
    """(name, process) of the Node process that acts for this account, or (None, None).

    THE RULE: every request names its account's scope, and only a process started for that
    scope acts for it. The account's own live process first (registered under this name AND
    started for this scope). Otherwise the only running process, but ONLY when it was started
    for the same scope (the same account under another spelling of its name). A request
    without a scope gets no process at all. Every lane that writes to a Node process asks
    here, and asks without holding `_process_lock` (this takes it): the sender loop, the send
    lanes and the reply lane with the scope of their queue item (`config.messenger_lane_identity`
    gives a scopeless send the local admin's, or refuses it), the read and sync lanes (the
    chat list and its sync, history, avatars, names, LID mappings, the connection check) with
    the signed-in account's scope (`user_routes.request_account` refuses a sign-in without
    one), the bridge's own chat-sync loop and the reconnect check with each process's
    registered scope.

    A name is no proof of an account: it can be a caller's default, a deleted account's name
    given to a new one, or a twin that differs only by Unicode case folding. Measured with the
    read lanes still settling a scopeless request by its name: a deleted "bob"'s process,
    still registered under "bob", answered the NEW account "bob"'s chat list, history,
    avatars and connection check; and with the account directory unreachable, an account
    "Straße" (and "STRASSE") reached the local admin "strasse"'s process, because the
    one-process fallback compared against the scope the name resolved to. Before that, twelve
    hand copies took the only process whoever it belonged to: an account without a linked
    WhatsApp sent from another account's agent number, read its chat list and loaded its
    history; and a non-admin's nameless notification arrived as "admin" and went out through
    the admin's process."""
    name = (username or "").strip()
    wanted = str(user_scope_id or "").strip()
    if not wanted:
        return None, None
    with _process_lock:
        own = _processes.get(name) if name else None
        own_scope = str(_scope_by_user.get(name) or "").strip() if own is not None else ""
        if own is not None and own.poll() is None and not _is_retired(own) and own_scope == wanted:
            return name, own
        only_name, only = next(iter(_processes.items())) if len(_processes) == 1 else (None, None)
        if _is_retired(only):
            only_name, only = None, None
        only_scope = str(_scope_by_user.get(only_name) or "").strip() if only_name else ""
    if only is None or only.poll() is not None or not only_scope:
        return None, None
    if wanted != only_scope:
        return None, None
    return only_name, only


def _preferred_lid_jid(username: str, chat_jid: str, kind: str) -> str:
    """The JID a send to `chat_jid` goes to: the genuine LID this account's map binds to
    EXACTLY the recipient's number (some LID-migrated accounts receive voice, and some text,
    only on their LID), else `chat_jid` itself.

    The match is `_lid_keys_for_digits`, the exact canonical number. The copies this replaced
    matched by suffix in both directions, so a LID mapped to "+1" took every send to a number
    ending in 1 (measured: ten bindings to +0 ... +9 caught three unrelated recipients). A
    LID whose digits equal the number's is not a LID (WhatsApp sends `<number>@lid` at times)
    and is skipped, as is a key that is not digits."""
    jid = (chat_jid or "").strip()
    if not jid or jid.endswith("@lid"):
        return chat_jid
    digits = _phone_digits_canonical(jid)
    if not digits:
        return chat_jid
    try:
        from vaf.core.contacts_store import _lid_keys_for_digits, whatsapp_lid_map
        candidates = _lid_keys_for_digits([digits], whatsapp_lid_map(_account_scope(username) or None))
    except Exception:
        return chat_jid
    for lid in candidates:
        lid_digits = lid.split("@", 1)[0]
        if not lid_digits.isascii() or not lid_digits.isdigit():
            continue
        if lid_digits == digits:
            logger.info("WhatsApp %s: skipping fake LID %s (lid digits == phone digits)", kind, lid)
            continue
        logger.info("WhatsApp %s: resolved %s → %s via the LID map", kind, chat_jid, lid)
        try:
            from vaf.core.log_helper import log_whatsapp_reply
            log_whatsapp_reply(f"{kind.upper()}_LID_RESOLVE {chat_jid} → {lid}")
        except Exception:
            pass
        return lid
    return chat_jid


def _refuse_after_stop(req_id: Optional[str], response_path: Optional[Path], username: str) -> None:
    """Answer a send its loop's run ended before it was written: it did not leave."""
    logger.warning("WhatsApp: a send for %s was not written, its bridge run has ended", username)
    if req_id:
        _complete_send_request(req_id, False, BRIDGE_STOPPING, response_path=response_path)


def _sender_loop(q: Optional[queue.Queue] = None, stop: Optional[threading.Event] = None) -> None:
    """Read (username, chat_jid, text, voice_path?, req_id?, document_path?, response_path?, origin?,
    user_scope_id?) from the queue and write to that user's Node stdin. `origin` is "owner" when
    the person sent the message from the dashboard; the stored row then carries OWNER_SENDER so
    the reply window does not count it as the agent writing. `user_scope_id` is the scope the
    request was made for; only that scope's process takes it.

    One loop per bridge run: `q` and `stop` are that run's queue and stop (`start_bridge`;
    without them, the ones current at entry). It ends on None or, between items, once its run
    has ended, and claims no request file after that: an orphaned loop polling the request
    folder next to a newer run's loop sent one file twice."""
    if q is None:
        q = _outgoing_queue
    if stop is None:
        stop = _bridge_stop
    while True:
        try:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                if stop.is_set():
                    break
                item = _dequeue_external_send_request(stop)
                if item is None:
                    continue
            if item is None:
                break
            req_id = item[4] if len(item) >= 5 else None
            voice_path = item[3] if len(item) >= 4 else None
            document_path = item[5] if len(item) >= 6 else None
            response_path = item[6] if len(item) >= 7 else None
            origin = item[7] if len(item) >= 8 else None
            # The scope the request was made for, when its lane knows it: the process must be
            # that scope's (see _process_for).
            item_scope = item[8] if len(item) >= 9 else None
            username, chat_jid, text = item[0], item[1], (item[2] or "")
            _register_external_response_path(req_id, response_path)
            if not username or not chat_jid:
                continue
            if not voice_path and not text and not document_path:
                continue
            if not str(item_scope or "").strip():
                # Both send entries convert before they queue, so only a request file from a
                # process still running older code arrives without a scope: the same rule here,
                # never a process picked by its name alone.
                ident = messenger_lane_identity(None, username)
                if ident is None:
                    logger.warning("WhatsApp: refused a send without an account for %r", username)
                    if req_id:
                        _complete_send_request(req_id, False, NO_ACCOUNT_FOR_REQUEST,
                                               response_path=response_path)
                    continue
                item_scope, username = str(ident[0]), ident[1]
            _, proc = _process_for(username, item_scope)
            if stop.is_set():
                # The run has ended (an item queued ahead of the shutdown's None, or one
                # taken while it ended): the process picked may be the next run's
                # replacement, which this loop must not write to (two writers on one stdin).
                # Checked after the pick, so a stop still clear means the pick was this run's
                # (a stop is never cleared, and a new run spawns only after the old stop). A
                # write that follows and races the end keeps SEND_UNCONFIRMED, because it may
                # have reached Node.
                _refuse_after_stop(req_id, response_path, username)
                continue
            if not proc or proc.poll() is not None:
                # Never another account's process: that would send from its agent number.
                logger.warning("WhatsApp process for %s not running, dropped reply", username)
                if req_id:
                    _complete_send_request(
                        req_id,
                        False,
                        _no_process_reason(username, item_scope),
                        response_path=response_path,
                    )
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(f"DROPPED process_not_running username={username} jid={chat_jid}")
                except Exception:
                    pass
                continue
            if voice_path:
                try:
                    from pathlib import Path
                    # For voice messages, prefer @lid JID over @s.whatsapp.net when known.
                    # Some WhatsApp accounts (privacy-migrated) only receive voice via @lid;
                    # text works with @s.whatsapp.net but voice does not.
                    voice_jid = _preferred_lid_jid(username, chat_jid, "voice")
                    p = Path(voice_path)
                    if p.is_file():
                        size = p.stat().st_size
                        logger.info("WhatsApp sending voice to %s, path=%s, size=%s", voice_jid, p, size)
                        cmd = {"cmd": "send_voice", "to": voice_jid, "path": str(p.resolve())}
                        if req_id:
                            cmd["req_id"] = req_id
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    else:
                        err = "Voice file not found"
                        logger.warning("WhatsApp voice file not found: %s", voice_path)
                        if req_id:
                            _complete_send_request(req_id, False, err, response_path=response_path)
                except Exception as e:
                    logger.warning("WhatsApp voice send failed for %s: %s", username, e)
                    if req_id:
                        _complete_send_request(req_id, False, str(e), response_path=response_path)
            elif document_path:
                try:
                    from pathlib import Path
                    p = Path(document_path)
                    if p.is_file():
                        cmd = {"cmd": "send_document", "to": chat_jid, "path": str(p.resolve()), "caption": (text or "")[:1024]}
                        if req_id:
                            cmd["req_id"] = req_id
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    else:
                        err = "Document file not found"
                        logger.warning("WhatsApp document not found: %s", document_path)
                        if req_id:
                            _complete_send_request(req_id, False, err, response_path=response_path)
                except Exception as e:
                    logger.warning("WhatsApp document send failed for %s: %s", username, e)
                    if req_id:
                        _complete_send_request(req_id, False, str(e), response_path=response_path)
            else:
                # For text messages, also prefer @lid JID when known (same reason as voice).
                # Some LID-migrated WhatsApp accounts only reliably receive messages via @lid JID.
                text_jid = _preferred_lid_jid(username, chat_jid, "text")
                chunks = chunk_whatsapp_text(text)
                for i, chunk in enumerate(chunks):
                    cmd = {"cmd": "send", "to": text_jid, "text": chunk}
                    if req_id and i == len(chunks) - 1:
                        cmd["req_id"] = req_id
                    try:
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    except Exception as e:
                        logger.warning("WhatsApp send failed for %s: %s", username, e)
                        if req_id:
                            _complete_send_request(req_id, False, str(e), response_path=response_path)
                        break
            try:
                from vaf.core.log_helper import log_whatsapp_reply
                log_whatsapp_reply(f"SENDER ok username={username} jid={chat_jid}")
            except Exception:
                pass
            try:
                chat_id = f"+{_jid_to_e164(chat_jid)}" if _jid_to_e164(chat_jid) else str(chat_jid or "")
                from vaf.core.channel_message_store import OWNER_SENDER, append_message
                if voice_path:
                    body = "[Voice message]"
                    ctype = "voice"
                elif document_path:
                    body = "[Document] " + (text or "") if text else "[Document]"
                    ctype = "document"
                else:
                    body = text
                    ctype = "text"
                # Same per-scope store as the inbound lane, so the reply window and the
                # read tools see both directions of one conversation.
                _scope = _scope_by_user.get(username)
                append_message(username, chat_id or chat_jid, body, direction="out", content_type=ctype,
                               sender_jid=(OWNER_SENDER if origin == "owner" else None), user_scope_id=_scope)
                if chat_id:
                    _append_chat_activity(chat_id, _scope, "out")
            except Exception:
                pass
        except Exception as e:
            logger.exception("WhatsApp sender error: %s", e)


def _jid_to_chat_id(username: str, chat_jid: str, user_scope_id: Optional[str] = None) -> str:
    """The store key (E.164 display form) for a JID. An @lid resolves through the account's
    persisted LID map (`whatsapp_lid_map`) or the Node's mapping; an unresolved @lid has no
    key, because a LID is not a number and must not be matched against one."""
    jid = (chat_jid or "").strip()
    if not jid:
        return ""
    if jid.endswith("@lid"):
        scope = str(user_scope_id or "").strip()
        try:
            from vaf.core.contacts_store import whatsapp_lid_map
            scope = scope or _account_scope(username)
            mapped = str(whatsapp_lid_map(scope or None).get(jid) or "").strip()
        except Exception:
            mapped = ""
        if not mapped:
            with _lid_mappings_lock:
                for m in _owned(_lid_mappings, _lid_mappings_owner, (username or "").strip() or "admin", scope, []):
                    if m.get("lid") == jid and (m.get("e164") or "").strip():
                        mapped = m["e164"].strip()
                        break
        return _to_e164_display(mapped) if mapped else ""
    return _to_e164_display(_jid_to_e164(jid)) if _jid_to_e164(jid) else ""


def _is_reply_allowed(username: str, chat_jid: str, user_scope_id: Optional[str] = None) -> bool:
    """May the agent send to this JID on a REPLY lane (headless reply, owner delivery)?

    The SAME question the inbound side asks, through the same function: the registered
    main-user number always, a contact the owner allowed always, a person they denied never,
    and anybody else exactly while this channel's Inbound is open. Asking it twice in two
    shapes is how the two sides drift: the agent could be allowed to answer somebody who is
    no longer allowed to write, or refuse to answer a stranger the open channel just admitted.
    An unresolved @lid matches nothing. Explicit recipients (`send_whatsapp(to_phone=...)`)
    do not pass through here.

    A missing scope is still resolved from the name below, for a direct caller only: both
    send entries hand this the scope and name of `config.messenger_lane_identity` first."""
    from vaf.core.config import scope_id_for_username
    uname = (username or "").strip() or "admin"
    scope = str(user_scope_id).strip() if user_scope_id else None
    chat_id = _jid_to_chat_id(uname, chat_jid, user_scope_id=scope)
    if not chat_id:
        return False
    # A missing scope is resolved from the NAME (the admin's own scope for the admin, a
    # tenant's own for a tenant, nothing for a stranger), never defaulted to the admin's:
    # that default read the admin's own contacts for every scopeless caller.
    _scope = scope or scope_id_for_username(uname) or ""
    config_phones, _ = _get_allowed_phones_for_user(uname, _scope)
    if _allow_from_match(chat_id, config_phones):
        return True
    try:
        from vaf.core.contacts_store import contact_access, find_contact_by_channel
        access = contact_access(find_contact_by_channel("whatsapp", chat_id, uname, _scope or None))
    except Exception:
        access = None
    allowed, _reason = evaluate_ingress(
        "whatsapp", Config.get("channel_ingress_policy"), explicit_match=False, access=access)
    return bool(allowed)


def _enqueue_reply(username: str, chat_jid: str, text: str, voice_path: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    """Callback for headless_runner: enqueue reply. If voice_path, send as voice message.
    When user sent a voice message, auto-reply with voice (TTS) when possible. user_scope_id helps resolve FO contacts (scoped storage).

    Returns True only if the reply was actually placed on the outgoing queue for the Node bridge, False if
    it was dropped (recipient not allowed, no outgoing queue, or a queue error). Callers that need a real
    delivery signal (e.g. send_to_main_messenger's fallback decision) rely on this.

    A reply without a scope is the local admin's under the admin's own scope and name, and one
    naming anybody else is refused (`config.messenger_lane_identity`): read by its name, the
    literal "admin" went out through an ordinary account named "admin"."""
    ident = messenger_lane_identity(user_scope_id, username)
    if ident is None:
        logger.warning("WhatsApp: refused a reply without an account for %r", username)
        return False
    user_scope_id, username = ident
    if not _is_reply_allowed(username, chat_jid, user_scope_id=user_scope_id):
        logger.warning("WhatsApp: blocked reply to JID %s for user %s (not owner, contact or open conversation)", chat_jid, username)
        return False
    # If no voice_path but user sent voice, try to synthesize TTS (like Telegram)
    if not voice_path and text:
        with _voice_reply_lock:
            lang = _voice_reply_pending.pop(f"{username}|{chat_jid}", None)
        if lang:
            from vaf.core.fair_queue import QueueBusy
            try:
                voice_path = _synthesize_voice_for_reply(text, lang, user_scope_id,
                                                         busy_raises=True)
                # Fallback: if TTS returns empty (e.g. unsupported lang like tr), try English so we still send voice
                if not voice_path and (lang or "")[:2].lower() != "en":
                    logger.info("WhatsApp TTS: fallback to en after empty/fail for lang=%s", lang)
                    voice_path = _synthesize_voice_for_reply(text, "en", user_scope_id,
                                                             busy_raises=True)
                    if voice_path:
                        lang = "en"
            except QueueBusy as busy:
                # Busy is not a language problem: no second wait in English, the text
                # goes out now.
                logger.warning("WhatsApp TTS busy, replying as text: %s", busy)
                voice_path = None
            if voice_path:
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(f"BRIDGE enqueue voice_reply username={username} lang={lang}")
                except Exception:
                    pass
    try:
        from vaf.core.log_helper import log_whatsapp_reply
        log_whatsapp_reply(f"BRIDGE enqueue username={username} jid={chat_jid} len={len(text)} voice={bool(voice_path)}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put((username, chat_jid, text, voice_path, None, None, None, None,
                                 str(user_scope_id or "").strip() or None))
            return True
        except Exception:
            return False
    return False


def send_whatsapp_with_confirmation(
    username: str,
    chat_jid: str,
    text: str,
    voice_path: Optional[str] = None,
    document_path: Optional[str] = None,
    timeout: float = 15.0,
    allow_contact_send: bool = False,
    origin: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """
    Send a WhatsApp message (text, voice, or document) and wait for delivery confirmation from the Node bridge.
    Returns a success message or an error string for the agent to report.
    When allow_contact_send is True, the recipient may be any phone/JID (e.g. a contact); otherwise only whitelisted.
    origin="owner" marks a message the person sent from the dashboard: it is stored under
    OWNER_SENDER and opens no reply window (the agent did not write to that number).
    user_scope_id, when given, is the scope the send is made for: only that scope's process
    sends it (`_process_for`), on both the in-process and the file-IPC path. Without one the
    send is the local admin's under the admin's own scope and name, or, naming anybody else,
    refused with NO_ACCOUNT_FOR_REQUEST (`config.messenger_lane_identity`).
    """
    ident = messenger_lane_identity(user_scope_id, username)
    if ident is None:
        logger.warning("WhatsApp: refused a send without an account for %r", username)
        return NO_ACCOUNT_FOR_REQUEST
    user_scope_id, username = ident
    # Voice/document need more time (TTS synthesis, file upload)
    if voice_path or document_path:
        timeout = max(timeout, 45.0)
    if not allow_contact_send and not _is_reply_allowed(username, chat_jid, user_scope_id=user_scope_id):
        return (
            "WhatsApp: Cannot send - this number is neither the registered main-user number, "
            "a contact who can reach the assistant, nor an open conversation. "
            "Register the number in Settings → Connections → WhatsApp, or address a contact with to_phone."
        )
    scope = str(user_scope_id or "").strip() or None
    _, proc = _process_for(username, scope)
    # While this process's bridge is down, nothing would read the Node's answer: the send
    # takes the file path, whose state says at once that the bridge is not running.
    use_external_ipc = not proc or proc.poll() is not None or not is_bridge_running()
    if use_external_ipc:
        state = _read_bridge_state()
        if not state.get("running"):
            return (
                "WhatsApp bridge is not running. Start it in Settings → Connections → WhatsApp (click Start)."
            )
        usernames = [str(u).strip() for u in (state.get("usernames") or []) if str(u).strip()]
        uname = (username or "").strip() or "admin"
        if usernames and uname not in usernames and len(usernames) != 1:
            return (
                "WhatsApp process for this user is not running. "
                "Try: Settings → Connections → WhatsApp → Stop, then Start. "
                "Ensure WhatsApp is linked (QR scanned) and your number is in the whitelist."
            )
        req_id = str(uuid.uuid4())
        response_path = _ipc_results_dir() / f"{req_id}.json"
        request_path = _ipc_requests_dir() / f"{time.time_ns()}_{req_id}.json"
        try:
            _write_json_atomic(
                request_path,
                {
                    "req_id": req_id,
                    "username": uname,
                    "chat_jid": chat_jid,
                    "text": text or "",
                    "voice_path": str(voice_path or ""),
                    "document_path": str(document_path or ""),
                    "response_path": str(response_path),
                    "origin": str(origin or ""),
                    "user_scope_id": str(scope or ""),
                },
            )
        except Exception as e:
            return f"Failed to enqueue WhatsApp message for bridge delivery: {e}"
        return _wait_for_external_send_result(
            response_path,
            timeout=timeout,
            voice_path=voice_path,
            document_path=document_path,
        )
    req_id = str(uuid.uuid4())
    result_queue: queue.Queue = queue.Queue()
    with _pending_sends_lock:
        _pending_sends[req_id] = result_queue
    try:
        _outgoing_queue.put((username, chat_jid, text or "", voice_path, req_id, document_path, None, origin, scope))
    except Exception as e:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        return f"Failed to enqueue message: {e}"
    try:
        success, error = result_queue.get(timeout=timeout)
        if success:
            return _sent_text(voice_path, document_path)
        return f"WhatsApp could not deliver the message: {error}"
    except queue.Empty:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        return SEND_UNCONFIRMED


def _read_user_process(
    username: str,
    user_scope_id: str,
    auth_dir: Path,
    proc: subprocess.Popen,
    stop: Optional[threading.Event] = None,
) -> None:
    """Read JSON lines from process stdout, handle events. allowed_phones are fetched per message so new Front Office contacts work without bridge restart.

    `stop` is the run's stop the process was spawned into. Once it is set, or the process is
    retired (`_retire`), the reader ends at the next line it sees without acting on it: a
    buffered `qr` line of an account's old process otherwise stopped the account's NEW one
    (`stop_account` acts on whatever is registered under the name)."""
    if stop is None:
        stop = _bridge_stop
    try:
        for line in proc.stdout:
            if stop.is_set() or _is_retired(proc):
                break
            line = (line or "").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] JSON decode error: {e}")
                except Exception:
                    pass
                continue
            typ = obj.get("type")
            if typ in ("message", "chats", "connected", "connection_closed", "lid_mappings", "history_messages"):
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] got type={typ!r}")
                except Exception:
                    pass
            try:
                _dispatch_bridge_event(username, user_scope_id, typ, obj, proc=proc)
            except Exception as e:
                logger.exception("WhatsApp bridge event handler failed for type=%s: %s", typ, e)
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] ERROR handling type={typ!r}: {e}")
                except Exception:
                    pass
    except Exception as e:
        logger.exception("WhatsApp bridge stdout read loop failed: %s", e)
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[Python] FATAL read loop: {type(e).__name__}: {e}")
        except Exception:
            pass


def _wa_flush(pending_key: str) -> None:
    """Debounce timer fired: flush accumulated messages for this chat to TaskQueue."""
    with _wa_pending_lock:
        rec = _wa_pending.pop(pending_key, None)
    if not rec:
        return
    body = rec.get("body", "").strip()
    if not body:
        return
    session_id = rec["session_id"]
    metadata = rec["metadata"]
    if isinstance(metadata, dict):
        metadata.setdefault("origin_channel", "whatsapp")
        metadata.setdefault("task_class", "interactive")
    username = rec["username"]
    from_jid = rec["from_jid"]
    voice_lang = rec.get("voice_lang")

    # Set voice_reply_pending for the *final* combined message (last voice_lang wins)
    if voice_lang:
        with _voice_reply_lock:
            _voice_reply_pending[f"{username}|{from_jid}"] = voice_lang

    tq = TaskQueue()
    tq.add(
        session_id=session_id,
        input_text=body,
        source="whatsapp",
        metadata=metadata,
    )
    try:
        from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
        parts = rec.get("parts", 1)
        log_whatsapp_inbound(f"DEBOUNCE_FLUSH session={session_id} user={username} parts={parts} body_len={len(body)}")
        log_whatsapp_qr(f"[inbound] DEBOUNCE_FLUSH session={session_id} parts={parts}")
    except Exception:
        pass
    logger.info("WhatsApp debounce flush: %s parts → session %s user %s", rec.get("parts", 1), session_id, username)


_contact_sync_last: Dict[str, float] = {}
CONTACT_SYNC_MIN_INTERVAL = 60.0


def _sync_contacts_from_chats(username: str, user_scope_id: str, chats: List[Dict[str, Any]], force: bool = False) -> Dict[str, int]:
    """Fold the chat list into the contact book (see contacts_store.sync_channel_contacts).

    Only direct chats with a phone number AND a name WhatsApp shows qualify: groups,
    newsletters, broadcasts and unresolved LIDs have no number, a number-only chat has no
    name, and the contact book is for people the user can address, not for every
    address that ever wrote. Throttled per user, because the Node re-emits the whole
    list on every change during a history sync."""
    now = time.time()
    if not force and now - _contact_sync_last.get(username, 0.0) < CONTACT_SYNC_MIN_INTERVAL:
        return {"created": 0, "linked": 0, "skipped": 0}
    _contact_sync_last[username] = now
    entries: List[Dict[str, Any]] = []
    for c in chats or []:
        if not isinstance(c, dict) or c.get("is_group"):
            continue
        jid = str(c.get("jid") or "")
        if not jid.endswith("@s.whatsapp.net"):
            continue
        phone = _to_e164_display(str(c.get("phone") or _jid_to_e164(jid) or ""))
        name = str(c.get("name") or "").strip()
        if not phone or not name or name == phone or name == jid or name == phone.lstrip("+"):
            continue
        entries.append({"endpoint": phone, "display_name": name, "last_seen_ts": c.get("last_ts") or None})
    if not entries:
        return {"created": 0, "linked": 0, "skipped": 0}
    try:
        from vaf.core.contacts_store import sync_channel_contacts
        result = sync_channel_contacts("whatsapp", entries, username, user_scope_id=user_scope_id)
    except Exception as e:
        logger.warning("WhatsApp: contact sync failed for %s: %s", username, e)
        return {"created": 0, "linked": 0, "skipped": 0}
    if result.get("created") or result.get("linked"):
        logger.info("WhatsApp: contact book synced for %s: %s", username, result)
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[contacts] synced from chat list: {result}")
        except Exception:
            pass
    return result


def _drop_self_number_from_whitelist(username: str, user_scope_id: str, self_phone: str) -> None:
    """Installs from the old model registered the linked number itself as the user's own
    number (the wizard did it automatically). That entry can never be right now: the linked
    account is the agent, and a message from it is dropped. Remove it once, say so."""
    try:
        cfg = Config.load()
        wc = cfg.get("whatsapp_config") or {}
        if not isinstance(wc, dict):
            return
        whitelist = list(wc.get("whitelist") or [])
        self_digits = _phone_digits_canonical(self_phone)
        keep = [
            e for e in whitelist
            if not (isinstance(e, dict) and _phone_digits_canonical(str(e.get("phone_number") or "")) == self_digits)
        ]
        if len(keep) == len(whitelist):
            return
        wc = dict(wc)
        wc["whitelist"] = keep
        cfg["whatsapp_config"] = wc
        Config.save(cfg)
        logger.warning("WhatsApp: removed the agent's own number %s from the whitelist for %s", self_phone, username)
        try:
            from vaf.core.log_helper import log_whatsapp_inbound
            log_whatsapp_inbound(f"WHITELIST removed agent's own number {self_phone} user={username}")
        except Exception:
            pass
        try:
            from vaf.core.user_notifications import append_notification
            append_notification(
                user_scope_id,
                kind="channel_reply",
                title="WhatsApp: your own number was removed from the whitelist",
                status="warning",
                summary=(
                    f"{self_phone} is the linked account, so it is the agent's number now. Register the "
                    "number you chat from in Settings → Connections → WhatsApp."
                ),
                channel="WhatsApp",
            )
        except Exception:
            pass
    except Exception as e:
        logger.warning("WhatsApp: whitelist cleanup failed for %s: %s", username, e)


def _payload_ts(obj: Dict[str, Any]) -> Optional[float]:
    """The message's own time from a bridge payload (seconds), or None for the arrival
    time: a backlog delivered after an outage keeps its order against a reply the person
    sent from the phone meanwhile."""
    ts = obj.get("ts")
    return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 else None


def _not_a_persons_chat(jid: str) -> bool:
    """A status post, a channel post, a broadcast list or a group is not a person's chat:
    nothing the person sends there is stored as a reply to anybody."""
    j = str(jid or "").strip().lower()
    return (not j or j == "status@broadcast" or j.endswith("@newsletter") or j.endswith("@broadcast")
            or j.endswith("@g.us"))


def _lid_map_e164(jid: str, user_scope_id: Optional[str]) -> str:
    """The number this account's LID map (`whatsapp_lid_map`) binds a JID to, or ""."""
    try:
        from vaf.core.contacts_store import whatsapp_lid_map
        e164 = str(whatsapp_lid_map(user_scope_id).get(str(jid)) or "").strip()
        return ("+" + e164) if e164 and not e164.startswith("+") else e164
    except Exception:
        return ""


_MEDIA_FAMILY = {"voice": "audio", "audio": "audio", "document": "document", "image": "image", "video": "video", "sticker": "sticker"}


def _already_stored_outbound(username: str, chat_id: str, body: str, ts: Optional[float], user_scope_id: Optional[str],
                             content_type: str = "text") -> bool:
    """Whether the store already holds this outbound message of the bridge's under another
    key: the send path stores a row the moment a message leaves (with no WhatsApp id yet,
    and with its own labels: "[Voice message]", "[Document] caption"), so a history batch
    that carries the same message under its id must not add a second row, nor relabel it.
    A text is the same text within ten minutes; a media message is the same family (voice,
    document, image) within ten minutes. Only asked for ids of the bridge's own shape: a
    phone-shaped id was never stored by the send path, so a reply from the phone that
    happens to repeat the agent's words is kept. Without a timestamp there is no window to
    match in, so nothing counts as already stored."""
    if ts is None:
        return False
    try:
        from vaf.core.channel_message_store import get_chat_messages
        want = " ".join(str(body or "").split())
        family = _MEDIA_FAMILY.get(str(content_type or "text").lower())
        for row in get_chat_messages(username, chat_id, limit=60, user_scope_id=user_scope_id):
            if row.get("direction") != "out":
                continue
            if ts is not None and abs(float(row.get("ts") or 0.0) - float(ts)) > 600:
                continue
            if " ".join(str(row.get("body") or "").split()) == want:
                return True
            if family and _MEDIA_FAMILY.get(str(row.get("content_type") or "").lower()) == family:
                return True
    except Exception:
        pass
    return False


def _sent_by_this_bridge(message_id: Any) -> bool:
    """Whether a message id has the shape the bridge's own sends carry: Baileys generates
    its ids as 3EB0 followed by hex, while a message the agent number's phone sent carries
    the phone's own shape. The history import tells the agent's sends from the person's by
    it; the live path needs no guess, the bridge knows its own echoes."""
    return str(message_id or "").upper().startswith("3EB0")


def _dispatch_bridge_event(username: str, user_scope_id: str, typ: str, obj: Dict[str, Any],
                           proc: Any = None) -> None:
    """Handle one JSON event from the bridge (pong, message, chats, etc.). `proc` is the
    process whose reader got the line; an event that ends a process (`qr`) acts only while it
    is still the account's registration."""
    if typ == "pong":
        connected = bool(obj.get("connected", False))
        with _connection_lock:
            _connection_status[username] = connected
            ev = _connection_events.get(username)
            if ev:
                ev.set()
        if not connected:
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                log_whatsapp_qr(f"[Python] pong connected=false → UI shows orange (bridge running, WhatsApp not connected)")
            except Exception:
                pass
    elif typ == "avatar":
        req_id = obj.get("req_id")
        with _avatar_results_lock:
            q = _avatar_results.pop(str(req_id), None) if req_id else None
        if q is not None:
            try:
                q.put(obj)
            except Exception:
                pass
    elif typ in ("send_result", "fetch_history_result", "resync_contacts_result"):
        # Both are "the command you queued under req_id has left (or not)": the same
        # per-request queue hands the answer to the waiting caller.
        req_id = obj.get("req_id")
        if req_id:
            _complete_send_request(str(req_id), bool(obj.get("success")), obj.get("error", ""))
    elif typ == "qr":
        # A stored login that cannot sign in without a new scan. It stops this account's own
        # process and nobody else's, and changes no switch, the local admin's included: the
        # account still wants WhatsApp, so the window says "switched on but not running ...
        # link again if the session expired", which is the cause; a switch turned off would
        # name the wrong one. Without its registration the reconnect worker leaves the account
        # alone (`Reconnector`), so no restart loop asks for a QR again and again; a whole
        # restart or a VAF start spawns it once, it asks for a QR and stops itself. It used to
        # stop the whole bridge and turn the global switch off, so one account's expired
        # session ended every account's WhatsApp.
        try:
            with _spawn_lock:
                # The line's own process only: a reader can pass its retirement check and be
                # dispatched after a restart replaced that process, and a stop by name would
                # then end the replacement.
                if proc is not None and not _is_registration(username, proc):
                    logger.info("WhatsApp: an expiry line of a replaced process of %s was ignored", username)
                    return
                logger.warning("WhatsApp session expired for %s – the process needs a QR but cannot show it. "
                               "Stopping this account's process.", username)
                if _stop_account_held(username, user_scope_id):
                    _account_stopped(username)
                # An expired login asks for a QR again on every start: nothing brings it back.
                _leave_run(username, user_scope_id)
        except Exception as e:
            logger.exception("Failed to stop WhatsApp on session expiry: %s", e)
    elif typ == "connected":
        with _connection_lock:
            _connection_status[username] = True
        self_phone = _to_e164_display(_jid_to_e164(str(obj.get("selfJid") or ""))) or (get_linked_phone(username) or "")
        if self_phone:
            _self_phone[username] = self_phone
            _drop_self_number_from_whitelist(username, user_scope_id, self_phone)
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[Python] connected → status=open for {username} agent_number={self_phone or '?'}")
        except Exception:
            pass
        logger.info("WhatsApp connected for user %s (agent number %s)", username, self_phone or "?")
    elif typ == "connection_closed":
        with _connection_lock:
            _connection_status[username] = False
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            code = obj.get("statusCode")
            log_whatsapp_qr(f"[Python] connection_closed statusCode={code} → UI will show orange")
        except Exception:
            pass
    elif typ == "history_messages":
        # Chat history from WhatsApp (messaging-history.set); store so inbox has full history
        try:
            from vaf.core.channel_message_store import OWNER_SENDER, append_message
            stored = 0
            for m in obj.get("messages") or []:
                chat_id = (m.get("chat_id") or "").strip()
                if not chat_id:
                    continue
                body = (m.get("body") or "").strip() or "<message>"
                direction = "out" if m.get("direction") == "out" else "in"
                ts_val = m.get("ts")
                ts_float = float(ts_val) if ts_val is not None else None
                # An outbound row the bridge did not send is the person's own (the agent
                # number's phone): labelled as theirs, so the inbox never credits it to the agent.
                # A message the store already holds (the send path stored it the moment it
                # left, the compose box under the person's label) is neither added again nor
                # relabelled: the import keeps what a row already says.
                ours = direction == "out" and _sent_by_this_bridge(m.get("message_id"))
                sender = None if direction == "in" or ours else OWNER_SENDER
                if ours and _already_stored_outbound(username, chat_id, body, ts_float, user_scope_id,
                                                     content_type=str(m.get("content_type") or "text")):
                    continue
                append_message(
                    username,
                    chat_id,
                    body,
                    direction=direction,
                    sender_jid=sender,
                    message_id=m.get("message_id"),
                    keep_existing=True,
                    content_type=m.get("content_type") or "text",
                    user_scope_id=user_scope_id,
                    ts=ts_float,
                )
                stored += 1
            if stored:
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] history_messages: stored {stored} to message store")
                except Exception:
                    pass
        except Exception as e:
            logger.warning("WhatsApp: store history_messages failed: %s", e)
    elif typ == "owner_sent":
        from_jid = (obj.get("from") or "").strip()
        ts = obj.get("ts")
        if from_jid and isinstance(ts, (int, float)) and ts > 0:
            try:
                cfg = Config.load()
                wc = cfg.get("whatsapp_config") or {}
                if not isinstance(wc, dict):
                    wc = {}
                else:
                    wc = dict(wc)
                owner_control = dict(wc.get("owner_control") or {})
                owner_control[from_jid] = int(ts)
                now = int(time.time())
                max_age = 24 * 3600
                owner_control = {k: v for k, v in owner_control.items() if (now - v) <= max_age}
                if len(owner_control) > 50:
                    by_ts = sorted(owner_control.items(), key=lambda x: -x[1])
                    owner_control = dict(by_ts[:50])
                wc["owner_control"] = owner_control
                cfg["whatsapp_config"] = wc
                Config.save(cfg)
                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"owner_sent chat={from_jid} → owner has control")
                    log_whatsapp_qr(f"[inbound] owner_sent chat={from_jid} → owner has control")
                except Exception:
                    pass
            except Exception as e:
                logger.warning("WhatsApp: failed to save owner_control: %s", e)
        # The person's own words, sent from the agent number's phone: stored as their row
        # (OWNER_SENDER), so the inbox shows the reply and reads the chat as answered by the
        # person, not by the agent. The echo of the bridge's own send never reaches here.
        body = str(obj.get("body") or "").strip()
        ctype = str(obj.get("content_type") or "text").strip().lower() or "text"
        if from_jid and body and ctype not in ("reaction", "other") and not _not_a_persons_chat(from_jid):
            try:
                from vaf.core.channel_message_store import OWNER_SENDER, append_message
                msg_ts = _payload_ts({"ts": obj.get("msg_ts")}) or _payload_ts(obj)
                # The same key the inbound path derives: the resolved number first, then the
                # configured LID map, then the JID's own digits; an unresolved @lid keeps the JID.
                raw = str(obj.get("fromE164") or "").strip() or _lid_map_e164(from_jid, user_scope_id) or _jid_to_e164(from_jid) or ""
                chat_id = (_to_e164_display(raw) if raw else "") or str(from_jid)
                append_message(username, chat_id, body, direction="out",
                               sender_jid=OWNER_SENDER, message_id=str(obj.get("message_id") or "") or None,
                               content_type=ctype, user_scope_id=user_scope_id, ts=msg_ts)
            except Exception as e:
                logger.warning("WhatsApp: failed to store the person's own message: %s", e)
    elif typ == "message":
        from_jid = obj.get("from") or obj.get("senderJid")
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[inbound] MESSAGE from={from_jid}")
        except Exception:
            pass
        try:
            config_phones, allowed_phones = _get_allowed_phones_for_user(username, user_scope_id)
        except Exception as e:
            logger.warning("WhatsApp: get_allowed_phones_for_user failed: %s", e)
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                log_whatsapp_qr(f"[inbound] ERROR get_allowed_phones: {e}")
            except Exception:
                pass
            return
        # A status update, a newsletter post or a broadcast list is not a sender that could
        # ever be paired; dropping it here keeps it out of the store, the activity and the
        # inbound log (live: a REJECT for status@broadcast every few minutes).
        _fj = str(from_jid or "")
        if _fj == "status@broadcast" or _fj.endswith("@newsletter") or _fj.endswith("@broadcast") or _fj.endswith("@g.us"):
            try:
                from vaf.core.log_helper import log_whatsapp_inbound
                log_whatsapp_inbound(f"SKIP not_a_person from={_fj}")
            except Exception:
                pass
            return
        if not allowed_phones:
            # Neither a registered number nor an allowed contact: unless this channel's
            # Inbound is open, nothing from here will be answered.
            logger.debug("WhatsApp: no registered number and no allowed contact for %s (inbound from %s)",
                         username, obj.get("from"))
        from_e164 = obj.get("fromE164")  # Resolved via Baileys lidMapping when @lid
        body = (obj.get("body") or "").strip()
        voice_path = obj.get("voice_path")
        # The linked account is the agent's own number. A message in its "message
        # yourself" chat is somebody typing on the agent's phone, not the owner talking
        # to the agent: dropped before it reaches the store, the activity or the queue.
        if obj.get("selfChat") is True:
            if voice_path:
                try:
                    Path(str(voice_path)).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                log_whatsapp_inbound(f"SELF_CHAT dropped from={from_jid} (linked account is the agent's number)")
                log_whatsapp_qr(f"[inbound] SELF_CHAT dropped from={from_jid}")
            except Exception:
                pass
            return
        voice_lang: Optional[str] = None
        was_voice = bool(voice_path)
        if voice_path and body == "<voice>":
            try:
                transcript, voice_lang = _transcribe_voice_file(voice_path, user_scope_id)
                body = transcript if transcript else "<media:audio>"
            except Exception as e:
                logger.warning("WhatsApp: transcription failed: %s", e)
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[inbound] ERROR transcribe: {e}")
                except Exception:
                    pass
                body = "<media:audio>"
        if not body:
            try:
                from vaf.core.log_helper import log_whatsapp_inbound
                log_whatsapp_inbound(f"SKIP no_body from={from_jid}")
            except Exception:
                pass
            return
        # Resolve an unresolved @lid through this account's LID map (the shared one plus its
        # own assignments, never another account's), so a known contact is still accepted
        # when the Node sends no fromE164.
        resolved_e164_from_config: Optional[str] = None
        if (from_jid or "").endswith("@lid") and not from_e164:
            resolved_e164_from_config = _lid_map_e164(str(from_jid), user_scope_id) or None
        # Store key: fromE164 when available (resolved @lid from Node); else the LID map;
        # else derived from the JID. An unresolved @lid keeps the JID as chat_id.
        raw = from_e164 or resolved_e164_from_config or _jid_to_e164(from_jid) or ""
        chat_id = _to_e164_display(raw) if raw else str(from_jid or "")
        if not chat_id:
            chat_id = str(from_jid or "")
        # The owner's own number, by JID, by fromE164, or through a manually mapped @lid. The
        # contact side is no longer a phone-set membership test: a set built from the flag
        # could say "allowed" while the record said "denied", and the set was read first, so
        # the denial never got a look. One record lookup below answers that half.
        explicit_allow = bool(config_phones) and (
            _allow_from_match(from_jid or "", config_phones)
            or (from_e164 and _allow_from_match(from_e164, config_phones))
            or (resolved_e164_from_config and _allow_from_match(resolved_e164_from_config, config_phones))
        )
        ingress_policy = Config.get("channel_ingress_policy")
        # ONE record lookup answers both halves: who this is, and what the owner decided about
        # them (allowed on every channel, denied on every channel, or nothing decided). The
        # phone-set detour is gone with the reply window: a set built from the flag could say
        # "allowed" while the record said "denied", and the set was consulted first, so the
        # denial was never read.
        access = None
        if not explicit_allow and raw:
            try:
                from vaf.core.contacts_store import contact_access, find_contact_by_channel
                access = contact_access(
                    find_contact_by_channel("whatsapp", chat_id, username, user_scope_id))
            except Exception:
                access = None
        if not explicit_allow and not raw:
            # An unresolved @lid carries no number, so nothing can match it and nothing can be
            # enrolled for it: the open channel does not apply until the LID is assigned to a
            # number (REJECT, with the note below). Refused here rather than by handing
            # evaluate_ingress a "denied" it never got from the book: nobody decided anything
            # about a sender the bridge cannot even name, and the reason is read by the log and
            # by whoever asks why a message went unanswered.
            policy_allowed, policy_reason = False, "not_paired"
        else:
            policy_allowed, policy_reason = evaluate_ingress(
                "whatsapp",
                ingress_policy,
                explicit_match=explicit_allow,
                access=access,
            )
        # An unresolved @lid is not a phone number, so no whitelist or contact can match it;
        # the note rides on the one REJECT line below instead of being a second line.
        lid_note = ""
        if (from_jid or "").endswith("@lid") and not from_e164 and not resolved_e164_from_config and not policy_allowed:
            lid_note = " unresolved @lid (not in whitelist/contacts; assign the LID to a number in the dashboard)"
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                log_whatsapp_qr(f"[inbound] REJECT unresolved @lid from={from_jid} (not in whitelist/contacts)")
            except Exception:
                pass
        if not policy_allowed:
            from_digits = _phone_digits_canonical(from_jid or "") or (_phone_digits_canonical(from_e164 or "") if from_e164 else "")
            try:
                from vaf.core.log_helper import log_channel_inbound, log_whatsapp_qr
                log_whatsapp_qr(
                    f"[inbound] REJECT from={from_jid} from_digits={from_digits or '?'} "
                    f"access={access or 'undecided'} reason={policy_reason}"
                )
            except Exception:
                pass
            sender_for_throttle = str(from_jid or from_e164 or "")
            # One REJECT line per sender and throttle window, written with debug logging off
            # too: the sender gets no reply, so this is the only trace of the attempt. It is
            # the channel's log and not a security event, because a stranger writing to the
            # agent's number is everyday traffic here (the message is kept for the owner
            # below); as a security event it lit the alert dot on every such message.
            if should_log_unauthorized("whatsapp", sender_for_throttle, ingress_policy):
                logger.warning(
                    "WhatsApp: dropped unauthorized inbound from=%s reason=%s explicit=%s access=%s",
                    from_jid,
                    policy_reason,
                    explicit_allow,
                    access or "undecided",
                )
                try:
                    log_channel_inbound(
                        "whatsapp",
                        f"REJECT from={from_jid} reason={policy_reason} access={access or 'undecided'}{lid_note}",
                        always=True,
                    )
                except Exception:
                    pass
            # The policy decides whether the AGENT reacts, not whether the OWNER may read
            # their own number's mail: the message is kept in the store like any other
            # inbound (the dashboard shows the chat as read-only, the inbox tools read it,
            # the history sync stores the same senders anyway), and nothing runs on it.
            # Under the same key the accept path uses, so the chat sits under the phone
            # number and the contact's name, not under a bare @lid.
            _append_chat_activity(chat_id, user_scope_id, "in")
            try:
                from vaf.core.channel_message_store import append_message
                append_message(
                    username, chat_id, body, direction="in", sender_jid=from_jid,
                    chat_name=(str(obj.get("pushName") or "").strip() or None),
                    message_id=obj.get("messageId") or obj.get("message_id"),
                    content_type="voice" if was_voice else "text",
                    user_scope_id=user_scope_id,
                    ts=_payload_ts(obj),
                )
            except Exception:
                pass
            return
        try:
            from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
            log_whatsapp_inbound(f"ACCEPT from={from_jid} reason={policy_reason} body_len={len(body)}")
            log_whatsapp_qr(f"[inbound] ACCEPT from={from_jid} reason={policy_reason} body_len={len(body)}")
            if (from_jid or "").endswith("@lid") and not from_e164:
                log_whatsapp_qr(f"[inbound] ACCEPT unresolved @lid (LID→E.164 not available); reply will go to this chat")
        except Exception:
            pass
        if explicit_allow:
            # The owner's endpoint for proactive sends is the registered main-user number
            # only; a contact's message must never become "where the owner is".
            save_whatsapp_chat_jid(user_scope_id, username, from_jid)
        if (policy_reason == "front_office_open" and raw and chat_id.startswith("+")
                and front_office_open("whatsapp", ingress_policy)):
            # A sender the open Front Office let in becomes a contact with NO decision on the
            # record: the open channel is what answers them, and a record that carried a
            # permission would outlive the switch being turned off. The owner sees them in the
            # book and the WhatsApp window and decides there. Recorded like a pairing; the
            # bridge never repeats it for a record that exists.
            #
            # `front_office_open` and not the policy flag the reason came from: with
            # `inbound_to_agent` off this bridge hands NOTHING to the agent (the gate below),
            # so the door answers nobody however the flag reads. Enrolling there wrote a record
            # and a security event saying the open Front Office had admitted somebody the agent
            # would never answer. The ingress verdict itself is left alone on purpose: the
            # message is still stored for the owner and still logged as accepted, which is what
            # happened.
            try:
                from vaf.core.contacts_store import enrol_front_office_contact, find_contact_by_channel
                from vaf.core.security_events import log_security_event
                if find_contact_by_channel("whatsapp", chat_id, username, user_scope_id) is None:
                    _new = enrol_front_office_contact(
                        "whatsapp", chat_id, str(obj.get("pushName") or ""), username, user_scope_id)
                    log_security_event("contact_access_changed", channel="whatsapp", username=username,
                                       path=str(_new.get("id") or ""),
                                       detail=f"added by the open Front Office: {_new.get('name') or chat_id}")
            except Exception:
                pass
        if raw:
            resolved_digits = _normalize_phone(raw)
        elif (from_jid or "").endswith("@lid"):
            resolved_digits = (from_jid or "").split("@")[0].strip() or "lid"
        else:
            resolved_digits = ""
        # Persist LID→E.164 so dashboard can show this chat under the contact's phone (Web UI session/history).
        # GUARD: only store genuine LIDs - a real LID has different digits than the phone number.
        # If lid_digits == e164_digits it means Node sent phone@lid (not a real privacy LID) → skip.
        if from_e164 and (from_jid or "").endswith("@lid"):
            try:
                _lid_digits = "".join(c for c in (from_jid or "").split("@")[0] if c.isdigit())
                _e164_digits = "".join(c for c in (from_e164 or "") if c.isdigit())
                _is_genuine_lid = bool(_lid_digits and _e164_digits and _lid_digits != _e164_digits)
                if _is_genuine_lid:
                    cfg = Config.load()
                    wc = cfg.get("whatsapp_config") or {}
                    if isinstance(wc, dict):
                        wc = dict(wc)
                        lid_map = dict(wc.get("lid_to_e164") or {})
                        lid_map[str(from_jid)] = _to_e164_display(from_e164)
                        wc["lid_to_e164"] = lid_map
                        cfg["whatsapp_config"] = wc
                        Config.save(cfg)
            except Exception:
                pass
        _append_chat_activity(chat_id, user_scope_id, "in")
        try:
            from vaf.core.channel_message_store import append_message
            # chat_name: the name WhatsApp shows for this person (phone contact, business
            # name or their own push name), so the list can show it like WhatsApp Web.
            _push = str(obj.get("pushName") or "").strip()
            if _push and raw and chat_id.startswith("+"):
                try:
                    from vaf.core.contacts_store import sync_channel_contacts
                    sync_channel_contacts("whatsapp", [{"endpoint": chat_id, "display_name": _push, "last_seen_ts": time.time()}],
                                          username, user_scope_id=user_scope_id)
                except Exception:
                    pass
            append_message(
                username, chat_id, body, direction="in", sender_jid=from_jid,
                chat_name=(str(obj.get("pushName") or "").strip() or None),
                message_id=obj.get("messageId") or obj.get("message_id"),
                content_type="voice" if was_voice else "text",
                user_scope_id=user_scope_id,
                ts=_payload_ts(obj),
            )
        except Exception:
            pass
        whatsapp_config = Config.get("whatsapp_config") or {}
        from vaf.core.messaging_connections import whatsapp_inbound_to_agent
        inbound_to_agent = whatsapp_inbound_to_agent()
        if inbound_to_agent:
            owner_control = (whatsapp_config.get("owner_control") or {}) if isinstance(whatsapp_config, dict) else {}
            last_owner_ts = owner_control.get(from_jid) if from_jid else None
            if last_owner_ts is not None and (time.time() - last_owner_ts) < 600:
                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"owner_control skip from={from_jid} (owner has control, 10 min not elapsed)")
                    log_whatsapp_qr(f"[inbound] owner_control: skip reply from={from_jid} (owner has control, 10 min not elapsed)")
                except Exception:
                    pass
                logger.info("WhatsApp: skip agent reply for %s (owner has control, 10 min not elapsed)", from_jid)
            else:
                session_id = whatsapp_session_id(username, resolved_digits, fallback="unknown")
                # Only the registered main-user number is the owner; a contact and a
                # reply-window sender both land in Front Office.
                from_contact = policy_reason != "explicit_pair"
                metadata: Dict[str, Any] = {
                    "user_scope_id": user_scope_id,
                    "username": username,
                    "whatsapp_chat_jid": from_jid,
                    "ingress_reason": policy_reason,
                }
                if from_contact:
                    metadata["from_contact"] = True
                    metadata["chat_label"] = _chat_label(chat_id, str(obj.get("pushName") or ""), username, user_scope_id)
                if voice_lang:
                    metadata["voice_lang"] = voice_lang

                # --- 7-second debounce (like Telegram 5s rule) ---
                # Accumulate follow-up messages for WA_DEBOUNCE_SECONDS, then flush all at once.
                pending_key = f"{username}|{from_jid}"
                with _wa_pending_lock:
                    existing = _wa_pending.get(pending_key)
                    if existing:
                        # Reset timer: cancel old, append text
                        old_timer: Optional[threading.Timer] = existing.get("timer")
                        if old_timer is not None:
                            old_timer.cancel()
                        sep = "\n" if existing["body"] else ""
                        existing["body"] = existing["body"] + sep + body
                        existing["parts"] = existing.get("parts", 1) + 1
                        # Last voice_lang wins (so reply is in the language of the most recent voice msg)
                        if voice_lang:
                            existing["voice_lang"] = voice_lang
                            existing["metadata"]["voice_lang"] = voice_lang
                        action = "DEBOUNCE_RESET"
                        parts = existing["parts"]
                    else:
                        # First message for this chat: create new record
                        _wa_pending[pending_key] = {
                            "session_id": session_id,
                            "username": username,
                            "from_jid": from_jid,
                            "body": body,
                            "parts": 1,
                            "voice_lang": voice_lang,
                            "metadata": metadata,
                            "timer": None,  # set below
                        }
                        existing = _wa_pending[pending_key]
                        action = "DEBOUNCE_START"
                        parts = 1

                    # Start/restart the flush timer (outside lock scope would be cleaner but
                    # we assign back to existing dict which is already in the map, so fine here)
                    new_timer = threading.Timer(WA_DEBOUNCE_SECONDS, _wa_flush, args=[pending_key])
                    new_timer.daemon = True
                    new_timer.start()
                    existing["timer"] = new_timer

                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"{action} session={session_id} user={username} parts={parts} body_len={len(body)}")
                    log_whatsapp_qr(f"[inbound] {action} session={session_id} parts={parts} from={from_jid}")
                except Exception:
                    pass
                logger.info("WhatsApp debounce %s: from=%s parts=%s user=%s (flush in %ss)", action, from_jid, parts, username, WA_DEBOUNCE_SECONDS)
        else:
            logger.debug("WhatsApp inbound not forwarded to agent (inbound_to_agent=false); user still reachable for sends.")
    elif typ == "chats":
        chats = obj.get("chats")
        if isinstance(chats, list):
            with _chat_lists_lock:
                _chat_lists[username] = chats
                _chat_lists_owner[username] = str(user_scope_id or "")
            ev = _chat_list_events.get(username)
            if ev:
                ev.set()
            _sync_contacts_from_chats(username, user_scope_id, chats)
    elif typ == "contacts":
        contacts = obj.get("contacts")
        if isinstance(contacts, list):
            names: Dict[str, Dict[str, str]] = {}
            for c in contacts:
                if not isinstance(c, dict):
                    continue
                digits = _phone_digits_canonical(str(c.get("e164") or ""))
                name = str(c.get("name") or "").strip()
                if digits and name:
                    names[digits] = {"name": name, "source": str(c.get("source") or ""), "e164": _to_e164_display(str(c.get("e164") or ""))}
            with _contact_names_lock:
                _contact_names[username] = names
                _contact_names_owner[username] = str(user_scope_id or "")
                ev = _contact_names_events.get(username)
                if ev:
                    ev.set()
            # The same names feed the contact book: a named number is a person, chat or no chat.
            try:
                from vaf.core.contacts_store import sync_channel_contacts
                entries = [{"endpoint": v["e164"], "display_name": v["name"]} for v in names.values() if v.get("e164")]
                if entries:
                    result = sync_channel_contacts("whatsapp", entries, username, user_scope_id=user_scope_id)
                    if result.get("created") or result.get("linked"):
                        logger.info("WhatsApp: contact book synced from names for %s: %s", username, result)
            except Exception as e:
                logger.warning("WhatsApp: contact sync from names failed for %s: %s", username, e)
    elif typ == "lid_mappings":
        mappings = obj.get("mappings")
        if isinstance(mappings, list):
            with _lid_mappings_lock:
                _lid_mappings[username] = [{"lid": str(m.get("lid", "")), "e164": str(m.get("e164", "")).strip() or ""} for m in mappings if m.get("lid")]
                _lid_mappings_owner[username] = str(user_scope_id or "")
            ev = _lid_mappings_events.get(username)
            if ev:
                ev.set()
            with _connection_lock:
                _connection_status[username] = True
                conn_ev = _connection_events.get(username)
                if conn_ev:
                    conn_ev.set()
    elif typ == "error":
        err_msg = obj.get("message", "")
        logger.warning("WhatsApp error for %s: %s", username, err_msg)
        try:
            from vaf.core.log_helper import log_whatsapp_reply, log_whatsapp_qr
            log_whatsapp_reply(f"ERROR username={username} msg={err_msg}")
            log_whatsapp_qr(f"[Python] bridge error: {err_msg}")
        except Exception:
            pass
        with _connection_lock:
            _connection_status[username] = False


def _forward_bridge_stderr(username: str, proc: subprocess.Popen,
                           stop: Optional[threading.Event] = None) -> None:
    """Read bridge stderr and append to whatsapp_qr.log for debugging connection issues. Ends
    like `_read_user_process`, on the run's stop or the process's retirement."""
    if stop is None:
        stop = _bridge_stop
    try:
        from vaf.core.log_helper import log_whatsapp_qr
        for line in (proc.stderr or []):
            if stop.is_set() or _is_retired(proc):
                break
            s = (line or "").strip()
            if s:
                log_whatsapp_qr(f"[bridge/{username}] {s}")
                logger.debug("[wa-bridge] %s", s)
    except Exception:
        pass


def _chat_sync_loop(stop: Optional[threading.Event] = None) -> None:
    """Periodically request full chat list from Node (getChats) so _chat_lists stays in sync.
    Runs until its run's `stop` (the one current at entry without it). It waits on that Event,
    never in sleep steps that look at the module's: a loop asleep in its warm-up through a
    restart otherwise woke into the next run and ran next to that run's own loop."""
    if stop is None:
        stop = _bridge_stop
    interval = 600  # default 10 min
    try:
        wc = Config.get("whatsapp_config") or {}
        if isinstance(wc, dict) and "chat_sync_interval_sec" in wc:
            interval = int(wc.get("chat_sync_interval_sec") or 0)
        if interval <= 0:
            return
    except Exception:
        pass
    # Give bridge time to start and populate _processes
    if stop.wait(60):
        return
    while not stop.is_set():
        with _process_lock:
            registered = [(name, _scope_by_user.get(name)) for name in _processes]
        for username, scope in registered:
            if stop.is_set():
                return
            _request_chats(username, scope)
        if stop.wait(interval):
            return
    logger.debug("WhatsApp chat sync loop exited")


def _registration(username: str) -> Tuple[Optional[subprocess.Popen], str]:
    """(process registered under exactly this name, alive or not, or None; the scope it was
    started for, stripped). The spawn gate's and the reconnect worker's question (is this
    account registered, and as whom?), never a lane's (which process acts for this account?,
    `_process_for`): it acts on no process."""
    with _process_lock:
        return _processes.get(username), str(_scope_by_user.get(username) or "").strip()


def _registered_live(username: str) -> Optional[subprocess.Popen]:
    """The live process registered under exactly this name, or None: the spawn gate's question
    (may this account get a process?), never a lane's (which process acts for this account?,
    `_process_for`). The one-process fallback has no place here: whether a spawn is due must
    not depend on how many other accounts run."""
    current, _ = _registration(username)
    return current if current is not None and current.poll() is None else None


def _registered_alive(username: str) -> bool:
    return _registered_live(username) is not None


def _blocked_by_previous(username: str, user_scope_id: str) -> bool:
    """Whether a live process stands in the way of a start: one registered under this name that
    VAF tried to end and could not (retired, a survivor: its readers stopped), one registered
    under it for another scope (an account that reuses a deleted account's name), or, with
    nothing registered, a Node VAF started on this account's login for something else (a QR
    link). None is this start's process, and a second Node next to it would share its login.
    A spawn that races past this check is refused by `spawn_node_bridge` itself (`LoginInUse`)
    and answers START_FAILED."""
    current = _registered_live(username)
    if current is None:
        return login_in_use(get_whatsapp_auth_dir((username or "").strip()))
    with _process_lock:
        scope = str(_scope_by_user.get(username) or "").strip()
        return _is_retired(current) or scope != str(user_scope_id or "").strip()


def _no_process_reason(username: str, user_scope_id: Optional[str]) -> str:
    """What a send for an account without a process to act through answers: PREVIOUS_NOT_ENDED
    when the account's own process is a survivor (registered for that scope, alive, retired),
    else NO_PROCESS_FOR_ACCOUNT, which would tell that account to link a WhatsApp it has."""
    current = _registered_live(username)
    if current is None or not _is_retired(current):
        return NO_PROCESS_FOR_ACCOUNT
    with _process_lock:
        scope = str(_scope_by_user.get(username) or "").strip()
    return PREVIOUS_NOT_ENDED if scope == str(user_scope_id or "").strip() else NO_PROCESS_FOR_ACCOUNT


def _spawn_account(user_scope_id: str, username: str, auth_dir: Path) -> bool:
    """Spawn one account's Node process into the running bridge and register it. The caller
    holds `_spawn_lock`. Refused (False, nothing spawned) once the bridge stops taking
    processes; True without a spawn when the account's own process is alive already, so two
    starts of one account never run two Baileys sockets on one login."""
    if _bridge_stop.is_set() or not _spawns_open:
        return False
    if _blocked_by_previous(username, user_scope_id):
        return False
    if _registered_alive(username):
        return True
    if not _account_may_start(user_scope_id):
        logger.warning("WhatsApp: %s not started, the account was deactivated or deleted or its switch is off",
                       username)
        return False
    proc = _run_user_process(username, auth_dir)
    if not proc:
        return False
    with _process_lock:
        replaced = _processes.pop(username, None)
        if replaced is not None:
            # Dead already, but its reader may still hold a line it has not acted on.
            _retire(replaced)
        _processes[username] = proc
        _scope_by_user[username] = str(user_scope_id)
    _run_outcome.members.add((username, _scope_key(user_scope_id)))
    _write_bridge_state(True)
    # The readers watch the stop of the run they were spawned into: spawns are open, so the
    # module's Event is that run's.
    stop = _bridge_stop
    threading.Thread(
        target=_read_user_process,
        args=(username, user_scope_id, auth_dir, proc, stop),
        daemon=True,
    ).start()
    threading.Thread(
        target=_forward_bridge_stderr,
        args=(username, proc, stop),
        daemon=True,
    ).start()
    return True


# How long `_terminate` waits after the terminate, and again after the kill.
TERMINATE_WAIT = 5.0


def _terminate(proc: subprocess.Popen) -> bool:
    """End a Node process: terminate, then kill after TERMINATE_WAIT seconds, and wait for it
    either way. False when it still runs after the kill, so no caller reports a stop that did
    not happen or starts a second process on the same login next to it."""
    try:
        proc.terminate()
        proc.wait(timeout=TERMINATE_WAIT)
        return True
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=TERMINATE_WAIT)
        return True
    except Exception:
        return proc.poll() is not None


def _resolve_pending(message: str) -> None:
    """Answer every account start still waiting on this bridge thread. Caller holds
    `_spawn_lock`."""
    for pending in _pending_starts.values():
        pending.resolve(False, START_CANCELLED if pending.cancelled else message)
    _pending_starts.clear()


def _run_bridge(only: Optional[str] = None, stop: Optional[threading.Event] = None,
                outcome: Optional[_RunOutcome] = None) -> None:
    """Main bridge loop: start processes for each user (or for the account `only` alone),
    register callback, wait for the run's `stop` (a private Event without one). However the
    run ends (stopped, nobody to run, or a failure) it ends through `_end_run`: one end path,
    so no way out leaves the run's loops, processes or state file behind."""
    run_stop = stop if stop is not None else threading.Event()
    run_outcome = outcome if outcome is not None else _RunOutcome()
    try:
        _run_bridge_until_stopped(only, run_stop, run_outcome)
    except Exception:
        run_outcome.failed = True
        logger.exception("WhatsApp bridge thread failed; its processes are stopped")
    finally:
        _end_run(run_stop, requested=run_stop.is_set())
        run_outcome.settled.set()


def _run_bridge_until_stopped(only: Optional[str], stop: threading.Event,
                              outcome: Optional[_RunOutcome] = None) -> None:
    if outcome is None:
        outcome = _RunOutcome()
    global _spawns_open
    # Owner numbers older versions filed under an admin's own scope move to the account they
    # were registered for, before any process reads the whitelist. Here because every lane
    # that acts on a number needs a running bridge, and off the event loop because the
    # account directory behind it is a database query. It never stops the bridge.
    try:
        from vaf.core.channel_pairing import refile_whatsapp_owner_numbers
        refile_whatsapp_owner_numbers()
    except Exception:
        logger.warning("WhatsApp: owner-number correction skipped", exc_info=True)
    users = _get_users_to_run()
    if only is not None:
        users = [u for u in users if u[1] == only]
    with _spawn_lock:
        # Whom the run was published for, as that stands now: an account stopped on request
        # since the publication (`_leave_run`) is not started by this spawn phase. Read again
        # before each spawn below, under the same lock as the stop.
        users = [u for u in users if (u[1], _scope_key(u[0])) in outcome.members]
        # An account start that raced this one may already have a process here: then the
        # bridge stays up for it.
        with _process_lock:
            nobody = not any(p.poll() is None for p in _processes.values())
        if not users and nobody:
            # Closed here, under the same lock as the check, so no start spawns into a run
            # that is about to end.
            _spawns_open = False
            _resolve_pending(NOT_STARTABLE)
            logger.info("WhatsApp bridge: no users with linked auth, exiting")
            return

    set_whatsapp_reply_callback(_enqueue_reply)

    for user_scope_id, username, auth_dir in users:
        with _spawn_lock:
            pending = _pending_starts.pop(username, None)
            if pending is not None and pending.cancelled:
                pending.resolve(False, START_CANCELLED)
                continue
            if (username, _scope_key(user_scope_id)) not in outcome.members:
                # Stopped on request after the users were read.
                if pending is not None:
                    pending.resolve(False, START_CANCELLED)
                continue
            try:
                blocked = _blocked_by_previous(username, user_scope_id)
                ok = _spawn_account(user_scope_id, username, auth_dir)
            except Exception:
                # Out of `_pending_starts` already, so the end's answer would not reach it:
                # answered here, before the failure ends the run.
                if pending is not None:
                    pending.resolve(False, START_FAILED)
                raise
            if blocked and not ok:
                # Not when the previous process ended between the check and the spawn: then
                # the account did start.
                outcome.skipped.append(username)
                logger.error("WhatsApp: %s not started, its previous process has not ended", username)
            if pending is not None:
                pending.resolve(ok, ACCOUNT_STARTED if ok else PREVIOUS_NOT_ENDED if blocked else
                                BRIDGE_STOPPING if stop.is_set() else
                                NOT_STARTABLE if not _account_may_start(user_scope_id) else START_FAILED)
    with _spawn_lock:
        # A start for an account this bridge did not find to run (switched off or unlinked
        # in the meantime) is answered now rather than left to its timeout.
        _resolve_pending(NOT_STARTABLE)
    outcome.settled.set()

    stop.wait()


def _end_run(stop: threading.Event, requested: bool) -> None:
    """The one end of a bridge run, after a stop, with nobody to run, or after a failure. Under
    `_spawn_lock`: spawns close, every waiting start is answered and cleared (a waiter left
    behind would time out, and its cancelled entry would make the next bridge skip that
    account), the run's `stop` is set (its
    sender loop, chat-sync loop and readers end on it), the reply callback goes, every
    registered process is retired and terminated, and the state file says not running.

    A process that survives the kill stays registered (retired), so the next start finds it
    and puts no second Node on its login. Measured before, when a failure skipped all of this:
    the state file kept saying running, the processes spawned before the failure stayed
    registered with their readers acting, and the sender loop kept polling the request folder
    next to the next run's, so one request file went out twice."""
    global _spawns_open, _terminating
    with _spawn_lock:
        _spawns_open = False
        _resolve_pending(BRIDGE_STOPPING if requested else START_FAILED)
        stop.set()
        set_whatsapp_reply_callback(None)
        with _process_lock:
            running = [(name, proc, _scope_by_user.get(name)) for name, proc in _processes.items()]
            _processes.clear()
            _scope_by_user.clear()
            _terminating += len(running)
        for username, proc, scope in running:
            _retire(proc)
            ended = _terminate(proc)
            with _process_lock:
                _terminating -= 1
                if not ended:
                    # It stays registered, so the next start finds it and puts no second Node
                    # on its login; retired, its readers stop and no lane acts through it.
                    _processes.setdefault(username, proc)
                    _scope_by_user.setdefault(username, str(scope or ""))
            if not ended:
                logger.error("WhatsApp: the process of %s did not end on the bridge's stop", username)
        _write_bridge_state(False)


def anyone_to_run() -> bool:
    """Whether any account is linked and has its own switch on (`_get_users_to_run`)."""
    return bool(_get_users_to_run())


def start_bridge(only: Optional[str] = None) -> bool:
    """Start the WhatsApp bridge. Returns True if started (or already running). With `only`,
    the bridge comes up with that account's process alone; nobody else's starts. Without it,
    False when no account is linked and switched on (`anyone_to_run`), before Node is looked
    for or the dependencies are installed: a VAF where nobody uses WhatsApp starts nothing and
    logs no missing Node. No global switch gates it: each account's own switch decides its
    own process (module docstring)."""
    return _start_bridge(only) in ("running", "started")


def _start_bridge(only: Optional[str] = None, *, recover: Optional["_RunOutcome"] = None) -> str:
    """`start_bridge`'s work, answering why: "running" (already), "started", "nobody" (no
    account to run), "cancelled" (a recovery a whole Stop overtook) or "failed" (Node, its
    dependencies, the threads).

    A crash recovery (`Reconnector`) passes the failed run's outcome as `recover`. The new run
    is for that run's CURRENT members that are still runnable (linked, switched on, standing,
    read again right before the publishing), the members read under `_spawn_lock` in the same
    step that publishes it, so an account whose own Stop landed at any point before
    (`_leave_run` takes that lock too) is not started; it is published only while `recover` is
    still the current run (`_leave_run` works on the current one, so a recovery of a run another
    run replaced would miss the Stops sent during that other run) and no whole Stop came since
    the failed run started (`_whole_stops`, read under `_marks_lock` with the publishing).
    A recovery overtaken that way answers "cancelled" or "nobody", also when its preflight
    failed meanwhile: it was no try. Measured before, with a copy of the members handed over: a
    Stop between the copy and the start was undone."""
    global _bridge_thread, _sender_thread, _chat_sync_thread, _outgoing_queue, _bridge_stop, _spawns_open
    global _run_outcome

    with _lifecycle_lock:
        # `_run_outcome` is replaced only here, under this lock: a recovery of a run that is no
        # longer the current one is not one, and an explicit Start that replaced it is not
        # this recovery's success either.
        if recover is not None and recover is not _run_outcome:
            return "cancelled"
        if _bridge_thread is not None and _bridge_thread.is_alive():
            return "running"
        # Whom this run is for: its members from the start, so a run that fails before its
        # spawn phase is still recovered for them (`_RunOutcome.members`).
        intended = {(name, _scope_key(scope)) for scope, name, _ in _get_users_to_run()
                    if only is None or name == only}
        if only is None and not intended:
            logger.info("WhatsApp bridge not started: %s", NOBODY_TO_RUN)
            return "nobody"
        whatsapp_config = Config.get("whatsapp_config") or {}
        if not isinstance(whatsapp_config, dict):
            whatsapp_config = {}
        def failed(*log_args) -> str:
            # A recovery that a Stop or the end of every member's WhatsApp overtook during its
            # preflight was no try, whatever the preflight found.
            overtaken = _recovery_overtaken(recover) if recover is not None else None
            if overtaken is not None:
                return overtaken
            logger.error(*log_args)
            return "failed"

        if not _node_path():
            return failed("Node.js not found. Install Node >= 18 for WhatsApp.")
        if not _wa_bridge_path().exists():
            return failed("wa-bridge.js not found at %s", _wa_bridge_path())

        deps_ok, deps_msg = ensure_bridge_deps()
        if not deps_ok:
            return failed("WhatsApp bridge not started: %s", deps_msg)

        # A new stop per run, never a cleared one: every thread of this run watches its own,
        # so a thread of an earlier run that missed its stop cannot wake into this one.
        run_stop = threading.Event()
        run_queue: queue.Queue = queue.Queue()
        run_outcome = _RunOutcome()
        _ensure_ipc_dirs()
        if recover is not None:
            # Who is runnable now, not when the pass looked: a switch turned off or an account
            # unlinked during the preflight takes its member out (a change after the
            # publication meets the spawn gate).
            intended &= _runnable_pairs()
        with _spawn_lock:
            if recover is not None:
                if recover is not _run_outcome:
                    return "cancelled"
                intended &= recover.members
                if not intended:
                    logger.info("WhatsApp: recovery not started: %s", NOBODY_TO_RUN)
                    return "nobody"
            run_outcome.members.update(intended)
            with _marks_lock:
                if recover is not None and _whole_stops != recover.started_after:
                    logger.info("WhatsApp: recovery not started, the bridge was stopped meanwhile")
                    return "cancelled"
                _bridge_stop = run_stop
                run_outcome.started_after = _whole_stops
                # With the stop, in the step a whole Stop reads both: its end marker goes to
                # the run it stopped, never to this one's queue.
                _outgoing_queue = run_queue
            _run_outcome = run_outcome
            _spawns_open = True
            # Before the bridge thread starts, so its end's False is the last word
            # (`_write_bridge_state`).
            _write_bridge_state(True)
        try:
            _sender_thread = threading.Thread(target=_sender_loop, kwargs={"q": run_queue, "stop": run_stop},
                                              daemon=True)
            _sender_thread.start()
            _bridge_thread = threading.Thread(target=_run_bridge,
                                              kwargs={"only": only, "stop": run_stop, "outcome": run_outcome},
                                              daemon=True)
            _bridge_thread.start()
        except Exception:
            # The run is published (running on disk, spawns open) but has no bridge thread to
            # end it: ended here instead, through the same end as every run, so no send waits
            # on it, no start waits on its spawn, and a process a start into it registered
            # meanwhile is retired and terminated rather than left without readers.
            logger.exception("WhatsApp bridge could not start its threads")
            if recover is not None:
                # The recovery's intent stays: its members, minus any Stop that landed meanwhile,
                # and the whole-Stop count it was published under. The next pass tries again
                # after its cooldown while tries are left.
                run_outcome.failed = True
            _end_run(run_stop, requested=False)
            run_outcome.settled.set()
            _bridge_thread = None
            return "failed"
        # Periodic chat list sync (default every 10 min) so bot has latest chats
        try:
            sync_interval = int((whatsapp_config.get("chat_sync_interval_sec") or 600))
            if sync_interval > 0:
                _chat_sync_thread = threading.Thread(target=_chat_sync_loop, kwargs={"stop": run_stop},
                                                     daemon=True)
                _chat_sync_thread.start()
                logger.info("WhatsApp chat sync started (interval=%ds)", sync_interval)
        except Exception:
            pass
        logger.info("WhatsApp bridge started%s", f" for {only}" if only is not None else "")
        return "started"


def _runnable_pairs() -> set:
    """(name, scope key) of every account linked and switched on now (`_get_users_to_run`)."""
    return {(name, _scope_key(scope)) for scope, name, _ in _get_users_to_run()}


def _recovery_overtaken(recover: "_RunOutcome") -> Optional[str]:
    """"cancelled" when the recovery of `recover` no longer stands (another run replaced it, or
    a whole Stop came since it started), "nobody" when none of its members is runnable any
    more, else None."""
    runnable = _runnable_pairs()
    with _spawn_lock:
        if recover is not _run_outcome:
            return "cancelled"
        with _marks_lock:
            if _whole_stops != recover.started_after:
                return "cancelled"
        if not (runnable & recover.members):
            return "nobody"
    return None


def stop_bridge() -> None:
    """Request bridge stop. Counted (`_whole_stops`) in one step with setting the current run's
    stop, so a crash recovery whose start is under way either finds the count changed and
    publishes nothing, or is the run this stop ends."""
    global _whole_stops
    with _marks_lock:
        _whole_stops += 1
        _bridge_stop.set()
        # The queue of the run this stop ends, read in the same step: a run published right
        # after must not get its end marker (its sender loop would end on it).
        stopped_queue = _outgoing_queue
    if stopped_queue is not None:
        try:
            stopped_queue.put(None)
        except Exception:
            pass
    _write_bridge_state(False)
    logger.info("WhatsApp bridge stop requested")


def _shutdown_deadline() -> float:
    """How long the bridge's shutdown may take: both waits of `_terminate` for every process
    still registered or still being terminated by the shutdown, one more for a spawn that was
    in flight, and a margin for the rest."""
    with _process_lock:
        count = len(_processes) + _terminating
    return 2 * TERMINATE_WAIT * (count + 1) + 5.0


def _restarted_text(not_started: List[str]) -> str:
    if not not_started:
        return ""
    return (f"WhatsApp restarted, but not for {', '.join(not_started)}: the previous connection has "
            "not ended yet. Press Restart WhatsApp again in a minute. If it still fails, restart VAF.")


def restart_bridge_result(report: bool = True) -> Tuple[bool, str, List[str]]:
    """Stop the bridge, wait for its full shutdown, then start again: (ok, message,
    not_started). Not ok when the old bridge did not end in time (SHUTDOWN_OVERRAN), since a
    start next to its still running thread would answer "already running" and start nothing,
    or when the new start failed (START_FAILED). `not_started` names the accounts the new run's
    spawn phase skipped because their previous process has not ended, and the message then
    says so: a restart that left one out answered a plain success before. `report=False`
    answers at once and names nobody (`restart_bridge`, whose callers read only `ok`)."""
    global _bridge_thread
    with _lifecycle_lock:
        if _bridge_thread is not None and _bridge_thread.is_alive():
            old = _bridge_thread
            # Spawning is closed first, so the count the deadline is taken from cannot grow.
            stop_bridge()
            deadline = _shutdown_deadline()
            try:
                old.join(timeout=deadline)
            except Exception:
                pass
            if old.is_alive():
                logger.error("WhatsApp: the bridge did not stop within %.0f s; not started again", deadline)
                return False, SHUTDOWN_OVERRAN, []
        if not start_bridge():
            return False, (START_FAILED if anyone_to_run() else NOBODY_TO_RUN), []
        outcome = _run_outcome
    if not report:
        return True, "", []
    # The new run's own spawn decisions, once its spawn phase is over; still pending after the
    # wait, nothing is named rather than a guess (a process that ended meanwhile is started).
    not_started = sorted(set(outcome.skipped)) if outcome.settled.wait(RESTART_REPORT_WAIT) else []
    return True, _restarted_text(not_started), not_started


def restart_bridge() -> bool:
    """`restart_bridge_result` as True or False."""
    return restart_bridge_result(report=False)[0]


def _runnable_account(username: str, user_scope_id: str) -> Optional[Tuple[str, str, Path]]:
    """The `_get_users_to_run` entry of exactly this name AND this scope, or None: an account
    that is not linked, whose own switch is off, or whose directory scope is another one."""
    name, scope = (username or "").strip(), str(user_scope_id or "").strip()
    if not name or not scope:
        return None
    for entry in _get_users_to_run():
        if entry[1] == name and str(entry[0]) == scope:
            return entry
    return None


def _start_into_running(entry: Tuple[str, str, Path]) -> Tuple[bool, str]:
    with _spawn_lock:
        return _start_into_running_held(entry)


def _start_into_running_held(entry: Tuple[str, str, Path]) -> Tuple[bool, str]:
    """`_start_into_running`'s work; the caller holds `_spawn_lock`."""
    user_scope_id, username, auth_dir = entry
    if _bridge_stop.is_set() or not _spawns_open:
        return False, BRIDGE_STOPPING
    if _blocked_by_previous(username, user_scope_id):
        return False, PREVIOUS_NOT_ENDED
    if _registered_alive(username):
        return True, ACCOUNT_ALREADY_RUNNING
    if not _account_may_start(user_scope_id):
        return False, NOT_STARTABLE
    if _spawn_account(user_scope_id, username, auth_dir):
        logger.info("WhatsApp: process started for %s", username)
        return True, ACCOUNT_STARTED
    # What refused the spawn, asked again: `stop_bridge` sets the run's stop without this
    # lock, and a switch-off writes the config without it, so either can land between the
    # checks above and the spawn's own. Neither is a failure of Node.
    if _bridge_stop.is_set() or not _spawns_open:
        return False, BRIDGE_STOPPING
    return False, NOT_STARTABLE if not _account_may_start(user_scope_id) else START_FAILED


def _reconnect_account(username: str, user_scope_id: str,
                       judged: Optional[Any]) -> Optional[Tuple[bool, str]]:
    """The reconnect worker's restart of one account: of exactly the process it judged down
    (`judged`), or, with None, of an account whose earlier automatic restart stopped its
    process and could not spawn the next one. None when there is nothing left to restart: the
    registration is no longer the judged process (its owner or an admin stopped it, a restart
    replaced it), the account is no longer a member of the run (stopped on request since), or
    the bridge no longer takes processes. Otherwise (ok, message).

    One hold of `_spawn_lock` from the check to the spawn, and only into the running bridge
    (`_start_into_running_held`), never `start_account`'s cold start: measured with
    `restart_account` in its place, an owner's Stop or an admin's whole Stop that landed while
    the pass waited for the connection check was undone by the restart, which brought the
    account (and the bridge) up again."""
    entry = _runnable_account(username, user_scope_id)
    if entry is None:
        return False, NOT_STARTABLE
    name, key = entry[1], _scope_key(user_scope_id)
    with _spawn_lock:
        if _bridge_stop.is_set() or not _spawns_open:
            return None
        current, registered = _registration(name)
        if judged is None:
            if current is not None or (name, key) not in _run_outcome.members:
                return None
        else:
            # Membership as well: a Stop on request whose process did not end leaves it
            # registered, retired, and out of the run; once it exits it is not restarted.
            if (current is not judged or registered != str(user_scope_id or "").strip()
                    or (name, key) not in _run_outcome.members):
                return None
            stopped = _stop_account_held(name, user_scope_id)
            if stopped is False:
                return False, STOP_FAILED
            if stopped:
                _account_stopped(name)
        return _start_into_running_held(entry)


def _runs_own_process(username: str, user_scope_id: str) -> bool:
    """Whether the live process registered under this name is the account's own and acts:
    started for this scope and not retired."""
    current = _registered_live(username)
    if current is None or _is_retired(current):
        return False
    with _process_lock:
        return str(_scope_by_user.get(username) or "").strip() == str(user_scope_id or "").strip()


def start_account(username: str, user_scope_id: str) -> Tuple[bool, str]:
    """Start this account's own process and nobody else's: into the running bridge, or by
    bringing the bridge up with this process alone and waiting for its spawn. (ok, message);
    the message is one of NOT_STARTABLE, BRIDGE_STOPPING, START_FAILED, START_CANCELLED,
    ACCOUNT_STARTED, ACCOUNT_ALREADY_RUNNING."""
    entry = _runnable_account(username, user_scope_id)
    if entry is None:
        return False, NOT_STARTABLE
    pending: Optional[_PendingStart] = None
    with _lifecycle_lock:
        if not is_bridge_running():
            pending = _PendingStart(entry[0])
            with _spawn_lock:
                _pending_starts[entry[1]] = pending
            if not start_bridge(only=entry[1]):
                with _spawn_lock:
                    if _pending_starts.get(entry[1]) is pending:
                        del _pending_starts[entry[1]]
                return False, START_FAILED
    if pending is None:
        return _start_into_running(entry)
    # The bridge thread spawns this account's process and answers here; a failed spawn is
    # this account's failure only, the bridge stays up for anybody who started meanwhile.
    if not pending.done.wait(timeout=COLD_START_TIMEOUT):
        with _spawn_lock:
            if _runs_own_process(entry[1], pending.user_scope_id):
                # A start into the running bridge brought the account up meanwhile: it runs,
                # so this start must not answer 500 for it.
                pending.resolve(True, ACCOUNT_ALREADY_RUNNING)
                if _pending_starts.get(entry[1]) is pending:
                    del _pending_starts[entry[1]]
            else:
                pending.cancelled = True
                pending.resolve(False, START_FAILED)
    return pending.ok, pending.message


def stop_account(username: str, user_scope_id: Optional[str] = None) -> Optional[bool]:
    """Stop this account's own process, and cancel its start still waiting for the bridge
    thread. Never touches the bridge, the sender loop or any other account. True when it
    stopped something, None when the account had nothing to stop, False when its process did
    not end (it stays registered, so nothing starts a second one next to it).

    With `user_scope_id`, a process or pending start registered under this name for another
    scope is not this account's and is left alone: an account's name can equal another
    account's (an ordinary account named like the local admin), its scope cannot."""
    with _spawn_lock:
        stopped = _stop_account_held(username, user_scope_id)
        if stopped:
            # Still under the lock: once it is released a replacement can start and report
            # `connected`, which a later reset here would overwrite.
            _account_stopped(username)
        # A stop on request: no automatic reconnect or crash recovery brings it back, even
        # when nothing was registered (an automatic restart whose spawn had failed).
        _leave_run(username, user_scope_id)
    return stopped


def _leave_run(username: str, user_scope_id: Optional[str]) -> None:
    """Drop this account from the current run's members (`_RunOutcome.members`): every pair
    under this name, or only this scope's when one is given. Caller holds `_spawn_lock`."""
    name = (username or "").strip()
    key = _scope_key(user_scope_id)
    for member in [m for m in _run_outcome.members if m[0] == name and (not key or m[1] == key)]:
        _run_outcome.members.discard(member)


def _is_registration(username: str, proc: Any) -> bool:
    """Whether `proc` is still the live process registered under this name, and not retired.
    A process that died after its line is no longer one; the next start replaces it."""
    return proc is not None and _registered_live((username or "").strip()) is proc and not _is_retired(proc)


def _stop_account_held(username: str, user_scope_id: Optional[str] = None) -> Optional[bool]:
    """`stop_account`'s work; the caller holds `_spawn_lock` and, on True, calls
    `_account_stopped` before it releases it."""
    name = (username or "").strip()
    wanted = str(user_scope_id or "").strip()
    pending = _pending_starts.get(name)
    cancelled = False
    if pending is not None and (not wanted or pending.user_scope_id == wanted):
        pending.cancelled = True
        pending.resolve(False, START_CANCELLED)
        cancelled = True
    with _process_lock:
        own_scope = str(_scope_by_user.get(name) or "").strip()
        proc = None
        if name in _processes and (not wanted or own_scope == wanted):
            proc = _processes.pop(name)
            _scope_by_user.pop(name, None)
    if proc is None:
        return True if cancelled else None
    # Before the terminate: a line its reader still holds (a `qr` that would stop this
    # account again, now its next process) is not acted on.
    _retire(proc)
    if not _terminate(proc):
        # Registered again, retired: a survivor, like one of the bridge's shutdown.
        with _process_lock:
            _processes.setdefault(name, proc)
            _scope_by_user.setdefault(name, own_scope)
        logger.error("WhatsApp: the process of %s did not end", name)
        return False
    # Running only while this run still takes processes: after its end (a survivor stopped
    # once it died) a True here would outlive the bridge.
    _write_bridge_state(_spawns_open and not _bridge_stop.is_set())
    return True


def _account_stopped(username: str) -> None:
    """The stopped account's connection reads as down. Caller holds `_spawn_lock`
    (`_connection_lock` is a leaf: nothing takes another lock while holding it)."""
    name = (username or "").strip()
    with _connection_lock:
        _connection_status[name] = False
    logger.info("WhatsApp: process stopped for %s", name)


def restart_account(username: str, user_scope_id: str) -> Tuple[bool, str]:
    """`stop_account`, then `start_account`; nothing is stopped for an account that could not
    start again (not linked, switched off, or a sign-in whose scope is not the account's)."""
    if _runnable_account(username, user_scope_id) is None:
        return False, NOT_STARTABLE
    if stop_account(username, user_scope_id) is False:
        return False, STOP_FAILED
    return start_account(username, user_scope_id)


def start_missing_accounts() -> List[str]:
    """While the bridge runs, start every switched-on linked account that has no live process
    (a newly linked one, or one whose own process was stopped). The names started."""
    if not is_bridge_running():
        return []
    started: List[str] = []
    for entry in _get_users_to_run():
        ok, msg = _start_into_running(entry)
        if ok and msg == ACCOUNT_STARTED:
            started.append(entry[1])
    return started


def _scope_key(user_scope_id: Any) -> str:
    """A scope as this module and the WhatsApp routes compare it: stripped, lowercased, and ""
    for none. Deliberately not `trust._scope_key` and its siblings in vaf/core, which read a
    missing scope as the owner's ("default"): here a missing scope is no account at all."""
    return str(user_scope_id or "").strip().lower()


def _owned(cache: Dict[str, Any], owners: Dict[str, str], name: str, user_scope_id: Any, empty: Any) -> Any:
    """`cache[name]` when the process that wrote it was started for `user_scope_id`, else
    `empty`. The caller holds the cache's lock. Measured before: with a deleted "bob"'s chat
    list still cached under "bob", a new account "bob" read it through every lane's fallback
    (the bridge down, or no process of its own answering)."""
    want = _scope_key(user_scope_id)
    if not want or _scope_key(owners.get(name)) != want:
        return empty
    return cache.get(name, empty)


def _account_may_start(user_scope_id: Any) -> bool:
    """Whether a Node may start for this account now: not revoked in this process, still
    standing (`revocation.account_stands`: active in the account directory), and its own
    WhatsApp switch on (`channel_enabled_for_scope`). Asked right before every spawn, under the
    lock the spawn holds: a start that passed its own checks before a deactivation or deletion
    landed would otherwise spawn after the revocation listener had already stopped everything
    (measured: `_spawn_account` after `revoke_account` answered True and registered the
    process). The QR link asks the first two halves itself: the store read before its
    `_qr_lock`, which `/qr` takes on the event loop, and the in-memory mark under it.

    The switch, and why under this lock: the switch route writes the switch BEFORE it stops
    the account (`stop_account` takes `_spawn_lock` too), so a spawn that still read it on is
    stopped afterwards, and every spawn after the write refuses. Measured before: a switch-off
    written and stopped between `start_account`'s own check and its spawn left the account
    running with its switch off. That holds for every spawn path alike (an account's start,
    `start_missing_accounts`, the bridge thread's spawn loop, the reconnect worker), because
    all of them spawn through `_spawn_account`."""
    from vaf.core import revocation
    scope = str(user_scope_id or "").strip()
    return (not revocation.is_revoked(scope) and revocation.account_stands(scope)
            and channel_enabled_for_scope("whatsapp", scope))


def _registrations_of(user_scope_id: Any) -> List[Tuple[str, str]]:
    """(name, scope as registered) of every process and pending start of this scope, the
    scope compared stripped and case-insensitively. Sorted by name."""
    key = _scope_key(user_scope_id)
    if not key:
        return []
    found: Dict[str, str] = {}
    with _spawn_lock:
        for name, pending in _pending_starts.items():
            if _scope_key(pending.user_scope_id) == key:
                found[name] = pending.user_scope_id
        with _process_lock:
            for name, scope in _scope_by_user.items():
                if _scope_key(scope) == key:
                    found[name] = scope
    return sorted(found.items())


def stop_scope(user_scope_id: Any) -> Optional[bool]:
    """`stop_account` for every process and pending start of this account's scope, under
    whichever name each is registered: the account's end (a deactivation, a deletion). True
    when it stopped something, None when there was nothing, False when a process did not end.
    Never touches another scope's process, even one under the same name."""
    result: Optional[bool] = None
    for name, scope in _registrations_of(user_scope_id):
        stopped = stop_account(name, scope)
        if stopped is False:
            result = False
        elif stopped and result is None:
            result = True
    return result


def _on_account_revoked(user_scope_id: str) -> None:
    """Revocation listener: a deactivated or deleted account's WhatsApp stops; a narrowing of
    its rights (work stopped, account not revoked) or a demotion stops nothing. Reactivation
    starts nothing by itself (the admin's Start, the account's own Start or Refresh does)."""
    if not _registrations_of(user_scope_id):
        return      # nothing runs for it: no directory query for every narrowing
    from vaf.core import revocation
    scope = str(user_scope_id or "").strip()
    if revocation.is_revoked(scope) or not revocation.account_stands(scope):
        if stop_scope(scope) is False:
            logger.error("WhatsApp: the process of a revoked account did not end")


def register_revocation_listener() -> None:
    from vaf.core import revocation
    revocation.add_revocation_listener(_on_account_revoked)


def _forget_cached(names, user_scope_id: Any, *, keep_unowned: bool = False) -> None:
    """Drop what the bridge cached under these names for this scope: the chat list, LID
    mappings and contact names this scope's process wrote (an entry another scope wrote stays,
    it is that account's), and, unless `keep_unowned` (another account carries exactly one of
    these names), the connection state, own number and contact-sync throttle, which record no
    scope."""
    names = [n for n in names if n]
    gone = _scope_key(user_scope_id)

    def mine(owners: Dict[str, str], n: str) -> bool:
        return n not in owners or _scope_key(owners.get(n)) == gone
    for lock, cache, owners, events in (
            (_chat_lists_lock, _chat_lists, _chat_lists_owner, _chat_list_events),
            (_lid_mappings_lock, _lid_mappings, _lid_mappings_owner, _lid_mappings_events),
            (_contact_names_lock, _contact_names, _contact_names_owner, _contact_names_events)):
        with lock:
            for n in names:
                if mine(owners, n):
                    cache.pop(n, None)
                    owners.pop(n, None)
                    events.pop(n, None)
    if keep_unowned:
        return
    with _connection_lock:
        for n in names:
            _connection_status.pop(n, None)
            _connection_events.pop(n, None)
    for n in names:
        _self_phone.pop(n, None)
        _contact_sync_last.pop(n, None)


class DirectoryUnknown(RuntimeError):
    """The account directory gave no answer to decide by."""


def _other_account_names(user_scope_id: Any) -> List[str]:
    """Every account name but this scope's: the local admin's from the config and the account
    directory's. Empty names are left out.

    Raises `DirectoryUnknown` when the directory answers nothing at all: the framework reads an
    unreachable store as an empty directory (`tool_dispatch.resolve_account_directory`, a
    lookup by design), and an empty list would tell the shared-folder check that no other
    account exists, so a deletion during an outage removed a login another account opens.
    Asked right after a deletion committed, an empty answer is an outage, or an installation
    with no other account left whose login could be at stake; either way nothing is removed."""
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    from vaf.core.tool_dispatch import resolve_account_directory
    gone = _scope_key(user_scope_id)
    rows = resolve_account_directory()
    if not rows:
        raise DirectoryUnknown("the account directory answered nothing")
    names: List[str] = [str(r.get("username") or "").strip() for r in rows
                        if _scope_key(r.get("user_scope_id")) != gone]
    if _scope_key(get_local_admin_scope_id()) != gone:
        names.append(str(get_local_admin_username() or "").strip())
    return [n for n in names if n]


def _folder_of_another_account(username: str, others: List[str]) -> Optional[str]:
    """The name of another account (the local admin included) whose login folder is this
    name's folder on this file system, or None. Asked of EVERY other account by the kernel
    (`os.path.samefile`), not only of names that look alike: the same name under another scope
    (an ordinary account named like a local admin that has no row of its own), a twin from
    before names were compared by `portable_name_key` on a case-insensitive file system, and
    any spelling the kernel resolves to the same folder. Removing such a folder would remove
    the other account's login. `others` are the other accounts' names
    (`_other_account_names`). Accounts are counted in tens."""
    name = (username or "").strip()
    mine = get_whatsapp_auth_dir(name)
    for other in others:
        try:
            if os.path.samefile(get_whatsapp_auth_dir(other), mine):
                return other
        except OSError:
            continue
    return None


def forget_account_login(username: str, user_scope_id: Any) -> bool:
    """A deleted account's WhatsApp ends with it: its process stops and its pending start is
    cancelled (`stop_scope`), the name's cached chat list, LID mappings, names, own number and
    connection state are dropped, and its login and picture cache are removed
    (`whatsapp_auth.remove_login`). True when the login is gone.

    The caches go first, whatever happens to the folder: this scope's entries always, the
    unscoped ones unless another account carries exactly this name. Then nothing is removed
    (False, a warning) when the name is not one folder name (`whatsapp_auth.is_one_folder_name`:
    "admin/" or ".." would open another account's folder or one outside `users/`), while a
    Node still runs on the folder (one that survived its kill writes into it), or when the
    folder is another account's too. Such a folder is moved aside when the name is used again
    (`user_admin.create_local_user`)."""
    from vaf.core.whatsapp_auth import is_one_folder_name, remove_login
    name = (username or "").strip()
    if not name:
        return False
    names = {n for n, _ in _registrations_of(user_scope_id)} | {name}
    if stop_scope(user_scope_id) is False:
        logger.error("WhatsApp: the process of the deleted account %s did not end", name)
    try:
        others = _other_account_names(user_scope_id)
    except Exception as e:
        # Which other account opens this folder cannot be told: keep the login, and the
        # cached state no scope is recorded for, rather than remove another account's.
        _forget_cached(names, user_scope_id, keep_unowned=True)
        logger.warning("WhatsApp: the login of the deleted account %s is kept, the account "
                       "directory could not be read (%s)", name, e)
        return False
    _forget_cached(names, user_scope_id, keep_unowned=name in others)
    if not is_one_folder_name(name):
        logger.warning("WhatsApp: the login of the deleted account %r is kept, the name is not "
                       "one folder name and could open another folder", name)
        return False
    folder = get_whatsapp_auth_dir(name)
    if login_in_use(folder):
        logger.warning("WhatsApp: the login of the deleted account %s is kept, a process still "
                       "runs on it", name)
        return False
    other = _folder_of_another_account(name, others)
    if other is not None:
        logger.warning("WhatsApp: the login of the deleted account %s is kept, it is the folder "
                       "of the account %s on this file system", name, other)
        return False
    return remove_login(name, avatars=True)


def is_bridge_running() -> bool:
    return _bridge_thread is not None and _bridge_thread.is_alive()


def last_run_failed() -> bool:
    """Whether the current (or last) bridge run ended because its thread raised."""
    return _run_outcome.failed


def running_by_account() -> Dict[str, bool]:
    """{name: whether that account's own process runs and acts} for every account that is
    linked and switched on. An admin's view (the dashboard's `running_by_account`): it names
    accounts."""
    return {name: _runs_own_process(name, scope) for scope, name, _ in _get_users_to_run()}


# The reconnect worker's bounds (`Reconnector`).
RECONNECT_GRACE = 90.0       # how long an account's connection is down before it is restarted
RECONNECT_COOLDOWN = 180.0   # between two tries for one account, and between two crash restarts
RECONNECT_MAX_TRIES = 3      # restarts of one account before it is left alone
CRASH_RESTARTS_MAX = 3       # crash restarts of the bridge in a row without a connection seen


class _DownAccount:
    """What the reconnector knows about one account whose connection is down."""

    def __init__(self, since: float) -> None:
        self.since = since
        self.tries = 0
        self.last_try: Optional[float] = None
        self.gave_up = False
        # This worker restarted the account: while it stays a member of the run (no stop on
        # request since), a restart whose spawn failed is tried again, up to the bound.
        self.intent = False


class Reconnector:
    """The reconnect worker's decisions, one pass at a time (`step`). It reconnects accounts,
    not the bridge, and does no I/O beyond the bridge's own calls; the time is handed in.

    While the bridge runs, a candidate is an account that is linked and switched on, has a
    registration under its name for its own scope, and whose process has ended (a Node that
    exited, or a survivor an admin's restart skipped that has ended since) or is alive, acts
    and is not connected. One that has been down for RECONNECT_GRACE is restarted, at most
    RECONNECT_MAX_TRIES times, RECONNECT_COOLDOWN apart: exactly the process it judged, into
    the running bridge (`_reconnect_account`), so a Stop that lands during the pass is never
    undone. A restart whose spawn failed leaves no registration; it is tried again while the
    account is still a member of the run, and a stop on request ends that. A refusal is logged
    with its reason, and the last try once as an error. Left alone: an account without a
    registration that this worker did not restart (its owner or an admin stopped it, its
    session expired), a survivor that still runs, and an account seen connected, which also
    resets its count.

    While the bridge is down, it is started again only when its last run failed (the thread
    raised), no whole-bridge Stop came since that run started (an admin's `POST /stop` counts
    one whether or not a run is up), and fewer than CRASH_RESTARTS_MAX such starts were made in
    a row without an account seen connected; it starts only that run's current members
    (`_start_bridge(recover=...)`, read in the step that publishes the new run), so an account
    stopped on request before the failure, or during the recovery, stays stopped. A start that
    a Stop or the end of every member's WhatsApp overtook is no try: it spends no count and
    logs no failure. A stop on request or a run with nobody to run stays down.

    Measured before, with a worker that restarted the whole bridge: an account's own Stop was
    undone for everybody at the next pass (its empty bridge counted as "nothing connected"),
    `restart_bridge`'s False was ignored without a log line, nothing ran while the local
    admin's switch was off, and a crashed bridge stayed down until somebody noticed."""

    def __init__(self, *, grace: float = RECONNECT_GRACE, cooldown: float = RECONNECT_COOLDOWN,
                 max_tries: int = RECONNECT_MAX_TRIES, crash_restarts_max: int = CRASH_RESTARTS_MAX) -> None:
        self.grace = grace
        self.cooldown = cooldown
        self.max_tries = max_tries
        self.crash_restarts_max = crash_restarts_max
        self._down: Dict[Tuple[str, str], _DownAccount] = {}
        self._crash_restarts = 0
        self._last_crash_restart: Optional[float] = None

    def step(self, now: float) -> List[Tuple[str, bool, str]]:
        """One pass. Returns what it did: (account name, or "*" for the bridge, ok, message)."""
        if not is_bridge_running():
            self._down.clear()
            return self._bring_back(now)
        actions: List[Tuple[str, bool, str]] = []
        seen = set()
        for scope, name, _ in _get_users_to_run():
            key = (_scope_key(scope), name)
            seen.add(key)
            proc, state = self._account_state(name, scope)
            if state == "connected":
                self._crash_restarts = 0
            if state == "none" and key in self._down and self._down[key].intent:
                judged = None       # this worker's restart, whose spawn failed: go on with it
            elif state != "down":
                self._down.pop(key, None)
                continue
            else:
                judged = proc
            down = self._down.setdefault(key, _DownAccount(now))
            if down.gave_up or now - down.since < self.grace:
                continue
            if down.last_try is not None and now - down.last_try < self.cooldown:
                continue
            result = _reconnect_account(name, scope, judged)
            if result is None:
                # Stopped, restarted or ended meanwhile: nothing of this worker's to go on with.
                self._down.pop(key, None)
                continue
            ok, message = result
            down.tries += 1
            down.last_try = now
            down.intent = True
            actions.append((name, ok, message))
            if not ok:
                logger.warning("WhatsApp: reconnecting %s failed: %s", name, message)
            if down.tries >= self.max_tries:
                down.gave_up = True
                logger.error("WhatsApp: %s did not reconnect after %d restarts; restart it by hand",
                             name, down.tries)
        # An account no longer linked or switched on is nobody's business here any more.
        for key in [k for k in self._down if k not in seen]:
            del self._down[key]
        return actions

    @staticmethod
    def _account_state(name: str, scope: str) -> Tuple[Optional[Any], str]:
        """(the registered process, state): "none" (no registration of this account's own:
        stopped, expired, or another scope's process under the name), "survivor" (retired and
        still running), "connected", or "down" (its process ended, or it acts and is not
        connected)."""
        proc, registered = _registration(name)
        if proc is None or registered != str(scope or "").strip():
            return None, "none"
        if proc.poll() is not None:
            return proc, "down"
        if _is_retired(proc):
            return proc, "survivor"
        if get_connection_status(name, wait_timeout=2.0, user_scope_id=scope):
            return proc, "connected"
        return proc, "down"

    def _bring_back(self, now: float) -> List[Tuple[str, bool, str]]:
        outcome = _run_outcome
        if not outcome.failed or self._crash_restarts >= self.crash_restarts_max:
            return []
        if self._last_crash_restart is not None and now - self._last_crash_restart < self.cooldown:
            return []
        with _marks_lock:
            if _whole_stops != outcome.started_after:
                return []       # stopped on request since that run started
        with _spawn_lock:
            members = set(outcome.members)
        if not {(name, _scope_key(scope)) for scope, name, _ in _get_users_to_run()} & members:
            return []
        # The members are read again, and the Stop count too, in the start's own publishing
        # step: what was read here only decides whether a start is worth trying.
        answer = _start_bridge(recover=outcome)
        if answer in ("nobody", "cancelled"):
            # Not a try: a Stop or the end of every member's WhatsApp overtook it.
            return []
        self._crash_restarts += 1
        self._last_crash_restart = now
        ok = answer in ("started", "running")
        message = "WhatsApp bridge started again after a failed run." if ok else START_FAILED
        if ok:
            logger.warning("WhatsApp: %s (%d of %d)", message, self._crash_restarts, self.crash_restarts_max)
        else:
            logger.error("WhatsApp: the bridge did not start again after a failed run (%d of %d)",
                         self._crash_restarts, self.crash_restarts_max)
        return [("*", ok, message)]


def has_process_for_user(username: str, user_scope_id: Optional[str] = None) -> bool:
    """True if a running Node process acts for this account (so sends will not be dropped):
    one started for `user_scope_id` (`_process_for`). Without a scope, False."""
    if not is_bridge_running():
        return False
    uname = (username or "").strip() or "admin"
    _, proc = _process_for(uname, user_scope_id)
    return bool(proc is not None and proc.poll() is None and proc.stdin)


def _request_chats(username: str, user_scope_id: Optional[str]) -> Optional[str]:
    """Send getChats to the Node process. Returns the username whose process was used (for lookup), or None."""
    target_username, proc = _process_for(username, user_scope_id)
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(json.dumps({"cmd": "getChats"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp getChats failed for %s: %s", username, e)
    return None


def _request_sync_chats(username: str, user_scope_id: Optional[str]) -> Optional[str]:
    """Send syncChats to the Node (triggers Baileys fetchMessageHistory so full chat list is synced). Returns username used, or None."""
    target_username, proc = _process_for(username, user_scope_id)
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(json.dumps({"cmd": "syncChats"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp syncChats failed for %s: %s", username, e)
    return None


def get_whatsapp_chats(username: str, force_refresh: bool = False, wait_timeout: float = 3.0, *,
                       user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return the list of all WhatsApp chats. Requests from the account's own process
    (`user_scope_id`) when it runs, else the last list stored under this name."""
    if not is_bridge_running():
        with _chat_lists_lock:
            return list(_owned(_chat_lists, _chat_lists_owner, username, user_scope_id, []))
    used_username = _request_chats(username, user_scope_id)
    if not used_username:
        with _chat_lists_lock:
            return list(_owned(_chat_lists, _chat_lists_owner, username, user_scope_id, []))
    with _chat_lists_lock:
        if used_username not in _chat_list_events:
            _chat_list_events[used_username] = threading.Event()
        ev = _chat_list_events[used_username]
        ev.clear()
    ev.wait(timeout=wait_timeout)
    with _chat_lists_lock:
        return list(_owned(_chat_lists, _chat_lists_owner, used_username, user_scope_id, []))


def sync_whatsapp_chats(username: str, wait_timeout: float = 25.0, *,
                        user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    """Request full chat list sync from WhatsApp (Node calls Baileys fetchMessageHistory), then return the updated list. Use when the left-side list is incomplete."""
    if not is_bridge_running():
        with _chat_lists_lock:
            return list(_owned(_chat_lists, _chat_lists_owner, username, user_scope_id, []))
    used_username = _request_sync_chats(username, user_scope_id)
    if not used_username:
        with _chat_lists_lock:
            return list(_owned(_chat_lists, _chat_lists_owner, username, user_scope_id, []))
    with _chat_lists_lock:
        if used_username not in _chat_list_events:
            _chat_list_events[used_username] = threading.Event()
        ev = _chat_list_events[used_username]
        ev.clear()
    ev.wait(timeout=wait_timeout)
    with _chat_lists_lock:
        return list(_owned(_chat_lists, _chat_lists_owner, used_username, user_scope_id, []))


def fetch_older_messages(
    username: str,
    chat_jid: str,
    chat_id: str,
    user_scope_id: Optional[str] = None,
    count: int = 50,
    wait_timeout: float = 20.0,
) -> Dict[str, Any]:
    """Ask WhatsApp for `count` messages of one chat older than the oldest we hold.

    Two-step by nature of the protocol: the Node's `fetchHistory` command sends the
    on-demand request (its result only says the request left), and the phone answers
    later with a history batch that the ordinary `history_messages` path writes into
    the store. So "done" is measured on the store: this returns once the chat's row
    count grew, or after `wait_timeout`. Returns {ok, stored_before, stored_after,
    error}; never raises."""
    from vaf.core.channel_message_store import get_chat_messages, oldest_message

    uname = (username or "").strip() or "admin"
    out: Dict[str, Any] = {"ok": False, "stored_before": 0, "stored_after": 0, "error": ""}
    try:
        before = len(get_chat_messages(uname, chat_id, limit=200, user_scope_id=user_scope_id))
    except Exception:
        before = 0
    out["stored_before"] = before
    oldest = None
    try:
        oldest = oldest_message(uname, chat_id, user_scope_id=user_scope_id)
    except Exception:
        oldest = None
    if oldest is None:
        # Baileys pages backwards from a message key; a chat with nothing stored has no
        # key to page from, and a request with an empty one comes back with nothing. Say so
        # instead of asking: the first stored message (an inbound, a send) is the cursor.
        out.update({"ok": True, "no_cursor": True})
        return out
    _, proc = _process_for(uname, user_scope_id)
    if not proc or proc.poll() is not None or not proc.stdin:
        out["error"] = "WhatsApp bridge is not running."
        return out
    req_id = str(uuid.uuid4())
    result_queue: queue.Queue = queue.Queue()
    with _pending_sends_lock:
        _pending_sends[req_id] = result_queue
    cmd = {
        "cmd": "fetchHistory",
        "jid": chat_jid,
        "count": int(count or 50),
        "oldestId": (oldest or {}).get("message_id") or "",
        "oldestFromMe": ((oldest or {}).get("direction") == "out"),
        "oldestTs": float((oldest or {}).get("ts") or time.time()),
        "req_id": req_id,
    }
    try:
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()
    except Exception as e:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        out["error"] = f"Could not reach the bridge: {e}"
        return out
    try:
        success, error = result_queue.get(timeout=min(10.0, wait_timeout))
    except queue.Empty:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        out["error"] = "No answer from the bridge."
        return out
    if not success:
        out["error"] = error or "WhatsApp refused the history request."
        return out
    # The batch arrives asynchronously; watch the store until it grows or time runs out.
    deadline = time.time() + wait_timeout
    after = before
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            after = len(get_chat_messages(uname, chat_id, limit=200, user_scope_id=user_scope_id))
        except Exception:
            after = before
        if after > before:
            break
    out["stored_after"] = after
    out["ok"] = True
    return out


def avatar_cache_dir(username: str) -> Path:
    """Per-user cache of profile pictures (`whatsapp_auth.get_whatsapp_avatar_dir`)."""
    from vaf.core.whatsapp_auth import get_whatsapp_avatar_dir
    return get_whatsapp_avatar_dir(username)


def _avatar_key(chat_jid: str) -> str:
    part = (chat_jid or "").split("@", 1)[0].split(":", 1)[0]
    digits = "".join(c for c in part if c.isdigit())
    return (digits or "x") + ("_lid" if (chat_jid or "").endswith("@lid") else "")


def get_avatar(username: str, chat_jid: str, wait_timeout: float = 8.0, *,
               user_scope_id: Optional[str]) -> Optional[Tuple[bytes, str]]:
    """The profile picture of a chat as (bytes, mime), or None when the person shows none
    (privacy setting, no picture, or the bridge is down), asked of the account's own process
    (`user_scope_id`; without one, None).

    Cached on disk for AVATAR_TTL_SECONDS in both directions: a `.none` marker records
    "asked, nothing there", so a private profile is not queried on every render. One
    Node query runs at a time (see _avatar_query_lock)."""
    if not str(user_scope_id or "").strip():
        return None
    uname = (username or "").strip() or "admin"
    jid = (chat_jid or "").strip()
    # Only people have a profile picture worth asking for: newsletters, groups and the
    # status broadcast either time out or answer with nothing, and each such query
    # would hold the one-at-a-time lock for the whole timeout.
    if not (jid.endswith("@s.whatsapp.net") or jid.endswith("@lid")):
        return None
    cache = avatar_cache_dir(uname)
    key = _avatar_key(jid)
    hit = cache / f"{key}.jpg"
    none = cache / f"{key}.none"
    now = time.time()
    try:
        if hit.is_file() and now - hit.stat().st_mtime < AVATAR_TTL_SECONDS:
            return hit.read_bytes(), "image/jpeg"
        if none.is_file() and now - none.stat().st_mtime < AVATAR_TTL_SECONDS:
            return None
    except OSError:
        pass
    _, proc = _process_for(uname, user_scope_id)
    if not proc or proc.poll() is not None or not proc.stdin:
        return None
    with _avatar_query_lock:
        req_id = str(uuid.uuid4())
        result_queue: queue.Queue = queue.Queue()
        with _avatar_results_lock:
            _avatar_results[req_id] = result_queue
        try:
            proc.stdin.write(json.dumps({"cmd": "getAvatar", "jid": chat_jid, "req_id": req_id}) + "\n")
            proc.stdin.flush()
            obj = result_queue.get(timeout=wait_timeout)
        except Exception:
            with _avatar_results_lock:
                _avatar_results.pop(req_id, None)
            return None
    if not obj.get("success"):
        err = str(obj.get("error") or "").lower()
        if any(s in err for s in ("item-not-found", "not-authorized", "forbidden", "not-allowed")):
            # WhatsApp's own "no picture for you": remember it like an empty answer.
            try:
                cache.mkdir(parents=True, exist_ok=True)
                none.write_text(str(int(now)), encoding="utf-8")
            except OSError:
                pass
            return None
        # A transport failure is not "no picture": leave the cache alone so the next
        # render asks again once the bridge is back.
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if obj.get("found") and obj.get("b64"):
            import base64
            data = base64.b64decode(obj["b64"])
            hit.write_bytes(data)
            try:
                none.unlink(missing_ok=True)
            except OSError:
                pass
            return data, str(obj.get("mime") or "image/jpeg")
        none.write_text(str(int(now)), encoding="utf-8")
    except OSError:
        pass
    return None


def resync_contacts(username: str, wait_timeout: float = 60.0, user_scope_id: Optional[str] = None) -> Tuple[bool, str]:
    """Ask the Node to fetch the phone's contact names again (a full snapshot of the
    app-state contact collection; see `resyncContacts` in wa-bridge.js). Returns
    (ok, error). Slow by nature: WhatsApp streams the whole collection."""
    uname = (username or "").strip() or "admin"
    _, proc = _process_for(uname, user_scope_id)
    if not proc or proc.poll() is not None or not proc.stdin:
        return False, "WhatsApp bridge is not running."
    req_id = str(uuid.uuid4())
    result_queue: queue.Queue = queue.Queue()
    with _pending_sends_lock:
        _pending_sends[req_id] = result_queue
    try:
        proc.stdin.write(json.dumps({"cmd": "resyncContacts", "req_id": req_id}) + "\n")
        proc.stdin.flush()
        success, error = result_queue.get(timeout=wait_timeout)
    except queue.Empty:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        return False, "No answer from the bridge."
    except Exception as e:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        return False, f"Could not reach the bridge: {e}"
    if success:
        _contact_sync_last.pop(uname, None)   # the next chat list may feed the contact book at once
        threading.Thread(target=_fetch_names_per_chat, args=(uname, user_scope_id), daemon=True).start()
        return True, ""
    return False, error or "WhatsApp refused the contact sync."


NAMES_PASS_MAX_CHATS = 120
NAMES_PASS_INTERVAL = 1.0


def _fetch_names_per_chat(username: str, user_scope_id: Optional[str]) -> int:
    """Second half of "reload names": the app-state contact collection carries no names
    (verified live: its records hold a timestamp and nothing else), so the names have to
    come the way the first sync delivered them, inside a history batch. One on-demand
    history request per stored chat (count 1) makes WhatsApp answer with that
    conversation, name included; the Node folds it into its stores and the chat list
    re-emits with names. One request per second, capped, in the background."""
    from vaf.core.channel_message_store import get_chat_messages, list_chats_from_store
    uname = (username or "").strip() or "admin"
    try:
        rows = list_chats_from_store(uname, limit=NAMES_PASS_MAX_CHATS, user_scope_id=user_scope_id)
    except Exception:
        return 0
    sent = 0
    for row in rows:
        chat_id = str(row.get("chat_id") or "").strip()
        if not chat_id:
            continue
        jid = chat_id if "@" in chat_id else whatsapp_send_jid(chat_id)
        if not jid or not (jid.endswith("@s.whatsapp.net") or jid.endswith("@lid")):
            continue
        try:
            newest = get_chat_messages(uname, chat_id, limit=1, user_scope_id=user_scope_id)
        except Exception:
            newest = []
        if not newest or not newest[0].get("message_id") or str(newest[0]["message_id"]).startswith("_"):
            continue
        _, proc = _process_for(uname, user_scope_id)
        if not proc or proc.poll() is not None or not proc.stdin:
            break
        cmd = {
            "cmd": "fetchHistory", "jid": jid, "count": 1,
            "oldestId": newest[0]["message_id"], "oldestFromMe": newest[0].get("direction") == "out",
            "oldestTs": float(newest[0].get("ts") or time.time()),
        }
        try:
            proc.stdin.write(json.dumps(cmd) + "\n")
            proc.stdin.flush()
        except Exception:
            break
        sent += 1
        time.sleep(NAMES_PASS_INTERVAL)
    try:
        from vaf.core.log_helper import log_whatsapp_qr
        log_whatsapp_qr(f"[contacts] names pass: asked WhatsApp for {sent} conversations")
    except Exception:
        pass
    return sent


def _request_lid_mappings(username: str, user_scope_id: Optional[str]) -> Optional[str]:
    """Ask Node for LID→E.164 from Baileys lidMapping. Returns username used for lookup."""
    target_username, proc = _process_for(username, user_scope_id)
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(json.dumps({"cmd": "getLidMappings"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp getLidMappings failed for %s: %s", username, e)
    return None


def _request_contacts(username: str, user_scope_id: Optional[str]) -> Optional[str]:
    """Ask the Node for its named numbers. Returns the username whose process answered, or None."""
    target_username, proc = _process_for(username, user_scope_id)
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(json.dumps({"cmd": "getContacts"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp getContacts failed for %s: %s", username, e)
    return None


def get_contact_names(username: str, wait_timeout: float = 2.5, *,
                      user_scope_id: Optional[str]) -> Dict[str, Dict[str, str]]:
    """Phone digits -> {name, source, e164} as WhatsApp shows them (address book, business
    name or push name), from the Node's contact store. The last answer when the bridge is
    down or slow; empty when it never answered."""
    if not is_bridge_running():
        with _contact_names_lock:
            return dict(_owned(_contact_names, _contact_names_owner, username, user_scope_id, {}))
    used = _request_contacts(username, user_scope_id)
    if not used:
        with _contact_names_lock:
            return dict(_owned(_contact_names, _contact_names_owner, username, user_scope_id, {}))
    with _contact_names_lock:
        if used not in _contact_names_events:
            _contact_names_events[used] = threading.Event()
        ev = _contact_names_events[used]
        ev.clear()
    ev.wait(timeout=wait_timeout)
    with _contact_names_lock:
        return dict(_owned(_contact_names, _contact_names_owner, used, user_scope_id, {}))


def get_lid_mappings(username: str, wait_timeout: float = 2.5, *,
                     user_scope_id: Optional[str]) -> List[Dict[str, str]]:
    """Return LID→E.164 known by Node (Baileys). Requests from bridge when running. Empty if not available."""
    if not is_bridge_running():
        with _lid_mappings_lock:
            return list(_owned(_lid_mappings, _lid_mappings_owner, username, user_scope_id, []))
    used_username = _request_lid_mappings(username, user_scope_id)
    if not used_username:
        with _lid_mappings_lock:
            return list(_owned(_lid_mappings, _lid_mappings_owner, username, user_scope_id, []))
    with _lid_mappings_lock:
        if used_username not in _lid_mappings_events:
            _lid_mappings_events[used_username] = threading.Event()
        ev = _lid_mappings_events[used_username]
        ev.clear()
    ev.wait(timeout=wait_timeout)
    with _lid_mappings_lock:
        return list(_owned(_lid_mappings, _lid_mappings_owner, used_username, user_scope_id, []))


def get_connection_status(username: str, wait_timeout: float = 5.0, *,
                          user_scope_id: Optional[str]) -> bool:
    """Check if the account's WhatsApp socket is connected (Node has currentSock): ping/pong
    with the process started for `user_scope_id`. Timeout 5s so read loop can be busy (e.g. transcribing)."""
    if not is_bridge_running():
        return False
    uname = (username or "").strip() or "admin"
    target, proc = _process_for(uname, user_scope_id)
    if not proc or proc.poll() is not None or not proc.stdin:
        return False
    with _connection_lock:
        if target not in _connection_events:
            _connection_events[target] = threading.Event()
        ev = _connection_events[target]
        ev.clear()
    try:
        proc.stdin.write(json.dumps({"cmd": "ping"}) + "\n")
        proc.stdin.flush()
    except Exception:
        return False
    ev.wait(timeout=wait_timeout)
    with _connection_lock:
        return _connection_status.get(target, False)


register_revocation_listener()
