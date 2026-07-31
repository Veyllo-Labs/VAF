# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared test isolation, plus the one helper eight dispatch tests need.

THE SUITE MUST NOT WRITE INTO THE DEVELOPER'S REAL STORES, and getting that right needs
more than one variable. `Platform` resolves ten directories; seven of them hang off
`Path.home()` and are therefore covered by running with a throwaway HOME. THREE ARE NOT:
`config_dir`, `data_dir` and `cache_dir` read `XDG_CONFIG_HOME` / `XDG_DATA_HOME` /
`XDG_CACHE_HOME`, which desktop sessions set INDEPENDENTLY of HOME. On a machine where
they are set, `HOME=$(mktemp -d) pytest` isolates nothing on those three axes - the runs
go straight into the real store, and both the house rule and the person applying it
believed otherwise.

That is not hypothetical. It produced a false SECURITY finding: 980 rows sat in a
literal-named channel-message store and were reported as user traffic orphaned by a
naming defect. They were suite output - 980 rows carrying two distinct message bodies,
one of them 653 times. The count was correct and answered a question nobody had asked.
Three further synthetic scope directories held ~3600 more rows. Same class as the earlier
incident where suite runs left synthetic security events in the production log and made
the dashboard's "threats blocked today" counter lie.

So all four axes are redirected for the WHOLE session. Tests that need their own log dir
still monkeypatch VAF_LOG_DIR per-test. The counter-proof that this actually holds -
including for directories a future `Platform` axis might add - lives in
`tests/test_suite_writes_nowhere_real.py`; it is the half that makes this docstring more
than a claim.
"""
import pytest

# The environment axes that decide where VAF writes. VAF_LOG_DIR is VAF's own; the rest are
# the ones a throwaway HOME does NOT cover, and WHICH of them applies depends on the
# platform: Linux reads the XDG names, Windows reads %LOCALAPPDATA%/%APPDATA%, and macOS
# puts all three under Library inside HOME (so it needs none of these). All of them are
# redirected everywhere - a name the platform ignores costs nothing, and leaving it out
# costs a whole operating system.
#
# The Windows half was missing until CI said so. The first version of this isolation was
# measured on Linux and frozen as if the mapping were universal, so `data_dir` on Windows
# followed neither redirected mechanism and the suite kept writing into the real
# %LOCALAPPDATA%. Same shape as the count that started this: measured on one platform, read
# as an answer about all of them.
ISOLATED_ENV_AXES = (
    "VAF_LOG_DIR",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",   # Linux
    "LOCALAPPDATA", "APPDATA",                              # Windows
)


@pytest.fixture(autouse=True, scope="session")
def _isolated_store_dirs(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("vaf-test-stores")
    previous = {}
    for var in ISOLATED_ENV_AXES:
        previous[var] = os.environ.get(var)
        target = root / var.lower()
        target.mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(target)
    # Exposed so the counter-proof can assert against the same root rather than
    # recomputing it - a proof that derives its own expectation is not a proof.
    os.environ["VAF_TEST_STORE_ROOT"] = str(root)
    yield root
    for var, old in previous.items():
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old
    os.environ.pop("VAF_TEST_STORE_ROOT", None)


# ── duck-typed agents for the dispatch tests ─────────────────────────────────
#
# Eight tests drive `Agent.execute_tool` against a `SimpleNamespace` instead of a real
# Agent, because building one costs a model, a session store and a tool registry. The
# dispatch pipeline lives in vaf/core/tool_dispatch.py and calls back into the chat turn
# through hooks, so a fake now has to answer for those stages too.
#
# They are BOUND FROM THE REAL CLASS rather than stubbed out. Stubbing would be less work
# and would quietly gut every one of those tests: the plumbing cascade, the duplicate
# guard and the post-dispatch hooks ARE the behaviour under measurement, so a fake that
# answers them with no-ops would be a test agreeing with itself.
#
# `execute_tool` itself is in the list because the dispatcher re-enters itself: the
# `multi_tool_use.parallel` wrapper runs each call it carries through the front door again,
# so a fake that can be dispatched must also be re-enterable.
CHAT_STAGES = (
    "execute_tool", "_dispatch_session_id", "_is_channel_turn",
    "_chat_turn_gates", "_chat_session_plumbing", "_chat_post_dispatch",
    "_chat_after_dispatch_bookkeeping", "_ask_user_about_gate",
    "_push_gate_to_websocket", "_run_multi_tool_use",
)


def bind_chat_stages(fake):
    """Give a duck-typed agent the real chat-turn stages, bound to its own state."""
    from vaf.core.agent import Agent

    for name in CHAT_STAGES:
        setattr(fake, name, getattr(Agent, name).__get__(fake))
    return fake
