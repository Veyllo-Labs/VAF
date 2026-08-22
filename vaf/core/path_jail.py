# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Keeping a caller-supplied path inside the directory it is allowed to touch.

Every surface that takes a path fragment from outside - an HTTP body, a tool
argument, a peer's frame - has to answer the same question: does this name stay
inside the root I opened it against? The answer is only correct when it is
decided on RESOLVED paths. A check built from string prefixes accepts a symlink
that lives inside the root and points anywhere on the host, and the write then
lands at the link's target while every lexical test still says "inside".

That is not hypothetical here: the workspace endpoints compared normalized
strings, so a link planted inside a chat's workspace by any shell-capable tool
run carried mkdir, upload and delete straight out of the jail.

Two helpers, because the same two decisions were being hand-rolled side by side:
one for a path fragment that may address folders (`contained_path`), one for a
single entry name that may not address anything but itself (`safe_entry_name`).

Deliberately free of any web or tool dependency: the callers are an HTTP layer,
an agent-to-agent store and file tools, so the primitive raises `PathEscape` and
lets each surface translate that into its own refusal.

Scope of the conversion so far: the workspace lane and the room workspace, the
two places whose own copies were WRONG. Several correct hand-rolled copies of
the same comparison remain (the coder's documentation write, the threat list,
the skill scanner). Converting them deletes a few lines each and is mechanical,
but each sits in a different subsystem with its own design doc, and none of them
is broken - so they are a follow-up, not a silent drive-by inside this one.
"""
import os
import unicodedata
from pathlib import Path

__all__ = ["PathEscape", "contained_path", "safe_entry_name"]


class PathEscape(ValueError):
    """A caller-supplied path or name did not stay inside its root.

    Rejected rather than sanitised. Trimming an escape into something harmless
    would hand the caller a DIFFERENT path than the one they named, and a
    silently redirected write is harder to reason about than a refusal.
    """


def contained_path(root, relative: str = "", *, must_exist: bool = False) -> Path:
    """The absolute, resolved path ``relative`` names inside ``root``.

    Raises `PathEscape` if the fragment is absolute, walks upwards, or resolves
    to somewhere outside ``root`` - including by way of a symlink that lives
    inside the root and points out of it, which is exactly the case a prefix
    comparison cannot see.

    Both sides are resolved before they are compared, so a root that itself
    lies under a symlinked parent (a home directory on many systems) does not
    produce a false refusal. Resolution is non-strict, so a path that does not
    exist yet still has a containment answer: callers create entries through
    this (a new folder, an uploaded file) and must be told where the write
    would land BEFORE it happens.

    ``must_exist=True`` additionally requires the resolved path to be an
    existing directory - the shape a listing or a browse needs.

    The returned path is the RESOLVED one, which is the location a write
    actually reaches. Callers that keep displaying the unresolved name are
    showing a label, not a target.
    """
    fragment = str(relative or "").strip()
    # Both separators, whatever the host: a fragment reaches this from a JSON
    # body or a peer, so it carries the sender's convention, not ours.
    normalized = fragment.replace("\\", "/").strip("/")
    if normalized:
        parts = [p for p in normalized.split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise PathEscape("path must not walk upwards")
        if any("\x00" in p for p in parts):
            raise PathEscape("path must not contain a null byte")
    else:
        parts = []
    if os.path.isabs(fragment) or (len(fragment) > 1 and fragment[1] == ":"):
        raise PathEscape("path must be relative")

    root_resolved = Path(root).resolve()
    target = root_resolved.joinpath(*parts).resolve() if parts else root_resolved
    if target != root_resolved and root_resolved not in target.parents:
        raise PathEscape("path must stay inside the root")
    if must_exist and not target.is_dir():
        raise PathEscape("folder not found")
    return target


def safe_entry_name(name: str, *, allow_hidden: bool = False) -> str:
    """One entry name, or `PathEscape`.

    For the case where a caller may name a single file or folder and nothing
    about where it sits: the parent comes from the surface, never from the
    name. So a name carrying a separator is refused rather than trimmed to its
    last component, because trimming would quietly accept ``a/b`` as ``b``.

    Control characters are refused too. They are never part of a name anyone
    meant to write, and a null byte in particular reaches the filesystem call
    as a `ValueError` - which a surface then reports as an internal failure
    rather than as the bad input it is.

    Hidden names stay out by default: the surfaces that hand a name straight to
    a filesystem call are the ones that also hide dotfiles when listing, and a
    name that cannot be seen afterwards is not a name the caller can manage.
    """
    raw = str(name or "").strip()
    if not raw:
        raise PathEscape("name must not be empty")
    if raw in (".", ".."):
        raise PathEscape("name must not be a relative marker")
    if "/" in raw or "\\" in raw:
        raise PathEscape("name must not contain a path separator")
    if os.path.basename(raw) != raw:
        raise PathEscape("name must be a single entry")
    if any(unicodedata.category(ch) == "Cc" for ch in raw):
        raise PathEscape("name must not contain control characters")
    if not allow_hidden and raw.startswith("."):
        raise PathEscape("name must not be hidden")
    return raw
