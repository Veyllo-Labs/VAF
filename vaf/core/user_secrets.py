# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A person's own credentials for the services their agent works with - an FTP login, an API
token, a database password - handed to a command as an environment variable, never to the model.

A credential typed into the chat stays in the chat: in the session file, in the timeline, in the
request to the model provider, and in the context of every turn after it (measured in a long
build session: an FTP password pasted once travelled with every later turn). The confirmation
dialog already hides what looks like a password in a command (vaf/core/arg_preview.py); the
command itself, the session and the timeline still carried it.

How it works:

- The person stores a value under a NAME: in Settings (Connections), with `vaf secrets set NAME`,
  or through `PUT /api/secrets/{name}` - or hands it to the agent in the chat, and the agent
  stores it with the store_credential tool. The value then leaves everywhere VAF keeps that
  chat (vaf/core/forget_secrets.py); what already left the machine - the turn at the model
  provider, a message on a messaging platform that cannot delete it - stays there.
- The agent sees only the names (`prompt_note`, in the tool section of the turn block) and writes
  `$VAF_SECRET_<NAME>` into a host_bash command, or `os.environ["VAF_SECRET_<NAME>"]` into
  python_exec code.
- The tool hands over only the secrets the command NAMES (`env_for`), never the whole store: a
  command that dumps its environment gets nothing it did not ask for.
- What the command prints is scrubbed of those values before the model reads it (`scrub`).

