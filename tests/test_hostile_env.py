# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The sitecustomize scripts/hostile_env.py writes, run in a child interpreter.

A child, because the snippet patches the import system, platform, email.utils,
posixpath.expanduser and the socket layer for the whole process it runs in. Every
child gets an environment without this script's own variables and PYTHONPATH, so
the tests mean the same thing when the suite itself runs under hostile_env.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
    module = _module()
    return module._BLOCKER.format(names=repr(set()), refusal_s=module._WINDOWS_REFUSAL_S)


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
    try:
        sock.connect(address)
    except ConnectionRefusedError:
        return True
    return False


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


def test_a_refused_local_connect_is_charged_what_windows_waits(tmp_path):
    """A blocking refusal costs about 2 s on a Windows runner, or the caller's shorter
    connect timeout; it is counted, not slept, and a connect that succeeds costs nothing.
    MUTATION: drop the min() with the timeout - red (0.5 charged 2.0); drop the connect
    wrapper - red (no line); sleep the charge - red (elapsed)."""
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
''')
    assert [(who, cost) for who, _addr, cost in lines] == [
        (_TEST_NAME, "2.0"), (_TEST_NAME, "0.5"), (_TEST_NAME, "2.0")]
    assert all(addr.startswith("127.0.0.1:") for _who, addr, _cost in lines)
    assert float(stdout) < 1.0


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


def test_a_vaf_service_port_is_refused_without_a_dial(tmp_path):
    """A port on the service list is refused before any SYN leaves: a listener of the
    test's own on it accepts nothing, blocking or through asyncio. (A free port of the
    test's own stands in for the list; the real ports are never dialed here.)
    MUTATION: drop the forced check in the connect wrapper (blocking and asyncio both
    connect) or in the connect_ex wrapper - red (accept() returns)."""
    stdout, lines = _refusal_run(tmp_path, pre=r'''
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(8)
server.settimeout(0.5)
port = server.getsockname()[1]
os.environ["VAF_HOSTILE_REFUSE_PORTS"] = "1," + str(port)
''', post=r'''
assert refused(socket.socket(), ("127.0.0.1", port))
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
    print("nothing arrived")
''')
    assert stdout.strip() == "nothing arrived"
    assert [cost for _who, _addr, cost in lines] == ["2.0", "2.0", "2.0"]


def test_unix_udp_and_remote_refusals_are_not_charged(tmp_path):
    """Only a TCP refusal to a local address is a Windows wait. A plain connect that
    refuses everything stands in for the network, so a remote refusal needs no network;
    the local TCP control proves the stand-in reaches the counter. Without the log
    variable nothing is wrapped at all.
    MUTATION: drop the family/type check - red (UDP and Unix charged); treat every host
    as local - red (remote charged); install the wrappers without the log - red."""
    _stdout, lines = _refusal_run(tmp_path, pre=r'''
def _refuse_all(self, address):
    raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")


socket.socket.connect = _refuse_all
''', post=r'''
assert refused(socket.socket(), ("192.0.2.1", 80))
assert refused(socket.socket(), ("example.invalid", 80))
assert refused(socket.socket(socket.AF_INET, socket.SOCK_DGRAM), ("127.0.0.1", 80))
if hasattr(socket, "AF_UNIX"):
    assert refused(socket.socket(socket.AF_UNIX), "/nonexistent/vaf.sock")
assert refused(socket.socket(), ("127.0.0.1", 80))
''')
    assert [addr for _who, addr, _cost in lines] == ["127.0.0.1:80"]

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


def test_the_axis_survives_monkeypatches_children_and_finished_tests(tmp_path):
    """The real sitecustomize, loaded by the interpreter from PYTHONPATH: a test that
    patches socket.socket.connect and restores it leaves the counter in place, a child
    process charges the test that started it, and a refusal after the test ended is
    charged "after" the last finished test.
    MUTATION: pop VAF_HOSTILE_REFUSAL_LOG from the environment after reading it - red
    (the child charges nothing); empty the plugin's hook - red ("outside any test")."""
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
'''
    out = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, timeout=60,
        env=_clean_env(PYTHONPATH=str(site), VAF_HOSTILE_REFUSAL_LOG=str(log),
                       PYTEST_CURRENT_TEST=_TEST_NAME))
    assert out.returncode == 0, out.stderr
    who = [line.split("\t")[0] for line in log.read_text(encoding="utf-8").splitlines()]
    assert who == [_TEST_NAME, _TEST_NAME, "after tests/test_example.py::test_pays"]


def test_the_verdict_sums_phases_and_names_the_test():
    """setup + call + teardown of one test add up; each key over the budget is named,
    the most expensive first; a thread that outlived its test is judged as well.
    MUTATION: key on the raw name with its phase - red (no test reaches 61 s)."""
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


def test_main_fails_a_green_run_over_budget(tmp_path, monkeypatch, capsys):
    """A test over the budget fails a run pytest called green and is named; pytest's own
    failure is passed through unchanged; under the budget the run stays green.
    MUTATION: return pytest's code regardless of the offenders - red."""
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
            with open(env["VAF_HOSTILE_REFUSAL_LOG"], "a", encoding="utf-8") as log:
                log.write("tests/test_example.py::test_pays (call)\t127.0.0.1:5432\t2.0\n" * n_lines)
            return SimpleNamespace(returncode=pytest_rc)
        monkeypatch.setattr(module.subprocess, "run", fake_run)
        return module.main()

    assert run(0, 31) == 1
    out = capsys.readouterr().out
    assert ("FAILED slow refusal: tests/test_example.py::test_pays would wait 62.0 s"
            in out)
    assert seen[-1][-3:] == ["-p", "vaf_hostile_refusals", "tests/test_example.py"]
    assert run(2, 31) == 2
    capsys.readouterr()
    assert run(0, 30) == 0
    assert "FAILED slow refusal" not in capsys.readouterr().out
    assert run(0, 0) == 0


def test_every_port_the_door_harness_avoids_is_refused():
    """tests/door_harness.py names the ports a running VAF may listen on; the hostile
    run must refuse each of them without a dial.
    MUTATION: remove 8443 from _VAF_SERVICE_PORTS - red."""
    text = (_ROOT / "tests" / "door_harness.py").read_text(encoding="utf-8")
    named = re.search(r"Nothing here dials ([0-9, or]+), where", text).group(1)
    ports = {int(port) for port in re.findall(r"[0-9]+", named)}
    assert ports == {3000, 8001, 8005, 8443}
    assert ports | {5432, 8080, 9222} <= set(_module()._VAF_SERVICE_PORTS)
