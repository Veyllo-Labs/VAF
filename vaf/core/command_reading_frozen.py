# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The reading of a shell command this check had before it read substitutions as commands
they carry, FROZEN: the classifier exactly as it was, kept beside the current one.

vaf/core/command_policy.py classifies every command with its own reading AND with this one,
and refuses whenever either refuses, with both readings' categories. A new reading of the
shell's grammar may then only add refusals, never lose one this reading had: a reading that
cut a command differently (at a substitution, at `&`, at `(`) lost refusals one corner at a
time, each found by generated commands while a fixed corpus stayed green.

Do not edit anything below this docstring. tests/test_command_policy.py pins its text by a
hash; a change of the rules belongs in vaf/core/command_policy.py, which is the reading
that is maintained. The names here are this module's own (it is imported, never used by
name elsewhere), and `classify_command` is the frozen reading's entry point.

Two changes from the text it froze, both made so the gate stays bounded, both with the same
answers as the text they replace:
- the fork-bomb search runs `_fork_bomb_linear` instead of the pattern `_FORK_BOMB_RE`
  (kept below), which grew with the cube of the length (16 KB of `:(){ |:& `: 11 s). It
  refuses every text the pattern refused: the three parts in the same order, found once
  whitespace is removed.
- the search for a substitution where a command starts (`_SUBST_IN_CMD_POS_RE`) runs over
  `_collapsed_blanks(text)`: every run of whitespace becomes one newline when it holds one,
  else one blank. On a long run of newlines the pattern was quadratic (`"x;" + "\n" *
  131069 + "x"`: 125 s); collapsed, 0.001 s. Measured on 300 000 random strings: the same
  answer every time.

This module may be deleted only together with a reading of the full shell grammar that
replaces both readings, proven by the same differential: at least 300 000 generated
commands on the three lanes, none refused here and allowed there.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

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
# Logging in to another machine with THIS computer's OpenSSH setup: its owner's ~/.ssh, its
# config and its agent, for whichever account runs the command, and a password prompt that
# lands on a terminal nobody sees. The host lane refuses it and points at the ssh tool, which
# keeps each account's own key and servers (vaf/core/ssh.py). `git` over ssh is not this.
_REMOTE_LOGINS = frozenset({"ssh", "scp", "sftp"})
# Moving files over FTP with this computer's own clients: the password stands on a command
# line every process can read, nobody is asked about a new server, and a certificate that
# changed goes unnoticed. The host lane refuses it and points at the ftp tool
# (vaf/core/ftp.py). curl and wget count when an argument is an ftp:// or ftps:// address.
_FTP_CLIENTS = frozenset({"ftp", "lftp", "ncftp", "ncftpput", "ncftpget", "tnftp", "pftp"})
# The address alone, or as an option's value (`--url=ftp://...`).
_FTP_URL_RE = re.compile(r"^(?:--?[A-Za-z][\w-]*=)?['\"]?ftps?://", re.IGNORECASE)
# The Python interpreter by name (`python`, `python3`, `python3.13`, the free-threaded
# `python3.13t`, PyPy's `pypy3`; Windows' `py` launcher).
_PYTHON_EXE_RE = re.compile(r"^(?:(?:python|pypy)(?:\d+(?:\.\d+)?)?t?|py)$")
# Project runners that start the command given as a later word (`uv run python -c ...`).
_PYTHON_RUNNERS = frozenset({"uv", "uvx", "poetry", "pipenv", "pdm", "hatch", "rye", "conda",
                             "pixi"})
