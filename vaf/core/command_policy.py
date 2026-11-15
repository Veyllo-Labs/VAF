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
searches. The readings stay linear in the command's length (every pattern is matched in one
pass: an optional part never overlaps the next repetition, a run is matched from its start,
an early heredoc end is searched in the body's own lines only); shlex's own tokenizer is not
on one word of hundreds of kilobytes (`x=` repeated: 20 s at 1 MB, the same before), so a
command over _MAX_COMMAND_CHARS (128 K characters) is refused as unreadable before it is read.

Every verdict is joined with the FROZEN reading (vaf/core/command_reading_frozen.py, the
check as it read commands before substitutions were carried): the command is refused
whenever either reading refuses, with both readings' categories
(_join_the_frozen_reading). This reading may therefore only add refusals; every other
construction of a new reading lost some that bash runs, one corner at a time, each found
by generated commands. Its price: what the frozen reading over-refused stays refused
(`echo 'a; $(x)'`, words after a closed substitution such as `echo $(true) rm -rf /`).

THE GRAMMAR READING (_grammar_text, _join_the_grammar_reading). The reading above cuts a
command at its separators and judges simple commands; the shell's grammar around them hid
the command word (`{ rm -rf /; }`, `if ...; then rm -rf /; fi`, `>f mkfs x`). So the
command is read a third time, written out as bash reads it, and refused when that reading
refuses, with its categories: a union like the frozen one, so it only adds refusals.
- G1: a comment is gone, a `#` where a word starts to the end of its line (a backslash at its
  end joins nothing; measured with bash).
- G2: a heredoc ends where bash ends it, whatever its delimiter (`.`, `1`, `$X`, `$'EOF'`,
  `"E"OF`, `E\\OF`, `a$(b)c`): an unquoted body's lines joined at a line continuation first;
  one written inside `$( )` also at the first line that starts with the delimiter, the rest of
  that line read as commands (`EOF)`, measured with bash 5.3); without its delimiter line at
  the end of the text, as bash reads it. A delimiter the reading above would not take is
  renamed, to `H` and a number that is no line of the text (a body line equal to it would
  end that body early); one written in a list that becomes text before its body comes (a
  subshell, `( cat <<. )` NL ...) is renamed when that list ends. The body is data, an
  unquoted body's substitutions are written like the command's; for a heredoc read or
  written inside `$( )` or a subshell what the body leaves open (a quote, a parenthesis) is
  closed on a comment line after its last line, since the reading above counts quotes and
  parentheses across it (`"$(cat <<'EOF'` NL `it's` ..., a case pattern's `)`), while a
  script the body feeds a shell keeps its own lines (`bash -c 'cmd'` stays as written); in
  a subshell's heredoc, whose body that reading reads at the top of the text, a heredoc
  operator in the body's own text is written escaped (a body line `<<EOF` would open a heredoc
  of that reading's own and hide the lines after the body); a substitution in the body keeps
  its text, its own heredoc included.
  The search for such an early end stays inside the body's own lines, so every line is
  looked at for one body only.
  A substitution in a heredoc body that the reading above cannot read (a `case` in it)
  refuses as unreadable, as in a command.
  A heredoc's body waits for the end of the line that wrote it, also past a newline inside a
  `$( )` on that line. Inside `$( )` a body ends early only at a line that starts with
  its delimiter and holds a `)` (`EOFx)` runs `x`, `EOFx` is body; with `<<''` every line
  starts with it). For a heredoc read or written inside `$( )` or a subshell, an unquoted
  body's substitutions are written once more on a line of their own at its start, and what
  the body leaves open for the reading above (a quote, a parenthesis, a `$(` inside double
  quotes) is closed at its end. Inside `((...))` a `<<` is a shift and a `#` no
  comment, once its `))` is found; without one bash reads two subshells (`((echo a) # x`),
  written `( (`.
  A `$((` without its `))` ends where its parentheses balance (a `$'...'` span skipped with
  its `\\'`, a backquoted span whole), and the text between is read
  as a command substitution of its own, where a heredoc ends with that text (`$(( 1 << 2 )`
  NL `)`).
- G3: a line continuation is joined, also inside a word or an operator (`r\\` NL `m`,
  `$\\` NL `'...'`, `2\\` NL `>&1`, a pipe that goes on in the next line); a backslash at the
  very end lost its newline to the check's strip().
- G4: a carriage return, vertical tab or form feed inside a word is written quoted: bash
  keeps it in the word (`X=` CR `1 cmd` assigns and runs cmd), shlex would cut there.
  `$'...'` is decoded with bash's escapes into one single-quoted word, `$"..."` is
  `"..."`, and an escaped letter or digit loses its backslash (`r\\m` is `rm`). A backtick
  pair is written as `$(...)` whenever its text changed when unescaped (a nested backtick),
  leaves a quote open (an apostrophe, closed at its end) or starts with `(` (written `$( (`,
  never `$((`; a parenthesis it leaves open is closed too); bash joins a backtick text's
  line continuations first, also in a comment there. `$$` before a `(` is written `${$}`.
  A named descriptor `{fd}>` is one also across
  a line continuation, and a heredoc delimiter word may hold a backtick pair or a `${...}`
  with blanks in it.
- G5: reserved words count where a command starts. `!`, `time [-p]`, `coproc [NAME]`,
  `function NAME` and `NAME ()` are dropped (a reserved word after `coproc` is its command,
  never its NAME) (a function's body is read where it stands). A
  compound command (`{ }`, `if`, `while`/`until`, `for`/`select`, `case`) becomes a list:
  its reserved words are separators, `for NAME in WORDS` and `case WORD in` become
  `: WORDS`, each case pattern `: PATTERNS` (bash expands them), `for ((...))`
  `: $((...))` (also `for (\\` NL `(`, which bash joins). In a pipeline, or with a
  redirection after its closer, it becomes a subshell, so it reads the pipe the way a
  subshell does. `[[ ... ]]` stays as written; bash 5.3's `${ cmd; }` and `${| cmd; }` are
  command substitutions, closed at a `}` where a command starts whatever follows it (`}x`).
  `${...}` ends at its first `}`; inside double quotes its nested quotes are left out
  (`"${x#"'"}"`), so the reading above does not take the inner `"` for the closing one;
  outside them a `<(...)` in its word is a process substitution (`${x:=<(cmd)}` runs it).
  Arithmetic reads its text as if in double quotes, so a `'` there quotes nothing and is
  left out: in `$((...))`, `((...))`, `for ((...))`, `$[...]` and a substring's offset and
  length (`${x:'$(cmd)'}` runs cmd); bash still finds where the arithmetic ends with such a
  span skipped whole, so a `))` inside one ends nothing. An operator, a redirection or a `(`
  inside an array
  assignment's parentheses (`x=(a;` NL `cmd`) is a syntax error after which bash drops the
  line and reads on at the next one, outside any compound that held it: refused as
  "broken_array", since no text the reading writes places that next line. A subscript
  (`m=([a;b]=1)`) is read up to its `]`, a newline between the parentheses separates
  elements like a blank (`m=(` NL `[a]=1` NL `)`), and an extglob group `@(...)` or `+(...)`
  is a word when the text turns extglob on; an array inside an array (`x=(a=(b))`) is that
  syntax error too. Where a command starts, `NAME[...]=` reads its subscript up to the `]`,
  blanks, operators and newlines included (`a[x;y]=1 cmd`).
  An empty process substitution expands to nothing (`/<()` is `/`). Inside arithmetic an
  unclosed `$[` or `${` ends at that arithmetic's own `)` (`for((;;$[))`),
  and a blank or an operator character inside
  `${...}` is part of its word, written quoted (`${x#'/' }cmd`, `${x#a; b <<< c}`).
- G6: redirections move behind the last word of their simple command, in their order
  (`>f mkfs x`, `sudo >f rm -rf /`, `rm -rf ~>/dev/null`, `{fd}>f rm -rf /`); `>|` is
  written `>`, an escaped blank quoted.
Every reading of a text no enclosing grammar text covers (the top level, and text a
command runs: `bash -c`, `eval`, a runner's command, the texts below) writes its grammar
text and joins its reading when the text changed; a text it leaves as it is costs no second
reading. At the top level that reading has a budget of its own, so an ordinary script read
twice does not run out; a nested one spends from the shared budget. Nested deeper than
_GRAMMAR_MAX_DEPTH, a command is refused as nested too deep.

ADDITIVE CHECKS, in every reading. Each only adds a category or a text read as a local
nested command, never a cut, and its `opaque_command` shape comes after the existing ones,
so no existing reason changes.
- B1 (shape "expansion"): a program word named by an expansion: an unquoted `$` (`$X`,
  `${X:-rm}`, `$'..'`, `$".."`), a `$` inside double quotes in its last path component
  (`"$X"`, `"$DIR/$TOOL"`; `"$VIRTUAL_ENV/bin/python"` names python), an unquoted glob or
  brace expansion (`/bin/r?`, `{rm,-rf,/}`). A tilde is fine. Read on the grammar text or a
  text it leaves as it is, since there a case pattern or the second half of `[[ a || $b ]]`
  stands where the reading above sees a program; elsewhere only `$'..'` and `$".."`. Not
  under an evaluated `$((...))`, where a variable is an operand. Also the program a runner
  or `find -exec` starts, in its spelling as written (`uv run "$TOOL" -rf ~`; the text it
  hands on is joined back with shlex, which quotes the variable into a literal), also the
  program after its wrappers and assignments (`find . -exec env X=1 $CMD ...`).
- B2: what reaches a shell's input as its script (`bash`, `sh -s`, `. /dev/stdin`) is read as
  that script: what `echo`, `printf` and `yes` print (dash's echo always decodes escapes,
  so both spellings; printf's format used again while arguments are left, `%b` with its
  argument's escapes decoded, opaque past a bound), what a subshell, a group or a command
  run from text prints (`bash -c "echo ..." | bash`), a heredoc or a
  here-string, passed on through filters (`cat`, `sort`, `sed` and the like) and commands
  that run another one (`eval`, `sh -c`, `env -S`, a runner), into a carried command too
  (`find . -exec bash \\; <<< '...'`, `{ bash; } <<< '...'`, `exec <<< '...'; bash`). Such
  a text, and the text a command runs (`bash -c`, `eval`, `su`, `ssh`, `watch`, `find`,
  `trap`), is also read from its words as bash's quote removal leaves them (_bash_word):
  shlex keeps the backslash of `\\$` in double quotes, so its token cannot tell `\\\\\\$`
  from `\\\\$` (`bash <<< "\\$(echo rm) -rf /"` runs `rm`). A variable or a substitution in
  what echo or printf prints makes it "shell_text"; a variable bash expands in a double-quoted here-string or an unquoted
  heredoc before the shell reads it makes it "expanded_input" (a quoted heredoc leaves the
  variables to the shell and passes).
- B3 (shape "decoded"): text a decoder writes (`base64`/`base32`/`basenc -d`, `xxd -r`,
  `openssl ... -d`, `uudecode`, `rev`) reaching a shell's input, through any filter.
- B4 (shape "encoded"): PowerShell's `-EncodedCommand` (`-enc`, `-e`, `-ec`, any prefix of
  two letters or more, after `-`, `--` or `/`).
- B5: the commands carriers run: `env -S`/`--split-string` (read as shell text and as env
  splits it, quotes grouping and `\\_` a blank, then the program after env's assignments,
  also those whose names are no identifier, `env }x=1 cmd`, `env -S 'x=|1 cmd'`), `watch`,
  `find -exec`/`-execdir`/`-ok`/`-okdir` (`{}` the start paths when no test comes before it, `{}`
  otherwise), `find START -delete` as `rm -rf START`, `trap`'s first argument. A shell's
  `-c` under xargs whose script holds the replace string or has none is "xargs_text";
  xargs' `-i`, `-e`, `-l` take their value only written against them.
- B6: a descriptor counts as opened only by a redirection before the one that copies it
  (`bash <&3- 3<f`), and a copy keeps what that descriptor holds (`cat 3< <(base64 -d f)
  <&3 | bash`); a subshell gets what its redirections give its input (`{ bash; } <&3`,
  `(bash) <<< "$(curl x)"`); `exec` redirecting descriptor 0 gives that input to every later
  command (`exec < <(curl x); bash`).
- B7: a substitution left as text in a quoted `NAME[...]` is read, not refused: arithmetic
  runs it once the string is an array reference (`x='a[$(...)]'; echo $((x))`, `printf -v`,
  `let`). A `$(` there ends where its parentheses balance and where the grammar reading ends
  it (a case pattern's `)` closes nothing); both texts are read.
- Output (B2): what a text writes is what every pipeline in it ends with, in order, and
  those writes joined (`(printf 'r'; printf 'm ...') | bash`): a sequential list, a group or a
  subshell hands on every command's output, also when redirections after its closer leave
  the standard output where it is (`( echo ... ) 2>&1 | bash`; `>f` hands on nothing).
  printf's precision, `%c` and a `*` width or precision are followed (a negative `*`
  precision is none), `\\c` in a `%b` argument ends all of its output, the size bound is
  kept per directive, before any padding, and a directive whose output is not modelled (a
  number, `%ls`, `%(...)T`) makes the output opaque; a `%` that starts no directive ends
  printf's output there, as bash's printf stops at it. A redirection to `/dev/stdout`,
  `/dev/fd/1` or `/proc/self/fd/1` moves nothing. An unquoted heredoc expands a `$` after an
  even run of
  backslashes (`\\\\$P`), not after an odd one (`\\$HOME` hands on `$HOME`).
Also: eval's own redirections are no words of the text it runs (`eval bash <<EOF`; its words
with them, as read before, are read as well); a
variable after a protected root in a removal target is judged by the root (`rm -rf /$X`,
`"$HOME/$X"`), one that starts the target is not (`rm -rf "$BUILD_DIR"`); `&>file` written
against its target is no script name. Assignment words before a program are stepped over in
every form bash takes (`a+=1`, `a[0]=1`, `a[k]+=v`); one of the latter that holds a
substitution stays the program word it was read as before, refused as opaque. A shell reads
options after `-c` too, up
to `-`, `--` or the first word that is none, and runs that word (`bash -c -e '...'`); its own
redirections are no script (`xargs -0 bash -c <<< '...'` leaves the script to xargs); a
token cannot tell a quoted word from a redirection (`-c '>x;cmd'`), so both readings of the
script are taken. An unquoted expansion where a shell's script name stands may expand to
nothing, so the shell's input is read as its script as well (`bash $! <<< '...'`); a quoted
one (`bash "$S"`) is a word even when empty. A here-string written `$'...'` hands the shell
its decoded text and expands nothing (`bash <<< $'set -e\\nmake'`); the token as the reading
before had it is read too, its `$` escaped. Inside double quotes a single-quoted span in the
word of `${...}` still quotes (`"${x#'"'}"`) when it closes before the next `}`; a lone `'`
there is text for a POSIX sh. A process substitution holds across a line continuation
(`<\\` NL `(`). An unquoted heredoc's substitutions are found both with each one copied as
it stands and with the body escaped alike, as the reading before did.

NAMED BOUNDARY: cmd.exe's and PowerShell's own grammar (`%VAR%` as the program, the text
`pwsh -Command` runs); dash's differences from bash, read as bash, the larger grammar;
decompressors and other programs that write new text piped into a shell (`gunzip`, `tr`,
`awk`); a script file an earlier command wrote and a later one runs; a variable's value (the
text cannot show it, so a variable is refused only as the program, or after a protected root
of a removal); extglob patterns; carriers not stepped over (`flock`, `strace`, `chroot`,
`busybox`, `runuser`, the stdin shell of `sudo -s`, `su` or `at`); shell features that store
command text and run it later or elsewhere: `alias` with `expand_aliases`, `${x@P}`, `PS4`
with `set -x`, `PROMPT_COMMAND`, `BASH_ENV`, `mapfile`/`readarray -C`, `complete -C`,
`bind -x` (`trap` is read); a descriptor named through a variable (`exec {fd}< ...; bash
/dev/fd/$fd`); a coprocess's or a named pipe's text; the file names `find` or `xargs` hands a
command; the output of other programs piped into a shell beyond those above, also text
`sed` or `cut` changes on its way in; a function used as the shell for its input
(`f() { bash; }; echo ... | f`) or as what prints into one (`f() { echo ...; }; f | bash`); a
glob as a shell's script name, which `nullglob` can turn into nothing (`bash *.q <<< ...`);
extglob groups `!(...)`, `*(...)` and `?(...)` in an array, refused as a syntax error or an
expansion though bash runs them with extglob on; an empty substitution or expansion glued to an
option
(`rm $()-rf /`, `rm $@-rf /`, or two backticks there); a variable or an empty substitution
before a `/`
with no folder written in front of it (also `${ }/`, an empty `${ cmd; }`) (`rm -rf $X/`
passes, since the text cannot show that X is empty; only a
variable after a written protected folder, `~/.cache/$X`, is refused, and one after another
variable, `$HOME/.cache/$X`, passes too) and a
glob a file name turns into an option (`rm *` beside a file named `-rf`), both the state of the
machine, not the text; a shell's own error messages fed into a shell (`cmd 2>&1 | bash`
hands on `command not found` lines that quote the command word). On
Windows, cmd.exe's `for %i in (*.txt) do ...` reads as bash and is refused, its reason naming
PowerShell (a bash `for` there takes `(*.txt)` for a subshell running a glob). The reserved
word `case` where a command starts inside a substitution refuses the command in the reading
above, since a pattern's `)` would close the substitution early:
`case`, one shell word (quote-aware) and `in`; the word elsewhere, also at the start of a
line of prose ("case 2 is covered"), does not.

