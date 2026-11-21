# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The sitecustomize scripts/hostile_env.py writes, run in a child interpreter.

A child, because the snippet patches the import system, platform, email.utils,
posixpath.expanduser and the socket layer for the whole process it runs in. Every
child gets an environment without this script's own variables and PYTHONPATH, so
the tests mean the same thing when the suite itself runs under hostile_env.
"""
import ast
import importlib.util
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

_ROOT = Path(__file__).resolve().parent.parent

_CHILD = r'''
import os, sys
snippet, target, listing = sys.argv[1:4]
# Windows' normcase lowercases; posixpath's is the identity, which hid the mismatch.
os.path.normcase = lambda s: os.fspath(s).lower()
with open(listing, "w", encoding="utf-8") as f:
    f.write(os.path.normcase(os.path.realpath(target)))
os.environ["VAF_HOSTILE_CRLF_FILES"] = listing
with open(snippet, encoding="utf-8") as f:
    exec(compile(f.read(), "sitecustomize", "exec"))
import pathlib
sys.stdout.write(pathlib.Path(target).read_bytes().hex())
'''


def _module():
    spec = importlib.util.spec_from_file_location("hostile_env", _ROOT / "scripts" / "hostile_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocker() -> str:
    return _module().sitecustomize_source(set())


def test_crlf_checkout_converts_a_mixed_case_name_under_a_lowercasing_normcase(tmp_path):
    """The CRLF CHECKOUT axis compares normcase'd paths, so its name prefilter must
    normcase the name too, or a tracked README.md is skipped on a Windows host.
    MUTATION: test `self.name` raw against the name set - red."""
    snippet = tmp_path / "sitecustomize_snippet.py"
    snippet.write_text(_blocker(), encoding="utf-8")
    target = tmp_path / "ReadMe.TXT"
    target.write_bytes(b"one\ntwo\n")
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(snippet), str(target), str(tmp_path / "list.txt")],
        capture_output=True, text=True, check=True, timeout=60, env=_clean_env())
    assert bytes.fromhex(out.stdout) == b"one\r\ntwo\r\n"


def _clean_env(**extra) -> dict:
    """The whole environment minus what a surrounding hostile run injected."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("VAF_HOSTILE_")
           and key not in ("PYTHONPATH", "PYTEST_CURRENT_TEST")}
    env.update(extra)
    return env


# Which test runs where. On a Windows host the generated sitecustomize installs no
# refusal counter (the real wait is paid there), so every test that needs the counter
# carries this mark and is skipped on Windows; test_on_windows_the_axis_installs_nothing
# runs everywhere, on a POSIX host with os.name forced to "nt" in its child, and
# test_every_counter_test_is_skipped_on_windows keeps the mark from being forgotten.
# The verdict, main() and port-list tests are pure and run on every OS.
needs_counter = pytest.mark.skipif(
    os.name == "nt", reason="the refusal counter is not installed on a Windows host")


# The SLOW REFUSAL axis. `pre` runs before the snippet is installed, `post` after;
# `closed_port()` is a port nothing holds: bound, read and closed again. A socket kept
# bound without listen() is refused at once on Linux but times out on macOS (the
# UNLISTENED PORT axis), so it is no closed port on every host.
_REFUSAL_CHILD = r'''
import asyncio, errno, json, os, socket, subprocess, sys, time
snippet, pre, post = sys.argv[1:4]


def closed_port():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()
    return None, port


def refused(sock, address):
    return reason(sock, address) is not None


def reason(sock, address):
    """The refusal's message, or None when the connect went through."""
    try:
        sock.connect(address)
    except ConnectionRefusedError as exc:
        return str(exc)
    return None


exec(pre)
with open(snippet, encoding="utf-8") as f:
    exec(compile(f.read(), "sitecustomize", "exec"))
exec(post)
'''

_TEST_NAME = "tests/test_example.py::test_pays (call)"


def _refusal_run(tmp_path, post, pre="", **env):
    snippet = tmp_path / "sitecustomize_snippet.py"
    snippet.write_text(_blocker(), encoding="utf-8")
    log = tmp_path / "refusals.log"
    env.setdefault("VAF_HOSTILE_REFUSAL_LOG", str(log))
    out = subprocess.run(
        [sys.executable, "-c", _REFUSAL_CHILD, str(snippet), pre, post],
        capture_output=True, text=True, timeout=60,
        env=_clean_env(PYTEST_CURRENT_TEST=_TEST_NAME, **env))
    assert out.returncode == 0, out.stderr
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return out.stdout, [line.split("\t") for line in lines]


@needs_counter
def test_a_refused_local_connect_is_charged_what_windows_waits(tmp_path):
    """A blocking refusal costs about 2 s on a Windows runner, or the caller's shorter
    connect timeout; it is counted, not slept, and a connect that succeeds costs nothing.
    ::1 counts like 127.0.0.1 ("localhost" is tried on ::1 first).
    MUTATION: drop the min() with the timeout - red (0.5 charged 2.0); drop the connect
    wrapper - red (no line); sleep the charge - red (elapsed); count IPv4 loopback
    only - red (no ::1 line)."""
    stdout, lines = _refusal_run(tmp_path, r'''
holder, port = closed_port()
started = time.monotonic()
for timeout in (None, 0.5):
    s = socket.socket()
    s.settimeout(timeout)
    assert refused(s, ("127.0.0.1", port))
    s.close()
assert socket.socket().connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
socket.create_connection(server.getsockname(), timeout=5).close()
print(time.monotonic() - started)
try:
    holder6 = socket.socket(socket.AF_INET6)
    holder6.bind(("::1", 0))
except OSError:
    print("no ipv6")
else:
    port6 = holder6.getsockname()[1]
    holder6.close()
    assert refused(socket.socket(socket.AF_INET6), ("::1", port6))
''')
    elapsed, *ipv6 = stdout.split()
    assert float(elapsed) < 1.0
    expected = [(_TEST_NAME, "2.0"), (_TEST_NAME, "0.5"), (_TEST_NAME, "2.0")]
    if not ipv6:
        expected.append((_TEST_NAME, "2.0"))
        assert lines[-1][1].startswith("::1:")
    assert [(who, cost) for who, _addr, cost in lines] == expected
    assert all(addr.startswith("127.0.0.1:") for _who, addr, _cost in lines[:3])


