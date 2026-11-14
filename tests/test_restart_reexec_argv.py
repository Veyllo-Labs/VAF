# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`restart` in both terminal lanes starts exactly the command the interpreter was started with.

The restart exists in the classic lane (`_run_modern` in vaf/cli/cmd/run.py) and in the
full-screen app (`_exec_restart` in vaf/cli/tui_app/app.py); both end in
`vaf.cli.reexec.reexec`, which execs `[sys.executable, *sys.orig_argv[1:]]`. `sys.argv` is no
guide: pip's console-script wrapper strips `.exe` from argv[0] on Windows (a file that does not
exist), `-c` and interpreter options such as `-X utf8` are missing from it, and a wrapper may
rewrite it. `orig_argv[0]` is no guide either: it is the name as typed, a bare `python` under a
PATH lookup. A `python -m <module>` start comes back as one, so `python -m vaf` is never produced.

On Windows the C runtime joins exec arguments with spaces and quotes nothing, so every argument
is quoted with `subprocess.list2cmdline`. The quoting is checked against `crt_parse`, a
reference parser written from Microsoft's documented rules ("Parsing C command-line
arguments"), pinned by Microsoft's own table and fuzzed with a fixed seed.

Both lanes are driven with ONE argv table, so a drift between them turns one of the two tests
red, and `os.execl` is recorded. Two tests start real processes: a wrapper package started with
`python -m` that records the full-screen app's restart command and then runs it, and a real
restart through `reexec()` for every start form, including a console script that pip installed
from a wheel into a folder with a space. On Windows that last test is the measurement of what
the launcher hands to Python and of what the C runtime does with the exec arguments.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import types
import zipfile
from pathlib import Path

import psutil
import pytest

from test_cli_web_lane_queue_tasks import modern_lane  # noqa: F401 - modern_lane is a fixture

ROOT = Path(__file__).resolve().parents[1]


# --- The reference parser of the C runtime's command line ------------------------------------

