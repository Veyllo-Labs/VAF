# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Framework code must not consult the harness's auth layer directly.

THE INCIDENT CLASS: the per-user tool allowlist shipped with framework code
(`vaf/core/tool_dispatch.py`, `vaf/tools/coder.py`) hard-importing
`vaf.auth.permissions` - the harness's auth-DB resolver - from inside the funnel. An
embedder got a stage that silently resolved to "unrestricted" and no way to install
their own policy at that rank. The fix is the registered resolver
(`set_account_allowlist_resolver`, registered by the harness in vaf/main.py); this
guard keeps the deletion deleted.

Two rules, two failure modes:

- HARD BAN on `vaf.auth.permissions`: the one module a framework file was proven to
  reach for. AST walk over the whole tree, so function-local imports are seen - that is
  exactly where both deleted imports sat.
- SHRINK-ONLY BASELINE for every other `vaf.auth.*` import under vaf/core and vaf/tools:
  the debt register of the framework/harness split. FILE + COUNT, not file:line - a
  line-keyed baseline freezes on pure line shifts and trains people to update it without
  reading (the api-key baseline learned this twice in one day), while a new or vanished
  import still changes the count and still trips.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files that may import vaf.auth.permissions despite living under the scanned roots.
# Keyed by repo-relative path, value = the reason. Starts EMPTY on purpose: the day an
# entry appears here, the mission question ("which primitive is missing?") is being
# answered with an exemption instead.
PERMISSIONS_IMPORT_EXEMPTIONS: dict = {}

# The measured debt register: every remaining vaf.auth.* import under the framework
# roots, by file and count. GENERATED, never typed - regenerate with the collector below
# after a deliberate change. May only SHRINK: a new entry or a grown count means fresh
# framework code is reaching into the harness auth layer and belongs in review.
AUTH_IMPORT_BASELINE = {
    "vaf/core/agent.py": 1,
    "vaf/core/messaging_connections.py": 1,
    "vaf/core/system_prompt.py": 4,
    "vaf/core/thinking_mode.py": 6,
    "vaf/core/user_time.py": 1,
    "vaf/core/vocab/__init__.py": 1,
    "vaf/core/web_server.py": 9,
    "vaf/tools/user_identity.py": 1,
}


def _auth_imports(path: Path):
    """Every vaf.auth.* import in one file, as (lineno, module) - AST, so function-local
    imports count. Relative imports cannot reach vaf.auth from these roots and are
    ignored on purpose."""
    tree = ast.parse(path.read_bytes())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "vaf.auth" or node.module.startswith("vaf.auth.")
        ):
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "vaf.auth" or alias.name.startswith("vaf.auth."):
                    found.append((node.lineno, alias.name))
    return found


def _framework_files():
    for base in ("vaf/core", "vaf/tools"):
        yield from sorted((ROOT / base).rglob("*.py"))


def test_no_framework_file_imports_the_permissions_resolver():
    offenders = []
    for f in _framework_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in PERMISSIONS_IMPORT_EXEMPTIONS:
            continue
        for lineno, module in _auth_imports(f):
            if module == "vaf.auth.permissions" or module.startswith("vaf.auth.permissions."):
                offenders.append(f"{rel}:{lineno} imports {module}")
    assert not offenders, (
        "framework code reaches into the harness auth layer again:\n  "
        + "\n  ".join(offenders)
        + "\nConsult get_account_allowlist_resolver() / resolve_account_allowlist() in "
          "vaf.core.tool_dispatch instead; the harness registers its resolver in "
          "vaf/main.py."
    )


def test_the_auth_import_debt_only_shrinks():
    live = {}
    for f in _framework_files():
        rel = f.relative_to(ROOT).as_posix()
        n = len(_auth_imports(f))
        if n:
            live[rel] = n

    grown = {
        rel: (AUTH_IMPORT_BASELINE.get(rel, 0), n)
        for rel, n in live.items()
        if n > AUTH_IMPORT_BASELINE.get(rel, 0)
    }
    assert not grown, (
        f"new vaf.auth imports under vaf/core or vaf/tools: "
        f"{ {k: f'{a} -> {b}' for k, (a, b) in grown.items()} } - framework code is "
        f"reaching into the harness auth layer. Either the capability needs a framework "
        f"primitive (the allowlist resolver is the precedent) or the file belongs to the "
        f"harness; growing this baseline is a reviewed decision, not a rerun."
    )

    shrunk = {
        rel: (n, live.get(rel, 0))
        for rel, n in AUTH_IMPORT_BASELINE.items()
        if live.get(rel, 0) < n
    }
    assert not shrunk, (
        f"debt was paid down - lock it in so it cannot creep back: {shrunk}; update "
        f"AUTH_IMPORT_BASELINE in {Path(__file__).name} to the new (smaller) counts."
    )