@needs_counter
def test_an_asyncio_refusal_is_charged_once(tmp_path):
    """asyncio dials a non-blocking socket inside sock_connect: one refusal, one line,
    whether the kernel reports it later (Linux, through SO_ERROR) or at once from the
    non-blocking connect itself (the stand-in plain connect of the second run).
    MUTATION: drop the gettimeout() != 0.0 guard - red (two lines in the second run);
    drop the sock_connect wrapper - red (none)."""
    dial = r'''
holder, port = closed_port()


async def dial():
    try:
        await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return
    raise SystemExit("connected to a closed port")


asyncio.run(dial())
'''
    _stdout, lines = _refusal_run(tmp_path, dial)
    assert [(who, cost) for who, _addr, cost in lines] == [(_TEST_NAME, "2.0")]

    at_once = tmp_path / "at-once"
    at_once.mkdir()
    _stdout, lines = _refusal_run(at_once, dial, pre=r'''
def _refuse_at_once(self, address):
    raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")


socket.socket.connect = _refuse_at_once
''')
    assert [(who, cost) for who, _addr, cost in lines] == [(_TEST_NAME, "2.0")]


@needs_counter
@pytest.mark.parametrize("setup", ["as found", "no dual-stack", "ipv6 listener fails"])
def test_a_vaf_service_port_is_refused_without_a_dial(tmp_path, setup):
    """A port on the service list is refused before any SYN leaves, in every spelling of
    a local address: a listener of the test's own on it accepts nothing, blocking,
    through connect_ex or through asyncio. (A free port of the test's own stands in for
    the list; the real ports are never dialed here.) The alias comes from a hosts file of
    the test's own, so no name is looked up. The listener is dual-stack only where the
    host really offers it; the two other setups take that away (the runtime probe says
    no, or the IPv6 listener fails) and the IPv4 spellings must still be refused.
    MUTATION: drop the forced check in the connect or connect_ex wrapper - red; drop
    the numeric parse of "0"/"127.1", the IPv4-mapped form (judged with the older
    ipaddress answer), the wildcard, the trailing dot, the hosts-file names, the host
    name, the .localhost suffix, "" or bytes - red (that spelling connects, or is looked
    up); decide the listener by socket.has_ipv6, or let its failure escape - red."""
    hosts = tmp_path / "hosts"
    hosts.write_text("# a comment\n127.0.0.1 vaf-hostile-alias.example\n"
                     "192.0.2.1 vaf-remote.example\n", encoding="utf-8")
    stdout, lines = _refusal_run(tmp_path, VAF_HOSTILE_HOSTS_FILE=str(hosts),
                                 pre=f"SETUP = {setup!r}\n" + r'''
if SETUP == "no dual-stack":
    socket.has_dualstack_ipv6 = lambda: False
elif SETUP == "ipv6 listener fails":
    _create_server = socket.create_server

    def _no_ipv6(address, *, family=socket.AF_INET, **options):
        if family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
        return _create_server(address, family=family, **options)

    socket.create_server = _no_ipv6
dual = False
if socket.has_dualstack_ipv6():
    try:
        server = socket.create_server(("::", 0), family=socket.AF_INET6, backlog=32,
                                      dualstack_ipv6=True)
        dual = True
    except OSError:
        pass
if not dual:
    server = socket.create_server(("127.0.0.1", 0), backlog=32)
server.settimeout(0.5)
port = server.getsockname()[1]
os.environ["VAF_HOSTILE_REFUSE_PORTS"] = "1," + str(port)
# The answer of older patch releases: an IPv4-mapped loopback is not loopback. Current
# 3.12 and 3.13 say it is, which would hide a missing mapped-address branch.
import ipaddress
ipaddress.IPv6Address.is_loopback = property(lambda self: self._ip == 1)
''', post=r'''
spellings = ["127.0.0.1", "localhost", "localhost.", "LOCALHOST", "0", "0.0.0.0", "127.1",
             "", b"127.0.0.1", socket.gethostname(), "vaf.localhost",
             "vaf-hostile-alias.example", "vaf-hostile-alias.example."]
for host in spellings:
    assert refused(socket.socket(), (host, port)), host
v6 = ["::1", "::1%1", "::ffff:127.0.0.1", "::ffff:7f00:1", "::"] if dual else []
for host in v6:
    assert refused(socket.socket(socket.AF_INET6), (host, port)), host
assert socket.socket().connect_ex(("localhost", port)) == errno.ECONNREFUSED


async def dial():
    try:
        await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return
    raise SystemExit("asyncio reached the service port")


asyncio.run(dial())
try:
    server.accept()
except TimeoutError:
    print("nothing arrived", len(spellings) + len(v6) + 2, "dual" if dual else "ipv4")
''')
    word1, word2, attempts, listener = stdout.split()
    assert (word1, word2) == ("nothing", "arrived")
    if setup != "as found":
        assert listener == "ipv4"
    assert [cost for _who, _addr, cost in lines] == ["2.0"] * int(attempts)


