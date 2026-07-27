# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared test isolation, plus the one helper eight dispatch tests need.

VAF_LOG_DIR is pointed at a per-session temp directory for the WHOLE suite:
several code paths (security events, timeline, domain logs) write to the real
log directory as a side effect, and tests exercising them must never pollute
the developer's actual logs - the security dashboard counts those files as
real audit data (live incident: suite runs left synthetic skill_blocked events
in the production security log, making the "threats blocked today" counter lie).
Tests that need their own log dir still monkeypatch VAF_LOG_DIR per-test.
"""
import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_log_dir(tmp_path_factory):
    import os
    log_dir = tmp_path_factory.mktemp("vaf-test-logs")
    old = os.environ.get("VAF_LOG_DIR")
    os.environ["VAF_LOG_DIR"] = str(log_dir)
    yield
    if old is None:
        os.environ.pop("VAF_LOG_DIR", None)
    else:
        os.environ["VAF_LOG_DIR"] = old


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
