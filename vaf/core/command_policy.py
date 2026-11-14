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

Four profiles, because the lanes have different confinement:
- ``host``   - vaf/tools/host_bash.py runs unsandboxed on the machine, with
  the whole environment. Its only other control is the human approval, so the
  catastrophic set is refused outright.
- ``host_unattended`` - the same shell where nobody confirmed THIS call (the coding
  agent, a workflow step, a direct ``run``): the host set plus ``share_path``. A
  command that names a network share or a Windows device path (``\\\\host\\share``,
  ``\\\\?\\UNC\\...``, ``file://host/...``) makes Windows open an SMB connection that
  carries the account's NTLM login attempt to that host, and no person saw the command.
  In ``host`` the same category is a note the dialog shows, and a yes runs it.
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
by one or opened by an earlier command, _script_input) is `opaque_command` too, a literal
here-string fed to a shell is read as shell text, and every opaque refusal names the way
out for its shape (_OPAQUE_REASONS). Every `$((` is read both ways, as arithmetic and as a
command, since a `#` comment or a backtick moves where bash closes it; so is an arithmetic
command `(( ... ))`. An array assignment's elements (`arr=($(ls))`) are words, not a
program. Every reading spends from one budget per call (MAX_NESTED_COMMANDS), and a
runner's candidates and the readings of a `$((`, each nearly the whole text again, from a
second one in characters (_RUNNER_TEXT_SLACK); the fork-bomb search is three plain
searches. The readings stay linear in the command's length; shlex's own tokenizer is not
on one word of hundreds of kilobytes (`x=` repeated: 20 s at 1 MB, the same before), so a
command over _MAX_COMMAND_CHARS (128 K characters) is refused as unreadable before it is read.

Every verdict is joined with the FROZEN reading (vaf/core/command_reading_frozen.py, the
check as it read commands before substitutions were carried): the command is refused
whenever either reading refuses, with both readings' categories
(_join_the_frozen_reading). This reading may therefore only add refusals; every other
construction of a new reading lost some that bash runs, one corner at a time, each found
by generated commands. Its price: what the frozen reading over-refused stays refused
(`echo 'a; $(x)'`, words after a closed substitution such as `echo $(true) rm -rf /`).

NAMED BOUNDARY: the shell's compound grammar is not read at the top level: brace groups
(`curl x | { bash; }`), `if`/`for`/`while`/`case`, variables and `${...}` as the program,
ANSI-C quoting (`$'...'`), globs and brace expansion in the program name, line continuation
inside a word, and wrappers that take a command string of their own (`env -S`, `watch`,
`find -exec`). Text that reaches a shell by another way than its own arguments,
here-string or a substitution is not read: a literal piped in (`echo '...' | bash`,
`printf ... | bash`), `xargs -I{} sh -c {}`, a coprocess, a named pipe, a script file an
earlier command wrote and a later one runs. Arithmetic evaluates an array subscript held in
a variable as code (`x='a[$(...)]'; echo $((x))`, `printf -v 'a[$(...)]' v`), which the
text does not show. The reserved word `case` where a command starts inside a substitution
refuses the command, since a pattern's `)` would close the substitution early: `case`, one
shell word (quote-aware) and `in`. The word elsewhere, also at the start of a line of prose
("case 2 is covered"), does not. Carriers the check does not step over (`flock`, `strace`,
`chroot`, `busybox`, `runuser`, `coproc`, `find -exec`, `watch`) hide the command they run,
also behind a substitution (`flock f $(true) cmd`), as their plain forms did before; a
descriptor named through a variable (`exec {fd}< ...; bash /dev/fd/$fd`) is not followed.
A redirection glued to the target (`rm -rf ~>/dev/null`) and a `case` body at the top level
(`case x in x) rm -rf /;; esac`) are not read, by either reading.

NAMED BOUNDARY, comments and heredocs. Neither reading takes a `#` as a comment, so an
apostrophe in a comment opens a quote that swallows later lines (`echo hi # it's` then a
second line), as before, and a comment ending in a backslash joins the next line. The
frozen reading reads every line of a heredoc body as a command line: a body line that
STARTS with what reads as a command is refused (`rm -rf / was the bug`), one that only
mentions it is not, and an apostrophe in a body opens a quote there too. This reading takes
a heredoc body as data where it is surely one (`<<WORD`, `<<'WORD'`, `<<-WORD` at the top
level, outside quotes, `${...}`, `$[...]` and a comment, at most eight on one line, with
its delimiter line present, also when a CRLF text lost the last line's `\r`): no command
lines, no nested readings, so a file written through `cat > f <<'EOF'` passes when the
frozen reading lets it; an unquoted body's substitutions are read as commands, never as a
program word, and a body fed to a shell is that shell's script. Everywhere else the lines
are read as command lines, so a wrong guess here can only cost refusals this reading
adds, never one of the frozen reading. A `<<` after a `#` that starts a comment for bash
on its line (_comment_starts_at) opens no heredoc: bash takes the rest of the line as a
comment and runs the next lines (`echo ok # <<X`).
More than eight heredocs on one line are not something a person writes;
the rest are read as command lines. Its accepted cost: a heredoc piped into a shell is not
checked for substitutions inside its body (`cat <<'EOF' | bash` with `eval "$(...)"` in
the body), as before this reading; a dangerous command line in it is still refused.
Design rule for any reading: a new one may only
ADD refusals to the existing reading, never remove one; the frozen reading holds it.

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

import logging
import os
import posixpath
import re
import shlex
import traceback
import unicodedata
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from vaf.core.path_jail import is_remote_path

_log = logging.getLogger(__name__)

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
# Flags of a runner known to take no value. After one, the next word is the command, not a
# value; after any other flag both readings stay (_runner_commands). The npx and npm rows are
# measured with npm's own parser: bin/npx-cli.js's argument loop, then nopt with npm's types
# and shorthands, left the next word as the package for every flag here (npm 11). The other
# rows rest on each tool's documentation only, since none of them could be run where this
# was written.
_NPM_SWITCHES = frozenset({"-y", "--yes", "--no", "-q", "--quiet", "--workspaces",
                           "--include-workspace-root", "--legacy-peer-deps", "--prefer-offline",
                           "--prefer-online", "--offline", "--ignore-scripts", "--force",
                           "--silent", "--no-fund", "--no-audit"})
_UV_SWITCHES = frozenset({"-q", "--quiet", "-v", "--verbose", "--offline", "--isolated",
                          "--refresh", "--reinstall", "-U", "--upgrade", "-n", "--no-cache",
                          "--no-config", "--native-tls", "--compile-bytecode"})
_VALUELESS_RUNNER_FLAGS = {
    "uv": _UV_SWITCHES | {"--frozen", "--locked", "--no-sync", "--no-project", "--active",
                          "--exact", "--no-dev", "--all-extras", "-m", "--module",
                          "--no-editable", "--no-install-project", "--no-install-workspace",
                          "--only-dev", "--all-packages", "-s", "--script", "--no-build"},
    "uvx": _UV_SWITCHES,
    "npx": _NPM_SWITCHES | {"--no-install", "--ignore-existing"},
    "npm": _NPM_SWITCHES,
    "bunx": frozenset({"--bun"}),
    "pipx": frozenset({"-q", "--quiet", "-v", "--verbose", "--no-cache", "--pypackages"}),
    "pnpm": frozenset({"-s", "--silent"}),
    "yarn": frozenset({"-s", "--silent"}),
    "poetry": frozenset({"-q", "--quiet", "-v", "-vv", "-vvv", "--verbose", "-n",
                         "--no-interaction", "--no-plugins", "--no-cache", "--ansi",
                         "--no-ansi"}),
    "conda": frozenset({"--no-capture-output", "--live-stream", "-v", "--verbose", "--dev",
                        "--debug-wrapper-scripts"}),
}
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
# The frozen reading's readings, scaled to the text's length: as many as fit into this much
# text read in all, at most MAX_NESTED_COMMANDS: all 64 up to 12 KB, 6 at the length cap.
# Measured: 64 readings of its worst shape cost about 1 s at 13 KB.
_FROZEN_TEXT_BUDGET = 768 * 1024
# The opening of a refusal that names no pattern: the command was not read, or not all of it.
_REFUSED = "This command is refused: it "
# The longest command the check reads at all, in characters. A person approving a command
# cannot read more than this either, and both readings together, with shlex's own tokenizer
# quadratic on one long word, took up to 44 s at 1 MB (measured); a longer command is
# refused as unreadable.
_MAX_COMMAND_CHARS = 128 * 1024

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
    "too_long": "is longer than anyone can review before it runs; write the text to a file "
                "with the file tool and run the file",
    "unreadable": "could not be read by this check; write it as several simpler commands, or "
                  "put the text into a file",
    "remote_login": "logs in to another machine with this computer's own SSH setup; use the "
                    "ssh tool, which keeps each account's key and servers apart and answers "
                    "the password prompt",
    "ftp_transfer": "moves files over FTP with this computer's own tools (an FTP client, curl "
                    "or Python's ftplib), with the password in the command or the code; use the "
                    "ftp tool, which asks before a new server, checks its certificate and keeps "
                    "the password out of every command",
    "share_path": "names a network share or a Windows device path, and on Windows opening a "
                  "share on another computer sends this account's login to that computer",
}