# The words between a runner and its command that select what it does (`uv tool run`).
_RUNNER_VERBS = frozenset({"run", "exec", "tool"})
# Builtins that run Python code they are given as a string: the string is read like the code.
_CODE_STRING_CALLS = frozenset({"exec", "eval", "compile"})
# How deep `exec("exec('...')")` is parsed; deeper code is searched as text.
_MAX_CODE_DEPTH = 4
# Python's short options that take a value besides -c and -m.
_PYTHON_VALUE_LETTERS = frozenset("WX")
# Code that does not parse is searched as text: the module name, or a quoted ftp:// address.
_PYTHON_FTP_TEXT_RE = re.compile(r"\bftplib\b|['\"]\s*ftps?://", re.IGNORECASE)
# ssh's single-letter options that take a value (ssh(1) synopsis).
_SSH_VALUE_LETTERS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
# How deep `bash -c "sh -c '...'"` may nest before the command is refused as unreadable.
MAX_NESTING = 4
# How many nested commands one top-level command may carry in all before it is refused as
# unreadable. The depth limit alone does not bound the work: a runner's command is read at
# every option's value position, and each such candidate that is a runner again branches
# anew, so `uv run -a uv -a uv ... python` grew with the 4th power of its length (measured:
# 30 options 2.4 s, 80 options over 300 s, on the gate path before anyone is asked).
MAX_NESTED_COMMANDS = 64
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
def _fork_bomb_linear(text: str) -> bool:
    """`:(){`, `|:&` and `};:` in that order once whitespace is removed: refuses every text
    the pattern above matches (and may refuse more), with three plain searches instead of
    two unbounded `.*`, which grew with the cube of the length."""
    flat = re.sub(r"\s+", "", text)
    start = flat.find(":(){")
    pipe = flat.find("|:&", start + 4) if start >= 0 else -1
    return pipe >= 0 and flat.find("};:", pipe + 3) >= 0


def _collapsed_blanks(text: str) -> str:
    """Every run of whitespace as one newline when it holds one, else one blank: the same
    matches for `_SUBST_IN_CMD_POS_RE`, without its quadratic search on long runs."""
    return re.sub(r"\s+", lambda m: "\n" if "\n" in m.group(0) else " ", text)


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
    "remote_login": "logs in to another machine with this computer's own SSH setup; use the "
                    "ssh tool, which keeps each account's key and servers apart and answers "
                    "the password prompt",
    "ftp_transfer": "moves files over FTP with this computer's own tools (an FTP client, curl "
                    "or Python's ftplib), with the password in the command or the code; use the "
                    "ftp tool, which asks before a new server, checks its certificate and keeps "
                    "the password out of every command",
}