The two readings before the grammar reading take no `#` as a comment, so for them an
apostrophe in a comment opens a quote that swallows later lines; the grammar reading reads
those lines. The frozen reading reads every line of a heredoc body as a command line: a body
line that STARTS with what reads as a command is refused (`rm -rf / was the bug`), one that
only mentions it is not. The current reading takes a heredoc body as data where it surely is
one (`<<WORD`, `<<'WORD'`, `<<-WORD` at the top level, outside quotes, `${...}`, `$[...]` and
a comment, at most eight on one line, with its delimiter line present, also when a CRLF text
lost the last line's `\\r`): no command lines, no nested readings, so a file written through
`cat > f <<'EOF'` passes when the frozen reading lets it; an unquoted body's substitutions
are read as commands, never as a program word, and a body fed to a shell is that shell's
script. Everywhere else the lines are read as command lines, so a wrong guess here can only
cost refusals this reading adds, never one of the frozen reading. A `<<` after a `#` that
starts a comment for bash on its line (_comment_starts_at) opens no heredoc: bash takes the
rest of the line as a comment and runs the next lines (`echo ok # <<X`).
Design rule for any reading: a new one may only ADD refusals to the existing readings, never
remove one; the union holds it.

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
    # Not `-i`, `-e`, `-l`: GNU xargs takes their value only written against them (`-i{}`), so
    # the next word is the command (`xargs -i rm -rf /`).
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s",
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
_SUBST_MARKS = (_SUBST_BARE, _SUBST_QUOTED, _SUBST_PROC)
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
    "broken_array": "has an operator, a redirection or a `(` inside an array assignment's "
                    "parentheses, a syntax error after which bash goes on with the next line; "
                    "quote that element (`arr=('a;b' c)`)",
    "expansion": "takes its program name from a variable, a glob, a brace expansion or `$'...'` "
                 "quoting, so the text being approved does not show which program runs; write "
                 "the program name out (`python3 -m pytest`, not `$PYTHON -m pytest`), and for "
                 "`eval \"$cmd\"` or `bash -c \"$cmd\"` the command itself; a variable may stand "
                 "in an argument, and inside double quotes it may name the folder "
                 "(`\"$VIRTUAL_ENV/bin/python\"`)",
    "expanded_input": "feeds a shell text in which bash expands variables before the shell reads "
                      "it (an unquoted heredoc, a here-string in double quotes), so the text being "
                      "approved is not the text that will run; quote it (`<<'EOF'`, "
                      "`<<< '...'`) so the shell reads the variables itself (a variable that "
                      "is not exported is empty in that shell: export it, or write its value "
                      "in), or escape the variable (`\\$P`)",
    "xargs_text": "lets xargs fill in the script a shell's `-c` runs, so the text being approved "
                  "does not show what runs; hand each item over as an argument instead "
                  "(`xargs -I{} sh -c 'wc -l \"$1\"' _ {}`), or run the program directly "
                  "(`xargs -I{} wc -l {}`)",
    "decoded": "runs text it decodes first (`base64 -d | sh`), so the text being approved is not "
               "the text that will run; decode it into a file, read the file, then run it",
    "encoded": "hands PowerShell an encoded command, so the text being approved is not the text "
               "that will run; write the command out with `-Command`",
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
    # What the text writes into a pipe after it (B2, B3): an opaque shape and literal texts, so
    # a subshell or a group that prints hands its output on (`( echo '...' ) | sh`).
    output: tuple = field(default=("", ()), repr=False)

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
                # And the body as written, for what bash expands in it (a backslash run's
                # parity decides, which the supplied text no longer shows).
                target.append(("heredoc_raw", text))
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
    (start of the line, end of the line) of the first line equal to the delimiter. `joined`:
    a line ending in a line continuation goes on in the next one, as bash reads an unquoted
    heredoc's body before it compares a line with the delimiter (`EO\\` and `F` end a body
    delimited by `EOF`); the span then covers every physical line of it."""

    def __init__(self, command: str, joined: bool = False) -> None:
        self.exact: dict = {}
        self.tabbed: dict = {}
        pos = 0
        while pos <= len(command):
            end = command.find("\n", pos)
            end = len(command) if end < 0 else end
            line = command[pos:end]
            if joined and end < len(command) and _escaped(command, end):
                start, pieces = pos, []
                while end < len(command) and _escaped(command, end):
                    pieces.append(command[pos:end - 1])
                    pos = end + 1
                    end = command.find("\n", pos)
                    end = len(command) if end < 0 else end
                pieces.append(command[pos:end])
                line, pos = "".join(pieces), start
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
    backtick, a backslash and a newline, and every other character is text. Read twice: with
    each substitution copied as it stands, and with every character escaped alike, as the
    reading before read it (union: where the two find different ends, both are read)."""
    found: List[tuple] = []
    for whole in (True, False):
        for entry in _heredoc_substitutions_once(body, whole):
            if entry not in found:
                found.append(entry)
    return found


def _heredoc_substitutions_once(body: str, whole: bool) -> List[tuple]:
    out: List[str] = []
    k = 0
    while k < len(body):
        c = body[k]
        if whole and (c == "$" and body[k + 1:k + 2] == "(" or c == "`"):
            # A substitution's text is parsed afresh as a command: copied as it stands, so a
            # backslash in it keeps its meaning there (`$(bash <<< 'r\m -rf /')`).
            end = _substitution_end(body, k)
            out.append(body[k:end])
            k = end
            continue
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
        # A substitution the splitter could not read (a `case` inside it) is kept: it refuses
        # as unreadable, as the same substitution in a command does.
        found.extend(c for c in carried
                     if c[0] in ("command", "arithmetic", "arithmetic*", "unreadable"))
    return found


def _brace_group_end(text: str, k: int) -> int:
    """After the `}` that ends the `${` whose text starts at `k`, inside double quotes: quoted
    spans in it skipped whole (bash nests quotes there), an escaped character skipped; the end
    of the text when it does not close. One pass."""
    n = len(text)
    while k < n and text[k] != "}":
        c = text[k]
        if c == "\\":
            k += 2
        elif c in ("'", '"'):
            close = text.find(c, k + 1)
            k = n if close < 0 else close + 1
        else:
            k += 1
    return min(k + 1, n)


def _substitution_end(text: str, at: int, closed: Optional[List[bool]] = None,
                      ansi_c: bool = False) -> int:
    """Where the `$(` or backtick substitution at `at` ends (after its closer, or the end of
    the text): parentheses counted outside quotes, a quoted span skipped whole, an escaped
    character skipped. One pass from `at`. `closed` gets a True when the closer was found.
    `ansi_c`: a `$'...'` span is skipped with its `\\'` escapes, and a backquoted span whole, as
    bash's matching does."""
    n = len(text)
    if text[at] == "`":
        k = at + 1
        while k < n and text[k] != "`":
            k += 2 if text[k] == "\\" else 1
        return min(k + 1, n)
    depth = 0
    k = at + 1
    while k < n:
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if c == "`" and ansi_c:
            # A backquoted span is skipped whole, as bash's matching does (`$((` read as a
            # command substitution, a `)` inside backticks closes nothing).
            close = k + 1
            while close < n and text[close] != "`":
                close += 2 if text[close] == "\\" else 1
            k = close + 1
            continue
        if c in ("'", '"'):
            ansi = ansi_c and c == "'" and k > at and text[k - 1] == "$"
            close = k + 1
            while close < n and text[close] != c:
                close += 2 if (c == '"' or ansi) and text[close] == "\\" else 1
            k = close + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                if closed is not None:
                    closed.append(True)
                return k + 1
        k += 1
    return n


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
        if "=" in tok and (tok.split("=", 1)[0].isidentifier() or (
                _ASSIGNMENT_WORD_RE.match(tok) and not any(m in tok for m in _SUBST_MARKS))):
            # VAR=value prefix, also `a+=1`, `a[0]=1`, `a[k]+=v`; one of the latter holding a
            # substitution stays the program word it was read as before (refused as opaque).
            i += 1
            continue
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