@needs_counter
def test_unix_udp_and_remote_refusals_are_not_charged(tmp_path):
    """Only a TCP refusal to a local address is a Windows wait. A plain connect and
    connect_ex that refuse everything stand in for the network, so a remote refusal needs
    no network; the local TCP controls prove the stand-ins reach the counter. A remote
    host on a service port reaches the plain connect (it is no service of this machine),
    a non-blocking connect_ex is asyncio's business, and asyncio's own remote refusal is
    not charged either. Without the log variable nothing is wrapped at all.
    MUTATION: drop the family/type check - red; treat every host as local - red;
    refuse a service port without the local check - red (the remote one gets the forced
    message); count a non-blocking connect_ex - red; charge a remote sock_connect - red;
    install the wrappers without the log - red; keep a hosts-file name whatever its
    address - red (the remote name gets the forced message); read the hosts file
    without the size limit - red (the name past it is charged)."""
    hosts = tmp_path / "hosts"
    hosts.write_text("192.0.2.1 vaf-remote.example\n" + ("#" + "x" * 99 + "\n") * 11000
                     + "127.0.0.1 vaf-past-the-limit.example\n", encoding="utf-8")
    _stdout, lines = _refusal_run(tmp_path, VAF_HOSTILE_HOSTS_FILE=str(hosts), pre=r'''
os.environ["VAF_HOSTILE_REFUSE_PORTS"] = "80"


def _refuse_all(self, address):
    raise ConnectionRefusedError(errno.ECONNREFUSED, "stand-in refusal")


socket.socket.connect = _refuse_all
socket.socket.connect_ex = lambda self, address: errno.ECONNREFUSED
''', post=r'''
assert reason(socket.socket(), ("192.0.2.1", 80)) == "[Errno %d] stand-in refusal" % errno.ECONNREFUSED
assert refused(socket.socket(), ("example.invalid", 81))
assert reason(socket.socket(), ("vaf-remote.example", 80)) == "[Errno %d] stand-in refusal" % errno.ECONNREFUSED
assert refused(socket.socket(), ("vaf-past-the-limit.example", 81))
assert refused(socket.socket(socket.AF_INET, socket.SOCK_DGRAM), ("127.0.0.1", 81))
if hasattr(socket, "AF_UNIX"):
    assert refused(socket.socket(socket.AF_UNIX), "/nonexistent/vaf.sock")
nonblocking = socket.socket()
nonblocking.setblocking(False)
assert nonblocking.connect_ex(("127.0.0.1", 82)) == errno.ECONNREFUSED


async def dial():
    try:
        await asyncio.open_connection("192.0.2.1", 83)
    except ConnectionRefusedError:
        return
    raise SystemExit("connected")


asyncio.run(dial())
assert refused(socket.socket(), ("127.0.0.1", 80))
assert socket.socket().connect_ex(("127.0.0.1", 81)) == errno.ECONNREFUSED
''')
    assert [addr for _who, addr, _cost in lines] == ["127.0.0.1:80", "127.0.0.1:81"]

    unlogged = tmp_path / "unlogged"
    unlogged.mkdir()
    stdout, lines = _refusal_run(unlogged, VAF_HOSTILE_REFUSAL_LOG="", pre=r'''
import asyncio.selector_events as se
plain = (socket.socket.connect, socket.socket.connect_ex, se.BaseSelectorEventLoop.sock_connect)
''', post=r'''
print((socket.socket.connect, socket.socket.connect_ex,
       se.BaseSelectorEventLoop.sock_connect) == plain)
''')
    assert stdout.strip() == "True" and lines == []


@needs_counter
def test_the_hosts_file_is_read_only_for_a_name(tmp_path):
    """Every interpreter of the run loads the snippet, so the hosts file (megabytes on a
    blocklist machine) is read only when a name that is no literal address is judged.
    MUTATION: read the hosts file when the snippet installs - red."""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 vaf-hostile-alias.example\n", encoding="utf-8")
    stdout, _lines = _refusal_run(tmp_path, VAF_HOSTILE_HOSTS_FILE=str(hosts), pre=r'''
import builtins
opened = []
_open = builtins.open


def _recording_open(file, *args, **kwargs):
    opened.append(os.fspath(file) if not isinstance(file, int) else file)
    return _open(file, *args, **kwargs)


builtins.open = _recording_open
socket.socket.connect = lambda self, address: None
''', post=r'''
hosts = os.environ["VAF_HOSTILE_HOSTS_FILE"]
socket.socket().connect(("127.0.0.1", 9))
socket.socket().connect(("0", 9))
print(hosts in opened)
socket.socket().connect(("vaf-hostile-alias.example", 9))
print(hosts in opened)
''')
    assert stdout.split() == ["False", "True"]


@needs_counter
def test_a_name_cut_by_the_read_limit_is_not_local(tmp_path):
    """When the read limit of the hosts file falls inside a record, the cut record is
    dropped: "127.0.0.1 api" cut from api.internal must not make "api" local, while the
    complete rows before it still count.
    MUTATION: keep the text up to the limit as it is - red ("api" is force-refused)."""
    limit = _blocker().split("_HOSTS_READ_LIMIT = ", 1)[1].split("\n", 1)[0]
    limit = eval(limit, {})        # the literal expression in the snippet, e.g. 1 << 20
    head = "127.0.0.1 vaf-before.example\n"
    cut = "127.0.0.1 api"
    room = limit - len(head) - len(cut)
    padding = ("#" + "x" * 98 + "\n") * (room // 100)
    padding += "#" + "x" * (room - len(padding) - 2) + "\n"
    hosts = tmp_path / "hosts"
    hosts.write_text(head + padding + cut + ".internal\n", encoding="utf-8", newline="\n")
    assert len(head + padding + cut) == limit
    stdout, lines = _refusal_run(tmp_path, VAF_HOSTILE_HOSTS_FILE=str(hosts), pre=r"""
os.environ["VAF_HOSTILE_REFUSE_PORTS"] = "80"


def _refuse_all(self, address):
    raise ConnectionRefusedError(errno.ECONNREFUSED, "stand-in refusal")


socket.socket.connect = _refuse_all
""", post=r"""
print(reason(socket.socket(), ("api", 80)))
print(reason(socket.socket(), ("vaf-before.example", 80)))
""")
    cut_reason, before_reason = stdout.splitlines()
    assert cut_reason.endswith("stand-in refusal")
    assert "never dialed" in before_reason
    assert [addr for _who, addr, _cost in lines] == ["vaf-before.example:80"]


def test_on_windows_the_axis_installs_nothing(tmp_path):
    """A Windows host pays the real wait, so the generated sitecustomize installs no
    counter there and writes no log; main() then judges an empty log, which passes.
    Runs on every OS: on a POSIX host the child forces os.name to "nt" before the
    snippet runs, so the Windows branch is exercised here and not first on the runner.
    MUTATION: drop the os.name check from the install condition - red."""
    force_nt = "" if os.name == "nt" else "os.name = 'nt'\n"
    stdout, lines = _refusal_run(tmp_path, pre=force_nt + r'''
import asyncio.selector_events as se
plain = (socket.socket.connect, socket.socket.connect_ex, se.BaseSelectorEventLoop.sock_connect)
''', post=r'''
print((socket.socket.connect, socket.socket.connect_ex,
       se.BaseSelectorEventLoop.sock_connect) == plain)
''')
    assert stdout.strip() == "True" and lines == []
    assert _module()._refusal_verdict([]) == (
        ["  0 refused local connects, 0.0 s on a Windows runner"], [])


def test_every_counter_test_is_skipped_on_windows():
    """A test that needs the counter fails on the Windows runner, where nothing is
    installed, unless it carries needs_counter. Static, so it fails on any OS.
    MUTATION: remove the mark from one counter test - red; recognise a counter test
    only by _refusal_run or "sitecustomize.py" - red (the synthetic one passes)."""
    assert _unmarked_counter_tests(Path(__file__).read_text(encoding="utf-8")) == []
    # A test that builds its own child from _blocker() and sets the log is one too.
    assert _unmarked_counter_tests(
        "def test_own_child(tmp_path):\n"
        "    env = {'VAF_HOSTILE_REFUSAL_LOG': str(tmp_path)}\n"
        "    run(_blocker(), env)\n") == ["test_own_child"]


def _unmarked_counter_tests(source):
    tree = ast.parse(source)
    missing = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if node.name in ("test_on_windows_the_axis_installs_nothing",
                         "test_every_counter_test_is_skipped_on_windows"):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        texts = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str)}
        texts |= {k.arg for k in ast.walk(node) if isinstance(k, ast.keyword)}
        runs_counter = ("_refusal_run" in names or "sitecustomize.py" in texts
                        or ("_blocker" in names and "VAF_HOSTILE_REFUSAL_LOG" in texts))
        marked = any(isinstance(d, ast.Name) and d.id == "needs_counter"
                     for d in node.decorator_list)
        if runs_counter and not marked:
            missing.append(node.name)
    return missing