# What each profile refuses outright. Everything else in CATEGORY_REASONS is a note.
_BLOCKING = {
    "host": ("fork_bomb", "device_write", "destructive_removal", "pipe_to_shell",
             "opaque_command", "nested_too_deep", "remote_login", "ftp_transfer"),
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
        # `curl.exe` and `ftp.exe` on a Windows host are curl and ftp: judged by the name
        # without the suffix, or every rule below was one `.exe` away from passing.
        base = _exe_base(tok)
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


def _nested_commands(exe: str, args: List[str]) -> Iterable[tuple]:
    """(where, text) for every command this one runs from TEXT it is given: "local" for
    `sh -c`, `su -c`, `eval` and the command a project runner starts (`uv run ...`, joined
    back into one text, lazily, so a caller that stops early does not pay for the rest),
    "remote" for `ssh host ...`."""
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
    return (("local", shlex.join(args[start:])) for start in _runner_commands(exe, args))


def _is_ftplib(name) -> bool:
    return isinstance(name, str) and name.split(".", 1)[0] == "ftplib"


def _call_argument(node, keyword: str):
    """The constant first argument of a call, positional or as `keyword=`, or None."""
    import ast
    value = node.args[0] if node.args else next(
        (k.value for k in node.keywords if k.arg == keyword), None)
    return value.value if isinstance(value, ast.Constant) else None


def python_code_categories(code: str, *, _depth: int = 0) -> List[str]:
    """["ftp_transfer"] when Python `code` moves files over FTP, else []. Read from the syntax
    tree, so a comment naming ftplib does not count: an import of ftplib (also `from ftplib
    import ...`), a string that IS the module's name, `ftplib` or `ftplib.<name>` (whatever
    imports it: `__import__`, `importlib.import_module` also under an alias, a getattr of
    `__import__`, `-m ftplib` in a subprocess argument list), or a string that starts with
    ftp:// or ftps:// (urllib's FTP, an f-string's literal part). A constant string handed to
    exec, eval or compile is read as code too, a few levels deep. Code that does not parse,
    or nests deeper, is searched as text. Never raises."""
    import ast
    text = code.decode("utf-8", "replace") if isinstance(code, bytes) else str(code or "")
    if _depth > _MAX_CODE_DEPTH:
        return ["ftp_transfer"] if _PYTHON_FTP_TEXT_RE.search(text) else []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ["ftp_transfer"] if _PYTHON_FTP_TEXT_RE.search(text) else []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit = any(_is_ftplib(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            hit = _is_ftplib(node.module)
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if called in _CODE_STRING_CALLS:
                inner = _call_argument(node, "source")
                hit = isinstance(inner, (str, bytes)) and bool(
                    python_code_categories(inner, _depth=_depth + 1))
            else:
                hit = False
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = (_is_ftplib(node.value)
                   or node.value.lstrip().lower().startswith(("ftp://", "ftps://")))
        else:
            hit = False
        if hit:
            return ["ftp_transfer"]
    return []


def _runner_commands(exe: str, args: List[str]) -> List[int]:
    """Where in `args` each word list starts that may be the command a project runner starts
    (`uv run CMD ...`, `poetry run`, `conda run -n env`, `uv tool run`); [] when `exe` is no
    runner. Each such list runs to the end of `args`. The caller classifies each one like the
    text `bash -c` carries, so the wrapper unwrap, the shell descent, the depth limit and
    every category reach the runner's command too.

    A runner's words are read up to its command: its verbs (`run`, `exec`, `tool`), its
    options, and the word after an option, which may be that option's value. `--` names the
    command explicitly. The first plain word that is not in a value position is the command,
    and what follows it is that command's data (`uv run printf '%s' python -c ...` prints, it
    starts nothing).

    Named boundary: which options take a value is not known here, so the word after ANY
    option is read both as that option's possible value and as a possible command. A harmless
    command after an option without a value is therefore refused only when what follows it is
    refused in its own right (`conda run -n py --no-capture-output echo python -c 'import
    ftplib'`), and a value that is itself a refused program's name is refused as one (`uv run
    --with ssh pytest`); `uv run -q echo hi` passes. That is the safe side: the other reading
    would let `conda run --cwd /srv python -c ...` pass whenever an option is missing from a
    table, and such a table would be one more copy per runner (uv, poetry, conda, pipenv,
    hatch, pdm) of flags that change with each release. `--` before the command lifts both
    when no word stands in a value position before it; otherwise that word may be the
    command and `--` its first argument (`uv run -q ssh -- host` logs in to `host`), so both
    readings are classified. Writing the value with `=` (`uv run --with=ssh -- pytest`)
    leaves no word in a value position."""
    if exe not in _PYTHON_RUNNERS:
        return []
    found: List[int] = []
    maybe_value = False                 # the word before was an option without `=value`
    for pos, tok in enumerate(args):
        if tok == "--":
            # The rest is the command, whatever it looks like. A value-position word before
            # it stays a candidate: if it was the command, `--` is that command's argument.
            return found + ([pos + 1] if pos + 1 < len(args) else [])
        if tok.startswith("-"):
            maybe_value = "=" not in tok
            continue
        if maybe_value:                 # a possible value, and a possible command
            found.append(pos)
            maybe_value = False
            continue
        if tok in _RUNNER_VERBS:
            continue
        found.append(pos)               # the command; its words are its data
        return found
    return found


def _exe_base(word: str) -> str:
    """`/usr/bin/python3.exe` -> `python3`: the program name a path or a Windows name gives."""
    base = word.rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _python_command_categories(args: List[str]) -> List[str]:
    """What `python [options] -c CODE` or `python -m MODULE` does, judged like python_exec's
    code: `-c` as its own word, at the end of a short cluster (`-Ic CODE`) or with the code in
    the same word (`-cCODE`); `-m ftplib` is ftplib's own command-line client. A script file
    is not read (module docstring)."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            return []                       # a script file, stdin, or the end of the options
        if tok.startswith("--"):
            i += 2 if tok == "--check-hash-based-pycs" else 1
            continue
        letters = tok[1:]
        step = 1
        for pos, letter in enumerate(letters):
            rest = letters[pos + 1:]
            if letter in ("c", "m"):
                value = rest if rest else (args[i + 1] if i + 1 < len(args) else "")
                if letter == "c":
                    return python_code_categories(value)
                return ["ftp_transfer"] if _is_ftplib(value) else []
            if letter in _PYTHON_VALUE_LETTERS:
                step = 1 if rest else 2
                break
        i += step
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


def classify_command(command: str, *, profile: str = "host", _depth: int = 0,
                     _budget: Optional[List[int]] = None,
                     _stdin_fetch: bool = False) -> CommandVerdict:
    """Classify a shell command offline. Never raises, never executes anything.

    profile="host"   - unsandboxed lane: refuse the catastrophic set.
    profile="jailed" - bubblewrap/container lane: refuse only what escapes the
                       jail or hurts the machine.
    profile="remote" - runs on another machine: the host set minus the fetch into
                       a shell, which is named instead (module docstring).

    `_budget` (one shared counter) caps the classifications one top-level call makes in
    all (MAX_NESTED_COMMANDS); `_stdin_fetch` says a nested command's input is code fetched
    from the network (`curl x | uv run bash`).
    """
    verdict = CommandVerdict()
    text = (command or "").strip()
    if not text:
        return verdict
    if _budget is None:
        _budget = [MAX_NESTED_COMMANDS]
    # Every call spends from the budget, a refused one too: a refusal that cost nothing
    # let a runner at the depth limit try each of its value positions, each one joined
    # back into text, which made the work quadratic in the command's length.
    _budget[0] -= 1
    if _depth > MAX_NESTING or _budget[0] < 0:
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
    opaque = bool(_SUBST_IN_CMD_POS_RE.search(_collapsed_blanks(text)))
    cats: List[str] = []

    if _fork_bomb_linear(text):
        cats.append("fork_bomb")
    if opaque:
        cats.append("opaque_command")

    # What a nested command found. A refusal inside refuses the whole; a remote command's
    # categories are shown but judged by the remote profile, not by this one.
    nested_refusal = ""
    remote_cats: List[str] = []

    pipeline_fetch = _stdin_fetch
    for connector, seg in pairs:
        # Only a pipe continues a pipeline. `curl x > f; python parse.py` is a
        # download followed by a separate, human-visible command - the block is
        # reserved for the unreviewable direct pipe into an interpreter. A segment
        # outside a pipe reads this command's own input, fetched code when a fetch
        # pipes into the runner or shell that carries it.
        if connector != "pipe":
            pipeline_fetch = _stdin_fetch
        toks = _tokens(seg)
        if not toks:
            continue
        exe_at, exe = _executable_at(toks)
        args = toks[exe_at + 1:] if exe_at >= 0 else []
        low = seg.lower()

        # A local nested command's output is this segment's output: fetched or remote
        # content when it fetches (`uv run curl x | bash`). Applied after this segment's own
        # sink check, so `bash -c 'curl x'` alone is no pipe into a shell.
        nested_fetch = False
        # Once the budget is spent a refusal is recorded (the call that overdrew it was
        # refused, and its refusal reached this loop), so a later segment's nested commands
        # are not read at all: `uv run echo ok; uv run echo ok; ...` would otherwise join
        # and classify one candidate per segment, past the bound the budget sets per call.
        nested = _nested_commands(exe, args) if _budget[0] >= 0 else ()
        for where, inner in nested:
            local = where != "remote"
            sub = classify_command(inner, profile=profile if local else "remote",
                                   _depth=_depth + 1, _budget=_budget,
                                   _stdin_fetch=pipeline_fetch and local)
            target = cats if local else remote_cats
            target.extend(c for c in sub.categories if c not in target)
            if local and ("network_fetch" in sub.categories
                          or "remote_login" in sub.categories):
                nested_fetch = True
            if sub.blocked and not nested_refusal:
                nested_refusal = sub.reason
            # The budget is spent and this refusal is recorded: the remaining candidates
            # would each be refused the same way, after being joined back into text.
            if _budget[0] < 0:
                break

        if exe in _REMOTE_LOGINS and "remote_login" not in cats:
            cats.append("remote_login")
        if (exe in _FTP_CLIENTS or (exe in _NETWORK_FETCHERS
                                    and any(_FTP_URL_RE.match(a) for a in args))
                or (_PYTHON_EXE_RE.match(exe) and _python_command_categories(args))) \
                and "ftp_transfer" not in cats:
            cats.append("ftp_transfer")

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
        if nested_fetch:
            pipeline_fetch = True

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