def _bash_word(raw: str) -> str:
    """A word as written (`_raw_words`) after bash's own quote removal: single quotes keep
    everything, double quotes drop the backslash before `$`, a backtick, `"`, a backslash and
    a newline, `$'...'` is decoded, and outside quotes a backslash goes. shlex keeps `\\$` in
    double quotes, so from its token `\\\\\\$` and `\\\\$` look alike; bash makes `\\$` of the
    first and `\\` and an expansion of the second."""
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "'":
            close = raw.find("'", i + 1)
            close = n if close < 0 else close
            out.append(raw[i + 1:close])
            i = close + 1
        elif c == '"':
            i += 1
            while i < n and raw[i] != '"':
                if raw[i] == "\\" and raw[i + 1:i + 2] and raw[i + 1] in '$`"\\\n':
                    if raw[i + 1] != "\n":
                        out.append(raw[i + 1])
                    i += 2
                    continue
                out.append(raw[i])
                i += 1
            i += 1
        elif c == "$" and raw[i + 1:i + 2] == "'":
            writer = _GrammarText(raw)
            quoted = writer._ansi_c(i + 2)
            out.append(quoted[1:-1].replace("'\\''", "'"))
            i = writer.i
        elif c == "\\":
            if raw[i + 1:i + 2] not in ("", "\n"):
                out.append(raw[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# Commands that run a text given among their words (_nested_commands): their words are also
# read the way bash hands them over (_bash_word), when a backslash makes that differ.
_TEXT_RUNNERS = _COMMAND_STRING_SHELLS | frozenset({"su", "eval", "ssh", "watch", "find", "trap"})


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


def _nested_commands(exe: str, args: List[str], reparse: bool = True) -> Iterable[tuple]:
    """(where, text) for every command this one runs from TEXT it is given: "local" for
    `sh -c`, `su -c` and `eval`, "runner" for the command a project or package runner starts
    (`uv run ...`, `npx ...`, joined back into one text, lazily, so a caller that stops early
    does not pay for the rest), "remote" for `ssh host ...`. A text a shell parses again carries
    its substitutions as bare ones (_reparsed). `reparse` False: the words are already as
    bash hands them over (_bash_word), so only the marks change."""
    def again(where: str, text: str) -> List[tuple]:
        # As bash parses it again; also as it stands when that differs: a single-quoted
        # word keeps its `\\$` for the shell it reaches (`sh -c 'find ... "\\$(...)"'`).
        plain = text.replace(_SUBST_QUOTED, _SUBST_BARE)
        parsed = _reparsed(text) if reparse else plain
        return [(where, parsed)] + ([(where, plain)] if plain != parsed else [])
    if exe in _COMMAND_STRING_SHELLS:
        found: List[tuple] = []
        for pos, tok in enumerate(args):
            if tok in ("-o", "+o"):           # `bash -o pipefail -c ...`: -o takes a word
                continue
            if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                found = again("local", args[pos + 1]) if pos + 1 < len(args) else []
                break
        # bash reads options after `-c` too, up to `-`, `--` or a word that is none
        # (`bash -c -e '...'`, `bash -c -- '...'`): that word is the script, read as well.
        for strip in (True, False):
            script = _shell_c_script(args, strip)
            if isinstance(script, str):
                found += [entry for entry in again("local", script) if entry not in found]
        return found
    if exe == "su":
        for pos, tok in enumerate(args):
            if tok in ("-c", "--command") and pos + 1 < len(args):
                return again("local", args[pos + 1])
            if tok.startswith("--command="):
                return again("local", tok.split("=", 1)[1])
        return []
    if exe == "eval":
        return again("local", " ".join(args)) if args else []
    if exe == "ssh":
        remote = _ssh_remote_command(args)
        return again("remote", remote) if remote.strip() else []
    if exe == "trap":
        # `trap 'cmd' EXIT`: bash runs its first argument as a command later, through the
        # shell; with one word only (`trap EXIT`) or `-` it resets the signals.
        k = 1 if args[:1] == ["--"] else 0
        if k + 1 < len(args) and not args[k].startswith("-"):
            return again("local", args[k])
        return []
    if exe == "watch":
        watched = _watch_text(args)
        return [("local", watched)] if watched else []
    if exe == "find":
        found = _find_command_text(args)
        return [("local", found)] if found else []
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
    # A variable after a literal part is judged by that part, like a substitution: `/$X` and
    # `~/$X` start at a protected root whatever the variable holds. One that starts the
    # target (`"$BUILD_DIR"`) names a path the text does not show, judged as before.
    dollar = arg.find("$", 1)
    while dollar > 0:
        if arg[dollar + 1:dollar + 2] and arg[dollar + 1] in _DQ_EXPANSION_FOLLOWERS:
            arg = arg[:dollar]
            break
        dollar = arg.find("$", dollar + 1)
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
                     _stdin_fetch: bool = False, _arithmetic: bool = False,
                     _stdin_opaque: str = "", _stdin_texts: tuple = (),
                     _normalized: Optional[bool] = None,
                     _in_arith: bool = False) -> CommandVerdict:
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
    operand there (`$(($(nproc)+1))`), every other check holds. `_stdin_opaque` names how a
    nested command's input hides a script (B3, B6: "decoded", "shell_text", "descriptor");
    `_normalized` says the text is a grammar text, or one inside it (None: decide here, and
    join the grammar reading when it differs); `_in_arith` says the text is read under an
    evaluated `$((...))`, where a variable is an operand (B1 waived).
    """
    if _budget is None and _depth == 0:
        try:
            verdict = _read(command, profile=profile, _depth=0, _budget=None,
                            _stdin_fetch=_stdin_fetch, _arithmetic=_arithmetic,
                            _stdin_opaque=_stdin_opaque, _stdin_texts=_stdin_texts,
                            _normalized=_normalized, _in_arith=_in_arith)
            lead = "Command contains a forbidden pattern: "
            if verdict.blocked and verdict.opaque_shape == "expansion" and \
                    verdict.reason.startswith(lead + "it ") and _CMD_FOR_RE.search(command):
                # Refused by the reading of the glob in its `(...)`, which has no `for`: the
                # cmd.exe way out first, the reason the bash reading gives after it.
                verdict.reason = (lead + _CMD_FOR + "; read as bash, it "
                                  + verdict.reason[len(lead) + len("it "):])
            return verdict
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
                 _stdin_fetch=_stdin_fetch, _arithmetic=_arithmetic,
                 _stdin_opaque=_stdin_opaque, _stdin_texts=_stdin_texts,
                 _normalized=_normalized, _in_arith=_in_arith)


def _read(command: str, *, profile: str, _depth: int, _budget: Optional[List[int]],
          _stdin_fetch: bool, _arithmetic: bool, _stdin_opaque: str = "",
          _stdin_texts: tuple = (), _normalized: Optional[bool] = None, _in_arith: bool = False,
          _grammar: bool = False) -> CommandVerdict:
    """classify_command's reading; nested readings go through classify_command. `_grammar`:
    this is the grammar reading of a text (_join_the_grammar_reading), joined to nothing."""
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
    # The grammar reading's text: written by every reading no enclosing grammar text covers
    # (the top level, and text a command runs: `bash -c`, `eval`, a runner's command). The
    # program-word check (B1) needs bash's structure, so it reads a text the grammar reading
    # leaves as it is, or the grammar text itself; anywhere else only `$'..'` and `$".."`.
    g_text: Optional[str] = None
    too_deep = broken_array = False
    if _normalized is None:
        try:
            g_text = _grammar_text(text)
        except _GrammarTooDeep:
            too_deep = True                 # nested past anything a person reviews
        except _GrammarBrokenArray:
            broken_array = True
    normalized = _normalized if _normalized is not None else g_text == text

    triples = _split_segments(text)
    verdict.segments = [seg for _, seg, _ in triples if seg]
    cats: List[str] = ["nested_too_deep"] if too_deep else []
    if broken_array:
        cats.append("opaque_command")
        opaque_shape = "broken_array"

    if _is_fork_bomb(text):
        cats.append("fork_bomb")

    # What a nested command found. A refusal inside refuses the whole; a remote command's
    # categories are shown but judged by the remote profile, not by this one.
    nested_refusal = ""
    remote_cats: List[str] = []

    pipeline_fetch = _stdin_fetch
    # What the next segment's input carries, like the fetch above (B2, B3, B6): an opaque
    # shape (decoded text, another command's output, a descriptor an earlier command opened)
    # or literal text (a heredoc's body, a here-string, what echo prints). A shell that reads
    # its script from that input is refused for the shape and reads the text.
    pipeline_opaque = _stdin_opaque
    pipeline_texts: List[str] = list(_stdin_texts)
    exec_input: Optional[tuple] = None          # `exec <...`: the input of every later segment
    closer_output: Optional[tuple] = None       # what the subshell just closed writes
    text_output = _Output()                     # what this whole text writes (verdict.output)
    # The shapes of the additive checks, made `opaque_command` after the loop, so a shape an
    # existing check named stays the one the refusal names.
    late: List[str] = []
    in_test = False                             # inside a `[[ ... ]]` cut at `&&` or `||`
    for index, (connector, seg, carried) in enumerate(triples):
        # Only a pipe continues a pipeline. `curl x > f; python parse.py` is a
        # download followed by a separate, human-visible command - the block is
        # reserved for the unreviewable direct pipe into an interpreter. A segment
        # outside a pipe reads this command's own input, fetched code when a fetch
        # pipes into the runner or shell that carries it.
        if connector != "pipe":
            pipeline_fetch = _stdin_fetch
            pipeline_opaque, pipeline_texts = exec_input or (_stdin_opaque, list(_stdin_texts))
        toks = _tokens(seg)
        ansi_here: Optional[List[str]] = None
        if "<<<" in seg and "$'" in seg:
            # A here-string written `$'...'` hands the shell its decoded text; shlex keeps the
            # `$` in front of it (`bash <<< $'set -e\nmake'` read as `$set ...`). The token as
            # the reading before had it is read as well, its `$` escaped, so it names no program
            # by an expansion but keeps what that reading refused (`$x=$(...)`).
            written = _raw_words(seg)
            if len(written) == len(toks):
                for k in range(len(toks) - 1):
                    if _HERE_OPERATOR_RE.match(toks[k]) and written[k + 1].startswith("$'") and \
                            not any(m in toks[k + 1] for m in _SUBST_MARKS):
                        # (A token the reading before saw a substitution in stays as it was:
                        # that reading refused it as shell text another command prints.)
                        if ansi_here is None:
                            ansi_here = list(toks)
                        tok = toks[k + 1]
                        ansi_here[k + 1] = "\\" + tok if tok[:1] == "$" and \
                            tok[1:2] in _EXPANSION_FOLLOWERS and tok[1:2] != "(" else tok
                        toks[k + 1] = _bash_word(written[k + 1])
        exe_at, exe = _executable_at(toks)
        args = toks[exe_at + 1:] if exe_at >= 0 else []
        low = seg.lower()
        testing = in_test
        if exe == "[[":
            in_test = "]]" not in toks[exe_at + 1:]
        elif in_test and "]]" in toks:
            in_test = False
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
        from_pipe = False
        # A here-string's word as bash's quote removal leaves it (_bash_word): shlex keeps
        # `\$` in double quotes, so `\\\$` and `\\$` look alike in its token.
        spoken_toks: Optional[List[str]] = None
        if "<<<" in seg and "\\" in seg:
            raws = _raw_words(seg)
            if len(raws) == len(toks):
                spoken_toks = [_bash_word(r) for r in raws]
                if spoken_toks == toks:
                    spoken_toks = None
        # Other spellings of the words to read the input from as well (union).
        alternatives = [alt for alt in (spoken_toks, ansi_here) if alt is not None]
        if exe in _SHELL_SINKS:
            # The words as written tell a quoted expansion (`bash "$S"`, never empty words) from
            # a bare one that may vanish (`bash $S`).
            sink_raws = None
            if exe_at >= 0 and any(a[:1] in ("$", _SUBST_BARE) for a in args):
                written = _raw_words(seg)
                if len(written) == len(toks):
                    sink_raws = written[exe_at + 1:]
            from_where, script_literals, from_pipe = _script_input(exe, args, carried, sink_raws)
            for alt in alternatives if exe_at >= 0 else ():
                extra_literals = _script_input(exe, alt[exe_at + 1:], carried)[1]
                script_literals = script_literals + [
                    t for t in extra_literals if t not in script_literals]
            if from_where:
                if "opaque_command" not in cats:
                    cats.append("opaque_command")
                opaque_shape = (_shell_text_shape(carried) if from_where == "output"
                                else "descriptor")

        # The additive checks (module docstring, B1-B7): each adds a late shape or a text read
        # as a local nested command below, never a cut of the text.
        extra: List[str] = []
        if exe_at >= 0 and not testing and not _arithmetic and not _in_arith \
                and _program_named_by_expansion(seg, toks, exe_at, normalized):
            late.append("expansion")
        # The program a runner or `find -exec` starts, in its spelling as written: the text
        # handed on is joined back with shlex, which quotes `$TOOL` into a literal.
        if exe_at >= 0 and not testing and not _arithmetic and not _in_arith:
            started = _started_programs(exe, args)
            if any(_EXPANSION_HINT_RE.search(args[k]) for k in started):
                raws = _raw_words(seg)
                if len(raws) == len(toks) and any(
                        not _ASSIGNMENT_WORD_RE.match(raws[exe_at + 1 + k])
                        and _names_program_by_expansion(raws[exe_at + 1 + k], normalized)
                        for k in started):
                    late.append("expansion")
        # This segment's own input, else the pipe's (or the enclosing command's).
        own_shape, own_texts, own_redirected = _input_of(toks, carried)
        expanded = False
        if "<<" in seg:
            raws = _raw_words(seg)
            expanded = len(raws) == len(toks) and _expanded_input(raws, toks, carried)
            if expanded and own_redirected and not own_shape:
                own_shape = "expanded_input"  # text with variables bash expands first
            if expanded and exe in _STDIN_SHELLS and script_literals:
                late.append("expanded_input")
        for alt in alternatives:
            own_texts = own_texts + [t for t in _input_of(alt, carried)[1]
                                     if t not in own_texts]
        in_shape, in_texts = ((own_shape, own_texts) if own_redirected
                              else (pipeline_opaque, pipeline_texts))
        scripts: List[str] = []
        if from_pipe:
            if in_shape:
                late.append(in_shape)
            if exe in _STDIN_SHELLS:            # also `. /dev/stdin`, `source /dev/stdin`
                scripts.extend(in_texts)
        if exe in ("powershell", "pwsh") and _encoded_command(args):
            late.append("encoded")
        if exe_at > 0 and exe in _COMMAND_STRING_SHELLS and _xargs_runs_its_input(toks, exe_at,
                                                                                 args):
            late.append("xargs_text")
        extra.extend(_env_split_texts(toks, exe_at))
        extra.extend(_env_assignment_texts(toks, exe_at))
        # eval's redirections are its own, not words of the text it runs (`eval 'bash' <<EOF`
        # feeds the heredoc to the bash it starts); a quoted word is never one.
        run_args = args
        if exe == "eval" and any(_redirection_width(args, k) for k in range(len(args))):
            raws = _raw_words(seg)
            if len(raws) == len(toks):
                run_args = _without_redirections(args, raws[exe_at + 1:])
        # The text a command runs, as bash hands its words over (`bash -c "...\\\$(...)"`
        # nested a level deeper): read too when a backslash makes it differ (union).
        bash_args: Optional[List[str]] = None
        if exe in _TEXT_RUNNERS and any("\\" in a for a in args):
            raws = _raw_words(seg)
            if len(raws) == len(toks):
                spoken = [_bash_word(r) for r in raws[exe_at + 1:]]
                if exe == "eval":
                    spoken = _without_redirections(spoken, raws[exe_at + 1:])
                if spoken != args and spoken != run_args:
                    bash_args = spoken
        extra.extend(_subscript_text(seg))
        # A subshell's input redirections stand after it, in a segment of their own
        # (`( bash ) <&3`, `{ bash; } < <(curl x)` as the grammar reading writes it).
        subshell_input = (in_shape, in_texts)
        if not toks and index + 1 < len(triples) and any(k == "subshell" for k, _ in carried):
            after_connector, after_seg, after_carried = triples[index + 1]
            after_toks = _tokens(after_seg)
            if after_connector != "pipe" and _only_redirections(after_toks):
                after_shape, after_texts, after_redirected = _input_of(after_toks, after_carried)
                if after_redirected:
                    subshell_input = (after_shape, after_texts)
        decodes = exe_at >= 0 and _decodes(exe, args)
        # What this segment writes into the pipe: what echo or printf prints, decoded text, or
        # its own input passed on (a filter such as `cat` or `sort` hands it through).
        printed = _printed_text(seg, toks, exe_at, exe)
        if decodes:
            out_input = ("decoded", [])
        elif printed is not None:
            out_input = ("shell_text", []) if printed[1] else ("", printed[0])
        elif exe in _TEXT_FILTERS or exe in ("eval", "su") or (
                exe in _COMMAND_STRING_SHELLS and _shell_c_script(args) is not None) or (
                _env_split_texts(toks, exe_at)) or _runner_commands(exe, args):
            # A filter, or a command that runs another one which may be one (`eval 'cat'`,
            # `sh -c 'sed ...'`): its input may come out again.
            out_input = (in_shape, in_texts)
        else:
            out_input = (in_shape, [])

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
                if kind in ("heredoc_q", "heredoc_u", "heredoc_raw"):
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
            # A script a shell reads from its input ("script") has consumed that input.
            for literal in script_literals + scripts:
                yield "", "script", literal
            for where, inner in _nested_commands(exe, run_args) if exe_at >= 0 else ():
                yield "", where, inner
            if run_args is not args:
                # eval's words with its redirections as well, as the check read them before:
                # one reading more, so no earlier refusal is lost (union).
                for where, inner in _nested_commands(exe, args):
                    yield "", where, inner
            if bash_args is not None:
                for where, inner in _nested_commands(exe, bash_args, reparse=False):
                    yield "", where, inner
            for inner in extra:
                yield "", "local", inner

        # `>( )` receives what this segment writes: its fetched input passed on, or what it
        # fetches itself (`curl x | tee >(bash)`, `curl x > >(bash)`).
        writes_fetched = pipeline_fetch or exe in _NETWORK_FETCHERS or exe == "ssh"
        subshell_output: Optional[tuple] = None  # what a carried subshell writes
        carried_output: Optional[tuple] = None   # what a command it runs from text writes
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
                stdin_input = out_input
            else:
                stdin_fetch = pipeline_fetch and local and kind in (
                    "", "command", "subshell", "arithmetic", "arithmetic*")
                stdin_input = (subshell_input if kind == "subshell" else
                               (in_shape, in_texts) if local and where != "script"
                               and kind in ("", "command") else ("", []))
            # Text a command runs decides its own grammar reading; a substitution or a
            # subshell is part of this text's.
            sub = classify_command(inner, profile=profile if local else "remote",
                                   _depth=_depth + 1, _budget=_budget,
                                   _stdin_fetch=stdin_fetch,
                                   _arithmetic=kind == "arithmetic",
                                   _stdin_opaque=stdin_input[0],
                                   _stdin_texts=tuple(stdin_input[1]),
                                   _normalized=None if not kind else normalized,
                                   _in_arith=kind == "arithmetic" or (
                                       _in_arith and kind == "subshell"))
            if kind:
                verdict.segments.extend(sub.segments)
            if kind == "subshell" and sub.output != ("", ()):
                subshell_output = sub.output
            elif not kind and where in ("local", "runner") and sub.output != ("", ()):
                carried_output = sub.output     # `bash -c "echo ..." | bash`
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
        if not toks and subshell_output is not None:
            out_input = (subshell_output[0], list(subshell_output[1]))   # `( echo ... ) | sh`
        elif carried_output is not None and printed is None and not decodes:
            out_input = (carried_output[0] or out_input[0],
                         list(carried_output[1]) + list(out_input[1]))
        elif (toks and connector != "pipe" and closer_output is not None
              and _only_redirections(toks)):
            # The redirections after a subshell's or a group's closer stand in a segment of
            # their own: what it writes goes on unless they move the standard output away
            # (`( echo ... ) 2>&1 | bash`, `{ ...; } >&1 | sh`; `( ... ) >f | bash` hands on
            # nothing).
            out_input = ("", []) if _moves_stdout(toks) else closer_output
        closer_output = (out_input[0], list(out_input[1])) if not toks and (
            subshell_output is not None) else None
        pipeline_opaque, pipeline_texts = out_input
        # The text's own output: every pipeline's last segment writes to it, in order (a
        # sequential list, a group or a subshell prints what each of its commands prints, and
        # bash joins the writes: `(printf 'r'; printf 'm -rf ~')`). A subshell whose
        # redirections stand in the next segment hands its output to that segment.
        nxt = triples[index + 1] if index + 1 < len(triples) else None
        if nxt is None or nxt[0] != "pipe" and not (
                closer_output is not None and _only_redirections(_tokens(nxt[1]))):
            text_output.add(*out_input)
        if toks and _exe_base(toks[0]) == "exec" and _only_redirections(toks[1:]):
            # `exec < <(curl x)`: every later segment of this text reads that input (B6).
            exec_shape, exec_texts, exec_redirected = _input_of(toks[1:], carried)
            if exec_redirected:
                exec_input = (exec_shape, exec_texts)
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

    if late and "opaque_command" not in cats:
        cats.append("opaque_command")
        opaque_shape = opaque_shape or late[0]
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
    out_texts = tuple(text_output.texts)
    if len(out_texts) > 1:
        # The writes as bash joins them, one after the other (`printf 'r'; printf 'm ...'`).
        out_texts = out_texts + ("".join(out_texts),)
    verdict.output = (text_output.shape, out_texts)
    if blocking:
        verdict.blocked = True
        verdict.reason = _refusal(blocking[0], verdict.opaque_shape, text,
                                  share_piece=share_piece)
    elif nested_refusal:
        verdict.blocked = True
        verdict.reason = nested_refusal
    if g_text is not None and g_text != text:
        _join_the_grammar_reading(verdict, g_text, profile=profile, depth=_depth, budget=_budget,
                                  stdin_fetch=_stdin_fetch, arithmetic=_arithmetic,
                                  stdin_opaque=_stdin_opaque, stdin_texts=_stdin_texts,
                                  in_arith=_in_arith)
    if _depth == 0 and not _grammar:
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
                  and opaque_shape in ("program", "shell_text", "expansion")
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
        elif quote_cure and opaque_shape == "expansion":
            sentence = _HEREDOC_EXPANSION + _QUOTE_FIRST + _HEREDOC_EXPANSION_OR
        elif opaque_shape in ("program", "frozen") and "\n" in text:
            sentence += _OPAQUE_REASONS["lines"]    # perhaps a code fence or a continued line
    sentence += _WAY_OUT.get(category, "")
    if category == "destructive_removal" and _root_then_variable(text):
        sentence += _ROOT_THEN_VARIABLE
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
# A removal target with a variable after a protected folder (`~/.cache/$X`, `"$HOME/$X"`,
# `/var/tmp/$USER`): judged by that folder, since the variable may be empty.
_ROOT_THEN_VARIABLE_RE = re.compile(r"^[\"']?(?:~|/|\$HOME\b|\$\{HOME\})[^$]*\$[A-Za-z_{]")
# An `rm` and the words of its simple command, where such a target is looked for (only an
# `rm` target gets the sentence, not a path elsewhere in the command).
_RM_WORDS_RE = re.compile(r"(?:^|[\s;&|(`])rm\s[^;&|\n]*")


def _root_then_variable(text: str) -> bool:
    """Whether an `rm` in `text` has a target with a variable after a protected folder."""
    return any(_ROOT_THEN_VARIABLE_RE.match(word)
               for m in _RM_WORDS_RE.finditer(text) for word in m.group().split()[1:])
_ROOT_THEN_VARIABLE = ("; a variable after a folder may be empty, and then the folder itself "
                       "is deleted, so write the path out (`rm -rf ~/.cache/myapp`)")
# cmd.exe's `for %i in (...)`, which a bash reading takes for a subshell running a glob.
# An option's value never starts with `/`, so each text has one parse: linear (an optional value
# that may itself be the next `/d` backtracks exponentially).
_CMD_FOR_RE = re.compile(
    r"(?i)(?:^|[\s;&|(])for\s+(?:/[a-z]\s+(?:[^/\s]\S*\s+)?)*%%?[a-z]\s+in\s*\(")
_CMD_FOR = ("cmd.exe's `for %i in (...)` is read as bash here, so use PowerShell "
            "(`powershell -Command \"Get-ChildItem *.txt | ForEach-Object { ... }\"`)")
# Offered only while the frozen reading lets the command pass (_join_the_frozen_reading
# takes both out otherwise): that reading reads every line, quoted delimiter or not.
_QUOTE_THE_DELIMITER = ("; or, if the heredoc's text must not be expanded, quote its delimiter "
                        "(`<<'EOF'`)")
_QUOTE_FIRST = (" quote the heredoc's delimiter (`<<'EOF'`) if its text must not be "
                "expanded, or")
_HEREDOC_PROGRAM = ("takes a program name from a command substitution in an unquoted heredoc, "
                    "which the shell expands as it writes the text;")
_HEREDOC_PROGRAM_OR = " run the substitution as a command of its own first and write the result in"
# The same for a command in an unquoted heredoc whose program a variable names (a template
# literal `` `/api/${id}` `` in a body is such a command).
_HEREDOC_EXPANSION = ("runs a command in an unquoted heredoc whose program a variable, a glob or "
                      "a brace expansion names, and the shell runs it as it writes the text;")
_HEREDOC_EXPANSION_OR = " write the program name out"


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
        if kind not in ("heredoc_q", "heredoc_u", "heredoc_raw")])
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
# `"$SHELL" f`, and after a reserved word that starts one: `{ $SHELL f; }`, `then $SHELL f`);
# a substitution is not one, nor `'$X'`, which is literal.
_VARIABLE_COMMAND_RE = re.compile(
    r"(?:(?:^|[;&|\n(`])|(?:^|[\s;&|(`])(?:\{|then|do|else|elif|if|while|until|!|time)\s)"
    r"\s*\"?\$[A-Za-z_{0-9]")
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


def _join_the_grammar_reading(verdict: CommandVerdict, g_text: str, *, profile: str, depth: int,
                              budget: List[int], stdin_fetch: bool, arithmetic: bool,
                              stdin_opaque: str, stdin_texts: tuple, in_arith: bool) -> None:
    """Refuse whenever the grammar reading of the same command (_grammar_text) refuses, with its
    categories and segments. Like the frozen reading it may only ADD refusals: the reading of
    the text as written stays as it is. At the top level the grammar reading has a budget of
    its own (an ordinary script read twice must not run out); a nested one spends from the
    shared budget, which bounds the whole call."""
    own = budget if depth > 0 else [MAX_NESTED_COMMANDS, len(g_text) + _RUNNER_TEXT_SLACK]
    grammar = _read(g_text, profile=profile, _depth=depth, _budget=own, _stdin_fetch=stdin_fetch,
                    _arithmetic=arithmetic, _stdin_opaque=stdin_opaque,
                    _stdin_texts=stdin_texts, _normalized=True,
                    _in_arith=in_arith, _grammar=True)
    verdict.categories.extend(c for c in grammar.categories if c not in verdict.categories)
    seen = set(verdict.segments)
    for seg in grammar.segments:
        if seg not in seen:
            seen.add(seg)
            verdict.segments.append(seg)
    if "opaque_command" in grammar.categories and not verdict.opaque_shape:
        verdict.opaque_shape = grammar.opaque_shape
    verdict.share_piece = verdict.share_piece or grammar.share_piece
    # What the text writes, as either reading has it (`{ echo ...; } | sh`: a group as bash
    # reads it).
    verdict.output = _output_union(verdict.output, grammar.output)
    if grammar.blocked and not verdict.blocked:
        verdict.blocked = True
        verdict.reason = grammar.reason
        verdict.opaque_shape = grammar.opaque_shape or verdict.opaque_shape
        verdict.share_piece = grammar.share_piece or verdict.share_piece


# -- the grammar reading: the command as bash reads it -------------------------------------

# How deep the grammar reading follows quotes, substitutions and subshells inside each other.
# Far past anything a person writes and within Python's own recursion limit; deeper, the
# command is refused as nested too deep (`_read` catches _GrammarTooDeep).
_GRAMMAR_MAX_DEPTH = 64
# How far the grammar reading looks for the `))` that makes a `((` arithmetic: past it the text
# is read as subshells, the reading that keeps comments and heredocs. Bounded, so nested `((`
# stay linear.
_ARITH_SCAN = 4096
# A run of characters that are plain text in a word: no metacharacter, quote, backslash, `$`
# or backtick. Read in one step each, so a long word costs one match, not one loop per character.
_GRAMMAR_PLAIN_RE = re.compile(r"[^ \t\n;&|()<>'\"\\$`]+")
# Characters bash keeps in a word that shlex would cut it at.
_GRAMMAR_ODD_BLANK_RE = re.compile(r"[\r\x0b\x0c]")
_GRAMMAR_DQ_RE = re.compile(r"[^\"\\$`]+")
_GRAMMAR_BRACE_RE = re.compile(r"[^{}\[\]\\'\"$`<>();&| \t\n]+")
_GRAMMAR_BODY_RE = re.compile(r"[^\\$`]+")
_GRAMMAR_BLANKS_RE = re.compile(r"[ \t]+")
_GRAMMAR_DIGITS_RE = re.compile(r"[0-9]+")
# A redirection operator, with its descriptor number.
_GRAMMAR_REDIRECT_RE = re.compile(
    r"(?:[0-9]*|\{[A-Za-z_][A-Za-z0-9_]*\})(?:<<<|<<-|<<|<>|<&|>>|>&|>\||<|>)|&>>|&>")
# A redirection on a descriptor bash allocates and names (`{fd}>f`, `{x}<f`).
_NAMED_FD_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}(?=[<>])")
# `NAME[` where a command starts: an assignment with a subscript, read up to its `]`.
_NAME_SUBSCRIPT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[")
# What a subscript holds that the reading above would cut a word at, written quoted.
_GRAMMAR_SUBSCRIPT_QUOTE_RE = re.compile(r"[ \t\n;&|()<>]")
# Reserved words that start a compound command: after `coproc` such a word is the command,
# never the coprocess's NAME (`coproc for ((...)) do cmd; done`).
_COMPOUND_WORDS = frozenset({"for", "select", "while", "until", "if", "case", "function", "{",
                             "[[", "time", "!"})
# Where bash ends a word.
_GRAMMAR_BREAKS = frozenset(" \t\n;&|()<>")
# A word that `NAME ()` makes a function's name.
_GRAMMAR_FUNCTION_RE = re.compile(r"^[^\s;&|()<>'\"\\$`=]+$")
_GRAMMAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `${NAME:` and no `-`, `=`, `?` or `+` after the colon: a substring, whose offset and length
# bash evaluates as arithmetic.
_SUBSTRING_RE = re.compile(
    r"!?(?:[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?|[0-9]+|[@*#?$!-]):(?![-=?+])")
# The left side of an array assignment that a `(` right after it opens (`arr=(`, `a[1]+=(`).
_GRAMMAR_ARRAY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=$")
# A heredoc delimiter the current reading takes exactly as bash does; any other is renamed.
_GRAMMAR_PLAIN_DELIMITER_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*|'[A-Za-z_][A-Za-z0-9_]*'|\"[A-Za-z_][A-Za-z0-9_]*\")$")
# The name a renamed heredoc delimiter gets, with a number in base 36 (`_new_name`): short, so
# the grammar text stays near the command's length, and never a line of the text, since a
# body line equal to it would end that body early and let a later heredoc swallow commands.
_GRAMMAR_DELIMITER = "H"
# The reserved words that open a compound command, with the frame they open, and the closers.
_GRAMMAR_OPENERS = {"{": "{", "if": "if", "while": "loop", "until": "loop", "for": "loop",
                    "select": "loop", "case": "case"}
_GRAMMAR_CLOSERS = {"}": "{", "fi": "if", "done": "loop", "esac": "case"}
# `$'...'` escapes with a fixed meaning (bash's ANSI-C quoting).
_ANSI_C_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                   "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _reader_contexts(text: str) -> tuple:
    """(still open, lowest) for `text` as the reading above counts it: the quotes (`'`, `"`, a
    backtick) and parentheses (`(` for a plain one, a `$(`, also one inside double quotes) open
    at its end, innermost last, and how far a `)` with nothing to close went below zero. A
    backslash escapes the next character outside single quotes."""
    stack: List[str] = []
    lowest = depth = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        top = stack[-1] if stack else ""
        if top == "'":
            if c == "'":
                stack.pop()
        elif c == "\\":
            i += 1
        elif top == '"':
            if c == '"':
                stack.pop()
            elif c == "$" and text.startswith("(", i + 1):
                stack.append("(")
                i += 1
            elif c == "`":
                stack.append("`")
        elif c == "`" and top == "`":
            stack.pop()
        elif c in ("'", '"', "`"):
            stack.append(c)
        elif c == "(":
            stack.append("(")
        elif c == ")":
            if top == "(":
                stack.pop()
            elif not stack:
                depth -= 1
                lowest = min(lowest, depth)
        i += 1
    return stack, lowest


class _GrammarTooDeep(Exception):
    """A command nested deeper than _GRAMMAR_MAX_DEPTH: refused as unreadable."""


class _GrammarBrokenArray(Exception):
    """An array assignment holding an operator, a redirection or a `(`: refused as unreadable
    (shape "broken_array"), since bash resumes at a line the reading cannot place."""


class _Frame:
    """A compound command the grammar reading has opened: its kind, where its opener stands
    in the output (a list or a subshell is decided when it closes), and whether a pipe feeds
    it. A `case` frame also knows whether a pattern or a command comes next."""
    __slots__ = ("kind", "at", "piped", "state")

    def __init__(self, kind: str, at: int, piped: bool) -> None:
        self.kind, self.at, self.piped, self.state = kind, at, piped, "item"


class _Level:
    """One command list being written (the text, a `$( )`, a subshell): its output, its open
    compounds, the simple command being read (words and redirections apart, so redirections
    can move behind the words), and where the next word stands."""
    __slots__ = ("out", "frames", "simple", "blanks", "command", "last", "moved", "redirected",
                 "test", "operators")

    def __init__(self) -> None:
        self.out: list = []
        self.frames: List[_Frame] = []
        self.simple: list = []          # (whitespace, text or holder, is a redirection)
        self.blanks: List[str] = []
        self.command = True             # the next word stands where a command starts
        self.last = "seq"               # "pipe" right after `|` or `|&`
        self.moved = False              # a word came after a redirection
        self.redirected = False
        self.test = -1                  # inside `[[ ]]`: the depth of its parentheses
        self.operators = False          # an operator, a redirection or a `(` at this level


def _grammar_text(text: str) -> str:
    """The command as bash reads it, written for the current reading (`_read`): one pass,
    linear in the text. Read by `_join_the_grammar_reading`; the text as written is read too.

    - Comments are gone (a `#` where a word starts, to the end of the line; a backslash at
      its end joins nothing). A line continuation is joined, outside single quotes and a quoted
      heredoc's body.
    - Every heredoc whose delimiter line exists ends where bash ends it: any delimiter word
      (`.`, `1`, `$X`, `$'EOF'`, `"E"OF`, `E\\OF`; an unquoted body's lines joined at a line
      continuation first), renamed where the current reading would not take it. The body is
      data, an unquoted one with its substitutions written the same way.
    - `$'...'` is decoded into one single-quoted word, `$"..."` is `"..."`.
    - Reserved words count only where a command starts. `!`, `time [-p]`, `coproc [NAME]`,
      `function NAME`, `NAME ()` are dropped (a function's body is read where it stands). A
      compound command becomes a list: `then`, `elif`, `else`, `do`, `;;` and the closers are
      separators; `for NAME in WORDS` and `case WORD in` become `: WORDS`, each case pattern
      `: PATTERNS`, and `for ((...))` `: $((...))`. A compound in a pipeline or with a
      redirection after its closer becomes a subshell instead, so it reads the pipe as a
      subshell does. `[[ ... ]]` stays as written.
    - Redirections move behind the last word of their simple command, in their order
      (`>f mkfs x` is `mkfs x >f`); bash applies them the same wherever they stand.
    """
    return _GrammarText(text).run()


class _GrammarText:
    """The writer behind `_grammar_text`."""

    def __init__(self, text: str, depth: int = 0, names: Optional[list] = None) -> None:
        if depth > _GRAMMAR_MAX_DEPTH:
            raise _GrammarTooDeep()
        self.s, self.n, self.i, self.depth = text, len(text), 0, depth
        # Shared with the writers of a heredoc body or a backtick text: the next number, and the
        # top-level text whose lines no new name may equal (built on first use).
        self.names = names if names is not None else [0, None, text]
        # Heredocs waiting for the next newline, by the depth of the list that wrote them (a
        # newline takes those of its own list and of lists inside it, a lookup per depth).
        self.pending: dict = {}
        self.seq = 0
        self.comsub = 0                     # inside a `$( )`, `<( )` or `${ ; }`
        self.indexes: dict = {}
        self.closed = False                 # whether the last nested list found its `)`
        self.operators = False              # whether it held an operator at its own level
        self.array_level = -1               # the depth of the array elements being read
        # extglob patterns (`@(a|b)`) are words only once the text turns extglob on.
        self.extglob = "extglob" in self.names[2]
        # Inside `((...))`, `$((...))` or `for ((...))`: bash evaluates it, so a `<<` there
        # is a shift, not a heredoc, and a `#` is no comment.
        self.arith = 0

    def _new_name(self) -> str:
        """A delimiter name for a renamed heredoc that is no line of the text: not as written,
        not with its line continuations joined, its backslashes taken out (a backtick text is
        read unescaped), its leading tabs (`<<-`) or a trailing carriage return dropped."""
        if self.names[1] is None:
            text = self.names[2]
            taken = set()
            for spelling in (text, text.replace("\\\n", ""), text.replace("\\", "")):
                for line in spelling.split("\n"):
                    line = line.lstrip("\t")
                    taken.add(line)
                    taken.add(line.rstrip("\r"))
            self.names[1] = taken
        while True:
            number, k = "", self.names[0]
            self.names[0] += 1
            while True:
                k, digit = divmod(k, 36)
                number = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + number
                if not k:
                    break
            name = _GRAMMAR_DELIMITER + number
            if name not in self.names[1]:
                return name

    def run(self) -> str:
        return self._list("")

    def body(self, subs: Optional[List[str]] = None, escape_heredocs: bool = False) -> str:
        """An unquoted heredoc body: text, its substitutions written like the command's (and
        each command substitution also added to `subs`); `escape_heredocs`: a `<<` in the
        body's own text is written escaped (_bodies)."""
        s, n, parts = self.s, self.n, []
        while self.i < n:
            m = _GRAMMAR_BODY_RE.match(s, self.i)
            if m:
                text = m.group()
                parts.append(text.replace("<<", "\\<\\<") if escape_heredocs else text)
                self.i = m.end()
                continue
            ch = s[self.i]
            if ch == "\\":
                parts.append(s[self.i:self.i + 2])
                self.i += 2
            elif ch == "$":
                parts.append(self._dollar(True))
                if subs is not None and ("$(" in parts[-1] or "`" in parts[-1]):
                    # Written for the body, where it stands as in double quotes (also one
                    # inside `${...}`: `${u:-$(cmd)}`).
                    subs.append('"' + parts[-1] + '"')
            else:
                parts.append(self._backtick(False))
                if subs is not None:
                    subs.append(parts[-1])
        return "".join(parts)

    # -- nesting ---------------------------------------------------------------------------

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > _GRAMMAR_MAX_DEPTH:
            raise _GrammarTooDeep()

    def _leave(self) -> None:
        self.depth -= 1

    # -- the command list ------------------------------------------------------------------

    def _list(self, closer: str) -> str:
        """A command list up to `closer` (`)`, or the end of the text), with its `)` consumed;
        `self.closed` says whether it was there."""
        self._enter()
        s, n = self.s, self.n
        lv = _Level()
        closed = False
        while self.i < n:
            at = self.i
            ch = s[at]
            top = lv.frames[-1] if lv.frames else None
            if top is not None and top.kind == "case" and top.state == "pattern":
                self._patterns(lv, top)
                if self.i == at and top.state == "pattern":
                    top.state = "item"
                continue
            if ch == " " or ch == "\t":
                m = _GRAMMAR_BLANKS_RE.match(s, at)
                lv.blanks.append(m.group())
                self.i = m.end()
                continue
            if ch == "\\" and s.startswith("\n", at + 1):
                self.i += 2                     # a line continuation between words
                continue
            if ch == "\n":
                if self.depth == self.array_level and not self.pending:
                    # Between an array's parentheses a newline separates elements like a blank
                    # (`m=(` NL `[a]=1` NL `)`): no command starts on the next line.
                    lv.blanks.append(" ")
                    self.i += 1
                    continue
                if lv.test >= 0 or (lv.command and lv.last == "pipe" and not any(
                        depth >= self.depth for depth in self.pending)):
                    # Inside `[[ ]]`, and right after a `|` (the pipeline goes on in the next
                    # line), a newline is a blank.
                    lv.blanks.append(" " if lv.test < 0 else "\n")
                    self.i += 1
                    continue
                self.i += 1
                self._separator(lv, "\n", "seq")
                self._bodies(lv.out)
                continue
            if ch == "#" and not self.arith:    # a word starts here: a comment
                end = s.find("\n", at)
                self.i = n if end < 0 else end
                continue
            if closer == "}" and ch == "}" and lv.command and lv.test < 0 and (
                    top is None or top.kind != "{"):
                # The end of `${ cmd; }`: bash closes it at a `}` where a command starts,
                # whatever follows (`}x`, `}` and a carriage return), unlike a group's `}`.
                # This and the newline before a heredoc's delimiter line (_bodies) overlap:
                # either alone keeps `${| ...; }` CR-ended in a body readable; both are kept.
                self.i = at + 1
                closed = True
                break
            if lv.test >= 0 and ch in "()<>&|":
                if ch == "(":
                    lv.test += 1
                elif ch == ")" and lv.test > 0:
                    lv.test -= 1
                elif ch == ")":
                    lv.test = -1                # an unclosed `[[` ends with its list
                if lv.test >= 0:
                    self._add(lv, ch, False)
                    self.i += 1
                    continue
            if ch == ")" and closer == ")":
                self.i += 1
                closed = True
                break
            if ch == "(":
                lv.operators = True
                self.i += 1
                saved = self.arith
                if self._logical(self.i, 1)[0] == "(" and self._arith_closes(self.i + 1):
                    self.arith += 1             # `((`: an arithmetic command
                arithmetic = self.arith > saved
                inner = self._list(")")
                self.arith = saved
                # Two subshells (`((cmd); cmd)`, no `))` where they balance) are written `( (`,
                # so the reading above does not take them for arithmetic.
                gap = " " if not arithmetic and inner.startswith("(") else ""
                self._add(lv, "(" + gap + inner + (")" if self.closed else ""), False)
                continue
            if ch == ")":                       # nothing to close: bash refuses the line
                self.i += 1
                self._add(lv, ")", False)
                continue
            if ch in ";&|":
                # The operator as bash reads it, a line continuation inside joined.
                head, ends = self._logical(at, 3)
                lv.operators = True
            if ch == ";":
                length = 3 if head.startswith(";;&") else (
                    2 if head.startswith(";;") or head.startswith(";&") else 1)
                self.i = ends[length - 1]
                in_case = top is not None and top.kind == "case"
                self._separator(lv, ";" if in_case else head[:length], "seq")
                if length > 1 and in_case:
                    top.state = "pattern"
                continue
            if ch == "&" and not head.startswith("&>"):
                length = 2 if head.startswith("&&") else 1
                self.i = ends[length - 1]
                self._separator(lv, head[:length], "seq")
                continue
            if ch == "|":
                length = 2 if head.startswith("||") or head.startswith("|&") else 1
                self.i = ends[length - 1]
                self._separator(lv, head[:length], "seq" if head.startswith("||") else "pipe")
                continue
            if self._redirection_at(at):
                lv.operators = True
                self._redirection(lv)
                continue
            if lv.command and lv.test < 0 and self._reserved(lv):
                continue
            if lv.test < 0 and _NAME_SUBSCRIPT_RE.match(s, at) and all(
                    _ASSIGNMENT_WORD_RE.match(text) for _b, text, redir in lv.simple
                    if not redir and isinstance(text, str)):
                # An assignment where a command starts reads its subscript up to the `]`,
                # blanks, operators and newlines included (`a[x;y]=1 cmd`): written quoted.
                name_end = s.index("[", at)
                end = self._group_end(name_end, "[", "]")
                if (s.startswith("=", end) or s.startswith("+=", end)) and not any(
                        q in s[name_end:end] for q in "'\"\\$`"):
                    inner = _GRAMMAR_SUBSCRIPT_QUOTE_RE.sub(lambda m: "'" + m.group() + "'",
                                                           s[name_end + 1:end - 1])
                    self.i = end
                    word = s[at:name_end + 1] + inner + "]" + self._word()
                    self._add(lv, word, False)
                    continue
            word = self._word()
            if self.i == at:                    # nothing a word holds: keep the character
                word = ch
                self.i += 1
            self._add(lv, word, False)
            if lv.test >= 0 and word == "]]":
                lv.test = -1
        self._flush(lv)
        lv.out.append("".join(lv.blanks))
        if self.i >= n:
            # A heredoc on the last line has an empty body; its delimiter is renamed all the
            # same, since the current reading may misread it (`<<$'it\'s'`).
            for entries in self.pending.values():
                for _seq, _delim, quoted, _tabs, holder, prefix, plain, _sub, fixed in entries:
                    if not plain and fixed is None:
                        name = self._new_name()
                        holder[0] = prefix + ("'%s'" % name if quoted else name)
            self.pending = {}
        else:
            # A heredoc written in this list (or one inside it) whose body comes after it ends
            # (`( cat <<. )` NL ...): the list is joined into text now, so its delimiter is
            # renamed now; the body's line later gets that name.
            for depth, entries in self.pending.items():
                if depth < self.depth:
                    continue
                for k, entry in enumerate(entries):
                    if entry[8] is None:
                        name = self._new_name()
                        entry[4][0] = entry[5] + ("'%s'" % name if entry[2] else name)
                        entries[k] = entry[:8] + (name,)
        self._leave()
        self.closed = closed
        self.operators = lv.operators
        return "".join(p if isinstance(p, str) else p[0] for p in lv.out)

    def _add(self, lv: _Level, text, redirection: bool) -> None:
        blanks = "".join(lv.blanks)
        if redirection and not blanks and lv.simple:
            blanks = " "                    # `rm -rf ~>/dev/null`: bash ends the word at `>`
        lv.simple.append((blanks, text, redirection))
        lv.blanks = []
        if redirection:
            lv.redirected = True
        elif lv.redirected:
            lv.moved = True
        lv.command = False

    def _flush(self, lv: _Level) -> None:
        """Write out the simple command: as written, or its words first and then its
        redirections, in their order, when a word came after a redirection."""
        if lv.simple:
            entries = lv.simple
            if lv.moved:
                entries = [e for e in entries if not e[2]] + [e for e in entries if e[2]]
            for blanks, text, redirection in entries:
                lv.out.append(blanks or (" " if redirection and lv.moved else ""))
                lv.out.append(text)
        lv.simple = []
        lv.moved = lv.redirected = False

    def _separator(self, lv: _Level, text: str, last: str) -> None:
        self._flush(lv)
        lv.out.append("".join(lv.blanks))
        lv.blanks = []
        lv.out.append(text)
        lv.command, lv.last = True, last
        lv.test = -1

    def _blanks(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            if s[self.i] in " \t":
                self.i += 1
            elif s[self.i] == "\\" and s.startswith("\n", self.i + 1):
                self.i += 2
            else:
                return

    def _blanks_from(self, at: int) -> int:
        save, self.i = self.i, at
        self._blanks()
        end, self.i = self.i, save
        return end

    def _candidate(self, at: int) -> tuple:
        """(word, end) of a plain, unquoted word at `at` that a reserved word could be, its
        line continuations joined (`t\\` NL `hen` is `then`); ("", at) otherwise."""
        s, n = self.s, self.n
        pieces: List[str] = []
        end = at
        while True:
            m = _GRAMMAR_PLAIN_RE.match(s, end)
            if not m:
                break
            pieces.append(m.group())
            end = m.end()
            while s.startswith("\\\n", end):
                end += 2
            if end >= n or s[end] in _GRAMMAR_BREAKS:
                break
        if not pieces or (end < n and s[end] not in _GRAMMAR_BREAKS):
            return "", at
        return "".join(pieces), end

    # -- reserved words --------------------------------------------------------------------

    def _reserved(self, lv: _Level) -> bool:
        """Handle a reserved word or a function head where a command starts; True when one
        was consumed."""
        s = self.s
        word, end = self._candidate(self.i)
        if not word:
            return False
        top = lv.frames[-1] if lv.frames else None
        if word in ("!", "time"):
            self.i = end
            if word == "time":
                flag, flag_end = self._candidate(self._blanks_from(end))
                if flag == "-p":
                    self.i = flag_end
            return True
        if word == "coproc":
            # Its NAME, when a compound follows, read after quote removal (`coproc \X {`).
            self.i = self._blanks_from(end)
            name = self._word()
            after = self._blanks_from(self.i)
            if not (name and _GRAMMAR_NAME_RE.match(name) and name not in _COMPOUND_WORDS and (
                    s.startswith("(", after) or self._candidate(after)[0] == "{")):
                self.i = end
            return True
        if word == "function":
            name, name_end = self._candidate(self._blanks_from(end))
            self.i = name_end if name else end
            paren = self._blanks_from(self.i)
            if s.startswith("(", paren) and s.startswith(")", self._blanks_from(paren + 1)):
                self.i = self._blanks_from(paren + 1) + 1
            return True
        if word in _GRAMMAR_OPENERS:
            self._flush(lv)
            lv.out.append("".join(lv.blanks))
            lv.blanks = []
            frame = _Frame(_GRAMMAR_OPENERS[word], len(lv.out), lv.last == "pipe")
            lv.out.append(";")
            lv.frames.append(frame)
            self.i = end
            lv.command, lv.last = True, "seq"
            if word in ("for", "select"):
                self._loop_head(lv, word)
            elif word == "case":
                self._case_head(lv, frame)
            return True
        if word in ("then", "else", "elif", "do"):
            self.i = end
            self._separator(lv, ";", "seq")
            return True
        if word in _GRAMMAR_CLOSERS:
            if top is not None and top.kind == _GRAMMAR_CLOSERS[word]:
                self.i = end
                self._close(lv, top)
                return True
            if word == "}":
                return False                    # a word: nothing it closes
            self.i = end
            self._separator(lv, ";", "seq")
            return True
        if word == "[[":
            lv.test = 0
            return False
        if _GRAMMAR_FUNCTION_RE.match(word):
            paren = self._blanks_from(end)
            if s.startswith("(", paren):
                close = self._blanks_from(paren + 1)
                if s.startswith(")", close):
                    self.i = close + 1          # `NAME ()`: its body follows, read as it stands
                    return True
        return False

    def _close(self, lv: _Level, frame: _Frame) -> None:
        """Close a compound: a subshell when a pipe feeds it, or a pipe or a redirection
        follows its closer, so it reads and writes as one; a list otherwise."""
        self._flush(lv)
        lv.out.append("".join(lv.blanks))
        lv.blanks = []
        subshell = frame.piped or self._piped_or_redirected(self.i)
        lv.out[frame.at] = " ( " if subshell else ";"
        lv.out.append(" )" if subshell else ";")
        lv.frames.pop()
        lv.command = False

    def _piped_or_redirected(self, at: int) -> bool:
        s = self.s
        k = self._blanks_from(at)
        if s.startswith("|", k):
            return not s.startswith("||", k)
        return k < self.n and (s[k] in "<>" or s.startswith("&>", k) or self._redirection_at(k))

    def _loop_head(self, lv: _Level, word: str) -> None:
        """`for NAME in WORDS` and `select NAME in WORDS` as `: WORDS`; `for ((a; b; c))` as
        `: $((a; b; c))`."""
        self._blanks()
        # `for (\` NL `(`: bash joins the line continuation before it sees `((` here (unlike an
        # arithmetic command, whose `((` must stand together).
        head, ends = self._logical(self.i, 2)
        if word == "for" and head == "((":
            self.i = ends[1] - 1
            self.arith += 1
            inner = self._list(")")
            self.arith -= 1
            self._add(lv, ":", False)
            lv.blanks = [" "]
            self._add(lv, "$(" + inner + (")" if self.closed else ""), False)
            # `do` or `{` may follow `))` with no separator in between.
            lv.command = True
            return
        name, end = self._candidate(self.i)
        if name:
            self.i = end
        following, following_end = self._candidate(self._blanks_from(self.i))
        lv.blanks = [" "]
        self._add(lv, ":", False)
        if following == "in":
            self.i = following_end
        elif following == "do":
            self.i = following_end
            self._separator(lv, ";", "seq")

    def _case_head(self, lv: _Level, frame: _Frame) -> None:
        """`case WORD in` as `: WORD`; the patterns come next."""
        s, n = self.s, self.n
        self._blanks()
        lv.blanks = [" "]
        self._add(lv, ":", False)
        word = self._word()
        if word:
            lv.blanks = [" "]
            self._add(lv, word, False)
        self._flush(lv)
        k = self.i
        while k < n and (s[k] in " \t\n" or (s[k] == "\\" and s.startswith("\n", k + 1))):
            k += 2 if s[k] == "\\" else 1
        following, end = self._candidate(k)
        if following == "in":
            self.i = end
            frame.state = "pattern"
            lv.out.append(";")
        lv.command = True

    def _patterns(self, lv: _Level, frame: _Frame) -> None:
        """A case item's patterns, `[(]PAT[|PAT...])`, as `: PATTERNS` (their substitutions
        run when bash compares them), or `esac`."""
        s, n = self.s, self.n
        while self.i < n:
            ch = s[self.i]
            if ch in " \t\n":
                self.i += 1
            elif ch == "\\" and s.startswith("\n", self.i + 1):
                self.i += 2
            elif ch == "#":
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end
            else:
                break
        if self.i >= n:
            return
        word, end = self._candidate(self.i)
        if word == "esac":
            self.i = end
            self._close(lv, frame)
            return
        if s[self.i] == "(":
            self.i += 1
        patterns = []
        while self.i < n:
            self._blanks()
            start = self.i
            pattern = self._word()
            if pattern:
                patterns.append(pattern)
            self._blanks()
            if s.startswith("|", self.i) and self.i > start:
                self.i += 1
                continue
            if s.startswith(")", self.i):
                self.i += 1
            break
        lv.out.append(" : " + " ".join(patterns) + ";")
        frame.state = "item"
        lv.command, lv.last = True, "seq"

    # -- redirections and heredocs ---------------------------------------------------------

    def _redirection_at(self, at: int) -> bool:
        s = self.s
        ch = s[at]
        if ch in "<>":
            # `<(` is a process substitution, also across a line continuation.
            return self._logical(at, 2)[0][1:] != "("
        if ch == "&":
            return self._logical(at, 2)[0] == "&>"
        if ch in "0123456789":
            head = self._logical(at, 24)[0]     # `2\` NL `>&1` is `2>&1` for bash
            end = _GRAMMAR_DIGITS_RE.match(head).end()
            return head[end:end + 1] in ("<", ">") and head[end + 1:end + 2] != "("
        if ch == "{":
            # `{fd}>f rm -rf /`, also with a line continuation inside (`{fd\` NL `}>f`).
            return bool(_NAMED_FD_RE.match(self._logical(at, 80)[0]))
        return False

    def _redirection(self, lv: _Level) -> None:
        s = self.s
        m = _GRAMMAR_REDIRECT_RE.match(s, self.i)
        if m and "\\" not in s[self.i:m.end() + 1]:
            op, self.i = m.group(), m.end()     # no line continuation inside the operator
        else:
            head, ends = self._logical(self.i, 80)
            op = _GRAMMAR_REDIRECT_RE.match(head).group()
            self.i = ends[len(op) - 1]
        if op.endswith(">|"):
            op = op[:-1]                        # `>|` writes like `>`; its `|` is no pipe
        gap_start = self.i
        self._blanks()
        gap = s[gap_start:self.i].replace("\\\n", "")
        if op.lstrip("0123456789") in ("<<", "<<-") and not self.arith:
            holder = self._heredoc(op + gap)
            self._add(lv, holder if holder is not None else op + gap, True)
            return
        self._add(lv, op + gap + self._word(), True)

    def _heredoc(self, prefix: str) -> Optional[list]:
        """The delimiter word after `<<`/`<<-`: noted for the next newline, which starts the
        body; returned as a holder so a delimiter the current reading would not take can be
        renamed once its line is found."""
        s, n = self.s, self.n
        strip_tabs = prefix.rstrip(" \t").endswith("-")
        start = j = self.i
        parts: List[str] = []
        quoted = False
        while j < n and s[j] not in _GRAMMAR_BREAKS:
            ch = s[j]
            if ch == "\\":
                if s.startswith("\n", j + 1):
                    j += 2
                    continue
                parts.append(s[j + 1:j + 2])
                quoted, j = True, j + 2
                continue
            head, ends = self._logical(j, 2) if ch == "$" else ("", [])
            if head[1:2] == "(" or ch == "`":
                # `<<a$(b)c`, `<<.\`b\``: the substitution is part of the word, taken
                # literally (a quote inside it is no quote of the word).
                end = _substitution_end(s, j)
                parts.append(s[j:end])
                j = end
                continue
            if head[1:2] == "{":
                # `<<.${a b}`: a parameter expansion is part of the word up to its `}`, blanks
                # and operators included, taken literally.
                end = _brace_group_end(s, ends[1])
                parts.append(s[j:end])
                j = end
                continue
            if head[1:2] in ("'", '"') and head[1:2]:
                if head[1] == "'":
                    decoded = self._ansi_c(ends[1])
                    parts.append(decoded[1:-1].replace("'\\''", "'"))
                    quoted, j = True, self.i
                    continue
                j = ends[1] - 1                 # `$"..."`: the double quotes follow
                continue
            if ch == "'":
                close = s.find(ch, j + 1)
                close = n if close < 0 else close
                parts.append(s[j + 1:close])
                quoted, j = True, close + 1
                continue
            if ch == '"':
                # Double quotes: a line continuation is joined, `\"`, `\\`, `\$` and a
                # backslashed backtick lose their backslash.
                j += 1
                while j < n and s[j] != '"':
                    if s[j] == "\\" and j + 1 < n:
                        if s[j + 1] != "\n":
                            parts.append(s[j + 1] if s[j + 1] in '"\\$`' else s[j:j + 2])
                        j += 2
                        continue
                    parts.append(s[j])
                    j += 1
                quoted, j = True, j + 1
                continue
            parts.append(ch)
            j += 1
        self.i = min(j, n)
        raw = s[start:self.i]
        if not raw:
            return None
        holder = [prefix + raw]
        plain = bool(_GRAMMAR_PLAIN_DELIMITER_RE.match(raw))
        # Noted with the list it belongs to: a newline inside a `$(` on the same line does not
        # start its body (`cat <<EOF $(echo` NL `)`: the body follows the outer line).
        self.seq += 1                           # the bodies follow in the heredocs' order
        self.pending.setdefault(self.depth, []).append(
            (self.seq, "".join(parts), quoted, strip_tabs, holder, prefix, plain,
             self.comsub > 0, None))
        return holder

    def _prefix_line(self, delim: str, pos: int, strip_tabs: bool,
                     stop: int) -> Optional[tuple]:
        """For a heredoc written inside `$( )`, bash ends the body at the first line that STARTS
        with the delimiter and holds a `)` after it, and reads the rest of that line as commands
        (`EOF)`, `EOF) ; echo`, `EOFx)` runs `x`, `EOFx` alone is body; an empty delimiter
        `<<''` starts every line, so its body ends at the first line with a `)`; measured with
        bash 5.3): (start of that line, end of the delimiter in it). One pass from `pos` up to
        `stop` (the body's own delimiter line), so every line is scanned for one body only; the
        caller consumes what it passed over."""
        s, p = self.s, pos
        while p <= len(s) and p < stop:
            e = s.find("\n", p)
            e = len(s) if e < 0 else e
            q = p
            while strip_tabs and q < e and s[q] == "\t":
                q += 1
            if s.startswith(delim, q) and q + len(delim) <= e and ")" in s[q + len(delim):e]:
                return p, q + len(delim)
            p = e + 1
        return None

    def _bodies(self, out: list) -> None:
        """The bodies of the heredocs of the line just ended (those of this list, or of lists
        inside it that closed on this line), each up to its delimiter line. Without one bash
        reads the rest of the text as the body (and warns): written so, with a delimiter line
        added at the end, since an unquoted body runs its substitutions."""
        depths = [depth for depth in self.pending if depth >= self.depth]
        if not depths:
            return
        s, n = self.s, self.n
        pending = sorted(entry + (depth,) for depth in depths for entry in self.pending.pop(depth))
        for _seq, delim, quoted, strip_tabs, holder, prefix, plain, in_comsub, fixed, at in pending:
            if self.i >= n and out and not str(out[-1]).endswith("\n"):
                out.append("\n")                # the next delimiter line goes on a line of its own
            joined = not quoted
            index = self.indexes.get(joined)
            if index is None:
                index = self.indexes[joined] = _HeredocLines(s, joined=joined)
            span = index.after(delim, self.i, strip_tabs)
            early = self._prefix_line(delim, self.i, strip_tabs,
                                      n + 1 if span is None else span[0]) if in_comsub else None
            if early is not None and (span is None or early[0] < span[0]):
                span, resume = (early[0], early[0]), early[1]     # `EOF)`: `)` reads on
            else:
                resume = -1
            unterminated = span is None
            start, end = (n, n) if unterminated else span
            body = s[self.i:start]
            line = s[start:end]
            if unterminated or resume >= 0:
                plain = False
                if body and not body.endswith("\n"):
                    body += "\n"
            if fixed is not None:
                line = fixed
            elif not plain or "\n" in line:
                name = self._new_name()
                holder[0] = prefix + ("'%s'" % name if quoted else name)
                line = name
            # Only a substitution in an unquoted body is written anew; text without one is kept.
            plain_body = quoted or ("$" not in body and "`" not in body)
            # Inside `$( )` or a subshell (where the body is read, or where its heredoc stands:
            # `( cat <<. )` NL ...) the reading above counts quotes and parentheses across the
            # body, or reads its lines as commands.
            nested = self.depth > 1 or at > 1
            # A `<<` in a body read at the top of the text (a subshell's heredoc, where that
            # reading opens heredocs) could open one of its own that swallows the lines after
            # the body (`( cat <<. )` NL `<<EOF` ...): written escaped in the body's own text, so
            # that reading reads those lines (the frozen reading reads each body line anyway); a
            # substitution in the body keeps its text. Inside `$( )` or backticks the body is
            # the script a shell reads, kept as written.
            escape = nested and self.depth == 1
            subs: List[str] = []
            if plain_body:
                written = body.replace("<<", "\\<\\<") if escape else body
            else:
                writer = _GrammarText(body, self.depth + 1, self.names)
                written = writer.body(subs, escape)
            if nested and subs:
                # An unquoted body's substitutions run whatever its quotes: written once more
                # on a line of their own at its start, before any apostrophe of the body can
                # hide them from that reading (`don't $(cmd)`, `it's \`cmd\``).
                written = ": " + " ".join(subs) + "\n" + written
            if written and not written.endswith("\n"):
                # The delimiter on a line of its own, also after a body a nested heredoc left
                # unended; overlaps with the `${ cmd; }` close in _list (either alone suffices
                # for the forms measured, both are kept).
                written += "\n"
            if nested:
                # What the body leaves open for that reading is closed at its end: a quote, a
                # parenthesis (also a `$(` inside double quotes, which that reading counts), on
                # a comment line of its own, which that reading takes as text and bash and the
                # grammar reading of a script fed by the body skip (the body's own last line
                # stays as written: `bash -c 'cmd'`). A `)` too many (a case pattern's) is
                # balanced by a comment line in front.
                still_open, lowest = _reader_contexts(written)
                if lowest < 0:
                    written = "#" + "(" * -lowest + "\n" + written
                if still_open:
                    written += "#" + "".join(")" if c == "(" else c
                                             for c in reversed(still_open)) + "\n"
            out.append(written)
            out.append(line)
            if resume >= 0:
                out.append("\n")
                self.i = resume
                return
            if end < n:
                out.append("\n")
                self.i = end + 1
            else:
                self.i = end
            if unterminated:
                return                          # the later heredocs of the line get nothing

    # -- words -----------------------------------------------------------------------------

    def _group_end(self, at: int, opener: str, closer: str) -> int:
        """Where the bracket group opened at `at` closes (after its closer; the end of the text
        when it does not): nested ones counted, quoted spans and escaped characters skipped."""
        s, n = self.s, self.n
        depth, k = 0, at
        while k < n:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c in ("'", '"'):
                close = s.find(c, k + 1)
                k = n if close < 0 else close + 1
                continue
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return k + 1
            k += 1
        return n

    def _word(self) -> str:
        s, n = self.s, self.n
        parts: List[str] = []
        if self.depth == self.array_level and s.startswith("[", self.i):
            # An element's subscript (`m=([a;b]=1)`): bash reads it up to its `]`, operators
            # and blanks in it included.
            end = self._group_end(self.i, "[", "]")
            parts.append(s[self.i:end])
            self.i = end
        while self.i < n:
            m = _GRAMMAR_PLAIN_RE.match(s, self.i)
            if m:
                # A carriage return, vertical tab or form feed is part of the word for bash
                # (`X=` CR `1 cmd` assigns and runs cmd); shlex takes it for a blank, so it is
                # written quoted.
                text = m.group()
                parts.append(_GRAMMAR_ODD_BLANK_RE.sub(lambda c: "'" + c.group() + "'", text)
                             if "\r" in text or "\x0b" in text or "\x0c" in text else text)
                self.i = m.end()
                continue
            ch = s[self.i]
            if ch == "\\":
                if s.startswith("\n", self.i + 1) or self.i + 1 == n:
                    # A line continuation inside the word; at the very end of the text, the
                    # newline the check's strip() took off.
                    self.i += 2
                    continue
                escaped = s[self.i + 1]
                # Quote removal of a letter or digit: `r\m` is `rm`. An escaped blank is written
                # quoted (`' '`), so no strip() of a segment takes it off; every other escaped
                # character keeps its backslash.
                if escaped in " \t":
                    parts.append("'" + escaped + "'")
                else:
                    parts.append(escaped if escaped.isascii() and escaped.isalnum()
                                 else s[self.i:self.i + 2])
                self.i += 2
            elif ch == "'" and self.arith:
                # Arithmetic reads its text as if in double quotes: a `'` is no quote there, and
                # a substitution between two runs (`(( '$(cmd)' ))`). Left out, so none hides it;
                # but bash finds where the arithmetic ends with such a span skipped whole
                # (`for (( i+'+ )); ... ' ))` ends after the span), so its text goes on.
                # Its text is read as in double quotes: a substitution in it is written as one.
                end = s.find("'", self.i + 1)
                self.i += 1
                while 0 <= end and self.i < end:
                    if s[self.i] == "$":
                        parts.append(self._dollar(True))
                    elif s[self.i] == "`":
                        parts.append(self._backtick(True))
                    else:
                        parts.append(s[self.i])
                        self.i += 1
                    if self.i > end:            # a substitution ran past it: the next `'` ends
                        end = s.find("'", self.i)
                if 0 <= end and self.i == end:
                    self.i += 1
            elif ch == "'":
                end = s.find("'", self.i + 1)
                end = n if end < 0 else end + 1
                parts.append(s[self.i:end])
                self.i = end
            elif ch == '"':
                parts.append(self._dq())
            elif ch == "$":
                parts.append(self._dollar(False))
            elif ch == "`":
                parts.append(self._backtick(False))
            elif ch in "<>" and self._logical(self.i, 2)[0][1:] == "(":
                # A process substitution, part of the word (`<\` NL `(` too: bash joins it).
                self.i = self._logical(self.i, 2)[1][1]
                saved, self.arith = self.arith, 0
                self.comsub += 1
                inner = self._list(")")
                self.comsub -= 1
                self.arith = saved
                if not (self.closed and not inner.strip()):
                    parts.append(ch + "(" + inner + (")" if self.closed else ""))
                # else `<()`: bash expands an empty one to nothing (`/<()` is `/`; measured).
            elif ch == "(" and parts and self.extglob and self.depth == self.array_level \
                    and parts[-1][-1:] in ("@", "!", "+", "*", "?"):
                end = self._group_end(self.i, "(", ")")   # `x=(@(a|b))` with extglob on
                parts.append(s[self.i:end])
                self.i = end
            elif ch == "(" and parts and _GRAMMAR_ARRAY_RE.match("".join(parts)):
                if self.depth == self.array_level and not self.arith:
                    raise _GrammarBrokenArray()   # `x=(a=(b))`: no array inside an array
                self.i += 1                     # `arr=(...)`: its elements
                saved, self.arith = self.arith, 0
                saved_level, self.array_level = self.array_level, self.depth + 1
                inner = self._list(")")
                self.arith = saved
                self.array_level = saved_level
                if self.operators and not saved:
                    # An operator, a redirection or a `(` among the elements is a syntax error
                    # after which bash drops that line and reads on at the next one, outside
                    # whatever compound held it (`x=(a;` NL `cmd` runs cmd).
                    raise _GrammarBrokenArray()
                parts.append("(" + inner + (")" if self.closed else ""))
            else:
                break
        return "".join(parts)

    def _dq(self) -> str:
        self._enter()
        s, n = self.s, self.n
        parts = ['"']
        self.i += 1
        while self.i < n:
            m = _GRAMMAR_DQ_RE.match(s, self.i)
            if m:
                parts.append(m.group())
                self.i = m.end()
                continue
            ch = s[self.i]
            if ch == '"':
                parts.append('"')
                self.i += 1
                break
            if ch == "\\":
                if not s.startswith("\n", self.i + 1):
                    parts.append(s[self.i:self.i + 2])
                self.i += 2
            elif ch == "$":
                parts.append(self._dollar(True))
            else:
                parts.append(self._backtick(True))
        self._leave()
        return "".join(parts)

    def _arith_closes(self, at: int) -> bool:
        """Whether the `((` whose text starts at `at` closes with `))` where its parentheses
        balance: only then bash evaluates it; otherwise it reads two subshells (`((echo a) #
        it's` NL ...: the `#` is a comment). Quoted spans are skipped; the search stops after
        _ARITH_SCAN characters, past which the text counts as subshells."""
        s = self.s
        depth = 0
        k = at
        end = min(self.n, at + _ARITH_SCAN)
        while k < end:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c in ("'", '"'):
                close = s.find(c, k + 1, end)
                if close < 0:
                    return False
                k = close + 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return s.startswith(")", k + 1)
                depth -= 1
            k += 1
        return False

    def _logical(self, at: int, width: int) -> tuple:
        """The next `width` characters from `at` with line continuations taken out (bash joins
        them before it reads `$` + `'`, `<` + `<`), and where in the text each one ends."""
        s, n = self.s, self.n
        if "\\" not in s[at:at + width]:      # no continuation in reach: the text itself
            chunk = s[at:at + width]
            return chunk, list(range(at + 1, at + len(chunk) + 1))
        chars: List[str] = []
        ends: List[int] = []
        k = at
        while k < n and len(chars) < width:
            if s[k] == "\\" and s.startswith("\n", k + 1):
                k += 2
                continue
            chars.append(s[k])
            k += 1
            ends.append(k)
        return "".join(chars), ends

    def _dollar(self, in_dq: bool) -> str:
        """`$` and what it opens; a line continuation between them is joined first."""
        head, ends = self._logical(self.i, 3)
        nxt = head[1:2]
        if nxt == "$":
            self.i = ends[1]
            # The process id; a quote after it is a quote. Before a `(` it is written `${$}`,
            # so the readings above do not take `$(` for a substitution (`"$$("`).
            return "${$}" if self.s.startswith("(", self.i) else "$$"
        if nxt == "(" and head[2:3] == "(" and not self._arith_closes(ends[2]):
            # `$((` with no `))` where its parentheses balance: bash finds its end by those
            # parentheses first, then reads the text between as a command substitution of its
            # own, so a heredoc in it ends with that text (`$(( 1 << 2 )` NL `)`).
            closed: List[bool] = []
            end = _substitution_end(self.s, self.i, closed, ansi_c=True)
            inner_text = self.s[ends[1]:end - 1] if closed else self.s[ends[1]:]
            sub = _GrammarText(inner_text, self.depth + 1, self.names)
            sub.comsub = 1
            written = sub.run()
            self.i = end
            if closed and written and not written.endswith("\n"):
                written += "\n"                # a renamed delimiter line stays one of its own
            return "$(" + written + (")" if closed else "")
        if nxt == "(":
            saved = self.arith
            self.arith = saved + 1 if head[2:3] == "(" and self._arith_closes(ends[2]) else 0
            self.i = ends[1]
            self.comsub += 1
            inner = self._list(")")
            self.comsub -= 1
            self.arith = saved
            return "$(" + inner + (")" if self.closed else "")
        if nxt == "{" and head[2:3] in (" ", "\t", "\n", "|"):
            # `${ cmd; }` and `${| cmd; }` (bash 5.3): a command substitution run in this shell.
            saved = self.arith
            self.arith = 0
            self.i = ends[2] if head[2:3] == "|" else ends[1]
            self.comsub += 1
            inner = self._list("}")
            self.comsub -= 1
            self.arith = saved
            return "$(" + inner + (")" if self.closed else "")
        if nxt in ("{", "["):
            return self._enclosed(in_dq, nxt, ends[1])
        if not in_dq and nxt == "'":
            return self._ansi_c(ends[1])
        if not in_dq and nxt == '"':
            self.i = ends[1] - 1
            return self._dq()                   # `$"..."`: double quotes, translated
        self.i += 1
        return "$"

    def _enclosed(self, in_dq: bool, opener: str, start: int) -> str:
        """`${...}` or `$[...]` up to its closer (its text from `start`), its substitutions
        written like the rest."""
        self._enter()
        s, n = self.s, self.n
        closer = "}" if opener == "{" else "]"
        parts = ["$" + opener]
        # Text bash evaluates as arithmetic, where a `'` quotes nothing: `$[...]`, a substring's
        # offset and length (`${x:'$(cmd)'}`), and anything inside arithmetic.
        arith = bool(self.arith) or opener == "[" or bool(_SUBSTRING_RE.match(s, start))
        # Inside `((...))` or `$((...))` bash ends the arithmetic text by its parentheses before
        # it reads what is inside: an unclosed `$[` or `${` there ends at that text's `)`
        # (`for((;;$[))do cmd;done` runs cmd).
        parens = 0
        self.i = start
        depth = 0
        while self.i < n:
            m = _GRAMMAR_BRACE_RE.match(s, self.i)
            if m:
                parts.append(m.group())
                self.i = m.end()
                continue
            ch = s[self.i]
            if ch == closer and depth == 0:
                parts.append(ch)
                self.i += 1
                break
            if ch in " \t\n;&|" or (ch in "()" and not self.arith):
                # `${x#'/' }rm`, `${x#a;b}`: a blank or an operator character inside `${...}`
                # is part of the one word bash reads, written quoted so the reading above does
                # not cut the word there.
                parts.append("'" + ch + "'" if opener == "{" and not in_dq else ch)
                self.i += 1
                continue
            if ch in "()":
                if ch == ")" and parens == 0:
                    break                       # the arithmetic's own `)`, left to it
                parens += 1 if ch == "(" else -1
                parts.append(ch)
                self.i += 1
                continue
            if ch in "{[":
                # `$[ a[1] ]` counts its subscripts; `${x:-a{}` ends at the first `}`.
                depth += ch == opener == "["
                parts.append(ch)
                self.i += 1
            elif ch in "}]":
                depth -= ch == closer == "]"
                parts.append(ch)
                self.i += 1
            elif ch == "\\":
                if not s.startswith("\n", self.i + 1):
                    parts.append(s[self.i:self.i + 2])
                self.i += 2
            elif ch == "'" and not in_dq and not arith:
                end = s.find("'", self.i + 1)
                end = n if end < 0 else end + 1
                parts.append(s[self.i:end])
                self.i = end
            elif ch == "'" and in_dq and not arith and 0 <= s.find("'", self.i + 1) < (
                    s.find("}", self.i + 1) % (n + 1)):
                # `"${x#'"'}"`: inside double quotes bash still takes a single-quoted span in
                # the word for quoting, so a `"` in it ends nothing; written escaped as text.
                # Only a span that closes before the next `}`: a POSIX sh takes a lone `'` there
                # for text (`sh -c ': "${x:-'"'"'}"'`), where bash would refuse the line.
                end = s.find("'", self.i + 1)
                parts.append(re.sub(r'(["\\$`])', r"\\\1", s[self.i + 1:end]))
                self.i = end + 1
            elif ch == "'":
                self.i += 1                     # in `"${x#'a'}"`: no quote for the splitter
            elif ch == '"' and in_dq:
                # `"${x#"'"}"`: bash nests the quotes; the current reading would take the inner
                # `"` for the closing one, so they are left out, their substitutions kept.
                inner = self._dq()
                parts.append(inner[1:-1] if inner.endswith('"') and len(inner) > 1 else inner[1:])
            elif ch == '"':
                parts.append(self._dq())
            elif ch == "$":
                parts.append(self._dollar(in_dq))
            elif ch in "<>" and s.startswith("(", self.i + 1) and not in_dq and opener == "{":
                # `${x:=<(cmd)}`: the word of an expansion outside double quotes runs a process
                # substitution, written as one in a word.
                self.i += 2
                saved, self.arith = self.arith, 0
                self.comsub += 1
                inner = self._list(")")
                self.comsub -= 1
                self.arith = saved
                parts.append(ch + "(" + inner + (")" if self.closed else ""))
            elif ch in "<>":
                # Text of the word, quoted like a blank: the reading above takes no `<<<` in it.
                parts.append("'" + ch + "'" if opener == "{" and not in_dq else ch)
                self.i += 1
            else:
                parts.append(self._backtick(in_dq))
        self._leave()
        return "".join(parts)

    def _backtick(self, in_dq: bool) -> str:
        """A backtick pair: its text, unescaped as bash unescapes it, read as a command list of
        its own; written as `$(...)` when that changed anything, else as it stands."""
        s, n = self.s, self.n
        j = self.i + 1
        while j < n and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        j = min(j, n)
        raw = s[self.i:min(j + 1, n)]
        inner = s[self.i + 1:j]
        self.i = min(j + 1, n)
        if j >= n:
            return raw          # unclosed: bash refuses the line; left as the current reading has it
        special = "\\`$\"" if in_dq else "\\`$"
        # A backslash-newline goes too: bash joins the lines of a backtick text before it reads
        # it, also inside a comment (`#a \` NL `cmd` is one comment there; measured).
        unescaped = re.sub(r"\\([%s\n])" % re.escape(special),
                           lambda m: "" if m.group(1) == "\n" else m.group(1),
                           inner) if "\\" in inner else inner
        text = _GrammarText(unescaped, self.depth + 1, self.names).run()
        still_open = _reader_contexts(text)[0]
        if text == unescaped == inner and not still_open:
            return raw
        # Written as `$(...)`: the readings above take an escaped backtick inside a backtick
        # pair for a character, and a quote or a parenthesis the text leaves open (an
        # apostrophe, `(echo `, which bash refuses inside) would swallow the lines after it, so
        # it is closed at its end. A blank keeps a leading `(` from making it `$((`, and a
        # heredoc's delimiter line that may end the text gets the `)` on a line of its own.
        text += "".join(")" if c == "(" else c for c in reversed(still_open))
        return "$(" + (" " if text.startswith("(") else "") + text + (
            "\n)" if "\n" in text else ")")

    def _ansi_c(self, start: int) -> str:
        """`$'...'` decoded with bash's escapes (its text from `start`), as one single-quoted
        word."""
        s, n = self.s, self.n
        j = start
        out: List[str] = []
        while j < n:
            ch = s[j]
            if ch == "'":
                j += 1
                break
            if ch != "\\" or j + 1 >= n:
                out.append(ch)
                j += 1
                continue
            e = s[j + 1]
            if e in _ANSI_C_ESCAPES:
                out.append(_ANSI_C_ESCAPES[e])
                j += 2
            elif e in "01234567":
                digits = re.match(r"[0-7]{1,3}", s[j + 1:j + 4]).group()
                out.append(chr(int(digits, 8) & 0xFF))
                j += 1 + len(digits)
            elif e in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[e]
                digits = re.match(r"[0-9A-Fa-f]{0,%d}" % width, s[j + 2:j + 2 + width]).group()
                value = int(digits, 16) if digits else -1
                if value < 0:
                    out.append("\\" + e)
                elif value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF:
                    out.append(chr(value))
                j += 2 + len(digits)
            elif e == "c" and j + 2 < n:
                out.append(chr(ord(s[j + 2]) & 0x1F))
                j += 3
            else:
                out.append("\\" + e)
                j += 2
        self.i = min(j, n)
        text = "".join(out).split("\x00", 1)[0]     # bash ends the string at a NUL
        return "'" + text.replace("'", "'\\''") + "'"


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
# A redirection on a descriptor bash allocates and names (`{fd}>f`, `{x}<`): never the input,
# never a script name; its target follows in the next word when none is written against it.
_NAMED_FD_TOKEN_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}(?:<<<|<<-?|<>|<&|>>|>&|>\||<|>)(.*)$",
                                re.DOTALL)