@needs_counter
def test_the_axis_survives_monkeypatches_children_and_finished_tests(tmp_path):
    """The real sitecustomize, loaded by the interpreter from PYTHONPATH: a test that
    patches socket.socket.connect and restores it leaves the counter in place, a child
    process charges the test that started it, and a refusal after the test ended is
    charged "after" the last finished test; with neither name it is "outside any test".
    MUTATION: pop VAF_HOSTILE_REFUSAL_LOG from the environment after reading it - red
    (the child charges nothing); empty the plugin's hook - red ("outside any test");
    drop the fallback key - red."""
    module = _module()
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_blocker(), encoding="utf-8")
    (site / "vaf_hostile_refusals.py").write_text(module._LAST_TEST_PLUGIN, encoding="utf-8")
    log = tmp_path / "refusals.log"
    child = r'''
import os, socket, subprocess, sys
assert "sitecustomize" in sys.modules
holder = socket.socket()
holder.bind(("127.0.0.1", 0))
port = holder.getsockname()[1]
holder.close()                  # nothing holds it: refused on every host
counted = socket.socket.connect
socket.socket.connect = lambda self, address: None
socket.socket.connect = counted
assert socket.socket().connect_ex(("127.0.0.1", port)) != 0
assert subprocess.run([sys.executable, "-c",
    "import socket, sys; sys.exit(socket.socket().connect_ex(('127.0.0.1', %d)) == 0)" % port],
    env=dict(os.environ)).returncode == 0
import vaf_hostile_refusals
del os.environ["PYTEST_CURRENT_TEST"]
vaf_hostile_refusals.pytest_runtest_logfinish(nodeid="tests/test_example.py::test_pays", location=None)
assert socket.socket().connect_ex(("127.0.0.1", port)) != 0
bare = dict(os.environ)
del bare["VAF_HOSTILE_LAST_TEST"]
assert subprocess.run([sys.executable, "-c",
    "import socket, sys; sys.exit(socket.socket().connect_ex(('127.0.0.1', %d)) == 0)" % port],
    env=bare).returncode == 0
'''
    out = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, timeout=60,
        env=_clean_env(PYTHONPATH=str(site), VAF_HOSTILE_REFUSAL_LOG=str(log),
                       PYTEST_CURRENT_TEST=_TEST_NAME))
    assert out.returncode == 0, out.stderr
    who = [line.split("\t")[0] for line in log.read_text(encoding="utf-8").splitlines()]
    assert who == [_TEST_NAME, _TEST_NAME, "after tests/test_example.py::test_pays",
                   "outside any test"]


@needs_counter
@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the axis simulates macOS on Linux; elsewhere the host's own timeout "
                           "applies (a 0.5 s deadline raises TimeoutError without an errno)")
def test_a_bound_port_that_does_not_listen_times_out_as_on_macos(tmp_path):
    """A socket that holds a port bound without listen(): macOS lets a connect to it time
    out, blocking, through connect_ex and through asyncio, and nothing is charged (it is no
    refusal). A holder that set SO_REUSEADDR is a holder too, and a short form names its own
    address (127.2 is 127.0.0.2). A port nothing holds is refused and charged as before, and
    so is one a TIME_WAIT reserves.
    MUTATION: drop the held-port check in the connect wrapper - red (refused); in
    connect_ex - red (ECONNREFUSED); in sock_connect - red (refused); probe with
    SO_REUSEADDR - red (the reusing holder is refused); drop the kernel-table check - red
    (the TIME_WAIT times out); drop the inet_aton reading - red (127.2 probes 127.0.0.1)."""
    stdout, lines = _refusal_run(tmp_path, r'''
def times_out(sock, address):
    try:
        sock.connect(address)
    except TimeoutError as exc:
        return exc.errno == errno.ETIMEDOUT
    return False


holder = socket.socket()
holder.bind(("127.0.0.1", 0))
port = holder.getsockname()[1]
s = socket.socket()
s.settimeout(0.5)
assert times_out(s, ("127.0.0.1", port))
assert socket.socket().connect_ex(("127.0.0.1", port)) == errno.ETIMEDOUT


async def dial():
    try:
        await asyncio.open_connection("127.0.0.1", port)
    except TimeoutError:
        return "timed out"
    except ConnectionRefusedError:
        return "refused"


print(asyncio.run(dial()))
holder.close()
assert refused(socket.socket(), ("127.0.0.1", port))

reusing = socket.socket()
reusing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
reusing.bind(("127.0.0.1", 0))
assert times_out(socket.socket(), ("127.0.0.1", reusing.getsockname()[1])), "reusing holder"
reusing.close()

two = socket.socket()
two.bind(("127.0.0.2", 0))
assert times_out(socket.socket(), ("127.2", two.getsockname()[1])), "short form"
two.close()

server = socket.socket()                # no SO_REUSEADDR: its TIME_WAIT conflicts with a bind
server.bind(("127.0.0.1", 0))
server.listen(1)
waiting = server.getsockname()[1]
client = socket.create_connection(("127.0.0.1", waiting))
accepted, _ = server.accept()
accepted.close()                        # the server's end closes first and goes to TIME_WAIT
client.close()
server.close()
assert refused(socket.socket(), ("127.0.0.1", waiting)), "a TIME_WAIT is no holder"
''')
    assert stdout.split() == ["timed", "out"]
    assert [(who, cost) for who, _addr, cost in lines] == [(_TEST_NAME, "2.0")] * 2


