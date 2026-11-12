# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run the suite in an environment that is deliberately less generous than this one.

WHY THIS EXISTS. Three CI failures in a row were environment differences, not
logic errors, and the local suite could not have caught any of them because it
only ever runs in ONE environment: this workstation, UTF-8 output, Linux, every
optional dependency installed and every model already downloaded.

  - A key-placement branch that only Windows takes.
  - Output a cp1252 console cannot encode. Our stdout is UTF-8, so the crash was
    unreachable here.
  - A voice test that silently needed a model file this machine had cached and a
    fresh runner did not, reporting "engine unavailable" where the test expected
    "too short".

Each cost a round trip through CI, and the Windows entry takes about 90 minutes
to answer (measured 2026-11-12: the test step alone 89 minutes, against 21 on
ubuntu and 25 on macOS). This script makes the cheap two thirds of that variance reproducible
locally, in the time the suite already takes:

  NARROW OUTPUT   PYTHONIOENCODING=cp1252, so any print of a character outside
                  that code page fails here rather than on the Windows runner.
  NO EXTRAS       the optional, heavy, sometimes-network-fetching packages are
                  hidden from the import system, so a test that quietly depends
                  on one fails here rather than on the runner that lacks it.
  SCRATCH HOME    HOME points at a throwaway directory. Not a CI concern - a
                  safety one. The suite has twice written into the real user
                  store, once destroying a recovery key, and the isolation
                  fixtures do not cover every axis.
  WINDOWS HOME    "~" (and so Path.home()) resolves from USERPROFILE and
                  ignores HOME, as ntpath.expanduser does. A test that points
                  only HOME at a scratch directory still sees the real profile
                  on Windows; it failed on the Windows runner alone (the file
                  routes' ownership tests, 403 from the roots check) because
                  here HOME was the home. Set USERPROFILE too, or patch
                  Path.home, which every OS honours.
  LINKED HOME     the scratch home is reached through a symlink, so a resolved
                  path and the spelling HOME gives differ. On macOS that is the
                  normal case for a scratch home (/var is a link to /private/var)
                  and it failed only there: is_safe_path compared an unresolved
                  path with the resolved data dir and refused every skill file.
                  A home moved to another disk and linked back is the same case
                  on any OS. Where the host cannot create a link the real
                  directory is used and the axis says so.
  NO DOCKER       DOCKER_HOST points at a socket that does not exist, so every
                  docker call fails the way it does on a runner without VAF's
                  containers (macOS has no docker at all). This workstation has
                  the database container, and a test that reached it stayed
                  green here while it failed on every runner: the restore tests'
                  safety backup read the real volumes (five tests, the backup
                  round).
  OLD MAIL PARSER email.utils as Python 3.10.11 and 3.11.9 ship it, the last
                  Windows and macOS installers of those series and what
                  setup-python runs there: getaddresses and parseaddr without
                  the `strict` keyword and with the lenient pre-CVE-2023-27043
                  parse. The nightly matrix failed on exactly those versions for
                  thirteen nights (a display name read as the sender) and then
                  again on a test fixture that passed `strict=False`; every
                  local run had a patched stdlib and stayed green.
  FOREIGN OS      platform.system() reports Darwin and platform.machine() arm64,
                  on a Linux host only; a real macOS or Windows host has nothing
                  to add here. Code that detects its host reads these two, and a
                  test that drives it without pinning them asserts this
                  workstation's answer: two support_class tests stayed green here
                  and failed on the macOS runner, where support() answered
                  "apple" before it looked at the GPU (the Windows runner answers
                  "windows"). Measured when this was added: the full suite under
                  this fake failed exactly those two tests, 13821 passed. Darwin
                  and not Windows, because a faked Windows is incoherent on Linux:
                  code that believes it takes Windows-only branches, seven engine
                  tests reach for subprocess.CREATE_NO_WINDOW and five more get
                  nothing from gpu_detection's Windows probes, artefacts rather
                  than findings. Named boundary: only these two functions are
                  faked, not sys.platform or os.name; vaf/ reads
                  platform.system() at 66 sites in 22 files
                  and calls neither platform.uname() nor platform.platform(). A
                  test that is red under this axis but green on a real Mac is an
                  artefact of the half-faked host; none exists today, and one
                  that does gets an environment flag set here, the way
                  VAF_SIMULATED_WINDOWS_HOME serves the windows-home axis.
  CRLF CHECKOUT   Path.read_bytes hands every tracked text file back with CRLF,
                  as a Windows checkout with core.autocrlf holds it: text git
                  stores with LF and no eol attribute of its own, the list read
                  from `git ls-files --eol` (1851 files when this was added).
                  read_text needs nothing, its universal newlines read CRLF as
                  LF on every OS. A test that reads source as bytes and matches
                  it line by line passed here and failed on every Windows leg:
                  test_gpu_child_env's MULTILINE `$` sat before the \\r of
                  `hide()\\r\\n`. tests/test_windows_path_hygiene.py section 2
                  knows one spelling of that class statically; this catches any
                  spelling. Measured when this was added: the 133 test modules
                  that call read_bytes ran under it with exactly those two
                  failures (3331 passed once fixed). Named boundary: only
                  Path.read_bytes is converted, not open(path, "rb"), which the
                  tests use on tracked files only to feed ast.parse or a
                  substring check without a newline, both indifferent to CRLF.
  SLOW REFUSAL    a refused TCP connect to a local address is charged what a
                  Windows runner waits for it: about two seconds of SYN retries
                  (or the caller's connect timeout when that is shorter), where
                  Linux refuses in microseconds. It COUNTS and does not sleep:
                  every refusal appends one line to a log, and after the run a
                  test whose total exceeds the budget (60 s) fails the run and
                  is named. Sleeping would reproduce the wait itself, at its
                  price: with the account store's outage memory reverted, the
                  tray and app-link files alone made 10036 refused connects,
                  19914 s (5.5 hours) of waiting, one test 7296 s. Counted,
                  that run took 115 s here and failed with 53 tests named,
                  that one first; on the fixed code the same files took 65 to
                  86 s with the axis and without it (three runs each). The
                  instance is order dependent: the account directory resolver
                  is registered when vaf.main is imported,
                  so the app-link files run alone made no database connect at
                  all, and after a file that imports vaf.main every send asked
                  the absent store twice. Counted are blocking socket connect
                  and connect_ex and asyncio's selector sock_connect (asyncpg
                  and asyncio's own clients dial through it), each once; a
                  UDP, Unix-socket or remote refusal is not charged, nor is a
                  connect that succeeds. Local means a numeric loopback or
                  wildcard address in any spelling the C library parses ("0",
                  "127.1", ::ffff:127.0.0.1) or a name the hosts file or the
                  host name puts on this machine, decided without a lookup.
                  The ports VAF's own services use
                  (3000, 5432, 8001, 8005, 8080, 8443, 9222) are refused
                  without a dial: the runner has none of them, so the count
                  must not depend on what this machine runs, and the suite
                  must never post a test's event into a live app. A refusal
                  in a thread that outlived its test is charged "after" the
                  test that finished last; a child process inherits the log
                  and the running test's name.

WHAT IT CANNOT DO. Real Windows file semantics - ACLs, MoveFileEx sharing
violations, the read-only-flag-only chmod - need a real Windows machine. Those
are covered by seams and simulated branches in tests/test_at_rest_cross_platform.py
instead, and the honest boundary is written down there rather than pretended
away here. Windows-only SERIALIZATION defects (str(PurePath) renders with the
host's separator, invisible on Linux where it equals as_posix) are covered by a
static guard, tests/test_windows_path_hygiene.py, which fails on any OS. Nor can
it put the CHECKOUT INSIDE THE HOME, where the Linux and macOS runners have it
(/home/runner/work/...): the source tree is located by its resolved path, so
only a second checkout inside the scratch home would reproduce that, and the
suite needs files no fresh checkout has. A test that compares the real home with
the source tree passed here and failed there (the home's parent also overlapped
the source tree, and that refusal answered first); such a test gives itself a home
of its own (HOME and USERPROFILE under tmp_path), as test_refuses_a_folder_above_home
does. Measured when this was written: no other test compares the two.

The SLOW REFUSAL axis has named boundaries. It charges the wait but does not
take it, so a test that only fails BECAUSE of the wait (a timer that runs out
while a handshake retries) stays green here: the voice front door once asked
the absent account store twelve times per handshake, 24 s on Windows, and five
call-queue tests whose contention was held for 5 or 10 s failed only there.
The verdict sees that cause (many refusals in one test), and
test_the_front_door_asks_no_database pins that instance. An asyncio refusal is
charged the full two seconds even when the caller waits less. A deadline set
with asyncio.timeout() leaves nothing the counter can read, and before 3.12
asyncio.wait_for runs the connect in a task of its own; measured on 3.12 and
3.13, only wait_for's own frame shows its timeout and the task holds no
deadline. So a red run says that this may be the case. Nothing else of
Windows' socket layer is modelled: a connect to the wildcard address
(0.0.0.0, ::), which Linux routes to loopback, is charged like one, where
Windows refuses it at once; the proactor event loop and the order in which
"localhost" resolves are not reproduced. On a Windows host the axis installs
nothing, since the real wait is paid there. Invisible to the counter, neither
counted nor refused: a C extension that dials on its own (libpq through
psycopg2, not installed today); a client on uvloop, which uvicorn[standard]
installs and which dials through libuv, not the selector loop (no test dials
a client through it today, and uvicorn servers in tests may run on it, so it
is not hidden here); connect() called on the C base class _socket.socket
directly; and a literal address of one of this machine's own network
interfaces, through which a live app bound to all interfaces stays reachable.
The budget is per test; the whole-suite total is reported, not judged, since
it grows with every test. The whole-suite figure is the "Slow refusal" summary
that stage 7 of scripts/ci_check.sh prints on every run; no figure for it is
recorded here.
Nor does it change the OPENSSL BUILD:
python.org's 3.13 for Windows and macOS bundles OpenSSL 3.5.9, which loads a
certificate with a malformed subject alternative name that 3.5.3 refuses
(measured with both); a test about such a pair runs both verdicts through
ssl_utils' load seam (test_own_certificate).

The standing rule: every Windows CI red that the local gates could not have caught
adds its class to one of these three places in the SAME fix - a hostile axis
here, a simulated branch there, or a static guard.

Usage:  venv/bin/python scripts/hostile_env.py [pytest args...]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Installed on a developer machine, absent or broken on a fresh runner. Hiding
# them turns "works here" into "works anywhere" for the tests that touch them.
OPTIONAL_PACKAGES = (
    "sherpa_onnx",       # speaker id: also downloads a model on first use
    "faster_whisper",    # speech to text
    "playwright",        # browser automation
    "transformers",      # speech extra: the Smart Turn judge's feature extractor (fail-open)
    "pytesseract",
    # requirements marker python_version >= "3.11": absent on the 3.10 CI
    # runner, present on every dev machine - the exact gap that shipped an
    # unguarded import (test_browser_agent_vision_lane, the controller test).
    "browser_use",
)

_BLOCKER = '''
import sys


class _HideOptional:
    """Make a chosen set of imports fail the way a fresh runner fails them."""

    HIDDEN = {names}

    def find_module(self, name, path=None):
        return self if name.split(".")[0] in self.HIDDEN else None

    def load_module(self, name):
        raise ImportError(f"{{name}} is not installed in this environment")

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.HIDDEN:
            raise ImportError(f"{{name}} is not installed in this environment")
        return None


sys.meta_path.insert(0, _HideOptional())


import os as _os
import posixpath as _posixpath

_posix_expanduser = _posixpath.expanduser


def _windows_expanduser(path):
    """'~' from USERPROFILE, never from HOME - ntpath.expanduser's rule on Windows."""
    p = _os.fspath(path)
    if not isinstance(p, str) or not p.startswith("~"):
        return _posix_expanduser(path)
    end = p.find("/", 1)
    end = len(p) if end == -1 else end
    if end != 1:                        # ~user: not this axis's question
        return _posix_expanduser(path)
    profile = _os.environ.get("USERPROFILE")
    return (profile + p[1:]) if profile else p


_posixpath.expanduser = _windows_expanduser


import email.utils as _email_utils

_patched_getaddresses = _email_utils.getaddresses
_patched_parseaddr = _email_utils.parseaddr


def _old_getaddresses(fieldvalues):
    """The unpatched signature and parse: no strict keyword, the legacy result."""
    return _patched_getaddresses(fieldvalues, strict=False)


def _old_parseaddr(addr):
    return _patched_parseaddr(addr, strict=False)


if getattr(_email_utils, "supports_strict_parsing", False):
    _email_utils.getaddresses = _old_getaddresses
    _email_utils.parseaddr = _old_parseaddr
    del _email_utils.supports_strict_parsing


import platform as _platform

if sys.platform.startswith("linux"):
    _platform.system = lambda: "Darwin"
    _platform.machine = lambda: "arm64"


import pathlib as _pathlib

_crlf_list = _os.environ.get("VAF_HOSTILE_CRLF_FILES")
if _crlf_list:
    with open(_crlf_list, encoding="utf-8") as _f:
        _CRLF_FILES = frozenset(line for line in _f.read().split("\\n") if line)
    _CRLF_NAMES = frozenset(_os.path.basename(path) for path in _CRLF_FILES)
    _lf_read_bytes = _pathlib.Path.read_bytes

    def _crlf_read_bytes(self):
        """A tracked text file as a Windows checkout (core.autocrlf) holds it."""
        data = _lf_read_bytes(self)
        # normcase on both sides: the list holds normcase'd paths, and on Windows
        # that lowercases, so a raw README.md would never match readme.md.
        if (_os.path.normcase(self.name) in _CRLF_NAMES
                and _os.path.normcase(_os.path.realpath(self)) in _CRLF_FILES):
            data = data.replace(b"\\n", b"\\r\\n")
        return data

    _pathlib.Path.read_bytes = _crlf_read_bytes


_refusal_log = _os.environ.get("VAF_HOSTILE_REFUSAL_LOG")
if _refusal_log and _os.name != "nt":
    import asyncio.selector_events as _selector_events
    import errno as _errno
    import socket as _socket

    _WINDOWS_REFUSAL_S = {refusal_s!r}
    _REFUSE_PORTS = frozenset(
        int(port) for port in _os.environ.get("VAF_HOSTILE_REFUSE_PORTS", "").split(",")
        if port.strip().isdigit())
    _INET = (_socket.AF_INET, _socket.AF_INET6)

    def _tcp(sock):
        # UDP has no handshake to refuse, and a Unix socket no Windows retry.
        return sock.family in _INET and sock.type == _socket.SOCK_STREAM

    import ipaddress as _ipaddress

    def _local_ip(ip):
        # The wildcard counts: Linux routes a connect to it to loopback.
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        return ip.is_loopback or ip.is_unspecified

    def _hosts_file_names():
        """The names the hosts file maps to this machine, read without any lookup."""
        names = set()
        try:
            with open(_os.environ.get("VAF_HOSTILE_HOSTS_FILE") or "/etc/hosts",
                      encoding="utf-8", errors="replace") as hosts:
                for row in hosts:
                    fields = row.split("#", 1)[0].split()
                    try:
                        if fields and _local_ip(_ipaddress.ip_address(fields[0].split("%")[0])):
                            names.update(name.lower().rstrip(".") for name in fields[1:])
                    except ValueError:
                        continue
        except OSError:
            pass
        return names

    _LOCAL_NAMES = _hosts_file_names() | set(
        [_socket.gethostname().lower().rstrip("."), "localhost"])

    def _local(address):
        """A TCP address on this machine, decided without a lookup: a numeric address
        (any spelling the C library accepts, "0" and "127.1" included) that is loopback
        or the wildcard, or a name the hosts file or the host name puts here."""
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        host = str(address[0])
        try:
            return _local_ip(_ipaddress.ip_address(host.split("%")[0]))
        except ValueError:
            pass
        try:
            return _local_ip(_ipaddress.ip_address(_socket.inet_aton(host)))
        except (OSError, ValueError):
            pass
        name = host.lower().rstrip(".")
        return name in _LOCAL_NAMES or name.endswith(".localhost")

    def _forced(address):
        return _local(address) and address[1] in _REFUSE_PORTS

    def _charge(address, timeout):
        """One line per refusal: who paid, where to, what a Windows runner waits."""
        cost = _WINDOWS_REFUSAL_S if timeout is None else min(_WINDOWS_REFUSAL_S, timeout)
        who = _os.environ.get("PYTEST_CURRENT_TEST")
        if not who:
            last = _os.environ.get("VAF_HOSTILE_LAST_TEST")
            who = ("after " + last) if last else "outside any test"
        line = who.replace("\\t", " ") + "\\t" + str(address[0]) + ":" + str(address[1]) \\
            + "\\t" + repr(float(cost)) + "\\n"
        try:
            # Open, append, close: threads and child processes share this file.
            with open(_refusal_log, "a", encoding="utf-8") as log:
                log.write(line)
        except OSError:
            pass

    def _refused(address):
        return ConnectionRefusedError(
            _errno.ECONNREFUSED, "refused by the hostile environment, never dialed: "
            + str(address[0]) + ":" + str(address[1]))

    _plain_connect = _socket.socket.connect
    _plain_connect_ex = _socket.socket.connect_ex

    def _counted_connect(self, address):
        # A non-blocking socket is asyncio's: its refusal is charged once, in
        # sock_connect below, never a second time here.
        counted = _tcp(self) and self.gettimeout() != 0.0 and _local(address)
        if _tcp(self) and _forced(address):
            if counted:
                _charge(address, self.gettimeout())
            raise _refused(address)
        try:
            return _plain_connect(self, address)
        except ConnectionRefusedError:
            if counted:
                _charge(address, self.gettimeout())
            raise

    def _counted_connect_ex(self, address):
        counted = _tcp(self) and self.gettimeout() != 0.0 and _local(address)
        if _tcp(self) and _forced(address):
            if counted:
                _charge(address, self.gettimeout())
            return _errno.ECONNREFUSED
        code = _plain_connect_ex(self, address)
        if code == _errno.ECONNREFUSED and counted:
            _charge(address, self.gettimeout())
        return code

    _socket.socket.connect = _counted_connect
    _socket.socket.connect_ex = _counted_connect_ex

    _plain_sock_connect = _selector_events.BaseSelectorEventLoop.sock_connect

    async def _counted_sock_connect(self, sock, address):
        # asyncpg and every asyncio client dial through here with their own
        # timeout around it (asyncpg: 60 s), so a refusal costs the full wait. A
        # service port needs no check of its own: the loop's non-blocking
        # sock.connect above refuses it before a SYN leaves.
        counted = _tcp(sock) and _local(address)
        try:
            return await _plain_sock_connect(self, sock, address)
        except ConnectionRefusedError:
            if counted:
                _charge(address, None)
            raise

    _selector_events.BaseSelectorEventLoop.sock_connect = _counted_sock_connect
'''

# What a refused connect to a closed local port costs on a Windows runner: Windows
# retries the SYN for about two seconds before it reports the refusal, where Linux
# and macOS refuse at once. The figure is the Windows CI measurement of the hang
# that the account store's outage memory fixed (vaf/auth/permissions.py).
_WINDOWS_REFUSAL_S = 2.0

# Per test (setup, call and teardown together). Measured on the fixed code: the
# most expensive test of two disjoint tenths of the suite paid 36 s, the worst
# of the first 72 files in suite order 12 s; with the outage memory reverted nine
# tests paid over 200 s and one 7296 s.
_REFUSAL_BUDGET_S = 60.0

# Ports where VAF's own services listen on a developer machine. A connect to them
# is refused without dialing: the CI runner has none of them, so the count must
# not depend on whether this machine runs them, and the suite must never deliver
# a test's request to a live app.
_VAF_SERVICE_PORTS = (
    3000,   # the web UI (Next.js)
    5432,   # the memory and auth database (memory_db_url's default)
    8001,   # the API server (web_server.py)
    8005,   # the voice server
    8080,   # the local llama server
    8443,   # the HTTPS door
    9222,   # the browser's DevTools port (service_stack.py)
)

# Loaded with `-p` in the argv of the one pytest this script starts, not through
# PYTEST_ADDOPTS, so a pytest a test spawns does not load it.
_LAST_TEST_PLUGIN = '''
import os


def pytest_runtest_logfinish(nodeid, location):
    """A thread that dials after its test ended is charged as "after <nodeid>"."""
    os.environ["VAF_HOSTILE_LAST_TEST"] = nodeid
'''

_PHASES = (" (setup)", " (call)", " (teardown)")


def _refusal_verdict(lines, budget=_REFUSAL_BUDGET_S):
    """Sum the refusal log per test and judge each test against the budget.

    Returns (report_lines, offender_lines). A test's setup, call and teardown add
    up; "after <nodeid>" (a thread that outlived its test) is its own key and is
    judged too. Lines that do not parse (a write cut short) are skipped."""
    seconds, counts, ports, by_port = {}, {}, {}, {}
    total_n, total_s = 0, 0.0
    for raw in lines:
        parts = raw.rstrip("\n").rsplit("\t", 2)
        if len(parts) != 3:
            continue
        who, address, cost_text = parts
        try:
            cost = float(cost_text)
        except ValueError:
            continue
        for phase in _PHASES:
            if who.endswith(phase):
                who = who[:-len(phase)]
                break
        port = address.rsplit(":", 1)[-1]
        seconds[who] = seconds.get(who, 0.0) + cost
        counts[who] = counts.get(who, 0) + 1
        ports.setdefault(who, set()).add(port)
        by_port[port] = by_port.get(port, 0) + 1
        total_n += 1
        total_s += cost
    ranked = sorted(seconds, key=lambda key: (-seconds[key], key))

    def _line(key):
        return (f"{key} would wait {seconds[key]:.1f} s on a Windows runner "
                f"({counts[key]} refused connect{'' if counts[key] == 1 else 's'} to "
                f"{', '.join(sorted(ports[key], key=_port_key))})")

    report = [f"  {total_n} refused local connects, {total_s:.1f} s on a Windows runner"]
    if total_n:
        report.append("  by port: " + ", ".join(
            f"{port}: {by_port[port]}" for port in sorted(by_port, key=lambda p: (-by_port[p], p))))
        report.append("  most expensive:")
        report.extend(f"    {_line(key)}" for key in ranked[:10])
    offenders = [_line(key) for key in ranked if seconds[key] > budget]
    return report, offenders


def _port_key(port):
    return (0, int(port), "") if port.isdigit() else (1, 0, port)


_NO_DOCKER = ("npipe:////./pipe/vaf-hostile-no-docker" if os.name == "nt"
              else "unix:///nonexistent/vaf-hostile/docker.sock")


def _crlf_checkout_files() -> list:
    """The tracked files a Windows checkout with core.autocrlf holds with CRLF: text git
    stores with LF, with no eol attribute of its own, and LF in this working tree. Each as
    the normalised real path the sitecustomize compares with. Empty without git."""
    try:
        out = subprocess.run(["git", "ls-files", "--eol", "-z"], cwd=str(ROOT),
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    files = []
    for record in out.decode("utf-8", errors="replace").split("\0"):
        meta, _, rel = record.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or fields[0] != "i/lf" or fields[1] != "w/lf":
            continue
        attrs = meta[meta.index("attr/"):] if "attr/" in meta else ""
        if "eol=" in attrs or "-text" in attrs:
            continue
        files.append(os.path.normcase(os.path.realpath(ROOT / rel)))
    return files


def main() -> int:
    args = sys.argv[1:] or ["tests/", "--ignore=tests/test_gpu_inference.py", "-q"]

    with tempfile.TemporaryDirectory(prefix="vaf-hostile-") as tmp:
        real_home = Path(tmp) / "home-real"
        (real_home / ".vaf").mkdir(parents=True)
        home = Path(tmp) / "home"
        try:
            home.symlink_to(real_home, target_is_directory=True)
            linked = True
        except OSError:
            home, linked = real_home, False
        site = Path(tmp) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            _BLOCKER.format(names=repr(set(OPTIONAL_PACKAGES)), refusal_s=_WINDOWS_REFUSAL_S),
            encoding="utf-8")
        (site / "vaf_hostile_refusals.py").write_text(_LAST_TEST_PLUGIN, encoding="utf-8")
        refusal_log = Path(tmp) / "refusals.log"
        crlf_files = _crlf_checkout_files()
        crlf_list = Path(tmp) / "crlf-checkout.txt"
        crlf_list.write_text("\n".join(crlf_files), encoding="utf-8")

        env = dict(
            os.environ,
            HOME=str(home),
            USERPROFILE=str(home),
            # Tells the two tests that MEASURE this platform's home semantics that the
            # windows-home axis has replaced them on purpose.
            VAF_SIMULATED_WINDOWS_HOME="1",
            PYTHONIOENCODING="cp1252",
            PYTHONPATH=os.pathsep.join([str(site), str(ROOT)]),
            DOCKER_HOST=_NO_DOCKER,
            VAF_HOSTILE_CRLF_FILES=str(crlf_list),
            VAF_HOSTILE_REFUSAL_LOG=str(refusal_log),
            VAF_HOSTILE_REFUSE_PORTS=",".join(str(port) for port in _VAF_SERVICE_PORTS),
        )

        print("Running the suite with:")
        print(f"  narrow output   PYTHONIOENCODING=cp1252")
        print(f"  no extras       {', '.join(OPTIONAL_PACKAGES)}")
        print(f"  scratch home    {home}")
        print(f"  windows home    '~' reads USERPROFILE, not HOME")
        print(f"  linked home     " + (f"{home} -> {real_home}" if linked
                                     else "not available here (no symlinks)"))
        print(f"  no docker       DOCKER_HOST={_NO_DOCKER}")
        print(f"  old mail parser email.utils without strict parsing (3.10.11, 3.11.9)")
        print(f"  foreign os      " + ("platform.system() reports Darwin (arm64)"
                                     if sys.platform.startswith("linux")
                                     else "real host, not faked"))
        print(f"  crlf checkout   " + (f"{len(crlf_files)} tracked text files read back with CRLF "
                                     "through Path.read_bytes" if crlf_files
                                     else "not available here (no git checkout)"))
        print(f"  slow refusal    " + (
            f"a refused local connect is charged {_WINDOWS_REFUSAL_S:g} s (or the caller's "
            f"shorter timeout), at most {_REFUSAL_BUDGET_S:g} s per test; ports "
            f"{', '.join(str(port) for port in _VAF_SERVICE_PORTS)} refused without a dial"
            if os.name != "nt" else "real host, Windows pays the real wait"))
        print()
        code = subprocess.run([sys.executable, "-m", "pytest", "-p", "vaf_hostile_refusals", *args],
                              cwd=str(ROOT), env=env).returncode
        try:
            with open(refusal_log, encoding="utf-8", errors="replace") as log:
                lines = log.readlines()
        except FileNotFoundError:
            lines = []
        report, offenders = _refusal_verdict(lines)
        print()
        print(f"Slow refusal (Windows waits about {_WINDOWS_REFUSAL_S:g} s per refused local connect):")
        for line in report:
            print(line)
        for line in offenders:
            print(f"FAILED slow refusal: {line}")
        if offenders:
            print(f"{len(offenders)} test(s) over the budget of {_REFUSAL_BUDGET_S:g} s: a hot path "
                  "connects to a closed local port again and again (keep an outage memory, as "
                  "vaf/auth/permissions.py does for the account store)")
            print("A refusal through asyncio is charged the full "
                  f"{_WINDOWS_REFUSAL_S:g} s: a shorter deadline around it (asyncio.wait_for, "
                  "asyncio.timeout) is not visible to this count, so a test that sets one may "
                  "be charged more than a Windows runner would wait.")
        return code if code or not offenders else 1


if __name__ == "__main__":
    raise SystemExit(main())
