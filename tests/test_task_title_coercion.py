# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Task titles are coerced to strings at the data-model boundary.

Regression cover for `'dict' object has no attribute 'lower'`: a coder run RESUMED a
`tasks.json` whose title was a dict (written by an earlier run before set_todos
normalization existed). The resume/load path bypassed the set_todos-side fix, so the
dict title reached `get_current_task().lower()`. `Task.__post_init__` now coerces the
title for every construction path — fresh set_todos AND from_dict/resume.
"""
from vaf.core.persistence import Task, ProjectState, coerce_task_title

POISONED = {"text": "Modify getValidMoves() in index.html", "status": "pending"}


def test_task_construct_with_dict_title_coerces():
    t = Task(id=1, title=POISONED)
    assert t.title == "Modify getValidMoves() in index.html"
    assert t.title.lower()  # the exact call that crashed now works
    assert t.title[:50] == t.title[:50]  # sliceable, no KeyError


def test_task_construct_with_string_is_unchanged():
    t = Task(id=1, title="plain task")
    assert t.title == "plain task"


def test_from_dict_load_coerces_poisoned_title():
    # Simulates loading a tasks.json written before the fix.
    ps = ProjectState.from_dict({
        "project_name": "p",
        "tasks": [{"id": 1, "title": POISONED, "status": "pending"}],
    })
    assert ps.tasks[0].title == "Modify getValidMoves() in index.html"
    assert isinstance(ps.tasks[0].title, str)


def test_roundtrip_self_heals_persisted_dict_title():
    # Load poisoned -> to_dict -> the title is now a plain string on disk.
    ps = ProjectState.from_dict({"project_name": "p", "tasks": [{"id": 1, "title": POISONED}]})
    dumped = ps.to_dict()
    assert dumped["tasks"][0]["title"] == "Modify getValidMoves() in index.html"
    # And a second load stays clean.
    ps2 = ProjectState.from_dict(dumped)
    assert ps2.tasks[0].title == "Modify getValidMoves() in index.html"


def test_coerce_helper_shapes():
    assert coerce_task_title("s") == "s"
    assert coerce_task_title({"task": "T"}) == "T"
    assert coerce_task_title({"description": "D"}) == "D"
    assert coerce_task_title(None) == ""
    assert coerce_task_title(7) == "7"
    # dict without a known key -> JSON string (not a crash)
    out = coerce_task_title({"foo": "bar"})
    assert isinstance(out, str) and "foo" in out