def test_the_verdict_sums_phases_and_names_the_test():
    """setup + call + teardown of one test add up; each key over the budget is named,
    the most expensive first; a thread that outlived its test is judged as well.
    MUTATION: key on the raw name with its phase - red (no test reaches 61 s); rank by
    name - red; cut the top list at 11 - red."""
    verdict = _module()._refusal_verdict
    lines = (["t.py::a (setup)\t127.0.0.1:5432\t2.0\n"] * 15
             + ["t.py::a (call)\t::1:5432\t2.0\n"] * 15
             + ["t.py::a (teardown)\t127.0.0.1:8001\t0.5\n"] * 2
             + ["t.py::b (call)\t127.0.0.1:5432\t2.0\n"] * 29 + ["t.py::b (call)\t127.0.0.1:5432\t1.0\n"]
             + ["after t.py::c\t127.0.0.1:8001\t2.0\n"] * 40
             + ["t.py::d (call)\t127.0.0.1:9222\t2.0\n"]
             + ["cut short\n"])
    report, offenders = verdict(lines, budget=60.0)
    assert offenders == [
        "after t.py::c would wait 80.0 s on a Windows runner (40 refused connects to 8001)",
        "t.py::a would wait 61.0 s on a Windows runner (32 refused connects to 5432, 8001)",
    ]
    assert report[0] == "  103 refused local connects, 202.0 s on a Windows runner"
    assert report[1] == "  by port: 5432: 60, 8001: 42, 9222: 1"
    assert "t.py::d would wait 2.0 s on a Windows runner (1 refused connect to 9222)" in report[-1]
    assert "t.py::b would wait 59.0 s" in report[-2]
    assert verdict([], budget=60.0) == (["  0 refused local connects, 0.0 s on a Windows runner"], [])

    # Ranked by cost, not by name, and cut at ten: k11 is the dearest and sorts last.
    many = [f"t.py::k{i:02d} (call)\t127.0.0.1:5432\t{i + 1}.0\n" for i in range(12)]
    report, offenders = verdict(many, budget=10.5)
    ranked = [line.split()[0] for line in report[3:]]
    assert ranked == [f"t.py::k{i:02d}" for i in range(11, 1, -1)]
    assert [line.split()[0] for line in offenders] == ["t.py::k11", "t.py::k10"]


def test_main_fails_a_green_run_over_budget(tmp_path, monkeypatch, capsys):
    """A test over the budget fails a run pytest called green and is named; pytest's own
    failure is passed through unchanged; under the budget, or with no log at all, the
    run stays green.
    MUTATION: return pytest's code regardless of the offenders - red; read the log
    without the missing-file fallback - red; drop the asyncio deadline hint - red."""
    module = _module()
    monkeypatch.setattr(module, "_crlf_checkout_files", lambda: [])
    monkeypatch.setattr(module, "_venv_launcher", _no_launcher)
    monkeypatch.setattr(sys, "argv", ["hostile_env.py", "tests/test_example.py"])
    seen = []

    def run(pytest_rc, n_lines):
        def fake_run(argv, cwd, env):
            seen.append(argv)
            site = Path(env["PYTHONPATH"].split(os.pathsep)[0])
            assert (site / "vaf_hostile_refusals.py").is_file()
            assert "8443" in env["VAF_HOSTILE_REFUSE_PORTS"].split(",")
            if n_lines is not None:     # None: the run left no log at all
                with open(env["VAF_HOSTILE_REFUSAL_LOG"], "a", encoding="utf-8") as log:
                    log.write("tests/test_example.py::test_pays (call)\t127.0.0.1:5432\t2.0\n"
                              * n_lines)
            return SimpleNamespace(returncode=pytest_rc)
        monkeypatch.setattr(module.subprocess, "run", fake_run)
        return module.main()

    assert run(0, 31) == 1
    out = capsys.readouterr().out
    assert ("FAILED slow refusal: tests/test_example.py::test_pays would wait 62.0 s"
            in out)
    assert "asyncio.wait_for" in out          # the deadline the count cannot see
    assert seen[-1][-3:] == ["-p", "vaf_hostile_refusals", "tests/test_example.py"]
    assert run(2, 31) == 2
    capsys.readouterr()
    assert run(0, 30) == 0
    assert "FAILED slow refusal" not in capsys.readouterr().out
    assert run(0, 0) == 0
    capsys.readouterr()
    assert run(0, None) == 0
    assert "  0 refused local connects" in capsys.readouterr().out


def _no_launcher(directory, real):
    return None, "not available here (stubbed)"