# The other ways a command hides what runs, each with its way out (all `opaque_command`).
_OPAQUE_REASONS = {
    "tool_setup": "runs the shell setup another tool prints, so the text being approved is not "
                  "the text that will run; use the tool's own direct form (`conda run -n ENV "
                  "...`, `pyenv exec ...`, `direnv exec . ...`, `opam exec -- ...`, "
                  "`ssh-agent bash -c '...'`), or save its output to a file, read the file, "
                  "then run it with `.`",
    "shell_text": "runs shell text that another command prints, so the text being approved is "
                  "not the text that will run; save the output to a file, read the file, then "
                  "run it (for an installer: `curl -fsSL -o install.sh URL`, then "
                  "`bash install.sh`)",
    "descriptor": "runs a script from a descriptor an earlier command opened, so the text being "
                  "approved does not show what runs; save the script to a file, read it, then "
                  "run the file",
    "option_value": "hands a runner option a value from a command substitution, words the "
                    "runner may also start as its program; write a long option's value with "
                    "`=` and in double quotes (`--with=\"$(cat r)\"`, "
                    "`--directory=\"$(pwd)\"`), or run the substitution first and write the "
                    "value in",
    "env_values": "takes its variables and program from a command substitution, so the text "
                  "being approved is not the text that will run; load a .env file with "
                  "`set -a; . ./.env; set +a; make`",
    "arith_backtick": "uses a backtick inside `$(( ))`, where bash may run the text as a command "
                      "instead of evaluating it; write `$(...)` instead of the backticks "
                      "(`echo $(( $(date +%s) - 10 ))`)",
    "lines": "; if it is text spanning lines (a continued line, quoted text, a heredoc or a "
             "code block), join the lines, or write the text to a file",
    "frozen": "has a substitution where a command may start: after a `;`, `&&` or `|`, also "
              "one inside quotes, or at the start of a line, it reads as the start of a new "
              "command; write the substitution's result into a variable first, or put the "
              "text into a file",
    "runner_args": "gives a runner a command substitution among its words after a flag this "
                   "check does not know, so the substitution may be read as the program the "
                   "runner starts; put its result into a variable first "
                   "(`files=$(git ls-files tests); uv run --frozen pytest $files`), or, if the "
                   "flag takes a value, write it as `--flag=value`",
    "unreadable": "has a `case` statement inside a substitution, which this check cannot "
                  "read; write the case as a command of its own",
}

# What a refusal adds after the reason, where the reason alone names no way out. Only for a
# refusal: on the remote lane a fetch into a shell is a note, and the note stays as it is.
_WAY_OUT = {
    "pipe_to_shell": "; download it to a file, read the file, then run it "
                     "(`curl -fsSL -o install.sh URL`, then `bash install.sh`)",
    "share_path": "; nobody confirmed this command, so tell the person which path you need: "
                  "they can run the command in the chat, where they see and confirm it, or "
                  "copy the files to a local folder, and you then work on the copy",
}

# What each profile refuses outright. Everything else in CATEGORY_REASONS is a note.
_BLOCKING = {
    "host": ("fork_bomb", "device_write", "destructive_removal", "pipe_to_shell",
             "opaque_command", "nested_too_deep", "remote_login", "ftp_transfer", "too_long"),
    # The jail has no network and its workspace is disposable; only what reaches the
    # machine or the jail root is refused.
    "jailed": ("fork_bomb", "device_write", "destructive_removal", "opaque_command",
               "nested_too_deep", "too_long"),
    # Another machine: see the module docstring for why the fetch-into-shell is only named.
    "remote": ("fork_bomb", "device_write", "destructive_removal", "opaque_command",
               "nested_too_deep", "too_long"),
}
# The host shell where nobody confirmed this call (the module docstring): a share or device
# path is refused there, a note in `host`.
_BLOCKING["host_unattended"] = _BLOCKING["host"] + ("share_path",)
# The profiles whose commands run on this machine with its own network login. The jail has
# no network and is Linux; a remote command runs on another machine.
_SHARE_PROFILES = ("host", "host_unattended")


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
    # The piece read as a share (`share_path`), so a refusal names it; also one only the
    # `python -c` code's own escapes spell, which the raw text does not show.
    share_piece: str = field(default="", repr=False)

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
    is joined to its predecessor: "pipe" only for a single `|` (and `|&`), everything else
    (";", "&&", a newline) breaks the pipeline. That distinction carries the whole
    pipe-to-shell judgement: `curl x | bash` is unreviewable, while `curl x > f; python
    parse.py` is a download followed by a separate, visible command (measured false
    positive of the first version). The `&` of a redirection (`2>&1`, `&>f`) is no separator.

    A substitution is a command the segment CARRIES, not a cut in it. Its text is returned
    whole, with its kind: "command" for `$( )` and backticks, "arithmetic" for a `$(( ))`
    whose parentheses show bash evaluates it (the one after `$(` closes right before the
    outer one, and no `#` or backtick inside, both of which move where bash closes it),
    "arithmetic*" for any other `$((`, "subshell" for `( )`, "array" for the elements of an
    array assignment (`arr=($(ls))`, words, not a program), "process_in" for `<( )`,
    "process_out" for `>( )`, "unreadable" for one holding a `case` statement. Where it
    stood in the segment a mark is left: _SUBST_BARE outside quotes, _SUBST_QUOTED inside
    double quotes, _SUBST_PROC for a process substitution (a file name), none for a
    subshell or an array. Cutting the segment there, as before, separated the command word
    from its wrapper (`env $(echo rm) -rf /` became `env`, `echo rm` and `-rf /`, and
    passed) and broke a pipe in two (`curl x | tee $(mktemp) | bash` passed). A segment that
    has no text of its own but carries a substitution is kept: `(bash)`.

    Nothing is hidden behind a `#`: it is no comment here. A `<<WORD` opens a heredoc only
    where it surely is one (at this level, outside quotes, `${...}`, `$[...]` and a comment,
    with its delimiter line present); its body is then data, attached to the segment that
    wrote `<<`, and otherwise its lines are read as command lines (named boundary in the
    module docstring).

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
    sub_opener = ""                 # what opened it: `$(`, `(` or a backtick
    sub_case = False                # a `case` statement inside the open substitution
    # The open `$((`: did its inner parenthesis close right before the outer one, and did a
    # `#` or a backtick (both of which change where bash closes it) appear inside?
    arith_closed, arith_clean, arith_tainted = False, False, False
    quote = ""       # active quote character, "" when outside quotes
    # Nesting of $( ), ( ) and ` `. A closer entered from inside double quotes is
    # stored with the quote in front ('")', '"`') so leaving it re-enters the quote.
    depth_stack: List[str] = []
    i = 0
    n = len(command)

    def _put(text: str) -> None:
        (sub_buf if depth_stack else buf).append(text)

    def _open(closer: str, kind: str, opener: str) -> None:
        nonlocal sub_kind, sub_buf, sub_case, sub_opener
        nonlocal arith_closed, arith_clean, arith_tainted
        if depth_stack:
            sub_buf.append(opener)          # an inner one is part of the outer's text
            if sub_kind == "arithmetic" and opener == "`":
                arith_tainted = True
        else:
            sub_kind, sub_buf, sub_case, sub_opener = kind, [], False, opener
            arith_closed, arith_clean, arith_tainted = False, False, False
            if kind in ("command", "arithmetic") and opener != "(":
                buf.append(_SUBST_QUOTED if closer.startswith('"') else _SUBST_BARE)
            elif kind in ("process_in", "process_out"):
                buf.append(_SUBST_PROC)
        depth_stack.append(closer)

    def _close(closer_text: str, nxt: str) -> bool:
        """True when the outermost substitution closed."""
        nonlocal quote, arith_closed, arith_clean
        if depth_stack.pop().startswith('"'):
            quote = '"'
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

    # Heredocs of this level waiting for their body after the line ends: (delimiter, quoted,
    # strip_tabs, segment that wrote `<<`). Only where it is surely one (_heredoc_word, a
    # delimiter line exists); otherwise its lines are read as command lines.
    pending: List[tuple] = []
    lines: Optional[_HeredocLines] = None
    # A `#` that starts a comment for bash earlier on this line (_comment_starts_at, at this
    # level, outside `${...}` and `$[...]`): the rest of the line is no code, so a `<<WORD`
    # there opens no heredoc and the next lines run. Its own variable, since a `;` flush
    # clears `buf`; every newline ends it, also one after a backslash.
    comment = False
    # The closers of the `${` and `$[` open at this level, outside quotes and comments: read
    # as the text goes, so a quoted or escaped `${` opens none (a tail search was blind to
    # quotes, and a `}` in a comment then let a fake heredoc open).
    expansions: List[str] = []
    word_paren = -1                     # the last `)` at this level that continues a word

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if ch == "\n" or (ch == "\\" and nxt == "\n"):
            comment = False
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
        if not depth_stack and not comment:
            if ch == "$" and nxt in ("{", "[") and _opens_expansion(command, i):
                expansions.append("}" if nxt == "{" else "]")
            elif expansions and ch == "[" and expansions[-1] == "]" and command[i - 1] != "$":
                expansions.append("]")      # `$[ a[1] ]`: a subscript inside
            elif expansions and ch == expansions[-1]:
                expansions.pop()
        if ch == "#" and not depth_stack and not expansions \
                and _comment_starts_at(command, i, word_paren):
            comment = True
        if (ch == "<" and nxt == "<" and not depth_stack and command[i + 2:i + 3] != "<"
                and command[i - 1:i] != "<" and len(pending) < 8 and not comment
                and not expansions):
            word = _heredoc_word(command, i)
            line_end = command.find("\n", i) if word is not None else -1
            if word is not None and line_end >= 0:
                end, delim, quoted, strip_tabs = word
                lines = lines or _HeredocLines(command)
                if lines.after(delim, line_end + 1, strip_tabs) is not None:
                    pending.append((delim, quoted, strip_tabs, len(segments)))
                    buf.append(command[i:end])
                    i = end
                    continue
        if ch == "\n" and pending and not depth_stack:
            # The bodies are data for this reading: not command lines, no reading spent. An
            # unquoted one's substitutions run and are read, never as a program word.
            pos = i + 1
            for delim, quoted, strip_tabs, segment in pending:
                span = lines.after(delim, pos, strip_tabs) if lines else None
                if span is None:
                    break
                text = command[pos:span[0]]
                target = subs if segment >= len(segments) else segments[segment][2]
                found = [] if quoted else _heredoc_substitutions(text)
                target.extend(found)
                # What the body supplies: an unquoted one after its own escapes are removed.
                target.append(("heredoc_u" if found else "heredoc_q",
                               text if quoted else _heredoc_supplied(text)))
                pos = span[1] + 1
            pending = []
            _flush()
            connector = "seq"
            i = max(i + 1, min(pos - 1, n))
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
            elif not depth_stack and _ARRAY_ASSIGNMENT_RE.search("".join(buf[-80:])):
                kind = "array"              # `arr=(...)`: its elements are words
            elif not depth_stack and nxt == "(" and _starts_a_command("".join(buf[-80:])):
                kind = "arithmetic"         # `(( ... ))`, the arithmetic command
            _open(")", kind, "(")
            i += 1
            continue
        if ch == ")" and depth_stack and depth_stack[-1] in (")", '")'):
            kind, opener = sub_kind, sub_opener
            closed = _close(")", nxt)
            if closed and (opener == "$(" or kind in ("process_in", "process_out", "array")):
                word_paren = i              # `$(x)#`, `<(x)#`, `x=(a)#`: the word goes on
            if closed and kind == "subshell":
                # bash takes no words after a subshell, only a separator or a redirection:
                # what follows is read as a command of its own, never as the data of the
                # command in front (`echo (a) rm -rf /`).
                _flush()
                connector = "seq"
            i += 1
            continue
        if depth_stack:
            if ch == "c" and _is_case_statement(command, i) and _command_position(sub_buf):
                sub_case = True
            elif ch == "#" and sub_kind == "arithmetic":
                arith_tainted = True        # a comment moves where bash closes `$((`
            _put(ch)                    # a separator inside a substitution is its own
            i += 1
            continue
        if ch == "&" and (nxt == ">" or (buf and buf[-1] in ("<", ">"))):
            buf.append(ch)                  # `&>f`, `2>&1`: a redirection, no separator
            i += 1
            continue
        if ch in ";\n&|":
            _flush()
            if ch == "|" and nxt == "&":    # `|&` pipes stderr too: still a pipe
                connector = "pipe"
                i += 2
                continue
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


