# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The two properties `file_access` stands on, neither of which existed until this file.

`BaseTool.file_access` turns "this tool touches files" into a declaration and installs the
per-user boundary around `run()`, so it holds on every lane - including the direct `.run()`
calls the coder, the workflow engine and automations make, which no dispatcher ever sees.

It was built and measured, and then three mutations were run against the full suite. One
(breaking the key the wrapper reads) turned nine tests red. The other two stayed GREEN:

  - neutralising the narrowing rule in `user_jail`
  - neutralising the hard error that rejects a mode without an identity

By the house rule those two properties therefore did not exist, however carefully they were
written. This file is what makes them exist. Both are the load-bearing half:

WHY THE HARD ERROR IS LOAD-BEARING. `user_jail` installs nothing for a falsy scope - correct,
because a direct consumer has no user - so a tool that declares a mode but never receives an
identity runs completely unconfined WHILE LOOKING CONFINED. That is the exact failure the
declaration exists to remove, reproduced one layer up. And it would be inherited: custom
tools are loaded as "the first BaseTool subclass found", so a stranger writes one line, reads
in the embedding guide that it confines, and gets nothing. Class-definition time is the only
moment this is noticed before production.

WHY THE NARROWING IS LOAD-BEARING. It closes a widening that is live today rather than a
hypothetical one. The librarian installs the default `write` mode around its whole run; its
read sub-tools enter `read`, which reaches further by the folders of the skills this user may
see. Stated at its true size: those extra roots are skills the caller IS separately entitled
to, decided by another authority - it widened onto the caller's own data, not onto someone
else's. The mechanism is the forbidden one all the same, and the next widening will not be
that harmless.
"""
import pathlib
from unittest.mock import patch

import pytest

from vaf.tools.base import BaseTool
from vaf.tools.filesystem import ReadFileTool, _librarian_scope_ctx, user_jail

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


# ── the hard error ───────────────────────────────────────────────────────────

def test_declaring_a_mode_without_an_identity_is_rejected_at_class_definition():
    """The failure this whole declaration exists to prevent, one layer up."""
    with pytest.raises(TypeError) as err:
        class NoIdentity(BaseTool):
            name = "zz_no_identity"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            file_access = "write"

            def run(self, **kwargs):
                return "written"

    msg = str(err.value)
    assert "identity_kwargs" in msg and "UNCONFINED" in msg, (
        f"the error must name what is missing and what happens without it: {msg}"
    )


def test_a_partial_identity_is_rejected_too():
    """A scope alone is not enough: `compute_user_jail` needs the role to recognise a SECOND
    administrator, who would otherwise be jailed here while every other gate treats them as
    a full admin."""
    with pytest.raises(TypeError):
        class ScopeOnly(BaseTool):
            name = "zz_scope_only"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            file_access = "read"
            identity_kwargs = ("user_scope_id",)

            def run(self, **kwargs):
                return "read"


def test_an_unknown_mode_is_rejected():
    """A typo must fail loudly rather than resolve to "no jail" - the same polarity lesson as
    the workflow switch, where a mistyped value silently meant the permissive branch."""
    with pytest.raises(TypeError) as err:
        class BadMode(BaseTool):
            name = "zz_bad_mode"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            file_access = "lesen"
            identity_kwargs = ("user_scope_id", "user_role")

            def run(self, **kwargs):
                return ""

    assert "'read', 'write' or None" in str(err.value)


def test_a_correct_declaration_is_accepted_and_wrapped():
    """The control. Without it every assertion above would also hold for a base class that
    rejected everything."""
    class Fine(BaseTool):
        name = "zz_fine"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        file_access = "read"
        identity_kwargs = ("user_scope_id", "user_role")

        def run(self, **kwargs):
            return "JAILED" if _librarian_scope_ctx.get(None) else "free"

    assert getattr(Fine.run, "_vaf_jailed", False)
    assert Fine().run() == "free", "a direct consumer with no scope must be unaffected"
    assert Fine().run(user_scope_id=SCOPE, user_role="user") == "JAILED"


# ── nesting narrows, asserted on ACCESS rather than on root counts ───────────

def _skill_file(tmp_path):
    d = tmp_path / "skills" / "shared_skill"
    d.mkdir(parents=True)
    f = d / "reference.md"
    f.write_text("skill reference material")
    return d, f


def test_a_read_inside_an_outer_write_jail_cannot_reach_the_skill_folders(tmp_path):
    """THE regression, and deliberately a behavioural test.

    Counting `allowed_roots` would pass while `_librarian_jail_ok` changed shape underneath
    it; what has to stay true is that the ACCESS is refused. The pair matters: the same read
    must succeed without the outer jail, or this would also pass for a rule that simply
    denies everything.
    """
    skill_dir, skill_file = _skill_file(tmp_path)

    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[skill_dir]):
        alone = ReadFileTool().run(path=str(skill_file), user_scope_id=SCOPE, user_role="user")
        with user_jail(SCOPE, "user", mode="write"):
            nested = ReadFileTool().run(path=str(skill_file), user_scope_id=SCOPE,
                                        user_role="user")

    assert "skill reference material" in alone, (
        f"precondition: read mode reaches visible skill folders on its own - got {alone[:80]!r}"
    )
    assert "denied" in nested.lower() or "[ERROR]" in nested, (
        "the inner read jail added a root the outer write jail did not have; nesting widened"
    )


def test_an_inner_admin_cannot_lift_an_outer_jail():
    """The other direction of the same rule. An inner resolution to admin means "no jail",
    and letting that replace an outer one would turn nesting into an escape hatch."""
    with user_jail(SCOPE, "user", mode="write"):
        outer = _librarian_scope_ctx.get(None)
        with user_jail(SCOPE, "admin", mode="write"):
            inner = _librarian_scope_ctx.get(None)

    assert outer, "precondition: the outer jail is installed"
    assert inner, "the inner admin resolution lifted the outer jail"
    assert inner.get("is_admin") is False


def test_narrowing_does_not_fire_without_an_outer_jail():
    """Guards the control half: the rule must only shrink what it INHERITS. A read tool on
    its own keeps the skill folders, which is the whole reason read mode exists."""
    fake = pathlib.Path("/vaf-test-skill-root")
    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[fake]):
        with user_jail(SCOPE, "user", mode="read"):
            roots = {str(r) for r in _librarian_scope_ctx.get()["allowed_roots"]}
    assert str(fake) in roots