def _child_env(module, monkeypatch, platform, tunables, launcher=_no_launcher, argv=None):
    """The environment main() hands pytest, on `platform`, with GLIBC_TUNABLES set to
    `tunables` in this process (None: unset); subprocess.run is faked, nothing runs. The
    venv launcher comes from `launcher` (a stand-in for _venv_launcher); `argv`, a list,
    receives the command main() ran."""
    monkeypatch.setattr(module, "_crlf_checkout_files", lambda: [])
    monkeypatch.setattr(module, "_venv_launcher", launcher)
    monkeypatch.setattr(sys, "argv", ["hostile_env.py", "tests/test_example.py"])
    monkeypatch.setattr(module.sys, "platform", platform)
    # A surrounding hostile run exports these; main() must be seen setting them itself.
    monkeypatch.delenv("VAF_HOSTILE_VENV_LAUNCHER", raising=False)
    monkeypatch.delenv("VAF_HOSTILE_REAL_PYTHON", raising=False)
    if tunables is None:
        monkeypatch.delenv("GLIBC_TUNABLES", raising=False)
    else:
        monkeypatch.setenv("GLIBC_TUNABLES", tunables)
    seen = []

    def fake_run(command, cwd, env):
        seen.append(dict(env))
        if argv is not None:
            argv[:] = command
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == 0
    return seen[-1]


def test_the_large_allocs_axis_pins_the_mmap_threshold_on_linux(monkeypatch, capsys):
    """On Linux the child pytest runs with glibc's mmap threshold pinned at 32 KiB, and a
    GLIBC_TUNABLES already set is kept, the threshold appended after glibc's ':'.
    MUTATION: drop the axis - red; overwrite the value already set instead of appending -
    red; drop its line from the announcement - red."""
    module = _module()
    env = _child_env(module, monkeypatch, "linux", None)
    assert env["GLIBC_TUNABLES"] == "glibc.malloc.mmap_threshold=32768"
    assert "  large allocs    glibc mmap threshold pinned at 32 KiB" in capsys.readouterr().out
    env = _child_env(module, monkeypatch, "linux", "glibc.foo=1")
    assert env["GLIBC_TUNABLES"] == "glibc.foo=1:glibc.malloc.mmap_threshold=32768"
    capsys.readouterr()


def test_the_large_allocs_axis_is_linux_only(monkeypatch, capsys):
    """macOS has its own allocator and Windows no glibc: there the variable is left as the
    caller had it, and the axis says the host is real.
    MUTATION: set the variable on every platform - red."""
    module = _module()
    for platform in ("darwin", "win32"):
        env = _child_env(module, monkeypatch, platform, None)
        assert "GLIBC_TUNABLES" not in env, platform
        assert "  large allocs    real host, not simulated" in capsys.readouterr().out
    env = _child_env(module, monkeypatch, "darwin", "glibc.foo=1")
    assert env["GLIBC_TUNABLES"] == "glibc.foo=1"
    capsys.readouterr()


def test_one_local_address_predicate_serves_the_snippet_and_the_conftest():
    """tests/conftest.py refuses the service ports through `local_address`, and the generated
    sitecustomize through `_local`; both are the one `_LOCAL_ADDRESS_SOURCE`, so the snippet
    tests above pin the conftest's answer too. Every spelling here is numeric or the
    wildcard, so no name is looked up.
    MUTATION: format a different predicate into the template than the one local_address
    is built from - red (the generated text no longer holds the shared source)."""
    module = _module()
    local = module.local_address
    for host in ("127.0.0.1", "127.1", "0", "", "::1", "::ffff:127.0.0.1", b"127.0.0.1"):
        assert local((host, 1)), host
    for host in ("192.0.2.1", "2001:db8::1"):
        assert not local((host, 1)), host
    assert not local("/run/vaf.sock")
    shared = "\n".join(("    " + line) if line.strip() else ""
                       for line in module._LOCAL_ADDRESS_SOURCE.strip("\n").splitlines())
    assert "def _local(address):" in shared
    assert shared in _blocker()


def test_every_port_the_door_harness_avoids_is_refused():
    """tests/door_harness.py names the ports a running VAF may listen on; the hostile
    run must refuse each of them without a dial.
    MUTATION: remove 8443 from _VAF_SERVICE_PORTS - red."""
    text = (_ROOT / "tests" / "door_harness.py").read_text(encoding="utf-8")
    named = re.search(r"Nothing here dials ([0-9, or]+), where", text).group(1)
    ports = {int(port) for port in re.findall(r"[0-9]+", named)}
    assert ports == {3000, 8001, 8005, 8443}
    assert ports | {5432, 8080, 9222} <= set(_module()._VAF_SERVICE_PORTS)


# The LONG ENV VALUE axis.

# Runs the snippet, then sets variables of a given size through os.environ (the path pytest's
# PYTEST_CURRENT_TEST takes) and os.putenv with bytes (what os.environ hands it on POSIX).
_ENV_CHILD = r'''
import os, subprocess, sys
snippet, force_nt = sys.argv[1:3]
plain = os.putenv
if force_nt == "1":
    os.name = "nt"
with open(snippet, encoding="utf-8") as f:
    exec(compile(f.read(), "sitecustomize", "exec"))
if force_nt == "1":
    print(os.putenv is plain)
    raise SystemExit(0)
NAME = "VAF_X"
ROOM = 32767 - len(NAME) - 1            # value length that makes name=value 32767 units


def refused(value):
    try:
        os.environ[NAME] = value
    except ValueError as exc:
        assert str(exc) == "the environment variable is longer than 32767 characters", exc
        return "refused"
    return "set"


print(refused("a" * ROOM))                              # 32767 units
seen = subprocess.run([sys.executable, "-c", "import os; print(len(os.environ['VAF_X']))"],
                      capture_output=True, text=True).stdout.strip()
print(seen)                                             # the real environment holds it
print(refused("a" * (ROOM + 1)))                        # 32768 units
print(refused("a" * (ROOM - 1) + "\U0001F600"))         # 32766 + a pair = 32768 units
print(refused("a" * (ROOM - 2) + "\U0001F600"))         # 32765 + a pair = 32767 units
print(refused("\u200b" * ROOM))                    # BMP: one unit each, 32767
try:
    os.putenv(NAME.encode(), b"a" * (ROOM + 1))
except ValueError:
    print("refused")
else:
    print("set")
'''