def _script_input(exe: str, args: List[str], carried: List[tuple],
                  raws: Optional[List[str]] = None) -> tuple:
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
    heredoc body, a shell runs that text, returned to be read like `bash -c` text.

    A descriptor counts as opened only by a redirection BEFORE the one that copies it to the
    input: `bash <&3- 3<f` copies the descriptor an earlier command opened, then opens 3.
    The third value says whether the script comes from the pipe: it reads its input and no
    redirection of its own replaces that input (B2, B3)."""
    shell = exe in _STDIN_SHELLS
    inline = "c" if shell else "cemr"          # -c CODE, -e/-r CODE, -m MODULE
    script = None
    from_stdin = False
    proc_fed = False
    stdin_fd = ""                           # `<&3`, `< /dev/fd/3`: the input is descriptor 3
    stdin_fd_opened = False                 # ...opened by a redirection before that one
    stdin_redirected = False
    opened = set()
    here_words: List[str] = []
    pos = 0
    while pos < len(args):
        tok = args[pos]
        nxt = args[pos + 1] if pos + 1 < len(args) else ""
        named = _NAMED_FD_TOKEN_RE.match(tok)
        if named:
            pos += 1 if named.group(1) else 2   # `{fd}>f`: a descriptor of its own
            continue
        if _HERE_OPERATOR_RE.match(tok):
            here_words.append(nxt)
            stdin_redirected = stdin_redirected or tok in ("<<<", "0<<<")
            pos += 2
            continue
        if _HEREDOC_OP_RE.match(tok):
            # `bash << EOF`: the delimiter is no script, the heredoc is the input.
            stdin_redirected = stdin_redirected or tok in ("<<", "<<-", "0<<", "0<<-")
            # An operator after it is no delimiter (`bash << <<< '...'`): read as it stands.
            pos += 1 if _HERE_OPERATOR_RE.match(nxt) or _HEREDOC_OP_RE.match(nxt) else 2
            continue
        if _REDIRECT_RE.match(tok):
            if _stdin_descriptor(tok, nxt):
                stdin_fd = _stdin_descriptor(tok, nxt)
                stdin_fd_opened = stdin_fd in opened
            elif tok.endswith("<") or tok.endswith("<>"):
                opened.add(tok.rstrip("<>") or "0")
                proc_fed = proc_fed or nxt == _SUBST_PROC
            stdin_redirected = stdin_redirected or tok in ("<", "0<", "<>", "0<>", "<&", "0<&")
            pos += 2
            continue
        if tok[:1] in "<>" or tok.startswith("&>") or (
                tok[:1].isdigit() and tok.lstrip("0123456789")[:1] in ("<", ">")):
            # `&>/dev/null` written against its target is a redirection too, never the script.
            glued = _GLUED_INPUT_RE.match(tok)
            if glued and _stdin_descriptor(glued.group(1), glued.group(2)):
                stdin_fd = _stdin_descriptor(glued.group(1), glued.group(2))
                stdin_fd_opened = stdin_fd in opened
            elif tok[:1].isdigit() and "<" in tok:
                opened.add(tok[:len(tok) - len(tok.lstrip("0123456789"))])
            stdin_redirected = stdin_redirected or bool(
                glued and glued.group(1).lstrip("0") in ("<", "<>", "<&")) or tok.startswith("<<")
            pos += 1                       # `2>/dev/null`, `<file`: an attached redirection
            continue
        # `--` is no script (`bash -- <<< ...` reads its input); an option-looking word after
        # it is still read as an option, the side that reads the input.
        if script is None and tok.startswith(("-", "+")) and tok != "-":
            letters = tok[1:] if not tok.startswith("--") else ""
            if any(c in inline for c in letters):
                return "", [], False        # code given inline: read elsewhere (`bash -c`)
            from_stdin = from_stdin or (shell and "s" in letters)
            # A value option never takes a redirection or a here-string as its value.
            takes_value = (tok in _SHELL_VALUE_OPTIONS and not _HERE_OPERATOR_RE.match(nxt)
                           and not _REDIRECT_RE.match(nxt) and nxt[:1] not in ("<", ">"))
            pos += 2 if takes_value else 1
            continue
        if script is None and shell and (tok.startswith("$") or tok.startswith(_SUBST_BARE)) and (
                raws is None or raws[pos][:1] in ("$", _SUBST_BARE, "`")):
            # An unquoted expansion where the script name stands may expand to nothing (`bash
            # $! <<< ...` with no background job reads its input): the input is read as well. A
            # quoted one is a word even when empty (`bash "$S"` runs no input).
            from_stdin = True
            pos += 1
            continue
        if script is None:
            script = tok
        pos += 1
    if script == _SUBST_PROC:
        return "output", [], False
    path = _normal_path(script) if script and script.startswith("/") else (script or "")
    fd = _FD_PATH_RE.match(path)
    if fd and int(fd.group(1)) > 2 and fd.group(1) not in opened:
        return "descriptor", [], False
    reads_input = script is None or from_stdin or path in ("/dev/stdin", "-") \
        or bool(fd and fd.group(1) == "0")
    if reads_input and stdin_fd.isdigit() \
            and int(stdin_fd) > 2 and (stdin_fd not in opened or not stdin_fd_opened):
        return "descriptor", [], False      # `bash <&3`: descriptor 3, opened earlier
    script = path or script
    if exe in ("source", "."):
        from_input = script is not None and bool(_DESCRIPTOR_RE.match(script))
    else:
        from_input = script is None or from_stdin or bool(_DESCRIPTOR_RE.match(script))
    if not from_input:
        return "", [], False
    kinds = [kind for kind, _ in carried]
    from_pipe = not (stdin_redirected or stdin_fd or here_words
                     or "heredoc_u" in kinds or "heredoc_q" in kinds)
    if proc_fed or "heredoc_u" in kinds or any(_SUBST_BARE in w or _SUBST_QUOTED in w
                                                for w in here_words):
        return "output", [], False
    if not shell:
        return "", [], from_pipe
    # A heredoc fed to a shell is its script: read as shell text, like `bash -c`.
    return "", here_words + [
        text for kind, text in carried if kind == "heredoc_q"], from_pipe


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


# -- the additive checks: each only adds a category or a nested reading -----------------------

# What a `$` starts an expansion with outside quotes (`$X`, `${X}`, `$1`, `$@`, `$'..'`, `$".."`,
# `$[..]`), and inside double quotes.
_EXPANSION_FOLLOWERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                 "0123456789_{@*#?$!-'\"[")
_DQ_EXPANSION_FOLLOWERS = _EXPANSION_FOLLOWERS - frozenset("'\"[")
# An assignment written where the program would stand (`a[0]=x`, `x+=$y`): no program.
_ASSIGNMENT_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
# What a program word needs for B1 to look closer.
_EXPANSION_HINT_RE = re.compile(r"[$*?\[{]")


def _raw_words(segment: str) -> List[str]:
    """The segment's words as written, quotes and all, cut where shlex cuts them (`_tokens`), so
    the n-th word is the n-th token's spelling. Unbalanced quotes: the same whitespace split
    `_tokens` falls back to."""
    if "<<<" in segment:
        segment = _spaced_here_strings(segment)
    words: List[str] = []
    start = -1
    quote = ""
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if quote:
            if quote == '"' and c == "\\" and segment[i + 1:i + 2] in ('"', "\\"):
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in " \t\r\n":
            if start >= 0:
                words.append(segment[start:i])
                start = -1
            i += 1
            continue
        if start < 0:
            start = i
        if c == "\\":
            if i + 1 >= n:
                return segment.split()          # shlex refuses a trailing backslash
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
        i += 1
    if quote:
        return segment.split()
    if start >= 0:
        words.append(segment[start:])
    return words


def _names_program_by_expansion(word: str, full: bool) -> bool:
    """Whether bash expands the program word `word` (as written) into a name the text does not
    show: an unquoted `$` expansion (`$X`, `${X}`, `$'..'`, `$".."`), a `$` inside double quotes
    in its last path component (`"$X"`, `"$DIR/$TOOL"`; `"$VIRTUAL_ENV/bin/python"` names
    python), an unquoted glob (`*`, `?`, a `[...]` class) or brace expansion (`{a,b}`, `{1..3}`).
    A tilde is no expansion of the name. `full` False: only `$'..'` and `$".."`."""
    quote = ""
    braces = 0
    last_slash = -1
    quoted_dollar = -1
    close_bracket = word.rfind("]")
    close_brace = word.rfind("}")
    brace_checked = False
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if quote == "'":
            if c == "'":
                quote = ""
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        nxt = word[i + 1:i + 2]
        if quote == '"':
            if c == '"':
                quote = ""
            elif c == "$" and nxt and nxt in _DQ_EXPANSION_FOLLOWERS:
                quoted_dollar = i
                braces += nxt == "{"
            elif c == "}" and braces:
                braces -= 1
            elif c == "/" and not braces:
                last_slash = i
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
        elif c == "$" and nxt and nxt in _EXPANSION_FOLLOWERS:
            if full or nxt in ("'", '"'):
                return True
        elif not full:
            pass
        elif c in "*?":
            return True
        elif c == "[" and i < close_bracket:
            return True
        elif c == "{" and not brace_checked:
            brace_checked = True
            inside = word[i + 1:close_brace] if i < close_brace else ""
            if "," in inside or ".." in inside:
                return True
        elif c == "/":
            last_slash = i
        i += 1
    return full and quoted_dollar > last_slash


def _program_named_by_expansion(seg: str, toks: List[str], exe_at: int, full: bool) -> bool:
    """B1: the program word of `seg` (the token at `exe_at`, read in its raw spelling) is named
    by an expansion (_names_program_by_expansion). When the raw words do not line up with the
    tokens, the token is read as if unquoted."""
    tok = toks[exe_at]
    if tok in ("[", "[[") or not _EXPANSION_HINT_RE.search(tok):
        return False
    words = _raw_words(seg)
    raw = words[exe_at] if len(words) == len(toks) else tok
    if _ASSIGNMENT_WORD_RE.match(raw):
        return False
    return _names_program_by_expansion(raw, full)


def _word_expands(word: str) -> bool:
    """Whether a word as written holds a `$` expansion bash makes (outside single quotes)."""
    quote = ""
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1
        elif c == '"':
            quote = "" if quote else '"'
        elif c == "'" and not quote:
            quote = "'"
        elif c == "$" and not quote and word[i + 1:i + 2] == "'":
            # `$'...'` expands nothing: its text up to the closing quote (`\'` inside it).
            i += 2
            while i < n and word[i] != "'":
                i += 2 if word[i] == "\\" else 1
        elif c == "$" and word[i + 1:i + 2] and word[i + 1] in _EXPANSION_FOLLOWERS:
            return True
        i += 1
    return False


# Escapes echo (dash's always, bash's with -e) and printf decode in the text they print.
_PRINT_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                  "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"'}
# A printf directive: its flags, width and precision (a number or `*`, which takes an argument)
# and its conversion letter.
_PRINTF_DIRECTIVE_RE = re.compile(r"%%|%([-+ #0']*)(\*|[0-9]+)?(?:\.(\*|[0-9]*))?([a-zA-Z])")
_ECHO_OPTION_RE = re.compile(r"^-[neE]+$")


def _unescape_printed(text: str, *, echo: bool, any_octal: bool = False,
                      stopped: Optional[List[bool]] = None) -> str:
    """`text` after echo's (`\\0NNN`, `\\c` ends it) or printf's (`\\NNN`) backslash escapes;
    `any_octal`: printf's `%b`, which takes both `\\0NNN` and `\\NNN` (measured with bash).
    `stopped` gets a True when a `\\c` ended the text (in `%b` it ends all of printf's output)."""
    if "\\" not in text:
        return text
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = text[i + 1]
        if e in _PRINT_ESCAPES:
            out.append(_PRINT_ESCAPES[e])
            i += 2
        elif e == "c" and echo:
            if stopped is not None:
                stopped.append(True)
            break
        elif e in "01234567" and (e == "0" or not echo or any_octal):
            first = i + 2 if echo and e == "0" else i + 1
            digits = re.match(r"[0-7]{0,3}", text[first:first + 3]).group()
            out.append(chr(int(digits or "0", 8) & 0xFF))
            i = first + len(digits)
        elif e in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[e]
            digits = re.match(r"[0-9A-Fa-f]{0,%d}" % width, text[i + 2:i + 2 + width]).group()
            value = int(digits, 16) if digits else -1
            if 0 <= value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF:
                out.append(chr(value))
            elif value < 0:
                out.append("\\" + e)
            i += 2 + len(digits)
        else:
            out.append("\\" + e)
            i += 2
    return "".join(out)


def _printed_text(seg: str, toks: List[str], exe_at: int, exe: str) -> Optional[tuple]:
    """B2: (texts, opaque) for an `echo`, `printf` or `yes` that prints literal text, else None.
    The texts are what it prints (echo's words joined by blanks, as written and with its escapes
    decoded, since dash's echo always decodes them; printf's format with its escapes decoded
    and its directives filled from the arguments, `%b` with the argument's escapes decoded, then
    the rest of them, a line each; yes' words), also from the words as bash's quote removal
    leaves them (_bash_word). Opaque when a substitution or a variable in its words supplies
    the text."""
    if exe_at < 0 or exe not in ("echo", "printf", "yes"):
        return None
    args = toks[exe_at + 1:]
    words = _raw_words(seg)
    raws = words[exe_at + 1:] if len(words) == len(toks) else None
    opaque = any(m in a for a in args for m in (_SUBST_BARE, _SUBST_QUOTED, _SUBST_PROC)) or (
        any(_word_expands(r) for r in raws) if raws is not None else any("$" in a for a in args))
    printed = _printed_by(exe, args, 4 * len(seg) + 4096)
    if printed is None:
        return None
    if raws is not None and any("\\" in r for r in raws):
        # The words as bash's own quote removal leaves them: shlex keeps `\$` in double
        # quotes, so its token cannot tell `\\\$` from `\\$` (_bash_word).
        spoken = _printed_by(exe, [_bash_word(r) for r in raws], 4 * len(seg) + 4096)
        if spoken is not None:
            printed = printed + [t for t in spoken if t not in printed]
    if printed and printed[0] is None:
        return [], True                         # more than the check writes out: opaque
    return printed, opaque


def _printed_by(exe: str, args: List[str], limit: int) -> Optional[List]:
    """What `exe` prints with these words (see _printed_text); [None] when printf's output
    passes `limit`, None when it prints nothing."""
    if exe == "yes":
        return [" ".join(args) or "y"]          # its line, again and again
    if exe == "echo":
        k = 0
        while k < len(args) and _ECHO_OPTION_RE.match(args[k]):
            k += 1
        text = " ".join(args[k:])
        decoded = _unescape_printed(text, echo=True)
        return [text] + ([decoded] if decoded != text else [])
    if args[:1] == ["-v"]:
        return None                             # assigns a variable, prints nothing
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        return None
    text = _printf_output(args[0], args[1:], limit=limit)
    return [None] if text is None else [text]


def _printf_output(fmt: str, args: List[str], *, limit: int) -> Optional[str]:
    """What printf prints: the format with its escapes decoded and its directives filled from
    the arguments (`%b` with the argument's escapes decoded), the format used again while
    arguments are left (`printf '%s' r m ' -rf ~'` prints `rm -rf ~`; measured with bash).
    None when the output would pass `limit` characters, or holds a directive whose output is
    not modelled here (a number, `%ls`, `%(...)T`): opaque, as text another program prints."""
    form = _unescape_printed(fmt, echo=False)
    if "%(" in form:
        return None                             # `%(fmt)T`: a time, as strftime prints it
    queue = list(args)
    taken = [0]
    size = [0]
    stopped: List[bool] = []
    opaque: List[bool] = []

    def _next() -> str:
        value = queue[taken[0]] if taken[0] < len(queue) else ""
        taken[0] += 1
        return value

    def _number(spec: Optional[str]) -> Optional[int]:
        if spec is None or spec == "":
            return None if spec is None else 0
        if spec == "*":                         # `*` takes the number from an argument
            try:
                return int(_next())
            except ValueError:
                return 0
        return int(spec)

    def _fill(m) -> str:
        if m.group() == "%%":
            return "%"
        flags, width, precision, conversion = m.groups()
        if conversion not in ("s", "b", "c", "q"):
            opaque.append(True)                 # a number, a length modifier (`%ls`)
            return ""
        width_n = _number(width)
        precision_n = _number(precision) if m.group().find(".") >= 0 else None
        if precision_n is not None and precision_n < 0:
            precision_n = None                  # a negative `*` precision is none at all
        value = _next()
        if conversion == "b":
            value = _unescape_printed(value, echo=True, any_octal=True, stopped=stopped)
        elif conversion == "c":
            value = value[:1]
        if precision_n is not None:
            value = value[:precision_n]
        if width_n and max(abs(width_n), len(value)) > limit - size[0]:
            opaque.append(True)                 # past the bound: opaque, never padded
            return ""
        if width_n:
            value = value.ljust(abs(width_n)) if "-" in flags or width_n < 0 \
                else value.rjust(width_n)
        return value

    parts: List[str] = []
    while True:
        before = taken[0]
        at = 0
        for m in _PRINTF_DIRECTIVE_RE.finditer(form):
            if "%" in form[at:m.start()]:
                # A `%` that starts no directive (`%\`, `%!`, a trailing `%`): printf prints
                # up to it and stops there (measured with bash).
                return "".join(parts) + form[at:form.index("%", at)]
            filled = _fill(m)
            if opaque:
                return None
            parts.append(form[at:m.start()])
            parts.append(filled)
            size[0] += m.start() - at + len(filled)
            if size[0] > limit:
                return None                     # checked per directive, so it never grows past
            at = m.end()
            if stopped:                         # `%b` met `\\c`: printf prints nothing more
                return "".join(parts)
        if "%" in form[at:]:
            return "".join(parts) + form[at:form.index("%", at)]
        parts.append(form[at:])
        size[0] += len(form) - at
        if size[0] > limit:
            return None
        if taken[0] >= len(queue) or taken[0] == before:
            return "".join(parts)              # all used, or a format that takes none


# Programs that decode text (B3): piped into a shell, what runs is not the text approved.
_BASE_DECODERS = frozenset({"base64", "base32", "basenc"})
# Programs that write what they read, changed or not: text that reaches their input (a
# heredoc, a here-string, what echo prints) can come out into the next pipe (B2).
_TEXT_FILTERS = frozenset({"cat", "tee", "sort", "uniq", "head", "tail", "grep", "egrep",
                           "fgrep", "sed", "awk", "gawk", "mawk", "tr", "cut", "column", "fold",
                           "fmt", "nl", "tac", "expand", "unexpand", "dos2unix", "unix2dos",
                           "iconv", "paste", "rev"})


def _decodes(exe: str, args: List[str]) -> bool:
    """B3: `base64`/`base32`/`basenc` with -d, --decode or -D, `xxd -r`, `openssl ... -d`,
    `uudecode`, `rev`."""
    if exe in _BASE_DECODERS:
        for a in args:
            if a in ("-d", "-D") or (a.startswith("--d") and "--decode".startswith(a)):
                return True
            if a.startswith("-") and not a.startswith("--") and ("d" in a[1:] or "D" in a[1:]):
                return True
        return False
    if exe == "xxd":
        return any(a.startswith("-r") for a in args)
    if exe == "openssl":
        return "-d" in args
    return exe in ("uudecode", "rev")


def _encoded_command(args: List[str]) -> bool:
    """B4: PowerShell told to run an encoded command: an option named `e`, `ec`, or two or more
    letters of `encodedcommand` or `encodedarguments` (after `-`, `--` or `/`), before the
    option that starts its command (`-Command`, `-File`)."""
    for a in args:
        if not a.startswith(("-", "/")):
            continue
        name = a.lstrip("-/").split(":", 1)[0].lower()
        if name and ("command".startswith(name) or "file".startswith(name)):
            return False
        if name in ("e", "ec") or (len(name) >= 2 and (
                "encodedcommand".startswith(name) or "encodedarguments".startswith(name))):
            return True
    return False


def _env_split_texts(toks: List[str], exe_at: int) -> List[str]:
    """B5: the command `env -S`/`--split-string` runs: its value (also written against the
    option, `-S'..'`, `--split-string=..`) and the words after it."""
    end = exe_at if exe_at >= 0 else len(toks)
    for k in range(end):
        if _exe_base(toks[k]) != "env":
            continue
        j = k + 1
        while j < end:
            t = toks[j]
            value: Optional[str] = None
            if t in ("-S", "--split-string"):
                if j + 1 >= len(toks):
                    return []
                value, rest = toks[j + 1], toks[j + 2:]
            elif t.startswith("--split-string="):
                value, rest = t.split("=", 1)[1], toks[j + 1:]
            elif t.startswith("-") and not t.startswith("--") and "S" in t[1:]:
                tail = t[t.index("S", 1) + 1:]
                if tail:
                    value, rest = tail, toks[j + 1:]
                elif j + 1 < len(toks):
                    value, rest = toks[j + 1], toks[j + 2:]
            if value is not None:
                # env's redirections are its own, no words of the command it starts.
                rest = _without_redirections(rest, None)
                texts = [" ".join([value] + ([shlex.join(rest)] if rest else []))]
                # env splits the string itself, no shell: quotes group, `\_` separates, and
                # every word holding a `=` before the program is an assignment, whatever its
                # name (`x=|1 rm ...` and `x<<=1 rm ...` run `rm`). Read again as `env` with
                # those words, so its options and assignments are taken as env takes them.
                try:
                    words = shlex.split(value.replace("\\_", " "))
                except ValueError:
                    words = None
                if words:
                    texts.append("env " + shlex.join(words + rest))
                return texts
            if t.startswith("-"):
                j += 2 if t in ("-u", "-C", "--unset", "--chdir") else 1
                continue
            if "=" in t:
                j += 1
                continue
            break                               # its program: an `env` after it may follow
    return []


# A variable an unquoted heredoc's body expands before the shell reads it, on the body as
# written: a `$` after an odd run of backslashes is escaped (`\$HOME` hands on `$HOME`), after
# an even one it expands (`\\$P` hands on a backslash and the value).
# Each run is matched from its first backslash only (the lookbehind), so a long run costs one
# pass, not one per position; the character after `$` is only looked at, so `\$$P` sees `$P`.
_HEREDOC_EXPANSION_RE = re.compile(r"(?<!\\)(\\*)\$(?=[A-Za-z_{0-9@*#?!$-])")


def _expanded_input(raws: List[str], toks: List[str], carried: List[tuple]) -> bool:
    """B2: whether text that reaches a command's input has variables bash expands before that
    command reads it: a here-string word with a `$` outside single quotes (`bash <<< "echo
    $P"`) or an unquoted heredoc whose body holds one (`bash <<EOF` ... `echo $P`). Such text is
    not the script approved; a quoted heredoc (`<<'EOF'`) leaves the variables to the shell."""
    bodies = [body for kind, body in carried if kind == "heredoc_raw"]
    k = 0
    for i, raw in enumerate(raws):
        tok = toks[i]
        if _HERE_OPERATOR_RE.match(tok):
            if i + 1 < len(raws) and _word_expands(raws[i + 1]):
                return True
            continue
        bare = raw.lstrip("0123456789")
        if not bare.startswith("<<") or bare.startswith("<<<"):
            continue
        word = bare[2:].lstrip("-")
        if not word and i + 1 < len(raws):
            word = raws[i + 1]                  # `<< EOF`: the delimiter is the next word
        body = bodies[k] if k < len(bodies) else ""
        k += 1
        if not any(q in word for q in ("'", '"', "\\")) and any(
                len(m.group(1)) % 2 == 0 for m in _HEREDOC_EXPANSION_RE.finditer(body)):
            return True
    return False


def _started_programs(exe: str, args: List[str]) -> List[int]:
    """Where in `args` the program word stands that a runner (its command, not an option's
    possible value) or `find -exec`/`-execdir`/`-ok`/`-okdir` starts, and the program after
    the wrappers and assignments in front of it (`find . -exec env X=1 $CMD ...`)."""
    if exe == "find":
        starts = [k + 1 for k, a in enumerate(args[:-1]) if a in _FIND_RUNNERS]
    else:
        candidates = _runner_commands(exe, args)
        starts = candidates[-1:] if candidates else []
    found: List[int] = []
    for start in starts:
        found.append(start)
        rel = _executable_at(args[start:])[0]
        if rel > 0:
            found.append(start + rel)
    return found


def _env_assignment_texts(toks: List[str], exe_at: int) -> List[str]:
    """B5: `env` takes every word with a `=` before its program as an assignment, also one
    whose name is no identifier (`env }x=1 cmd`), where the program search stops: the command
    after them."""
    if exe_at < 1 or "=" not in toks[exe_at] or toks[exe_at].startswith("-"):
        return []
    if not any(_exe_base(t) == "env" for t in toks[:exe_at]):
        return []
    k = exe_at
    while k < len(toks) and "=" in toks[k] and not toks[k].startswith("-"):
        k += 1
    return [shlex.join(toks[k:])] if k < len(toks) else []


# find's options that hold no test: an `-exec` after only these runs on every start path.
_FIND_GLOBALS = frozenset({"-depth", "-d", "-xdev", "-mount", "-noleaf", "-follow", "-daystart",
                           "-ignore_readdir_race", "-noignore_readdir_race", "-nowarn", "-warn"})
_FIND_GLOBALS_WITH_VALUE = frozenset({"-maxdepth", "-mindepth"})
_FIND_RUNNERS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _find_command_text(args: List[str]) -> str:
    """B5: the commands `find` runs, a line each: the words after `-exec`, `-execdir`, `-ok` or
    `-okdir` up to `;` or `+`, where `{}` stands for the start paths when no test comes before
    it (only -L -H -P -depth -d -xdev -mount -maxdepth N -mindepth N and the like) and stays
    `{}` otherwise; and `find START -delete` with no test before it as `rm -rf START`."""
    k = 0
    while k < len(args) and (args[k] in ("-H", "-L", "-P") or args[k].startswith("-O")):
        k += 1
    if k < len(args) and args[k] == "-D":
        k += 2
    starts: List[str] = []
    while k < len(args) and not (args[k].startswith("-") or args[k] in ("(", "!", ",")):
        starts.append(args[k])
        k += 1
    starts = starts or ["."]
    tested = False
    texts: List[str] = []
    while k < len(args):
        a = args[k]
        if a in _FIND_RUNNERS:
            j = k + 1
            words: List[str] = []
            while j < len(args) and args[j] not in (";", "+"):
                words.append(args[j])
                j += 1
            if not tested:
                expanded: List[str] = []
                for w in words:
                    if w == "{}":
                        expanded.extend(starts)
                    else:
                        expanded.append(w.replace("{}", starts[0]))
                words = expanded
            if words:
                texts.append(shlex.join(words))
            tested = True
            k = j + 1
            continue
        if a == "-delete":
            if not tested:
                texts.append(shlex.join(["rm", "-rf"] + starts))
            tested = True
        elif a in _FIND_GLOBALS_WITH_VALUE:
            k += 1
        elif a not in _FIND_GLOBALS:
            tested = True
        k += 1
    return "\n".join(texts)


def _watch_text(args: List[str]) -> str:
    """B5: the command `watch` runs: its words after its options, joined by blanks as watch
    hands them to `sh -c`, or as words with `-x`/`--exec`."""
    k = 0
    exec_words = False
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        a = args[k]
        if a == "--":
            k += 1
            break
        if a == "--exec" or (not a.startswith("--") and "x" in a[1:]):
            exec_words = True
        k += 2 if a in ("-n", "--interval", "-q", "--equexit") else 1
    words = args[k:]
    if not words:
        return ""
    return shlex.join(words) if exec_words else " ".join(words)


_SCRIPT_MISSING = object()



def _shell_c_script(args: List[str], strip_redirections: bool = True):
    """The script a shell's `-c` runs, `_SCRIPT_MISSING` when `-c` has none, None without `-c`.
    bash reads options after `-c` as well, up to `-` or `--` (both taken) or the first word
    that is no option, and runs that word (`bash -c -e '...'`, `sh -c - '...'`); its own
    redirections are no words (`xargs -0 bash -c <<< '...'` has no script, xargs supplies it).
    A token cannot tell a quoted word from a redirection (`-c '>x;cmd'`), so a reader of the
    script asks both ways (`strip_redirections` False keeps such words)."""
    # A word with a blank or a newline in it was quoted (`-c '2>/dev/null cmd'`): a script, never
    # a redirection, which shlex would have cut there.
    kept: List[str] = []
    k = 0
    while k < len(args):
        width = 0 if not strip_redirections or any(c in args[k] for c in " \t\n") \
            else _redirection_width(args, k)
        if width:
            k += width
            continue
        kept.append(args[k])
        k += 1
    args = kept
    has_c = False
    k = 0
    while k < len(args):
        tok = args[k]
        if tok in ("-", "--"):
            k += 1
            break
        if tok in _SHELL_VALUE_OPTIONS:
            k += 2
            continue
        if tok[:1] in ("-", "+") and len(tok) > 1:
            has_c = has_c or (tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:])
            k += 1
            continue
        break
    if not has_c:
        return None
    return args[k] if k < len(args) else _SCRIPT_MISSING


