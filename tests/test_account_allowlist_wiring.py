# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The harness registers its account-allowlist resolver - on every lane, provably.

The framework primitive (`set_account_allowlist_resolver`) is only as good as the
registration that feeds it: a harness process that forgot to register silently serves
every scoped tenant unrestricted. Registration lives at MODULE level in vaf/main.py
because that module is the one convergence point of every product process - the console
script, tray/web (`-m vaf.main tray`), and all subagent children (`-m vaf.main subagent
run`) - while an embedder imports only `vaf` and correctly stays unregistered.

THE LIBRARIAN-CHILD LANE is proven by composition, each conjunct mutation-tested on its
own: (i) the funnel consults the registry (tests/test_tool_account_allowlist.py), (ii) any
process that imports vaf.main has the registry populated (the subprocess test here - and
`-m vaf.main` executes a strict superset of a plain import, so moving the registration
under `if __name__ == "__main__":` turns the plain-import test red), (iii) every subagent
child is spawned through `-m vaf.main` (the spawn-shape test here), (iv) the child's
ToolCaller carries scope and role from the env (tests/test_coder_identity_boundary.py and
the librarian's own dispatch tests).
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_importing_vaf_main_registers_the_harness_resolver():
    """Subprocess on purpose, twice over: in-process, `import vaf.main` is a no-op when an
    earlier test already imported it (sys.modules), and a fresh import would register the
    real resolver into the running suite. VAF_SKIP_DEP_CHECK=1 is load-bearing: it makes
    this test red if the registration ever moves INSIDE bootstrap(), whose early return
    on that flag would skip it on exactly the lanes (CI, app bundles) where it matters.
    The `is` assert pins that the registered object IS the harness resolver, not a
    wrapper that could drift from it.
    """
    script = (
        "import vaf.main\n"
        "from vaf.core.tool_dispatch import get_account_allowlist_resolver\n"
        "from vaf.auth.permissions import resolve_allowed_tools\n"
        "assert get_account_allowlist_resolver() is resolve_allowed_tools, (\n"
        "    'vaf.main did not register the harness account-allowlist resolver')\n"
        "print('REGISTERED_OK')\n"
    )
    env = dict(os.environ)
    env["VAF_SKIP_DEP_CHECK"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT), env=env,
    )
    assert result.returncode == 0, (
        "importing vaf.main no longer registers the harness resolver - every scoped "
        "tenant in every product process would run unrestricted.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "REGISTERED_OK" in result.stdout


def test_subagent_children_are_spawned_through_vaf_main():
    """The child lane inherits the registration only because every spawner goes through
    `-m vaf.main`. A spawner that switches to a different entry module would start
    children with an EMPTY registry - scoped tenants unrestricted inside that child."""
    spawners = (
        "vaf/tools/coder.py",
        "vaf/tools/librarian.py",
        "vaf/tools/research_agent.py",
        "vaf/tools/document_agent.py",
        "vaf/tools/browser_agent.py",
    )
    missing = []
    for rel in spawners:
        src = (ROOT / rel).read_bytes().decode("utf-8", errors="replace")
        if "'-m', 'vaf.main'" not in src and '"-m", "vaf.main"' not in src:
            missing.append(rel)
    assert not missing, (
        f"subagent spawners no longer go through `-m vaf.main`: {missing} - their "
        f"children start without the account-allowlist registration; either keep the "
        f"spawn shape or register the resolver in the new entry module and update the "
        f"module docstring's composition proof"
    )