def _env_child(tmp_path, force_nt=False):
    snippet = tmp_path / "sitecustomize_snippet.py"
    snippet.write_text(_blocker(), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-c", _ENV_CHILD, str(snippet), "1" if force_nt else "0"],
        capture_output=True, text=True, timeout=60, env=_clean_env())
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


@pytest.mark.skipif(os.name == "nt", reason="a Windows host has the real limit, nothing is installed")
def test_the_long_env_axis_refuses_what_windows_refuses(tmp_path, monkeypatch, capsys):
    """Windows' putenv refuses name=value above 32767 UTF-16 code units with this text
    (CPython's win32_putenv), pytest's PYTEST_CURRENT_TEST included: a test id over the
    limit errored at setup and teardown on the Windows runner alone. 32767 is set, and
    really reaches the environment; 32768 is refused; a character outside the BMP counts
    twice, one inside it once; bytes from os.environ's POSIX path are measured decoded.
    MUTATION: drop the wrapper - red; `>=` for `>` - red (32767 refused); count code points
    instead of UTF-16 units - red (the pair passes); drop the announcement line - red."""
    assert _env_child(tmp_path) == ["set", "32761", "refused", "refused", "set", "set",
                                    "refused"]
    _child_env(_module(), monkeypatch, "linux", None)
    assert ("  long env value  a variable whose name=value exceeds 32767 UTF-16 units is "
            "refused") in capsys.readouterr().out


def test_the_long_env_axis_is_not_installed_on_windows(tmp_path):
    """On a Windows host the real limit applies, so os.putenv stays the host's own. Runs on
    every OS: the child forces os.name to "nt" before the snippet runs.
    MUTATION: drop the os.name check from the install condition - red."""
    assert _env_child(tmp_path, force_nt=True) == ["True"]



def test_the_last_test_plugin_survives_an_id_over_the_limit(tmp_path):
    """The hostile run's own plugin writes each finished test's id into the environment;
    an id over Windows' limit stopped the whole session there (INTERNALERROR) under the
    long env value axis. The start of the id is kept instead.
    MUTATION: set the full id without the fallback - red."""
    module = _module()
    snippet = tmp_path / "sitecustomize_snippet.py"
    snippet.write_text(_blocker(), encoding="utf-8")
    plugin = tmp_path / "plugin.py"
    plugin.write_text(module._LAST_TEST_PLUGIN, encoding="utf-8")
    code = r'''
import os, sys
exec(compile(open(sys.argv[1], encoding="utf-8").read(), "sitecustomize", "exec"))
namespace = {}
exec(compile(open(sys.argv[2], encoding="utf-8").read(), "plugin", "exec"), namespace)
namespace["pytest_runtest_logfinish"](nodeid="t.py::test_a[" + "x" * 40000 + "]", location=None)
print(os.environ["VAF_HOSTILE_LAST_TEST"] == "t.py::test_a[" + "x" * 4083)
namespace["pytest_runtest_logfinish"](nodeid="t.py::test_b", location=None)
print(os.environ["VAF_HOSTILE_LAST_TEST"])
'''
    out = subprocess.run([sys.executable, "-c", code, str(snippet), str(plugin)],
                         capture_output=True, text=True, timeout=60, env=_clean_env())
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "t.py::test_b"]


# The VENV LAUNCHER axis.

needs_launcher = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("cc") is None,
    reason="the venv launcher is built with a C compiler (cc), on Linux only")

# Inside a hostile run sys.executable is that run's own launcher; these tests build theirs
# around the real interpreter.
_REAL_PYTHON = os.environ.get("VAF_HOSTILE_REAL_PYTHON") or sys.executable


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    path, why = _module()._venv_launcher(tmp_path_factory.mktemp("venv"), _REAL_PYTHON)
    assert path is not None, why
    return str(path)


