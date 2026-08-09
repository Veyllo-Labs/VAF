# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Isolation for the embedder contract suite (tests/contract/).

This suite is designed to be VENDORED: an embedder copies this directory into
their own CI and runs it against a pip-installed vaf to detect breaking
changes before upgrading (see README.md in this directory). It therefore runs
in two modes:

- In-repo: the parent tests/conftest.py exists and already redirects the six
  store axes for the whole session; the repo test discipline additionally runs
  the suite under a scratch HOME (tests/README.md). This file adds only the
  per-test fixtures.
- Standalone (vendored): no parent conftest exists, so THIS file isolates the
  process itself, at import time - before any vaf module loads - because
  vaf.core.config computes its config directory when it is first imported.
  Without this, a vendored run would read and write the CI user's real
  ~/.vaf, ~/.config/vaf and platform data directories.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# The parent conftest is the marker for an in-repo run: a vendored copy ships
# only this directory, so the parent file is absent there.
STANDALONE = not (Path(__file__).resolve().parent.parent / "conftest.py").exists()

# Same axis list the repo conftest isolates: VAF_LOG_DIR is the log redirect,
# the XDG trio governs Linux (and, when set, macOS) store dirs, and the two
# Windows vars govern config_dir/data_dir/cache_dir there.
_ENV_AXES = (
    "VAF_LOG_DIR",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "LOCALAPPDATA",
    "APPDATA",
)

# Env vars vaf sniffs at import or construction time; ambient values from a
# developer shell or a parent VAF process would change contract outcomes.
_AMBIENT_VARS = (
    "VAF_SESSION_ID",
    "VAF_TURN_ID",
    "VAF_PROVIDER",
    "VAF_MODEL_OVERRIDE",
    "VAF_TOOL_MODEL",
    "VAF_NONINTERACTIVE",
    "VAF_ALLOWED_TOOLS",
    "VAF_IN_SUBAGENT_TERMINAL",
    "VAF_IN_WORKFLOW_TERMINAL",
    "VAF_THINKING_MODE",
    "VAF_IN_AUTOMATION",
    "VAF_DOCKER_MODE",
    "VAF_CONFIG_DIR",
)

if STANDALONE:
    # Config.APP_DIR (and friends) freeze Path.home() at first vaf import, so
    # isolation set inside a fixture would come too late. If vaf is already
    # loaded, this process cannot be isolated - fail loudly instead of
    # silently touching the real home directory.
    _already = sorted(m for m in sys.modules if m == "vaf" or m.startswith("vaf."))
    if _already:
        raise RuntimeError(
            "the vaf package was imported before tests/contract/conftest.py "
            f"could isolate the environment ({_already[:3]}...); run pytest "
            "in a fresh process with no plugin that imports vaf"
        )
    _root = Path(tempfile.mkdtemp(prefix="vaf-contract-"))
    _home = _root / "home"
    _home.mkdir()
    os.environ["HOME"] = str(_home)
    os.environ["USERPROFILE"] = str(_home)  # Windows: expanduser never reads HOME
    for _var in _ENV_AXES:
        _dir = _root / _var.lower()
        _dir.mkdir()
        os.environ[_var] = str(_dir)
    for _var in _AMBIENT_VARS:
        os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def contract_log_dir(tmp_path, monkeypatch):
    """Every test gets its own log directory.

    ToolCaller.execute() writes an audit line per call and BaseTool.log()
    appends to tools_<date>.log by default; VAF_LOG_DIR is the documented
    redirect and is read per call, so a per-test value both isolates the
    writes and lets log-asserting tests read them back deterministically.
    """
    log_dir = tmp_path / "vaf-logs"
    log_dir.mkdir()
    monkeypatch.setenv("VAF_LOG_DIR", str(log_dir))
    return log_dir


@pytest.fixture(autouse=True)
def _ambient_ids_cleared(monkeypatch):
    """Session/turn ids fall back to these env vars for a never-told context;
    an ambient value (developer shell, parent VAF process) would leak into the
    context-API assertions."""
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    monkeypatch.delenv("VAF_TURN_ID", raising=False)


@pytest.fixture(autouse=True)
def _account_allowlist_restored():
    """The account allowlist resolver is process-global by contract ('one
    resolver per process, the last registration wins'). Restore - never bare
    clear - so a test cannot leak its resolver into later tests, and a run
    that imported the product harness keeps the harness resolver.

    The getter lives in vaf.core.tool_dispatch (not on the facade): it exists
    for exactly this save/restore need, not for application code.
    """
    from vaf.core.tool_dispatch import (
        get_account_allowlist_resolver,
        set_account_allowlist_resolver,
    )

    previous = get_account_allowlist_resolver()
    yield
    set_account_allowlist_resolver(previous)
