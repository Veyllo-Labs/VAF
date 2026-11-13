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
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
# `closed_port()` binds without listen(), so a connect to it is refused at once.
_REFUSAL_CHILD = r'''
import asyncio, errno, json, os, socket, subprocess, sys, time
snippet, pre, post = sys.argv[1:4]


def closed_port():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    return holder, holder.getsockname()[1]


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
    assert refused(socket.socket(socket.AF_INET6), ("::1", holder6.getsockname()[1]))
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