def _wait_for(check, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


# Writes a line, closes its output, says so through a file and lives on until released.
_CLOSE_AND_WAIT = r'''
import os, sys, time
os.write(1, b"x\n")
os.close(1)
open(sys.argv[1], "w").close()
while not os.path.exists(sys.argv[2]):
    time.sleep(0.02)
'''

# Writes its pid to a file (whole, through a rename) and sleeps.
_SLEEPER = r'''
import os, sys, time
with open(sys.argv[1] + ".part", "w") as f:
    f.write(str(os.getpid()))
os.rename(sys.argv[1] + ".part", sys.argv[1])
time.sleep(60)
'''


def _holds_output(python, tmp_path):
    """Whether the reader still saw no end of output once the child had closed its own."""
    ready, release = tmp_path / "ready", tmp_path / "release"
    proc = subprocess.Popen([python, "-c", _CLOSE_AND_WAIT, str(ready), str(release)],
                            stdout=subprocess.PIPE, env=_clean_env())
    try:
        assert proc.stdout.readline() == b"x\n"
        assert _wait_for(ready.exists), "the child did not start"
        held = not select.select([proc.stdout], [], [], 0.5)[0]
        release.touch()
        assert proc.stdout.read() == b""
        assert proc.wait(timeout=10) == 0
        return held
    finally:
        release.touch()
        _stop(proc)


def _sleeper(python, tmp_path, *args):
    """(the Popen, the pid of the interpreter that runs the sleeper)."""
    pid_file = tmp_path / "pid"
    proc = subprocess.Popen([python, "-c", _SLEEPER, str(pid_file), *args], env=_clean_env())
    if not _wait_for(pid_file.exists):
        _stop(proc)
        raise AssertionError("the sleeper did not start")
    return proc, int(pid_file.read_text(encoding="utf-8"))


@needs_launcher
def test_the_launcher_holds_the_output_its_child_closed(launcher, tmp_path):
    """A Windows venv's python.exe keeps the child's standard handles until the child is
    gone, so a reader sees no end of output while the child lives on; the interpreter run
    directly gives it at once.
    MUTATION: execv without the fork (no launcher process) - red."""
    (tmp_path / "direct").mkdir()
    (tmp_path / "launched").mkdir()
    assert _holds_output(_REAL_PYTHON, tmp_path / "direct") is False
    assert _holds_output(launcher, tmp_path / "launched") is True


@needs_launcher
def test_a_killed_launcher_takes_its_child_along(launcher, tmp_path):
    """Windows' job object ends the interpreter with its launcher; without that a killed
    launcher left a child holding a lock.
    MUTATION: drop PR_SET_PDEATHSIG - red (the child survives)."""
    proc, pid = _sleeper(launcher, tmp_path)
    child = psutil.Process(pid)
    try:
        assert child.ppid() == proc.pid
        proc.kill()
        proc.wait(timeout=10)
        _gone, alive = psutil.wait_procs([child], timeout=10)
        assert alive == [], "the child outlived its killed launcher"
    finally:
        _stop(proc)
        if psutil.pid_exists(pid):
            try:
                child.kill()
            except psutil.Error:
                pass


@needs_launcher
def test_the_launcher_ends_as_its_child_did(launcher, tmp_path):
    """An exit code passes through; a SIGTERM to the launcher reaches the child, and the
    child's death by a signal is re-raised, so POSIX `returncode == -SIGTERM` keeps its
    meaning; so is a signal the child sent itself.
    MUTATION: exit(1) instead of re-raising - red; do not forward SIGTERM - red (hangs to
    the deadline)."""
    assert subprocess.run([launcher, "-c", "raise SystemExit(7)"], env=_clean_env(),
                          timeout=60).returncode == 7
    proc, _pid = _sleeper(launcher, tmp_path)
    try:
        proc.terminate()
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        _stop(proc)
    assert subprocess.run([launcher, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
                          env=_clean_env(), timeout=60).returncode == -signal.SIGKILL


@needs_launcher
def test_a_child_reports_the_launcher_as_its_interpreter(launcher, tmp_path):
    """A Windows interpreter started by the launcher reports the launcher as sys.executable,
    so a Python child spawned from it goes through the launcher again. The launcher hands
    its own path to its child alone, and the child clears it once read, as Windows does with
    __PYVENV_LAUNCHER__: an interpreter started any other way (another venv's, which a
    restart test re-executes) reports itself, even as a grandchild that inherits the rest of
    the environment.
    MUTATION: drop the sys.executable line from the snippet - red; read the variable
    without removing it - red (the grandchild reports the launcher); the launcher does not
    set it - red."""
    snippet = tmp_path / "sitecustomize_snippet.py"
    snippet.write_text(_blocker(), encoding="utf-8")
    load = ("import os, subprocess, sys; exec(compile(open(sys.argv[1], encoding='utf-8')"
            ".read(), 'sitecustomize', 'exec')); ")
    grandchild = (f"import sys; exec(compile(open({str(snippet)!r}, encoding='utf-8').read(), "
                  "'sitecustomize', 'exec')); print(sys.executable)")
    code = load + ("print(sys.executable); sys.stdout.flush(); "
                   f"subprocess.run([sys.argv[2], '-c', {grandchild!r}])")

    def reported(python):
        out = subprocess.run([python, "-c", code, str(snippet), _REAL_PYTHON],
                             capture_output=True, text=True, timeout=60, env=_clean_env())
        assert out.returncode == 0, out.stderr
        return out.stdout.split()
    assert reported(launcher) == [launcher, _REAL_PYTHON]
    assert reported(_REAL_PYTHON) == [_REAL_PYTHON, _REAL_PYTHON]


@needs_launcher
def test_the_launcher_shows_its_given_argv(launcher, tmp_path):
    """Both processes carry the same arguments in the process table, as on Windows, where
    a scan by command line finds the launcher and its interpreter.
    MUTATION: execv without the fork - red (the Popen's pid is the interpreter)."""
    proc, pid = _sleeper(launcher, tmp_path, "an argument")
    try:
        shown = psutil.Process(proc.pid)
        assert shown.cmdline() == [launcher, "-c", _SLEEPER, str(tmp_path / "pid"), "an argument"]
        assert [c.pid for c in shown.children()] == [pid]
        assert psutil.Process(pid).cmdline() == [_REAL_PYTHON, *shown.cmdline()[1:]]
    finally:
        _stop(proc)


def test_the_launcher_is_built_on_linux_only(monkeypatch, tmp_path):
    """macOS has no PR_SET_PDEATHSIG and Windows the real launcher; without a C compiler
    the axis is not installed, and the announcement says why.
    MUTATION: build it on darwin or win32 - red; drop the compiler check - red."""
    module = _module()
    for platform, reason in (("darwin", "not available here (Linux only"),
                             ("win32", "real host")):
        monkeypatch.setattr(module.sys, "platform", platform)
        path, why = module._venv_launcher(tmp_path, sys.executable)
        assert path is None and why.startswith(reason), (platform, why)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert module._venv_launcher(tmp_path, sys.executable) == (
        None, "not available here (no C compiler)")
    assert not (tmp_path / "venv-launcher").exists()


def test_main_runs_pytest_through_the_launcher(monkeypatch, capsys, tmp_path):
    """With a launcher, pytest itself starts through it (on the Windows runner `python -m
    pytest` has the launcher as its parent too), which hands its path to pytest itself, and a
    Linux-only test that inspects its own child finds the real interpreter; without one,
    the interpreter runs pytest and the announcement gives the reason.
    MUTATION: run pytest with sys.executable regardless - red; drop the exported real
    interpreter - red; drop the announcement line - red."""
    module = _module()
    fake = str(tmp_path / "venv-launcher" / "python")
    argv = []
    env = _child_env(module, monkeypatch, "linux", None,
                     launcher=lambda directory, real: (Path(fake), None), argv=argv)
    assert argv[0] == fake
    # The launcher hands its path to its own child; exported, it would reach interpreters
    # it never started.
    assert "VAF_HOSTILE_VENV_LAUNCHER" not in env
    assert env["VAF_HOSTILE_REAL_PYTHON"] == sys.executable
    assert (f"  venv launcher   Python runs as two processes through {fake}"
            in capsys.readouterr().out)
    env = _child_env(module, monkeypatch, "linux", None, argv=argv)
    assert argv[0] == sys.executable
    assert "VAF_HOSTILE_VENV_LAUNCHER" not in env and "VAF_HOSTILE_REAL_PYTHON" not in env
    assert "  venv launcher   not available here (stubbed)" in capsys.readouterr().out