Per person. The address is the shared credential key builder
(`credential_store.build_credential_key`, namespace "secret"), so "a scope wins over a name, the
local admin is the unscoped form" is the rule every credential lane uses; a direct consumer with
no identity (the jail's rule for the coder, workflows, automations) is the machine owner. Storage
is one envelope-encrypted file, `user_secrets.enc` (`SecureBlobStore`): the list of names needs
one readable index, which the OS keyring cannot give.

NAMED BOUNDARIES:
- python_sandbox and the coder's container get nothing: they are the confined lanes, and a
  credential inside a container is a credential the container's code can keep.
- A background command's log file (owner-only, per chat) holds the raw output; the scrub runs when
  the log is read into the conversation, because the command writes the file itself.
- Engine-internal like `channel_secrets`, not on the facade: the three consumers are VAF's own
  host tools, and no third-party tool has been measured to need it.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

ENV_PREFIX = "VAF_SECRET_"
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
_REFERENCE_RE = re.compile(r"VAF_SECRET_([A-Z0-9_]+)")
#: A value shorter than this is not scrubbed from output: replacing every "abc" in a build
#: log would wreck the log and protect nothing.
MIN_SCRUB_LENGTH = 4

_stores: Dict[str, object] = {}
_stores_lock = threading.Lock()


def _store():
    """The encrypted store at the CURRENT data dir (one per path, so a test's is its own)."""
    from vaf.core.platform import Platform
    from vaf.core.secure_store import SecureBlobStore
    path = Path(Platform.data_dir()) / "user_secrets.enc"
    with _stores_lock:
        store = _stores.get(str(path))
        if store is None:
            store = _stores[str(path)] = SecureBlobStore("user_secrets", path)
    return store


def normalize_name(name) -> str:
    """The stored form of a name: upper case, words joined by "_", without the env prefix.
    Raises ValueError for a name that cannot become an environment variable."""
    text = str(name or "").strip().lstrip("$")
    if text.upper().startswith(ENV_PREFIX):
        text = text[len(ENV_PREFIX):]
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    if not _NAME_RE.match(text):
        raise ValueError("a name needs a letter first, then letters, digits or '_' "
                         "(at most 63 characters)")
    return text


def env_name(name: str) -> str:
    return ENV_PREFIX + normalize_name(name)


def _key(name: str, user_scope_id, username) -> str:
    from vaf.core.credential_store import build_credential_key
    return build_credential_key(name, namespace="secret", user_scope_id=user_scope_id,
                                username=username)


def _prefix(user_scope_id, username) -> str:
    return _key("x", user_scope_id, username)[:-1]


def _own(blob: Dict[str, str], user_scope_id, username) -> Dict[str, str]:
    """This person's entries, by stored name. A deeper key belongs to someone else: the local
    admin's prefix `secret:` is the start of every scoped key too."""
    prefix = _prefix(user_scope_id, username)
    out = {}
    for key, value in blob.items():
        if key.startswith(prefix) and ":" not in key[len(prefix):]:
            out[key[len(prefix):].upper()] = value
    return out


def names(*, user_scope_id=None, username=None) -> List[str]:
    """This person's stored names, sorted. Never a value."""
    try:
        return sorted(_own(_store().load(), user_scope_id, username))
    except Exception:
        return []


def set_secret(name, value, *, user_scope_id=None, username=None) -> str:
    """Store (or replace) one value. Returns the stored name. Raises ValueError for a bad
    name or an empty value: an empty value would read as "stored" and hand a command nothing."""
    stored = normalize_name(name)
    value = str(value if value is not None else "")
    if not value.strip():
        raise ValueError("an empty value is not stored; delete the name instead")
    key = _key(stored, user_scope_id, username)
    _store().update(lambda blob: blob.__setitem__(key, value), strict=True)
    return stored


def delete_secret(name, *, user_scope_id=None, username=None) -> bool:
    """Remove one value. True when it existed."""
    try:
        key = _key(normalize_name(name), user_scope_id, username)
    except ValueError:
        return False
    found = []

    def _drop(blob):
        if key in blob:
            found.append(blob.pop(key))

    _store().update(_drop, strict=True)
    return bool(found)


def env_for(text, *, user_scope_id=None, username=None) -> Dict[str, str]:
    """The environment a command needs: exactly the `VAF_SECRET_<NAME>` it names and this
    person has stored. A name the person has not stored is left out, so the command sees an
    unset variable rather than a wrong one."""
    wanted = set(_REFERENCE_RE.findall(str(text or "")))
    if not wanted:
        return {}
    try:
        own = _own(_store().load(), user_scope_id, username)
    except Exception:
        return {}
    return {ENV_PREFIX + n: own[n] for n in sorted(wanted) if n in own}


def scrub(text, env: Optional[Dict[str, str]]) -> str:
    """`text` with every handed-over value replaced by its variable's name."""
    out = str(text or "")
    for var, value in sorted((env or {}).items(), key=lambda kv: -len(kv[1] or "")):
        if value and len(value) >= MIN_SCRUB_LENGTH and value in out:
            out = out.replace(value, f"[{var}]")
    return out


def prompt_note(*, user_scope_id=None, username=None, can_store: bool = False) -> str:
    """The lines the model reads about credentials: the stored names, and - when the
    store_credential tool is loaded - how a value the person gives in the chat gets stored.
    "" when there is nothing to say. Without the second part the model did not know the store
    existed while it was empty, and asked for the password to go into the chat."""
    stored = names(user_scope_id=user_scope_id, username=username)
    parts = []
    if stored:
        listed = ", ".join(f"${ENV_PREFIX}{n}" for n in stored)
        parts.append("**Stored credentials:** the user keeps these for your commands: " + listed
                     + ". Use them by name - `$VAF_SECRET_<NAME>` in host_bash, "
                     "`os.environ[\"VAF_SECRET_<NAME>\"]` in python_exec - and never print or "
                     "repeat the value.")
    if can_store:
        parts.append("**A password, token or login the user gives you:** store each value with "
                     "store_credential (a NAME like FTP_PASS) as your FIRST step - before a plan, "
                     "a note, a memory entry or any other tool call, and never repeat the value in "
                     "one: those are logged before the store can remove it. It is then removed from "
                     "this conversation, and you use it by name from then on.")
    return ("\n\n" + "\n".join(parts)) if parts else ""