def _xargs_runs_its_input(toks: List[str], exe_at: int, args: List[str]) -> bool:
    """B5: a shell's `-c` under xargs runs what xargs reads: its script holds xargs' replace
    string (`-I R`, `-iR`, `-i`/`--replace` with `{}`), or it has none, so xargs supplies it."""
    x = next((k for k in range(exe_at) if _exe_base(toks[k]) == "xargs"), -1)
    if x < 0:
        return False
    replace = ""
    k = x + 1
    while k < exe_at:
        t = toks[k]
        if t == "-I" and k + 1 < exe_at:
            replace, k = toks[k + 1], k + 2
            continue
        if t.startswith("-I") and len(t) > 2:
            replace = t[2:]
        elif t in ("-i", "--replace"):
            replace = "{}"
        elif t.startswith("-i") and len(t) > 2:
            replace = t[2:]
        elif t.startswith("--replace="):
            replace = t.split("=", 1)[1] or "{}"
        k += 1
    script = _shell_c_script(args)
    if script is None:
        return False
    return script is _SCRIPT_MISSING or bool(replace and replace in script)


# A heredoc operator standing as a word of its own (`<<`, `<<-`).
_HEREDOC_OP_RE = re.compile(r"^\d*<<-?$")


def _redirection_width(toks: List[str], k: int, raws: Optional[List[str]] = None) -> int:
    """How many words the redirection at `k` takes (2 for an operator and its target, 1 for
    one written against its target), 0 for no redirection. With `raws` (the words as written,
    lined up with `toks`), a quoted word is never one (`eval '<f cmd'`)."""
    t = toks[k]
    if raws is not None and raws[k][:1] in ("'", '"', "\\", "$"):
        return 0
    named = _NAMED_FD_TOKEN_RE.match(t)
    if named:
        return 1 if named.group(1) else 2
    if _REDIRECT_RE.match(t) or _HERE_OPERATOR_RE.match(t) or _HEREDOC_OP_RE.match(t):
        return 2
    if t[:1] in "<>" or t.startswith("&>") or (
            t[:1].isdigit() and t.lstrip("0123456789")[:1] in ("<", ">")):
        return 1
    return 0


