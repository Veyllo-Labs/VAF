# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A test imports another test module by its plain name, never through `tests.`.

`tests/` is not a package. `from tests.x import ...` resolves only when the repository root
happens to be on sys.path: `python -m pytest` puts it there, the `pytest` script does not, and
whether an editable install does depends on how setuptools built it. Measured: five test files
passed every `python -m pytest` run here and failed to even import under `scripts/ci_check.sh`,
which runs `pytest tests/` the way CI does. pytest puts `tests/` itself on sys.path, so
`from conftest import ...` and `from test_x import ...` resolve under every invocation."""
import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_PACKAGE_IMPORT = re.compile(r"^\s*(?:from\s+tests\.|import\s+tests(?:\.|\s|$))", re.M)


def test_no_test_imports_through_the_tests_package():
    offenders = []
    for path in sorted(_TESTS.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _PACKAGE_IMPORT.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(_TESTS.parent).as_posix()}:{line}")
    assert not offenders, "import test modules by their plain name: " + ", ".join(offenders)
