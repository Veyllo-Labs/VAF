# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A new coder request must plan fresh, not resume a leftover plan.

TaskManager persists tasks.json to survive a crash mid-run (a genuine resume). But a leftover
INCOMPLETE plan from a previous, DIFFERENT request must not be picked up by the next request -
that made the coder "continue the old todos". initialize() now stamps the request onto the state
and resumes only when the next invocation is the SAME request.
"""
from vaf.tools.coder import TaskManager


def test_different_request_plans_fresh(tmp_path):
    d = str(tmp_path)
    tm = TaskManager(d)
    tm.initialize(d, current_task="Task A: build the login page")
    tm.set_todos(["step 1", "step 2"])          # an incomplete plan for Task A
    assert len(tm.todos) == 2

    tm2 = TaskManager(d)
    tm2.initialize(d, current_task="Task B: fix the footer")   # a DIFFERENT request
    assert len(tm2.todos) == 0                  # fresh — did NOT resume Task A's leftover plan


def test_same_request_resumes(tmp_path):
    d = str(tmp_path)
    tm = TaskManager(d)
    tm.initialize(d, current_task="Task A: build the login page")
    tm.set_todos(["step 1", "step 2", "step 3"])   # incomplete plan for Task A

    tm2 = TaskManager(d)
    tm2.initialize(d, current_task="Task A: build the login page")   # SAME request (crash-resume)
    assert len(tm2.todos) == 3                  # resumed the incomplete plan