def _only_redirections(toks: List[str]) -> bool:
    """Whether every word is a redirection or its target (`<&3`, `< <(...)`, `2>/dev/null`)."""
    k = 0
    while k < len(toks):
        width = _redirection_width(toks, k)
        if not width:
            return False
        k += width
    return bool(toks)


# A redirection of the standard output: `>f`, `1>>f`, `>|f`, `&>f`, `>&2` (a copy onto itself,
# `>&1`, moves nothing).
_STDOUT_REDIRECT_RE = re.compile(r"^(1?|&)(>&|>\||>>?)(.*)$")


def _moves_stdout(toks: List[str]) -> bool:
    """Whether these redirections send the standard output elsewhere (`( echo x ) >f | bash`
    hands the pipe nothing; `2>&1`, `>&1` keep it)."""
    for k, tok in enumerate(toks):
        m = _STDOUT_REDIRECT_RE.match(tok)
        if not m:
            continue
        target = m.group(3) or (toks[k + 1] if k + 1 < len(toks) else "")
        if m.group(2) == ">&" and m.group(1) != "&" and target == "1":
            continue
        if m.group(1) != "&" and target in _STDOUT_PATHS:
            continue                            # `>/dev/stdout`: still the standard output
        return True
    return False


# Paths that name the standard output itself.
_STDOUT_PATHS = frozenset({"/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"})