def crt_parse(line: str) -> list[str]:
    """Split a Windows command line as the Microsoft C runtime does, one pass.

    argv[0] is a path: a double quote toggles quoting, whitespace outside quotes ends it, and a
    backslash is always literal. After it, whitespace (space or tab) outside quotes separates
    arguments; 2n backslashes before a double quote give n backslashes and the quote toggles
    quoting, 2n+1 backslashes before it give n backslashes and a literal quote, any other
    backslash is literal, and `""` inside quotes is a literal quote.
    """
    args: list[str] = []
    n = len(line)
    i = 0
    buf: list[str] = []
    quoted = False
    while i < n and (quoted or line[i] not in " \t"):
        if line[i] == '"':
            quoted = not quoted
        else:
            buf.append(line[i])
        i += 1
    args.append("".join(buf))
    while True:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            return args
        buf = []
        quoted = False
        while i < n:
            c = line[i]
            if c in " \t" and not quoted:
                break
            if c == "\\":
                j = i
                while j < n and line[j] == "\\":
                    j += 1
                run = j - i
                if j < n and line[j] == '"':
                    buf.append("\\" * (run // 2))
                    if run % 2:
                        buf.append('"')
                        i = j + 1
                    else:
                        i = j
                else:
                    buf.append("\\" * run)
                    i = j
                continue
            if c == '"':
                if quoted and i + 1 < n and line[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                quoted = not quoted
                i += 1
                continue
            buf.append(c)
            i += 1
        args.append("".join(buf))


# Microsoft's table in "Parsing C command-line arguments", each behind `prog ` so the input
# parses as arguments and not as argv[0].
CRT_TABLE = [
    ('"a b c" d e', ["a b c", "d", "e"]),
    ('"ab\\"c" "\\\\" d', ['ab"c', "\\", "d"]),
    ('a\\\\\\b d"e f"g h', ["a\\\\\\b", "de fg", "h"]),
    ('a\\\\\\"b c d', ['a\\"b', "c", "d"]),
    ('a\\\\\\\\"b c" d e', ["a\\\\b c", "d", "e"]),
    ('a"b"" c d', ['ab" c d']),
]


@pytest.mark.parametrize("line,expected", CRT_TABLE, ids=[r for r, _ in CRT_TABLE])
def test_reference_parser_follows_the_documented_table(line, expected):
    assert crt_parse("prog " + line) == ["prog", *expected]


def test_reference_parser_argv0_has_no_backslash_rules():
    assert crt_parse('"C:\\dir with space\\python.exe" x') == [
        "C:\\dir with space\\python.exe", "x"]
    assert crt_parse('C:\\a\\"b c"\\p.exe "" y') == ["C:\\a\\b c\\p.exe", "", "y"]


SPACED_CMD = ["C:\\dir with space\\python.exe", "C:\\dir with space\\vaf.exe", "run"]


def test_unquoted_join_of_a_spaced_command_is_split():
    # What the C runtime's exec makes of unquoted arguments: 7 items instead of 3.
    assert len(crt_parse(" ".join(SPACED_CMD))) == 7


# --- Both lanes, one table --------------------------------------------------------------------

# (id, sys.argv, sys.orig_argv, the name in __main__.__spec__). Synthetic paths; the argv and
# orig_argv pairs are the ones measured for each start form. The restart ignores sys.argv and
# the spec, and the stale-spec row proves it.
PY = "/opt/app/venv/bin/python"
ARGV_TABLE = [
    # python -m mypkg run: a wrapper package's __main__ module.
    ("module-mypkg", ["/opt/app/mypkg/__main__.py", "run"], [PY, "-m", "mypkg", "run"],
     "mypkg.__main__"),
    # python -m vaf.main run: run_vaf.sh, run_vaf.bat.
    ("module-vaf-main", ["/opt/app/vaf/main.py", "run"], [PY, "-m", "vaf.main", "run"],
     "vaf.main"),
    # python -m pkg.sub run: a subpackage's __main__ module.
    ("module-pkg-sub", ["/opt/app/pkg/sub/__main__.py", "run"], [PY, "-m", "pkg.sub", "run"],
     "pkg.sub.__main__"),
    # python /opt/app/vaf/main.py run and python vaf/main.py run: a script file.
    ("script-absolute", ["/opt/app/vaf/main.py", "run"], [PY, "/opt/app/vaf/main.py", "run"],
     None),
    ("script-relative", ["vaf/main.py", "run"], [PY, "vaf/main.py", "run"], None),
    # A script start in a process whose __main__ carries a stale module spec.
    ("stale-spec", ["/opt/app/mypkg/__main__.py", "run"],
     [PY, "/opt/app/mypkg/__main__.py", "run"], "vaf.main"),
    # The console script on POSIX.
    ("console-script-posix", ["/opt/app/venv/bin/vaf", "run"],
     [PY, "/opt/app/venv/bin/vaf", "run"], None),
    # The console script on Windows: pip's wrapper stripped `.exe` from argv[0], the launcher
    # handed Python the `.exe` itself, a zip archive whose spec name is the bare __main__.
    ("console-script-windows", ["C:\\app\\Scripts\\vaf", "run"],
     ["C:\\app\\Scripts\\python.exe", "C:\\app\\Scripts\\vaf.exe", "run"], "__main__"),
    # python /opt/app/tool.pyz run: a zip archive.
    ("zipapp", ["/opt/app/tool.pyz", "run"], [PY, "/opt/app/tool.pyz", "run"], "__main__"),
    # Interpreter options of the first start.
    ("interpreter-options", ["/opt/app/vaf/main.py", "run"],
     [PY, "-X", "utf8", "-u", "-m", "vaf.main", "run"], "vaf.main"),
    # python -c <code> run: argv holds `-c` where the code was.
    ("dash-c", ["-c", "run"], [PY, "-c", "import vaf.main as m; m.main()", "run"], None),
    # A wrapper script that rewrites sys.argv before it calls VAF.
    ("argv-rewriting-wrapper", ["vaf", "run"], [PY, "wrap.py", "--flag"], None),
    # The interpreter as typed under a PATH lookup.
    ("bare-python", ["/opt/app/vaf/main.py", "run"], ["python", "-m", "vaf.main", "run"],
     "vaf.main"),
]
_IDS = [row[0] for row in ARGV_TABLE]
_ROWS = [row[1:] for row in ARGV_TABLE]


def _expected(orig_argv: list[str]) -> tuple:
    return (sys.executable, sys.executable, *orig_argv[1:])


def _start(monkeypatch, argv, orig_argv, spec_name, windows=False) -> None:
    """Make this process look like one started that way."""
    from vaf.cli import reexec as reexec_mod
    monkeypatch.setattr(reexec_mod, "_WINDOWS", windows)
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(sys, "orig_argv", list(orig_argv))
    spec = types.SimpleNamespace(name=spec_name) if spec_name else None
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", spec, raising=False)


def _assert_never_module_vaf(calls) -> None:
    for call in calls:
        pairs = list(zip(call, call[1:]))
        assert ("-m", "vaf") not in pairs, call


def _assert_not_stripped(calls) -> None:
    for call in calls:
        assert "C:\\app\\Scripts\\vaf" not in call, call


@pytest.mark.parametrize("argv,orig_argv,spec_name", _ROWS, ids=_IDS)
def test_classic_lane_restart_starts_the_started_program(
        modern_lane, monkeypatch, argv, orig_argv, spec_name):  # noqa: F811
    _start(monkeypatch, argv, orig_argv, spec_name)
    modern_lane.rec["script"] = ["restart", None]
    # The recorded execl raises; the restart's own `except` prints "Restart failed" and the
    # loop goes on, so the console check of the fixture does not apply here.
    rec = modern_lane.run(console_breaks=True)
    assert rec["execl"] == [_expected(orig_argv)]
    _assert_never_module_vaf(rec["execl"])
    _assert_not_stripped(rec["execl"])


class _Stop(Exception):
    """Raised by the recorded os.execl, as the real one never returns."""


def _app_restart(monkeypatch):
    """Run the full-screen app's restart with os.execl recorded; (calls, errors)."""
    from vaf.cli.tui_app import app as app_mod
    from vaf.cli.ui import UI
    from vaf.core.frontend_manager import FrontendManager

    stops = []
    monkeypatch.setattr(FrontendManager, "stop_frontend",
                        lambda self, *a, **k: stops.append(k))
    calls = []

    def _execl(*a):
        calls.append(a)
        raise _Stop("execl")
    monkeypatch.setattr(os, "execl", _execl)
    errors = []
    monkeypatch.setattr(UI, "error", staticmethod(lambda msg, *a, **k: errors.append(str(msg))))

    app_mod._exec_restart()

    assert stops == [{"wait_for_exit": True}]
    return calls, errors


@pytest.mark.parametrize("argv,orig_argv,spec_name", _ROWS, ids=_IDS)
def test_app_exec_restart_starts_the_started_program(monkeypatch, argv, orig_argv, spec_name):
    _start(monkeypatch, argv, orig_argv, spec_name)
    calls, errors = _app_restart(monkeypatch)
    assert calls == [_expected(orig_argv)]
    _assert_never_module_vaf(calls)
    _assert_not_stripped(calls)
    assert errors and "Restart failed" in errors[0]


# --- An interpreter that does not report its start --------------------------------------------

EMPTY_RECORDS = [("orig_argv", []), ("executable", "")]


@pytest.mark.parametrize("attr,value", EMPTY_RECORDS, ids=[a for a, _ in EMPTY_RECORDS])
def test_classic_lane_refuses_without_a_start_record(
        modern_lane, monkeypatch, attr, value):  # noqa: F811
    _start(monkeypatch, ["/opt/app/vaf/main.py", "run"], [PY, "-m", "vaf.main", "run"], None)
    monkeypatch.setattr(sys, attr, value)
    modern_lane.rec["script"] = ["restart", None]
    rec = modern_lane.run(console_breaks=True)
    assert rec["execl"] == []
    assert [e for e in rec["errors"] if e.startswith("Restart failed")], rec["errors"]
    # The loop went on to the next prompt after the refused restart.
    assert rec["prompts"] == 2


@pytest.mark.parametrize("attr,value", EMPTY_RECORDS, ids=[a for a, _ in EMPTY_RECORDS])
def test_app_restart_refuses_without_a_start_record(monkeypatch, attr, value):
    _start(monkeypatch, ["/opt/app/vaf/main.py", "run"], [PY, "-m", "vaf.main", "run"], None)
    monkeypatch.setattr(sys, attr, value)
    calls, errors = _app_restart(monkeypatch)
    assert calls == []
    assert errors and errors[0].startswith("Restart failed")
    assert "cannot start itself again" in errors[0]


# --- The Windows quoting, through the real reexec() --------------------------------------------

WIN_PY = "C:\\dir with space\\venv\\Scripts\\python.exe"
WIN_ORIG = ["python", "C:\\dir with space\\venv\\Scripts\\vaf.exe", "run", "hello world",
            'say "hi"', "", "tail\\", "tab\there", "\\\\server\\share\\x"]


def _recorded_reexec(monkeypatch, executable, orig_argv, windows) -> tuple:
    from vaf.cli import reexec as reexec_mod
    monkeypatch.setattr(reexec_mod, "_WINDOWS", windows)
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr(sys, "orig_argv", list(orig_argv))
    calls = []

    def _execl(*a):
        calls.append(a)
        raise _Stop("execl")
    monkeypatch.setattr(os, "execl", _execl)
    with pytest.raises(_Stop):
        reexec_mod.reexec()
    assert len(calls) == 1
    return calls[0]


def test_windows_exec_arguments_are_quoted_for_the_c_runtime(monkeypatch):
    cmd = [WIN_PY, *WIN_ORIG[1:]]
    call = _recorded_reexec(monkeypatch, WIN_PY, WIN_ORIG, windows=True)
    path, args = call[0], list(call[1:])
    assert path == WIN_PY
    assert args == [subprocess.list2cmdline([a]) for a in cmd]
    assert crt_parse(" ".join(args)) == cmd


def test_posix_exec_arguments_pass_unchanged(monkeypatch):
    cmd = [WIN_PY, *WIN_ORIG[1:]]
    call = _recorded_reexec(monkeypatch, WIN_PY, WIN_ORIG, windows=False)
    assert call == (WIN_PY, *cmd)


# Seeded fuzz of the quoting step: random commands through the real reexec(), joined with
# single spaces as the C runtime does, parsed back by the reference parser. NUL is left out:
# os.execl refuses it before any quoting.
FUZZ_TOKENS = ["a", "b", " ", "\t", '"', "\\", "\\\\", "\u00e9", "x y", "\n", "-"]
FUZZ_INTERPRETERS = ["C:\\Python313\\python.exe", WIN_PY,
                     "C:\\Program Files\\Python313\\python.exe"]


def fuzz_command(rng: random.Random) -> list[str]:
    cmd = [rng.choice(FUZZ_INTERPRETERS)]
    for _ in range(rng.randint(0, 5)):
        cmd.append("".join(rng.choice(FUZZ_TOKENS) for _ in range(rng.randint(0, 8))))
    return cmd


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_windows_quoting_round_trips_random_commands(monkeypatch, seed):
    rng = random.Random(seed)
    mismatches = []
    for _ in range(300):
        cmd = fuzz_command(rng)
        call = _recorded_reexec(monkeypatch, cmd[0], ["python", *cmd[1:]], windows=True)
        if call[0] != cmd[0] or crt_parse(" ".join(call[1:])) != cmd:
            mismatches.append(cmd)
    assert mismatches == []


# --- A real wrapper package started with python -m ---------------------------------------------

# A wrapper package that calls VAF from its `__main__` module, with a relative import there.
# First start: it drives the full-screen app's real restart with os.execl recorded and prints
# the recorded command. Second start (the env marker is set): it reports how it was started.
_WRAPPER_ENTRY = '''\
import json
import os
import sys


def main():
    if os.environ.get("RESTARTWRAP_PHASE") == "restarted":
        spec = getattr(sys.modules["__main__"], "__spec__", None)
        here = os.path.dirname(os.path.abspath(__file__))
        print("REPORT " + json.dumps({
            "spec": getattr(spec, "name", None),
            "argv": sys.argv[1:],
            "package_dir_on_path": here in [os.path.abspath(p or ".") for p in sys.path],
        }))
        return
    from vaf.core.frontend_manager import FrontendManager
    FrontendManager.stop_frontend = lambda self, *a, **k: None
    calls = []

    def _execl(*a):
        calls.append(list(a))
        raise RuntimeError("recorded")
    os.execl = _execl
    from vaf.cli.tui_app.app import _exec_restart
    _exec_restart()
    print("RECORDED " + json.dumps(calls))
'''


def _line(stdout: str, tag: str):
    for line in stdout.splitlines():
        if line.startswith(tag + " "):
            return json.loads(line[len(tag) + 1:])
    return None


def _with_root_on_pythonpath(env: dict) -> dict:
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH")) if p)
    return env


def test_python_m_wrapper_with_relative_import_restarts_into_itself(tmp_path):
    pkg = tmp_path / "restartwrap"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "entry.py").write_text(_WRAPPER_ENTRY, encoding="utf-8")
    (pkg / "__main__.py").write_text("from .entry import main\n\nmain()\n", encoding="utf-8")
    env = _with_root_on_pythonpath(dict(os.environ))
    env.pop("RESTARTWRAP_PHASE", None)

    first = subprocess.run([sys.executable, "-m", "restartwrap", "run"], cwd=str(tmp_path),
                           env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr[-2000:]
    calls = _line(first.stdout, "RECORDED")
    assert calls and len(calls) == 1, first.stdout[-2000:] + first.stderr[-2000:]
    path, args = calls[0][0], calls[0][1:]
    if os.name == "nt":
        # The recorded arguments are quoted for the C runtime; read them back as it would.
        args = crt_parse(" ".join(args))

    # What the exec would have run, in the same directory and environment. It must start: a
    # script-mode rerun of restartwrap/__main__.py dies on its relative import.
    env["RESTARTWRAP_PHASE"] = "restarted"
    second = subprocess.run(args, executable=path, cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr[-2000:]
    report = _line(second.stdout, "REPORT")
    assert report == {"spec": "restartwrap.__main__", "argv": ["run"],
                      "package_dir_on_path": False}, second.stdout + second.stderr
    # A venv launcher (Windows) or a framework build (macOS) can name the same interpreter
    # by another path, so the executable is compared as a file.
    assert os.path.samefile(path, sys.executable), calls
    assert os.path.samefile(args[0], sys.executable), calls
    assert args[1:] == ["-m", "restartwrap", "run"], calls


# --- A real restart, every start form ---------------------------------------------------------

# The probe package. `report()` appends how this process was started to the file named in
# RESTARTPROBE_REPORT; in the first phase it then restarts through the real reexec().
_PROBE_MAIN = '''\
import json
import os
import sys


def report():
    phase = os.environ.get("RESTARTPROBE_PHASE") or "first"
    spec = getattr(sys.modules["__main__"], "__spec__", None)
    line = {"phase": phase, "pid": os.getpid(), "argv": sys.argv, "orig_argv": sys.orig_argv,
            "executable": sys.executable, "spec": getattr(spec, "name", None)}
    with open(os.environ["RESTARTPROBE_REPORT"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\\n")
    if phase == "restarted":
        return
    os.environ["RESTARTPROBE_PHASE"] = "restarted"
    from vaf.cli.reexec import reexec
    reexec()


if __name__ == "__main__":
    report()
'''
_PROBE_FILES = {
    "restartprobe/__init__.py": "",
    "restartprobe/main.py": _PROBE_MAIN,
    "restartprobe/__main__.py": "from restartprobe.main import report\n\nreport()\n",
}
_DIST = "restartprobe-1.0.dist-info"
_PROBE_ARGS = ["run", "hello world", 'say "hi"']


def _write_wheel(path: Path) -> None:
    """A minimal wheel of the probe with a `restartprobe` console script."""
    files = dict(_PROBE_FILES)
    files[f"{_DIST}/METADATA"] = "Metadata-Version: 2.1\nName: restartprobe\nVersion: 1.0\n"
    files[f"{_DIST}/WHEEL"] = ("Wheel-Version: 1.0\nGenerator: hand\nRoot-Is-Purelib: true\n"
                               "Tag: py3-none-any\n")
    files[f"{_DIST}/entry_points.txt"] = (
        "[console_scripts]\nrestartprobe = restartprobe.main:report\n")
    record = []
    for name, text in files.items():
        data = text.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append(f"{_DIST}/RECORD,,")
    files[f"{_DIST}/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)


@pytest.fixture(scope="module")
def probe_install(tmp_path_factory):
    """The probe as source in a folder with a space, and installed into a venv there."""
    base = tmp_path_factory.mktemp("restart") / "dir with space"
    base.mkdir()
    for name, text in _PROBE_FILES.items():
        target = base.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    wheel = base / "restartprobe-1.0-py3-none-any.whl"
    _write_wheel(wheel)
    venv = base / "venv"
    # venv and pip need nothing from PYTHONPATH, and a sitecustomize there that fakes the
    # host (scripts/hostile_env.py reports Darwin on Linux) would break pip's platform tags.
    # The probe starts below keep it.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], env=env,
                   check=True, capture_output=True, timeout=120)
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    done = subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "--python", str(python), "install",
         "--no-deps", "--no-index", "--disable-pip-version-check", str(wheel)],
        env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    script = bindir / ("restartprobe.exe" if os.name == "nt" else "restartprobe")
    assert script.is_file(), sorted(p.name for p in bindir.iterdir())
    return types.SimpleNamespace(base=base, python=python, script=script)


START_FORMS = {
    "console-script": lambda p: [str(p.script)],
    "module-package": lambda p: [str(p.python), "-m", "restartprobe"],
    "module": lambda p: [str(p.python), "-m", "restartprobe.main"],
    "script-file": lambda p: [str(p.python), os.path.join("restartprobe", "main.py")],
    "interpreter-option": lambda p: [str(p.python), "-X", "utf8", "-m", "restartprobe.main"],
    "dash-c": lambda p: [str(p.python), "-c", "import restartprobe.main as m; m.report()"],
}


def _report_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.mark.parametrize("form", list(START_FORMS))
def test_real_restart_comes_back_as_the_started_program(probe_install, tmp_path, form):
    report = tmp_path / "report.jsonl"
    env = _with_root_on_pythonpath(dict(os.environ))
    env.pop("RESTARTPROBE_PHASE", None)
    env["RESTARTPROBE_REPORT"] = str(report)
    out_path, err_path = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    restarted_pid = None
    try:
        # Files, not pipes: on Windows the restarted process inherits them and outlives the
        # first one, and reading a pipe would wait for it.
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            first = subprocess.run([*START_FORMS[form](probe_install), *_PROBE_ARGS],
                                   cwd=str(probe_install.base), env=env, stdout=out,
                                   stderr=err, timeout=120)
        # On Windows an exec starts the new process and ends the old one, so the restarted
        # line can arrive after the first process returned. A POSIX exec keeps the process,
        # so its line is there once the first start returned, or never.
        deadline = time.monotonic() + (60 if os.name == "nt" else 0)
        lines = _report_lines(report)
        while not any(x["phase"] == "restarted" for x in lines) and time.monotonic() < deadline:
            time.sleep(0.2)
            lines = _report_lines(report)
        diag = (f"exit {first.returncode}; lines {lines}; stderr "
                f"{err_path.read_text(encoding='utf-8', errors='replace')[-2000:]}")
        assert [x["phase"] for x in lines] == ["first", "restarted"], diag
        start, again = lines
        restarted_pid = again["pid"]
        assert again["orig_argv"][1:] == start["orig_argv"][1:], diag
        assert again["argv"][1:] == _PROBE_ARGS, diag
        assert start["argv"][1:] == _PROBE_ARGS, diag
        assert again["spec"] == start["spec"], diag
        if os.name != "nt":
            # A true exec: the same process carries on.
            assert again["pid"] == start["pid"], diag
            assert first.returncode == 0, diag
    finally:
        # Windows cannot remove the folder while the restarted process still runs in it.
        deadline = time.monotonic() + 30
        while (restarted_pid and psutil.pid_exists(restarted_pid)
               and restarted_pid != os.getpid() and time.monotonic() < deadline):
            time.sleep(0.2)
