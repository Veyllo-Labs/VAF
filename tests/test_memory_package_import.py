# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The memory package must stay importable from two threads at once.

The tray probes the database while the web server mounts the memory routes, and on
the direct-tray start path both happen concurrently. An eager re-export in the package
initialiser turns that into a module-lock deadlock: the routes never mount, every
/api/memory call answers 404, and the Memory page reports a failed graph fetch with
nothing in the UI naming the cause.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A barrier, so both imports genuinely start together. Without it the threads serialise
# and the deadlock hides: five unsynchronised runs passed while five synchronised ones
# failed, which is exactly why this reproduces the field report and a naive test does not.
_CONCURRENT_IMPORT = """
import threading
barrier = threading.Barrier(2)
errors = []

def importer(module):
    def run():
        barrier.wait()
        try:
            __import__(module)
        except BaseException as exc:          # _DeadlockError is not an Exception
            errors.append(f"{module}: {type(exc).__name__}: {exc}")
    return run

threads = [
    threading.Thread(target=importer("vaf.memory.database")),
    threading.Thread(target=importer("vaf.memory.routes")),
]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("ERRORS:" + "|".join(errors))
"""


def _fresh_interpreter(code, tmp_path):
    """Run code in a new interpreter, so sys.modules starts empty and HOME is scratch."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO, capture_output=True, text=True, timeout=180,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(REPO)},
    )


def test_database_and_routes_import_concurrently(tmp_path):
    result = _fresh_interpreter(_CONCURRENT_IMPORT, tmp_path)
    assert result.returncode == 0, f"interpreter died: {result.stderr[-2000:]}"
    reported = [line for line in result.stdout.splitlines() if line.startswith("ERRORS:")]
    assert reported, f"no verdict printed: {result.stdout[-2000:]} {result.stderr[-2000:]}"
    failures = reported[0][len("ERRORS:"):].strip()
    assert not failures, f"concurrent import of the memory package failed: {failures}"


def test_package_initialiser_imports_no_submodule_eagerly(tmp_path):
    """The structural guard: importing the package must not drag its submodules in.

    This is what keeps the deadlock from coming back through a newly added re-export.
    """
    code = (
        "import sys\n"
        "import vaf.memory\n"
        "pulled = sorted(m for m in sys.modules if m.startswith('vaf.memory.'))\n"
        "print('PULLED:' + '|'.join(pulled))\n"
    )
    result = _fresh_interpreter(code, tmp_path)
    assert result.returncode == 0, f"interpreter died: {result.stderr[-2000:]}"
    reported = [line for line in result.stdout.splitlines() if line.startswith("PULLED:")]
    assert reported, f"no verdict printed: {result.stdout[-2000:]} {result.stderr[-2000:]}"
    pulled = [m for m in reported[0][len("PULLED:"):].split("|") if m]
    assert not pulled, (
        "importing vaf.memory eagerly pulled in " + ", ".join(pulled) +
        "; re-export them through the lazy __getattr__ instead"
    )


def test_lazy_reexports_still_resolve(tmp_path):
    """Lazy must not mean gone: every name in __all__ stays importable from the package."""
    code = (
        "import vaf.memory as m\n"
        "missing = [n for n in m.__all__ if not hasattr(m, n)]\n"
        "print('MISSING:' + '|'.join(missing))\n"
        "print('COUNT:%d' % len(m.__all__))\n"
    )
    result = _fresh_interpreter(code, tmp_path)
    assert result.returncode == 0, f"interpreter died: {result.stderr[-2000:]}"
    missing = [ln[len("MISSING:"):] for ln in result.stdout.splitlines() if ln.startswith("MISSING:")]
    assert missing and not missing[0].strip(), f"re-exported names vanished: {missing}"
    count = [ln for ln in result.stdout.splitlines() if ln.startswith("COUNT:")]
    assert count and int(count[0][len("COUNT:"):]) >= 12, f"__all__ shrank: {count}"