class _Output:
    """What a text writes, gathered segment by segment: the first opaque shape, every text once
    in order (a set beside the list, so N segments cost N steps, not N squared)."""
    __slots__ = ("shape", "texts", "seen")

    def __init__(self) -> None:
        self.shape = ""
        self.texts: List[str] = []
        self.seen: set = set()

    def add(self, shape: str, texts) -> None:
        self.shape = self.shape or shape
        for t in texts:
            if t not in self.seen:
                self.seen.add(t)
                self.texts.append(t)


def _output_union(first: tuple, second: tuple) -> tuple:
    """Two outputs (shape, texts) as one: the first opaque shape, every text once."""
    merged = _Output()
    merged.add(*first)
    merged.add(*second)
    return (merged.shape, tuple(merged.texts))


def _without_redirections(toks: List[str], raws: Optional[List[str]] = None) -> List[str]:
    """The words that are no redirection and no redirection's target (`raws`: the words as
    written, lined up with `toks`)."""
    out: List[str] = []
    k = 0
    while k < len(toks):
        width = _redirection_width(toks, k, raws)
        if width:
            k += width
            continue
        out.append(toks[k])
        k += 1
    return out


def _input_of(toks: List[str], carried: List[tuple]) -> tuple:
    """B2, B6: what descriptor 0 reads after these words' redirections, in their order, as
    (shape, texts, redirected). The shape is "shell_text" for another command's output (a
    process substitution, a here-string or an unquoted heredoc holding a substitution) and
    "descriptor" for a descriptor above 2 that no EARLIER redirection of these opened; the
    texts are a literal here-string's word or a heredoc's body; `redirected` says descriptor 0
    is redirected at all, so the pipe or the enclosing command's input no longer reaches it."""
    # What each descriptor this command opens holds, so a copy onto the input keeps it
    # (`cat 3< <(base64 -d f) <&3 | bash`): (shape, texts) per descriptor number.
    opened: dict = {}
    shape = ""
    texts: List[str] = []
    redirected = False
    k = 0
    while k < len(toks):
        t = toks[k]
        nxt = toks[k + 1] if k + 1 < len(toks) else ""
        named = _NAMED_FD_TOKEN_RE.match(t)
        if named:
            k += 1 if named.group(1) else 2
            continue
        if _HERE_OPERATOR_RE.match(t):
            opaque = _SUBST_BARE in nxt or _SUBST_QUOTED in nxt
            held = ("shell_text", []) if opaque else ("", [nxt])
            fd = t[:len(t) - 3] or "0"
            if fd.lstrip("0") == "":
                redirected = True
                shape, texts = held
            else:
                opened[fd] = held
            k += 2
            continue
        if t.startswith("<<") or (t[:1] == "0" and t.lstrip("0").startswith("<<")):
            redirected = True
            if any(kind == "heredoc_u" for kind, _ in carried):
                shape, texts = "shell_text", []
            else:
                shape, texts = "", [body for kind, body in carried if kind == "heredoc_q"]
            k += 2 if _HEREDOC_OP_RE.match(t) else 1
            continue
        if _REDIRECT_RE.match(t):
            op, target, k = t, nxt, k + 2
        else:
            glued = _GLUED_INPUT_RE.match(t)
            k += 1
            if not glued:
                continue
            op, target = glued.group(1), glued.group(2)
        m = _INPUT_OPERATOR_RE.match(op)
        if not m:
            continue
        fd = m.group(1) or "0"
        dup = _stdin_descriptor("<&" if m.group(2) == "<&" else op.replace(fd, "", 1) or op,
                                target) if fd != "0" else _stdin_descriptor(op, target)
        if dup and dup in opened:
            held = opened[dup]                  # a copy keeps what the descriptor holds
        elif dup:
            held = ("descriptor" if dup.isdigit() and int(dup) > 2 else "", [])
        else:
            held = ("shell_text" if target == _SUBST_PROC else "", [])
        if fd != "0":
            opened[fd] = held
            continue
        redirected = True
        shape, texts = held
    return shape, texts, redirected