def _is_case_statement(command: str, i: int) -> bool:
    """`case WORD in` at `i`: the reserved word, exactly one shell word (quote-aware, so
    `"a b"` is one), and `in`, blanks and newlines between. Where a command starts
    (_command_position) such a statement's pattern `)` closes a substitution early. A line
    that merely starts with the word ("case 2 is covered") is no statement."""
    n = len(command)
    if not command.startswith("case", i):
        return False
    k = i + 4
    if k < n and not command[k].isspace():
        return False
    while k < n and command[k].isspace():
        k += 1
    start = k
    while k < n and not command[k].isspace() and command[k] not in ";&|()<>":
        if command[k] in ("'", '"'):
            close = command.find(command[k], k + 1)
            k = n if close < 0 else close + 1
            continue
        k += 2 if command[k] == "\\" else 1
    if k == start:
        return False
    while k < n and command[k].isspace():
        k += 1
    return command.startswith("in", k) and (k + 2 >= n or command[k + 2].isspace()
                                             or command[k + 2] == ";")


def _starts_a_command(tail: str) -> bool:
    """Whether the next word after `tail` (text before it on the same level) is a command."""
    tail = tail.rstrip(" \t")
    words = tail.split()                    # empty for a tail of `\r`, `\v` or `\f` alone
    return not words or tail[-1] in ";&|(\n`" or words[-1] in _COMMAND_LEADERS
# Words after which the next word is a command.
_COMMAND_LEADERS = frozenset({"if", "elif", "while", "until", "then", "else", "do", "{", "!",
                              "time"})
# `NAME=(`, `NAME+=(`, `NAME[k]=(`: an array assignment, after a blank, a separator or at
# the start (also after `declare`, `local`, `typeset`, `readonly`, `export`).
_ARRAY_ASSIGNMENT_RE = re.compile(r"(?:^|[\s;&|(])[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=$")


def _command_position(sub_buf: List[str]) -> bool:
    """Whether the next word of a substitution's text stands where a command starts."""
    return _starts_a_command("".join(sub_buf[-40:]))


class _HeredocLines:
    """The text's lines, indexed once, to find a heredoc's delimiter line after a position:
    (start of the line, end of the line) of the first line equal to the delimiter."""

    def __init__(self, command: str) -> None:
        self.exact: dict = {}
        self.tabbed: dict = {}
        pos = 0
        while pos <= len(command):
            end = command.find("\n", pos)
            end = len(command) if end < 0 else end
            line = command[pos:end]
            # The last line may have lost its `\r` when the text was stripped: a CRLF
            # heredoc's delimiter (`EOF\r`) matches it too. Unterminated, bash would take the
            # body to the end as well.
            for key in ((line, line + "\r") if end == len(command) and line else (line,)):
                self.exact.setdefault(key, []).append((pos, end))
                self.tabbed.setdefault(key.lstrip("\t"), []).append((pos, end))
            pos = end + 1

    def after(self, delimiter: str, pos: int, strip_tabs: bool) -> Optional[tuple]:
        import bisect
        spans = (self.tabbed if strip_tabs else self.exact).get(delimiter)
        if not spans:
            return None
        k = bisect.bisect_left(spans, (pos, -1))
        return spans[k] if k < len(spans) else None


def _heredoc_word(command: str, at: int) -> Optional[tuple]:
    """The delimiter after `<<` at `at`: (end of the word, delimiter, quoted, strip_tabs),
    or None when no word follows that is surely a delimiter (a `$` word, `1<<2`)."""
    n = len(command)
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
            if close < 0:
                return None
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
    if not delim or command[j:j + 1] == "$" or not (
            quoted or delim[0].isalpha() or delim[0] == "_"):
        return None
    return k, delim, quoted, strip_tabs


def _heredoc_substitutions(body: str) -> List[tuple]:
    """The substitutions an unquoted heredoc body runs. The body is read as the inside of
    double quotes: there too `$( )` and backticks run, a backslash escapes only `$`, a
    backtick, a backslash and a newline, and every other character is text."""
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
        found.extend(c for c in carried if c[0] in ("command", "arithmetic", "arithmetic*"))
    return found


def _heredoc_supplied(body: str) -> str:
    """The text an unquoted heredoc body supplies: a backslash before `$`, a backtick or a
    backslash is removed, and a backslash-newline joins the lines (`\\$(cmd)` in the body
    hands `$(cmd)` to the shell that reads it)."""
    out: List[str] = []
    k = 0
    while k < len(body):
        c = body[k]
        nx = body[k + 1:k + 2]
        if c == "\\" and nx in ("$", "`", "\\") and nx:
            out.append(nx)
            k += 2
        elif c == "\\" and nx == "\n":
            k += 2
        else:
            out.append(c)
            k += 1
    return "".join(out)


def _comment_starts_at(command: str, i: int, word_paren: int = -1) -> bool:
    """Whether the `#` at `i` starts a comment for bash: it starts a word, at the start of the
    text or after an unescaped blank, newline or operator character. A backslash-newline in
    front of it is joined away first, as bash does (`a \\` NL `#b` is a comment, `a\\` NL
    `#b` is the word `a#b`); `a\\ #b`, `$#`, `${#x}` and `16#ff` start none. A `)` ends a
    word, except the one that closes `$( )`, `$(( ))`, `<( )`, `>( )` or an array `x=( )`
    (`word_paren`), after which the word goes on (`$(printf x)#`); a subshell's, an
    arithmetic command's `(( ))` and a `case` pattern's `)` end it."""
    j = i
    while j >= 2 and command[j - 1] == "\n" and _escaped(command, j - 1):
        j -= 2                              # a line continuation: bash removes it
    if j == 0:
        return True
    if command[j - 1] == ")":
        return j - 1 != word_paren and not _escaped(command, j - 1)
    # No `(`: here it always opens a nested level, where no comment mark is set.
    return command[j - 1] in " \t\n;&|<>" and not _escaped(command, j - 1)