# A name and a `[` in quoted text: arithmetic runs a substitution in the subscript once the
# string becomes an array reference (B7).
_SUBSCRIPT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[")


def _parsed_substitution_end(text: str, at: int) -> int:
    """Where the `$(` at `at` ends for the grammar reading, which parses its commands as bash
    does; the end of the text when it does not close or nests too deep."""
    sub = _GrammarText(text)
    sub.i = at
    try:
        sub._dollar(False)
    except (_GrammarTooDeep, _GrammarBrokenArray):
        return len(text)
    return sub.i if sub.closed else len(text)


def _subscript_text(seg: str) -> List[str]:
    """B7: every substitution left as text in `seg` (single-quoted, or `\\$(` in double quotes)
    when it also holds a `NAME[`, as the arguments of the no-op `:`: `x='a[$(...)]'; echo
    $((x))`, `printf -v 'a[$(...)]' v` and `let` run it. Read, not refused. A `$(` ends where
    its parentheses balance, quotes inside skipped, and where bash's parser ends it (a case
    pattern's `)`, one in a comment or in a funsub closes nothing): one text for each."""
    if "[" not in seg or ("$(" not in seg and "`" not in seg) or not _SUBSCRIPT_RE.search(seg):
        return []
    texts: List[str] = []
    for ends_at in (_substitution_end, _parsed_substitution_end):
        found: List[str] = []
        i, n = 0, len(seg)
        next_dollar = seg.find("$(")
        next_tick = seg.find("`")
        while True:
            if next_dollar >= 0 and next_dollar < i:
                next_dollar = seg.find("$(", i)
            if next_tick >= 0 and next_tick < i:
                next_tick = seg.find("`", i)
            if next_dollar < 0 and next_tick < 0:
                break
            if next_tick < 0 or (0 <= next_dollar < next_tick):
                end = ends_at(seg, next_dollar)
                found.append(seg[next_dollar:end])
                i = end
            else:
                close = seg.find("`", next_tick + 1)
                close = n - 1 if close < 0 else close
                found.append(seg[next_tick:close + 1])
                i = close + 1
        text = ": " + " ".join(found) if found else ""
        if text and text not in texts:
            texts.append(text)
    return texts


def is_command_safe(command: str, *, profile: str = "host") -> tuple:
    """(is_safe, message) adapter kept for the two shell tools.

    The message is the refusal reason when blocked, else a note for the
    noteworthy-but-allowed categories.
    """
    v = classify_command(command, profile=profile)
    return (not v.blocked), (v.reason if v.blocked else v.warning)