def _opens_expansion(command: str, i: int) -> bool:
    """Whether the `$` at `i` (before `{` or `[`) opens `${` or `$[` for bash: `$$` is the
    process id, so only an odd run of unescaped `$` ending here opens one (`$${` is the pid
    and a literal `{`). Named boundary: a `$`, a backslash-newline and a `{` is no expansion
    here, though bash joins the lines first; that only keeps a `#` after it a comment, so
    the new reading refuses more, never less."""
    run = 1
    while i - run >= 0 and command[i - run] == "$":
        run += 1
    if _escaped(command, i - run + 1):
        run -= 1                            # `\$` is a literal dollar sign
    return run % 2 == 1


def _escaped(command: str, k: int) -> bool:
    """Whether the character at `k` follows an odd run of backslashes."""
    run = 0
    while k - run - 1 >= 0 and command[k - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _dollar_kind(command: str, i: int) -> str:
    """`$((` at `i` may open an arithmetic expansion, any other `$(` a command substitution.
    Whether bash evaluates it or runs it is not decided here: both readings are judged."""
    return "arithmetic" if command[i + 2:i + 3] == "(" else "command"


def _tokens(segment: str) -> List[str]:
    """shlex tokens, falling back to whitespace split on unbalanced quotes. A here-string
    operator written against its words (`bash<<<'x'`, `bash <<<"$(...)"`) is its own token,
    with its descriptor (`0<<<`), as the shell reads it; inside quotes it is text."""
    if "<<<" in segment:
        segment = _spaced_here_strings(segment)
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _spaced_here_strings(segment: str) -> str:
    """`segment` with a blank around every unquoted `<<<` (and its descriptor digits)."""
    out: List[str] = []
    quote = ""
    i = 0
    while i < len(segment):
        ch = segment[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(segment):
                out.append(segment[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
            out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "\\" and i + 1 < len(segment):
            out.append(segment[i:i + 2])
            i += 2
            continue
        elif segment.startswith("<<<", i):
            digits = 0
            while digits < len(out) and out[-1 - digits].isdigit():
                digits += 1
            word_start = len(out) - digits
            op = "".join(out[word_start:]) + "<<<"
            del out[word_start:]
            if out and not out[-1].isspace():
                out.append(" ")
            out.append(op + " ")
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
    a substitution (`eval "\\$(echo rm) -rf /"` runs `rm -rf /`, measured). Read in one pass
    from the left: a pair of backslashes is a literal backslash and stays a pair, so in
    `bash -c 'echo \\\\$(cmd)'` the substitution stays live, as bash runs it; a single one
    before `$` or a backtick is dropped. An odd escape is read as live too, the safe side."""
    text = text.replace(_SUBST_QUOTED, _SUBST_BARE)
    if "\\" not in text:
        return text
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1:i + 2]
        if ch == "\\" and nxt == "\\":
            out.append("\\\\")
            i += 2
        elif ch == "\\" and nxt in ("$", "`") and nxt:
            out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


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


def _windows_host() -> bool:
    """Whether this machine reads `//host/share` as a share (Windows). A seam the tests patch."""
    return os.name == "nt"


# Where a piece of command text ends for the share reading: whitespace, quotes, the shell's
# separators and redirections, `=` (`X=\\h\s`, `--path=\\h\s`) and commas (code), none of which
# a share name may hold. Parentheses may (`\\h\(share)`), so a piece is cut at them only as a
# second reading (`open(\\h\s)`, `(\\h\s)`), the whole piece read first.
_SHARE_PIECE_SPLIT_RE = re.compile(r"[\s\"'`,;|&<>=]+")
_SHARE_PAREN_SPLIT_RE = re.compile(r"[()]+")
# What cmd.exe and PowerShell take out of a word before the path reaches the program: quotes
# (`dir "\\h"\s` is `\\h\s`), cmd's caret and PowerShell's backtick escapes.
_SHARE_QUOTING_RE = re.compile(r"[\"'^`]")
# Two separators, a host and ONE separator before a share name that is not only dots. The host
# holds no regular-expression operator, so `\\d+\\.`, `\\w+\\s` and `\\n\\t` (a regex or an
# escape written with doubled backslashes) are no share; `%VAR%`, `h@80` and `wsl$` are hosts.
_UNC_SHAPE_RE = re.compile(r"^[\\/]{2}[^\\/+*?()\[\]{}|^]+[\\/](?!\.+(?:[\\/]|$))[^\\/]")
# On a Windows host also a host alone (`net view \\nas`, `sc \\nas query`: the login happens at
# the host, before any share is named) and a share after two separators (`\\nas\\share`), the
# host read the same way, unless it is what an escape looks like (`\\n`, `\\t`, `\\x41`, `\\012`)
# or a shell expansion behind an escaped backslash (`\\$(cmd)`, `\\$VAR`).
_LOOSE_SHARE_RE = re.compile(r"^\\[\\/](?P<host>[^\\/+*?()\[\]{}|^]+)")
_ESCAPE_HOST_RE = re.compile(
    r"^(?:[abefnrtvsdwSDWBN]|[0-7]{1,3}|[xXuU][0-9a-fA-F]{0,8}|\$.*)$", re.DOTALL)
# The Win32 device and long-path namespaces (`\\?\`, `\\.\`) and the NT object namespace
# (`\??\`), which Win32 passes through unchanged.
_DEVICE_SHAPE_RE = re.compile(r"^(?:[\\/]{2}[?.]|[\\/]\?\?)[\\/]")
_FILE_URL_RE = re.compile(r"^file:(?P<seps>[\\/]*)(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
# `file://C:/x` and the old `file://C|/x` name a drive of this machine, not a host.
_DRIVE_HOST_RE = re.compile(r"^[A-Za-z][:|]$")


def _file_url_names_share(url: str) -> bool:
    r"""A `file:` URL whose authority or path names a share: `file://h/s` names host h;
    `file:///x`, `file://localhost/x` and `file://C:/x` are this machine, unless their path is
    a UNC path itself (`file://localhost//h/s`, which Windows' url2pathname opens as
    `\\h\s`); four or more separators (`file:////h/s`, `file:\\\\h\s`) carry one whole."""
    match = _FILE_URL_RE.match(url)
    seps, rest = len(match.group("seps")), match.group("rest")
    host = re.split(r"[\\/]", rest, maxsplit=1)[0]
    if seps >= 4:
        return bool(host)
    if seps != 2:
        return False
    if host.lower() not in ("", "localhost") and not _DRIVE_HOST_RE.match(host):
        return True
    path = rest[len(host):]
    after = path.lstrip("\\/")
    return len(path) - len(after) >= 2 and bool(re.split(r"[\\/]", after, maxsplit=1)[0])


def _is_share_piece(piece: str, windows: bool) -> bool:
    """One piece of text (no blank, no quote) that names a share or a device path."""
    if _FILE_URL_RE.match(piece):
        # Read as written and percent-decoded, as url2pathname reads it
        # (`file://localhost/%5C%5Ch%5Cs`).
        decoded = urllib.parse.unquote(piece)
        return _file_url_names_share(piece) or (
            decoded != piece and bool(_FILE_URL_RE.match(decoded))
            and _file_url_names_share(decoded))
    if not is_remote_path(piece):
        return False
    if _DEVICE_SHAPE_RE.match(piece) or _UNC_SHAPE_RE.match(piece):
        # All forward slashes is a share only where the OS reads it as one: on POSIX
        # `//host/share` is a spelling of `/host/share`.
        return windows or "\\" in piece[:2]
    if windows:
        loose = _LOOSE_SHARE_RE.match(piece)
        return bool(loose) and not _ESCAPE_HOST_RE.match(loose.group("host"))
    return False


# Every character whose NFKC form holds a backslash, a slash or a colon (computed over all of
# Unicode; tests/test_unattended_host_share_paths.py recomputes it), and the table that reads
# each as its NFKC form. A text holding one is read a second time through the table, not
# through unicodedata.normalize: NFKC makes one character up to eighteen (U+FDFA), and older
# CPython patch releases sort a run of combining marks in quadratic time; the table is linear
# on every interpreter and gives these characters exactly what NFKC gives them.
_NFKC_FOLDS = frozenset("\u2100\u2101\u2105\u2106\u2a74\ufe13\ufe55\ufe68\uff0f\uff1a\uff3c")
_NFKC_FOLD_TABLE = {ord(c): unicodedata.normalize("NFKC", c) for c in _NFKC_FOLDS}
# What a `file:` URL holds up to a drive's colon: `//C`, `///C` (`file://C:/x`, `file:///C:/x`).
_FILE_DRIVE_RE = re.compile(r"[\\/]{2,3}[A-Za-z]$")
# What may stand in front of a drive letter at the start of a word inside a piece (a call's
# parenthesis, a list, an argument's comma, an assignment); never `-` or `/`, an option's.
_DRIVE_WORD_STARTS = frozenset("([{,;=")
# The text before a `:` that makes `x://...` a URL, not a path after an option's colon.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


def _colon_suffixes(piece: str) -> Iterable[str]:
    r"""A piece's text after each `:`, up to the next `:`, where it may be a path written
    behind an option's colon: one that starts with a backslash (`-Path:\\h\s`,
    `/out:\\h\s\x.exe`), a `file:` URL (`-Uri:file://h/s`), or one that starts with `/` when
    the text in front is no URL scheme (`-Path://h/s`, `FileSystem:://h/s`; `https://h/s` is
    a URL and read as one). Not after a drive letter (`C:\\Users\\x` in a one-liner is a
    local path written with doubled backslashes) nor after a user name (`user@host://srv/x`
    is rsync's or git's remote path). Up to the next colon so each character is read a fixed
    number of times: a suffix to the end of the piece made the reading quadratic (130 K
    characters of `\\h\s:`: 27.6 s and 2.7 GB)."""
    if ":" not in piece or _FILE_URL_RE.match(piece):
        return
    before = 0
    at = piece.find(":")
    while at >= 0:
        nxt = piece.find(":", at + 1)
        suffix = piece[at + 1:nxt if nxt >= 0 else len(piece)]
        front = piece[before:at]
        # A drive letter: one letter at the start of a word (`C:`, `open(C:`, `-Path:C:`),
        # never an option's letter (`-P:\\h\s` names the share).
        drive = (front[-1:].isascii() and front[-1:].isalpha()
                 and (len(front) == 1 or front[-2] in _DRIVE_WORD_STARTS))
        if suffix.startswith("\\") and not drive:
            yield suffix
        elif suffix.lower() == "file" and nxt >= 0:
            # `file:` ends the slice at its own colon: the URL runs to the colon after it, or
            # past one more when that colon is a drive's (`-Uri:file://C:/x` is a local file,
            # `file://C` would name host C). At most two colons on: still linear.
            after = piece.find(":", nxt + 1)
            if after >= 0 and _FILE_DRIVE_RE.match(piece, nxt + 1, after):
                after = piece.find(":", after + 1)
            yield piece[at + 1:after if after >= 0 else len(piece)]
        elif (suffix.startswith("/") and not _URL_SCHEME_RE.match(front)
              and "@" not in front):
            yield suffix
        before = at + 1
        at = nxt


def _share_candidates(text: str) -> Iterable[str]:
    r"""Every piece the share reading judges, each spelling of the text in turn: as written;
    without quotes, carets and backticks; with each doubled backslash read as one (a share in
    a one-liner's C-style string, `"open('\\\\h\\s')"`); both; and, for a text that holds a
    character NFKC folds into a backslash, a slash or a colon (_NFKC_FOLDS), the same four
    with those characters read as NFKC reads them (the fullwidth `＼` and `／` and the small
    `﹨` are a backslash and a slash there; _NFKC_FOLD_TABLE, never the whole text's NFKC). A piece's text after a `:` may be a piece too
    (`_colon_suffixes`). Linear in the text: every spelling, piece and suffix is a slice read
    once."""
    bases = [text]
    if not _NFKC_FOLDS.isdisjoint(text):
        bases.append(text.translate(_NFKC_FOLD_TABLE))
    spellings = []
    for base in bases:
        stripped = _SHARE_QUOTING_RE.sub("", base)
        for spelling in (base, stripped, base.replace("\\\\", "\\"),
                         stripped.replace("\\\\", "\\")):
            if spelling not in spellings:
                spellings.append(spelling)
    for spelling in spellings:
        for whole in _SHARE_PIECE_SPLIT_RE.split(spelling):
            parts = [whole]
            if "(" in whole or ")" in whole:
                parts += _SHARE_PAREN_SPLIT_RE.split(whole)
            for piece in parts:
                if not piece:
                    continue
                yield piece
                yield from _colon_suffixes(piece)


def _shares(text: str, windows: Optional[bool]) -> Iterable[str]:
    """Each piece of `text` that names a share, once, in order (share_paths' reading)."""
    if windows is None:
        windows = _windows_host()
    seen = set()
    for piece in _share_candidates(str(text or "")):
        if piece not in seen:
            seen.add(piece)
            if _is_share_piece(piece, windows):
                yield piece


def first_share_path(text: str, *, windows: Optional[bool] = None) -> str:
    """The first piece of `text` that names a share or a device path, or "" (share_paths'
    reading, stopped at the first answer: what a yes or no and a refusal need)."""
    try:
        return next(iter(_shares(text, windows)), "")
    except Exception:                   # noqa: BLE001 - a reading aid, never a crash
        return ""


def share_paths(text: str, *, windows: Optional[bool] = None) -> List[str]:
    r"""The pieces of `text` that name a network share or a Windows device path, in order.

    Read from the RAW text, never through shlex: a POSIX shell lexer turns `\\h\s` into
    `\hs`, so the tokens the classifier judges never show the share. The text is cut at
    whitespace, quotes, the shell's separators and redirections, `=` and commas (a share name
    holds none of them); each piece is read whole and then cut at parentheses, which a share
    name may hold (`\\h\(share)`). The text is read in four spellings (`_share_candidates`):
    as written, without the quoting cmd.exe and PowerShell take out (`dir "\\h"\s`,
    `dir \\h^\s`, PowerShell's backtick), with each doubled backslash read as one
    (`python -c "open('\\\\h\\s')"`, `node -e`, `perl -e`), and both; a text that holds a
    character NFKC folds into a backslash, a slash or a colon (_NFKC_FOLDS) also with those
    characters read as NFKC reads them (`＼＼h＼s`, `／／h／s`, `﹨﹨h﹨s`; a fixed table,
    never the whole text's NFKC, which is not linear on every interpreter). A piece's text after a `:` counts too
    (`-Path:\\h\s`, `/out:\\h\s\x.exe`, `-Uri:file://h/s`, `-Path://h/s`,
    `FileSystem:://h/s`), not after a drive letter or a user name (`open('C:\\Users\\x')`,
    `user@host://srv/x`; `_colon_suffixes`). A piece counts when it has
    - the backslash UNC shape, a host and a share (`\\host\share`, `\\%VAR%\s`, `\\h@80\s`),
      or a device prefix (`\\?\`, `\\.\`, `\??\`), on every host;
    - on a Windows host (`windows`, default: this machine), also the forward-slash shape
      (`//host/share`; on POSIX a spelling of `/host/share`), a host alone after a backslash
      pair (`net view \\nas`, `sc \\nas query`: the login happens at the host) and a share
      after two separators (`\\nas\\share`), unless the host is what an escape looks like
      (`\\n`, `\\t`, `\\x41`, `\\012`) or a shell expansion (`\\$(cmd)`, `\\$VAR`);
    - a `file:` URL naming a host other than empty, `localhost` or a drive letter
      (`file://h/s`, `file:////h/s`; `file://C:/x` is local), or a local one whose path is a
      UNC path (`file://localhost//h/s`), as written and percent-decoded, on every host.
    `path_jail.is_remote_path` judges every piece of the path shapes; the shape filter in
    front keeps `//`, `//tmp`, `// TODO`, `\\n`, `\\d+\\.`, `\\w\.`, `C:\x` and `https://...`
    out, several of which it answers True alone. Linear in the text's length (each piece is
    read once, a colon's suffix only up to the next colon); `first_share_path` stops at the
    first answer. Never raises.

    NAMED BOUNDARY: text only, and on POSIX the host-alone and two-separator spellings are not
    read (a POSIX shell has no SMB client of its own; refusing them there would refuse
    `printf '\\nDone'` for nothing). On Windows a host alone shaped like an escape or an
    expansion (`\\a`, `\\n`, `\\xface`, `\\012`, `\\$evil`) is not refused, even where such a
    host exists. Not seen anywhere: a share built at run time (a variable, a string joined in
    PowerShell, also through an empty `$()` as in `\\$()h\s`, `[char]92`, an environment
    variable's value such as `%CD:~0,0%`), one inside a script file, text decoded before it
    runs (`base64 -d | sh`, `powershell -EncodedCommand`), escapes a program decodes itself
    outside a doubled backslash (Python's `\x5c` in a one-liner cmd hands over unquoted or in
    its `""` form, node's `\x5c` and `\u005c`, perl's `\134`), backslash lookalikes NFKC keeps
    (`∖` U+2216, `⧵` U+29F5), Windows name resolution of a bare name, and an HTTP client told
    to log in as the Windows account (`curl --ntlm`, `-UseDefaultCredentials`). The text is
    read as a whole, so a share named in data (an `echo`, a heredoc body, the text
    `ssh host '...'` runs on another machine) counts as well."""
    try:
        return list(_shares(text, windows))
    except Exception:                   # noqa: BLE001 - a reading aid, never a crash
        return []


# The name the code check parses under. The model's code is data here: a string with an
# invalid escape (`open('\\h\s')`) made the parser print a SyntaxWarning to the process's
# stderr for every check; warnings under this name alone are silenced (a filter, not
# catch_warnings, which is not thread-safe).
_CODE_FILENAME = "<python-code-check>"
warnings.filterwarnings("ignore", category=SyntaxWarning, module=re.escape(_CODE_FILENAME))


def python_code_categories(code: str, *, found_paths: Optional[List[str]] = None,
                           _depth: int = 0) -> List[str]:
    r"""What Python `code` does that a host lane judges, in a fixed order: "ftp_transfer" when
    it moves files over FTP, "share_path" when a string constant names a network share or a
    Windows device path (`share_paths` on its value: `open(r"\\h\s\x")`,
    `os.listdir("\\\\h\\s")`, `pathlib.Path(r"\\?\UNC\h\s")`, `"file://h/s/x"`).

    Read from the syntax tree, so a comment does not count. FTP is an import of ftplib (also
    `from ftplib import ...`), a string that IS the module's name, `ftplib` or `ftplib.<name>`
    (whatever imports it: `__import__`, `importlib.import_module` also under an alias, a
    getattr of `__import__`, `-m ftplib` in a subprocess argument list), or a string that starts
    with ftp:// or ftps:// (urllib's FTP, an f-string's literal part). A constant string handed
    to exec, eval or compile is read as code too, a few levels deep. Code that does not parse,
    or nests deeper, is searched as text. `found_paths`, when given, receives the share as
    the code's string holds it, after Python's own escapes (`"\x5c\x5ch\x5cs"` is `\\h\s`),
    so a refusal can name what the raw text does not show. Never raises."""
    import ast
    text = code.decode("utf-8", "replace") if isinstance(code, bytes) else str(code or "")
    found = set()

    def _ordered() -> List[str]:
        return [c for c in ("ftp_transfer", "share_path") if c in found]

    def _as_text() -> List[str]:
        if _PYTHON_FTP_TEXT_RE.search(text):
            found.add("ftp_transfer")
        piece = first_share_path(text)
        if piece:
            found.add("share_path")
            if found_paths is not None:
                found_paths.append(piece)
        return _ordered()

    if _depth > _MAX_CODE_DEPTH:
        return _as_text()
    try:
        tree = ast.parse(text, filename=_CODE_FILENAME)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _as_text()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_ftplib(alias.name) for alias in node.names):
                found.add("ftp_transfer")
        elif isinstance(node, ast.ImportFrom):
            if _is_ftplib(node.module):
                found.add("ftp_transfer")
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if called in _CODE_STRING_CALLS:
                inner = _call_argument(node, "source")
                if isinstance(inner, (str, bytes)):
                    found.update(python_code_categories(inner, found_paths=found_paths,
                                                        _depth=_depth + 1))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = (node.value.decode("latin-1") if isinstance(node.value, bytes)
                     else node.value)
            if isinstance(node.value, str) and (
                    _is_ftplib(value)
                    or value.lstrip().lower().startswith(("ftp://", "ftps://"))):
                found.add("ftp_transfer")
            piece = "" if "share_path" in found else first_share_path(value.strip())
            if piece:
                found.add("share_path")
                if found_paths is not None:
                    found_paths.append(piece)
        if len(found) == 2:
            break
    return _ordered()


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
    valueless = _VALUELESS_RUNNER_FLAGS.get(exe, frozenset())
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
            # A flag known to take no value: the next word is no value but the command.
            maybe_value = "=" not in tok and tok not in valueless
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


def _python_command_categories(args: List[str], *,
                                found_paths: Optional[List[str]] = None) -> List[str]:
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
                    return python_code_categories(value, found_paths=found_paths)
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
    """Classify a shell command offline. Never executes anything, never raises: a reading that
    fails (either one) refuses the command as unreadable and is logged.

    profile="host"   - unsandboxed lane: refuse the catastrophic set.
    profile="host_unattended" - the same lane where nobody confirmed this call: also refuse
                       a network share or Windows device path (`share_path`).
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
    if _budget is None and _depth == 0:
        try:
            return _read(command, profile=profile, _depth=0, _budget=None,
                         _stdin_fetch=_stdin_fetch, _arithmetic=_arithmetic)
        except Exception as exc:        # noqa: BLE001 - fail closed, whatever went wrong
            # The type and the place only: an exception's message can quote the command,
            # and a command can carry a secret.
            where = traceback.extract_tb(exc.__traceback__)[-1:]
            _log.warning("command check failed, the command is refused: %s at %s:%s",
                         type(exc).__name__,
                         os.path.basename(where[0].filename) if where else "?",
                         where[0].lineno if where else "?")
            return CommandVerdict(blocked=True, categories=["unreadable"],
                                  reason=_REFUSED + CATEGORY_REASONS["unreadable"])
    return _read(command, profile=profile, _depth=_depth, _budget=_budget,
                 _stdin_fetch=_stdin_fetch, _arithmetic=_arithmetic)


def _read(command: str, *, profile: str, _depth: int, _budget: Optional[List[int]],
          _stdin_fetch: bool, _arithmetic: bool) -> CommandVerdict:
    """classify_command's reading; nested readings go through classify_command."""
    verdict = CommandVerdict()
    text = (command or "").strip()
    if not text:
        return verdict
    if _budget is None:
        if len(text) > _MAX_COMMAND_CHARS:
            verdict.blocked = True
            verdict.categories = ["too_long"]
            verdict.reason = _REFUSED + CATEGORY_REASONS["too_long"]
            return verdict
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
    share_piece = ""

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
        # Also after a leading `-`: zsh reads `-` as a precommand modifier (`- cmd` runs cmd).
        if exe_at >= 0 and "opaque_command" not in cats and not _arithmetic:
            word = toks[exe_at]
            if (_SUBST_BARE in word or _SUBST_PROC in word
                    or _SUBST_QUOTED in word.rsplit("/", 1)[-1]):
                cats.append("opaque_command")
                opaque_shape = opaque_shape or (
                    "env_values" if any(_exe_base(t) == "env" for t in toks[:exe_at])
                    else "program")
        # A runner's own option with an unquoted substitution as its value (`uv run
        # --with=$(cat r) pytest`): the value is split into words, and the runner reads the
        # words after the first as more of its arguments, a command among them. Quoted
        # (`--with="$(cat r)"`) it is one value.
        if exe_at >= 0 and "opaque_command" not in cats and not _arithmetic:
            candidates = _runner_commands(exe, args)
            if candidates and any(t.startswith("-") and _SUBST_BARE in t
                                  for t in args[:max(candidates)]):
                cats.append("opaque_command")
                opaque_shape = opaque_shape or "option_value"
        # A shell or interpreter that runs another command's OUTPUT as its script: through a
        # process substitution or a here-string holding a substitution, or a descriptor fed
        # by one (`source <(pyenv init -)`, `bash <<< "$(...)"`, `. /dev/fd/3 3< <(...)`), or
        # a descriptor opened by an earlier command: the same as `eval "$(...)"`, the
        # approved text does not show what runs. A literal here-string fed to a shell is read
        # as shell text, like `bash -c`. A file is read as before.
        script_literals: List[str] = []
        if exe in _SHELL_SINKS:
            from_where, script_literals = _script_input(exe, args, carried)
            if from_where:
                if "opaque_command" not in cats:
                    cats.append("opaque_command")
                opaque_shape = (_shell_text_shape(carried) if from_where == "output"
                                else "descriptor")

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
                if kind in ("heredoc_q", "heredoc_u"):
                    continue                # a heredoc body: data, its substitutions apart
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
                elif kind == "array":
                    # An array's elements are words, the arguments of the no-op `:`; their
                    # substitutions are read (`arr=($(ls))` runs `ls`, nothing else).
                    yield kind, "local", ": " + inner
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
                opaque_shape = (_shell_text_shape(carried)
                                if where in ("local", "remote") and not kind
                                and _SUBST_BARE in inner else sub.opaque_shape)
                if where == "runner" and opaque_shape == "program":
                    if _value_from_substitution(exe, args):
                        opaque_shape = "option_value"   # `uv run --with $(cat r) pytest`
                    elif _after_unknown_flag(exe, args):
                        opaque_shape = "runner_args"    # `uv run --x pytest $(...)`
                elif where == "charged" and kind == "command" and "`" in inner:
                    opaque_shape = "arith_backtick"  # `$(( `date +%s` - 10 ))`
            fetched = "network_fetch" in sub.categories or "remote_login" in sub.categories
            if local and fetched:
                nested_fetch = True
                # `bash <(curl x)` reads it as a file; `bash <<< "$(curl x)"` as its input.
                if kind == "process_in" or (kind == "command" and "<<<" in seg):
                    input_fetch = True
            if local and sub.share_piece and not share_piece:
                share_piece = sub.share_piece       # `bash -c 'python -c ...'`
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
        # Asked by name, not by truthiness: `python -c` code that names a share is
        # `share_path`, not an FTP transfer.
        py_paths: List[str] = []
        py_cats = (_python_command_categories(args, found_paths=py_paths)
                   if _PYTHON_EXE_RE.match(exe) else [])
        if (exe in _FTP_CLIENTS or (exe in _NETWORK_FETCHERS
                                    and any(_FTP_URL_RE.match(a) for a in args))
                or "ftp_transfer" in py_cats) \
                and "ftp_transfer" not in cats:
            cats.append("ftp_transfer")
        # The code's strings after Python's own escapes (`"\\\\h\\s"` is `\\h\s`), which the
        # raw scan below does not undo.
        if "share_path" in py_cats and profile in _SHARE_PROFILES and "share_path" not in cats:
            cats.append("share_path")
            share_piece = share_piece or (py_paths[0] if py_paths else "")

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

    # One scan of the whole raw text covers every nested `bash -c`, substitution and runner;
    # shlex has already dropped the backslashes from the tokens read above.
    if _depth == 0 and profile in _SHARE_PROFILES and "share_path" not in cats:
        share_piece = first_share_path(text)
        if share_piece:
            cats.append("share_path")
    blocking = [c for c in cats if c in _BLOCKING.get(profile, _BLOCKING["host"])]
    verdict.categories = cats + [c for c in remote_cats if c not in cats]
    if "opaque_command" in cats:
        verdict.opaque_shape = opaque_shape or "program"
    verdict.share_piece = share_piece
    if blocking:
        verdict.blocked = True
        verdict.reason = _refusal(blocking[0], verdict.opaque_shape, text,
                                  share_piece=share_piece)
    elif nested_refusal:
        verdict.blocked = True
        verdict.reason = nested_refusal
    if _depth == 0:
        _join_the_frozen_reading(verdict, text, profile)
    return verdict


def _clip_piece(piece: str, limit: int = 120) -> str:
    """A piece of the command as a refusal quotes it: at most `limit` characters."""
    return piece if len(piece) <= limit else piece[:limit] + "..."


def _refusal(category: str, opaque_shape: str, text: str, *, share_piece: str = "") -> str:
    """The refusal sentence for `category`, with the way out where one exists. `share_piece`
    is the piece the reading already took for a share; without it the text is read once
    more, stopping at the first share."""
    sentence = CATEGORY_REASONS[category]
    heredoc = "<<" in text.replace("<<<", "")
    # An unquoted heredoc's body is expanded as it is written: quoting its delimiter makes it
    # plain text, which helps a writer (`cat > f <<EOF`), never a shell that runs it.
    quote_cure = (heredoc and category == "opaque_command"
                  and opaque_shape in ("program", "shell_text")
                  and ("$(" in text or "`" in text) and _heredoc_writes_text(text))
    if category == "share_path":
        # Which piece it read as a share, so the model can tell the person, and a false
        # refusal is visible as one.
        piece = share_piece or first_share_path(text)
        if piece:
            sentence = sentence.replace(
                "device path", f"device path ({_clip_piece(piece)})", 1)
    if category == "opaque_command":
        sentence = _OPAQUE_REASONS.get(opaque_shape, sentence)
        if quote_cure and opaque_shape == "program":
            sentence = _HEREDOC_PROGRAM + _QUOTE_FIRST + _HEREDOC_PROGRAM_OR
        elif opaque_shape in ("program", "frozen") and "\n" in text:
            sentence += _OPAQUE_REASONS["lines"]    # perhaps a code fence or a continued line
    sentence += _WAY_OUT.get(category, "")
    if quote_cure and opaque_shape == "shell_text":
        sentence += _QUOTE_THE_DELIMITER
    if heredoc and (category in _HEREDOC_LINE_CATEGORIES or (
            category == "opaque_command" and opaque_shape in _HEREDOC_LINE_SHAPES)):
        # A heredoc line can be read as a command line (the frozen reading reads every one):
        # only a reason about a command or a substitution fits that.
        sentence += ("; a line of a heredoc can be read as a command line, so to write such "
                     "text into a file use the file tool, and for a commit or pull request "
                     "message `git commit -F FILE` or `gh pr create --body-file FILE`")
    return f"Command contains a forbidden pattern: it {sentence}"
# Offered only while the frozen reading lets the command pass (_join_the_frozen_reading
# takes both out otherwise): that reading reads every line, quoted delimiter or not.
_QUOTE_THE_DELIMITER = ("; or, if the heredoc's text must not be expanded, quote its delimiter "
                        "(`<<'EOF'`)")
_QUOTE_FIRST = (" quote the heredoc's delimiter (`<<'EOF'`) if its text must not be "
                "expanded, or")
_HEREDOC_PROGRAM = ("takes a program name from a command substitution in an unquoted heredoc, "
                    "which the shell expands as it writes the text;")
_HEREDOC_PROGRAM_OR = " run the substitution as a command of its own first and write the result in"


def _heredoc_writes_text(text: str) -> bool:
    """Whether every unquoted heredoc in `text` goes into a file and nowhere else: its line
    starts with a writer (`cat > f <<EOF`, `tee f <<EOF`) with a file to write, holds no
    `|` or `&&` (a pipe continues on the next line too), and no word anywhere in the command
    is a script reader (_SHELL_SINKS, `eval`; `b\\ash` and `/bin/bash` counted) nor a
    variable where a command starts (`$SHELL`). Only then does quoting the delimiter leave
    the body plain text; any other way it may reach a shell and run all the same."""
    # The command lines only: a heredoc body is text (a README that says `source`).
    triples = _split_segments(text)
    lines = "\n".join([seg for _, seg, _ in triples] + [
        body for _, _, carried in triples for kind, body in carried
        if kind not in ("heredoc_q", "heredoc_u")])
    words = {w.replace("\\", "").rsplit("/", 1)[-1]
             for w in re.split(r"[\s;&|()<>`'\"{}:=]+", lines) if w}
    if words & _HEREDOC_SCRIPT_READERS or _VARIABLE_COMMAND_RE.search(lines):
        return False
    found = False
    for m in _UNQUOTED_HEREDOC_RE.finditer(text):
        found = True
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.start())
        line = text[start:end if end >= 0 else len(text)]
        if "|" in line or "&&" in line:
            return False
        # The command that receives the heredoc, not the first one on its line
        # (`cat > f; at now <<EOF`).
        toks = re.split(r"[;&|]", line[:m.start() - start])[-1].split()
        if not toks or toks[0] not in _HEREDOC_FILE_WRITERS:
            return False
        has_file = any(t.startswith((">", "1>")) for t in toks[1:]) or (
            toks[0] == "tee" and any(not t.startswith("-") for t in toks[1:]))
        if not has_file:
            return False
    return found
# Commands that write a heredoc into a file as it is (the search for an existing set of
# writers came back empty).
_HEREDOC_FILE_WRITERS = frozenset({"cat", "tee"})
# A variable where a command starts (`| $SHELL`, `; ${SH:-bash} f`, also in double quotes:
# `"$SHELL" f`); a substitution is not one, nor `'$X'`, which is literal.
_VARIABLE_COMMAND_RE = re.compile(r"(?:^|[;&|\n(`])\s*\"?\$[A-Za-z_{0-9]")
# The reasons the heredoc sentence fits: a command line or a substitution in the text.
_HEREDOC_LINE_CATEGORIES = frozenset({"destructive_removal", "device_write", "fork_bomb"})
_HEREDOC_LINE_SHAPES = frozenset({"", "program", "frozen", "lines", "unreadable", "shell_text"})
# `<<WORD`, `<<-WORD` with a delimiter neither quoted nor escaped.
_UNQUOTED_HEREDOC_RE = re.compile(r"(?<!<)<<-?[ \t]*[A-Za-z_]")


def _join_the_frozen_reading(verdict: CommandVerdict, text: str, profile: str) -> None:
    """Refuse whenever the frozen reading (vaf/core/command_reading_frozen.py, the check as
    it read commands before substitutions were carried) refuses, with its categories.
    This reading may only add refusals to it, never lose one: every reading that cut a
    command differently lost some, and only a union makes that impossible by construction."""
    from vaf.core import command_reading_frozen
    # Its nested readings each read the rest of the text again (a runner's candidates); its
    # own bound counts readings only, so a long command gets fewer of them. A spent budget
    # refuses (nested_too_deep), so a smaller one can only add refusals.
    budget = min(MAX_NESTED_COMMANDS, _FROZEN_TEXT_BUDGET // max(1, len(text)))
    frozen = command_reading_frozen.classify_command(text, profile=profile, _budget=[budget])
    verdict.categories.extend(c for c in frozen.categories if c not in verdict.categories)
    # Its segments too: `command_needs_network` (vaf/tools/bash.py) reads them.
    seen = set(verdict.segments)            # a list lookup per segment was quadratic
    verdict.segments.extend(s for s in frozen.segments if s not in seen)
    if frozen.blocked:
        verdict.reason = verdict.reason.replace(_QUOTE_THE_DELIMITER, "").replace(
            _QUOTE_FIRST, "")
    if verdict.blocked or not frozen.blocked:
        return
    verdict.blocked = True
    first = next((c for c in frozen.categories
                  if c in _BLOCKING.get(profile, _BLOCKING["host"])), "")
    if not first:
        verdict.reason = frozen.reason
        return
    if first == "opaque_command":
        # Only the frozen reading refuses: it saw a substitution where a command would start
        # after a `;`, `&&` or `|`, also one inside quotes, or at the start of a line.
        verdict.opaque_shape = verdict.opaque_shape or "frozen"
    verdict.reason = _refusal(first, verdict.opaque_shape, text)


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


def _after_unknown_flag(exe: str, args: List[str]) -> bool:
    """Whether the first substitution in a runner's words comes after a word that followed a
    flag without `=` the check does not know: that word may be the flag's value, so the
    substitution may be read as the program (`uv run --x pytest $(...)`)."""
    valueless = _VALUELESS_RUNNER_FLAGS.get(exe, frozenset())
    for k, tok in enumerate(args):
        if _SUBST_BARE in tok or _SUBST_QUOTED in tok:
            return k >= 1 and not args[k - 1].startswith("-") and any(
                t.startswith("-") and "=" not in t and t not in valueless for t in args[:k - 1])
    return False


def _value_from_substitution(exe: str, args: List[str]) -> bool:
    """Whether a runner's option takes its value from a substitution as the next word (an
    option known to take no value takes none: `uv run --frozen $(...)` is the program)."""
    valueless = _VALUELESS_RUNNER_FLAGS.get(exe, frozenset())
    for pos, tok in enumerate(args[1:], start=1):
        prev = args[pos - 1]
        if (_SUBST_BARE in tok or _SUBST_QUOTED in tok) and prev.startswith("-") \
                and "=" not in prev and prev not in valueless:
            return True
    return False


# Shells that read their script from their input when given none, and the two builtins that
# run a file in the current shell.
_STDIN_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "source", "."})
# What runs the text it is given as code: the shells and interpreters that read a script
# (_SHELL_SINKS, _STDIN_SHELLS) and `eval`.
_HEREDOC_SCRIPT_READERS = _SHELL_SINKS | _STDIN_SHELLS | {"eval"}
# A script path that is this process's input or one of its descriptors.
_DESCRIPTOR_RE = re.compile(r"^(?:-|/dev/stdin|/dev/fd/\d+|/proc/[^/]+/fd/\d+)$")
# A redirection operator standing as a word of its own (`<`, `3<`, `2>`, `>>`, `&>`).
_REDIRECT_RE = re.compile(r"^\d*(?:<|>|>>|<>|>&|<&|&>|&>>)$")


def _script_input(exe: str, args: List[str], carried: List[tuple]) -> tuple:
    """(where its script comes from, literal script texts) for a shell or an interpreter:
    "output" when another command's output, "descriptor" when a descriptor this command
    does not open, "" otherwise.

    Its script is its first word that is no option, no option's value and no redirection.
    It comes from its input when there is none, when it is `-` or a descriptor path
    (`/dev/stdin`, `/dev/fd/N`, `/proc/self/fd/N`), or with `-s`. Fed by a process
    substitution (as the script itself or through `<`, `3<`), by a here-string or by an
    unquoted heredoc holding a substitution, it runs another command's output. A descriptor
    above 2 that no
    redirection of this command opens was opened by an earlier one (`exec 3< <(...);
    bash /dev/fd/3`), so what it reads is not in this text; so is an input read from one,
    one rule for every spelling of descriptor 0 redirected from it (_stdin_descriptor:
    `bash <&3`, `<&3-`, `< /dev/fd/3`, `<> /dev/fd/3`, `bash /dev/fd/0 <&3`). A descriptor
    path is read as Linux resolves it (`//dev/fd/3`, `/dev/./fd/3`). Fed by a literal here-string or
    heredoc body, a shell runs that text, returned to be read like `bash -c` text."""
    shell = exe in _STDIN_SHELLS
    inline = "c" if shell else "cemr"          # -c CODE, -e/-r CODE, -m MODULE
    script = None
    from_stdin = False
    proc_fed = False
    stdin_fd = ""                           # `<&3`, `< /dev/fd/3`: the input is descriptor 3
    opened = set()
    here_words: List[str] = []
    pos = 0
    while pos < len(args):
        tok = args[pos]
        nxt = args[pos + 1] if pos + 1 < len(args) else ""
        if _HERE_OPERATOR_RE.match(tok):
            here_words.append(nxt)
            pos += 2
            continue
        if _REDIRECT_RE.match(tok):
            if _stdin_descriptor(tok, nxt):
                stdin_fd = _stdin_descriptor(tok, nxt)
            elif tok.endswith("<") or tok.endswith("<>"):
                opened.add(tok.rstrip("<>") or "0")
                proc_fed = proc_fed or nxt == _SUBST_PROC
            pos += 2
            continue
        if tok[:1] in "<>" or (tok[:1].isdigit() and tok.lstrip("0123456789")[:1] in ("<", ">")):
            glued = _GLUED_INPUT_RE.match(tok)
            if glued and _stdin_descriptor(glued.group(1), glued.group(2)):
                stdin_fd = _stdin_descriptor(glued.group(1), glued.group(2))
            elif tok[:1].isdigit() and "<" in tok:
                opened.add(tok[:len(tok) - len(tok.lstrip("0123456789"))])
            pos += 1                       # `2>/dev/null`, `<file`: an attached redirection
            continue
        # `--` is no script (`bash -- <<< ...` reads its input); an option-looking word after
        # it is still read as an option, the side that reads the input.
        if script is None and tok.startswith(("-", "+")) and tok != "-":
            letters = tok[1:] if not tok.startswith("--") else ""
            if any(c in inline for c in letters):
                return "", []               # code given inline: read elsewhere (`bash -c`)
            from_stdin = from_stdin or (shell and "s" in letters)
            # A value option never takes a redirection or a here-string as its value.
            takes_value = (tok in _SHELL_VALUE_OPTIONS and not _HERE_OPERATOR_RE.match(nxt)
                           and not _REDIRECT_RE.match(nxt) and nxt[:1] not in ("<", ">"))
            pos += 2 if takes_value else 1
            continue
        if script is None:
            script = tok
        pos += 1
    if script == _SUBST_PROC:
        return "output", []
    path = _normal_path(script) if script and script.startswith("/") else (script or "")
    fd = _FD_PATH_RE.match(path)
    if fd and int(fd.group(1)) > 2 and fd.group(1) not in opened:
        return "descriptor", []
    reads_input = script is None or from_stdin or path in ("/dev/stdin", "-") \
        or bool(fd and fd.group(1) == "0")
    if reads_input and stdin_fd.isdigit() \
            and int(stdin_fd) > 2 and stdin_fd not in opened:
        return "descriptor", []             # `bash <&3`: descriptor 3, opened earlier
    script = path or script
    if exe in ("source", "."):
        from_input = script is not None and bool(_DESCRIPTOR_RE.match(script))
    else:
        from_input = script is None or from_stdin or bool(_DESCRIPTOR_RE.match(script))
    if not from_input:
        return "", []
    kinds = [kind for kind, _ in carried]
    if proc_fed or "heredoc_u" in kinds or any(_SUBST_BARE in w or _SUBST_QUOTED in w
                                                for w in here_words):
        return "output", []
    if not shell:
        return "", []
    # A heredoc fed to a shell is its script: read as shell text, like `bash -c`.
    return "", here_words + [text for kind, text in carried if kind == "heredoc_q"]


# The here-string operator, with its descriptor (`<<<`, `0<<<`).
_HERE_OPERATOR_RE = re.compile(r"^\d*<<<$")
# A descriptor path and its number.
_FD_PATH_RE = re.compile(r"^/(?:dev|proc/[^/]+)/fd/(\d+)$")


def _normal_path(path: str) -> str:
    """An absolute path as Linux resolves it: `normpath` keeps exactly two leading slashes
    (`//dev/fd/3`), which Linux reads as one."""
    return posixpath.normpath("/" + path.lstrip("/"))


def _fd_path(word: str) -> str:
    """The descriptor number a descriptor path names (`/dev/fd/3`, `//proc/self/fd/3`), else ""."""
    m = _FD_PATH_RE.match(_normal_path(word)) if word.startswith("/") else None
    return m.group(1) if m else ""


def _stdin_descriptor(op: str, target: str) -> str:
    """The descriptor a redirection of descriptor 0 reads from, else "": one rule for every
    spelling (`<&3`, the move form `<&3-`, `< /dev/fd/3`, `<> /dev/fd/3`, each also as `0<`)."""
    m = _INPUT_OPERATOR_RE.match(op)
    if not m or m.group(1) not in ("", "0"):
        return ""
    if m.group(2) == "<&":
        dup = re.match(r"^(\d+)-?$", target)
        return dup.group(1) if dup else ""
    return _fd_path(target)
# An input redirection's operator, with its descriptor.
_INPUT_OPERATOR_RE = re.compile(r"^(\d*)(<&|<>|<)$")
# An input redirection written against its target (`<&3-`, `0</dev/fd/3`, `<>/dev/fd/3`).
_GLUED_INPUT_RE = re.compile(r"^(\d*(?:<&|<>|<))([^<>&].*)$")
# A shell's options that take the next word as their value (`bash -O extglob`).
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
# The tools whose shell setup is commonly run through `eval "$(...)"` or `source <(...)`;
# each has a form that does the same without handing shell text over.
_SETUP_TOOLS = frozenset({"conda", "mamba", "micromamba", "pyenv", "rbenv", "nodenv", "fnm",
                          "direnv", "opam", "ssh-agent", "brew", "kubectl"})


def _shell_text_shape(carried: List[tuple]) -> str:
    """"tool_setup" when the shell text comes from a tool's own setup output (`eval
    "$(conda shell.bash hook)"`), whose direct form the reason names; "shell_text" else."""
    for kind, inner in carried:
        words = inner.split()
        if kind in ("command", "process_in") and words and _exe_base(words[0]) in _SETUP_TOOLS:
            return "tool_setup"
    return "shell_text"


def is_command_safe(command: str, *, profile: str = "host") -> tuple:
    """(is_safe, message) adapter kept for the two shell tools.

    The message is the refusal reason when blocked, else a note for the
    noteworthy-but-allowed categories.
    """
    v = classify_command(command, profile=profile)
    return (not v.blocked), (v.reason if v.blocked else v.warning)
