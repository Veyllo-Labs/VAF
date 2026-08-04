# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The live-view wire types, pinned across the two languages that share them.

Rule 2 says prefer a CI guard over a prose rule, and this is the cleanest case of
it in the repo: `vaf/core/progress.py` names eight wire types in Python and
`web/app/page.tsx` switches on the same eight literals in TypeScript, and NOTHING
else connects them. A rename on one side is not an error on the other - the
frontend's else-if chain has no default branch, so the payload arrives, matches
nothing and is dropped in silence.

THE INCIDENT CLASS THIS GUARDS. Two fields have already been lost exactly this
way, `diffs` and `activity`, and the comment recording it still sits in
page.tsx's coder handler. Both were backend payload changes that the field-by-field
rebuild on the frontend simply did not forward. A type rename is the same failure
one level up: instead of one missing field, the whole view goes dark.

WHAT THIS DELIBERATELY DOES NOT REQUIRE. An embedder's own view type does not have
to appear in `VAF_LIVE_VIEW_TYPES`. That set is VAF's own eight views, and it is
what the guard can check; a third party declares their own type and ships their own
consumer, which this repo cannot see.
"""
import re
from pathlib import Path

from vaf.core.progress import VAF_LIVE_VIEW_TYPES

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "app" / "page.tsx"
INTERFACE = ROOT / "vaf" / "core" / "web_interface.py"


def test_every_declared_view_type_has_a_frontend_handler() -> None:
    """A type the backend sends and the frontend does not know is a silent drop."""
    src = PAGE.read_text(encoding="utf-8")
    missing = [t for t in sorted(VAF_LIVE_VIEW_TYPES)
               if f"data.type === '{t}'" not in src]
    assert not missing, (
        f"no handler in web/app/page.tsx for: {missing}. The type dispatch there has no "
        "default branch, so these payloads arrive and are dropped without a trace."
    )


def test_every_declared_view_type_is_actually_emitted() -> None:
    """A type nobody sends is a promise the set should not be making."""
    src = INTERFACE.read_text(encoding="utf-8")
    unsent = [t for t in sorted(VAF_LIVE_VIEW_TYPES) if f'"{t}"' not in src]
    assert not unsent, (
        f"declared in VAF_LIVE_VIEW_TYPES but emitted nowhere in web_interface.py: "
        f"{unsent}. Either wire it up or drop it from the set."
    )


def test_the_set_is_complete_for_what_web_interface_emits() -> None:
    """The other direction: a new live view must join the set, or it is unguarded.

    Scoped to the emitters that go through the shared state path, so the many
    unrelated wire types in this module (logs, stats, tool updates) stay out of it.
    """
    src = INTERFACE.read_text(encoding="utf-8")
    emitted = set(re.findall(r'emit_agent_state\(\s*"([a-z_]+)"', src))
    emitted |= set(re.findall(r'_bridge_or_push\(\{\s*"type":\s*"([a-z_]+)"', src))
    unguarded = sorted(emitted - set(VAF_LIVE_VIEW_TYPES))
    assert not unguarded, (
        f"live view types emitted but not declared: {unguarded}. Add them to "
        "VAF_LIVE_VIEW_TYPES in vaf/core/progress.py so this guard covers them."
    )


def test_the_wire_type_is_never_derived_from_a_name() -> None:
    """`subagent_update` is the coder's live editor feed and matches no naming rule.

    Any `f"{kind}_state"` decoration renames it to a type the frontend does not
    handle, and the editor pane goes dark. The map is the identity map on purpose.
    """
    src = INTERFACE.read_text(encoding="utf-8")
    assert '"type": f"' not in src and "'type': f'" not in src, (
        "a wire type is being built with an f-string in web_interface.py; the type must "
        "be a literal, because subagent_update is not derivable from any agent name"
    )


def test_one_transport_fork_not_eight() -> None:
    """The reason this round happened: eight copies of the same six-line fork.

    Shrink-only. If a second copy appears, the sessionId asymmetry and the
    subprocess bridge have two places to be right about instead of one.
    """
    src = INTERFACE.read_text(encoding="utf-8")
    forks = src.count("_BRIDGE_POOL.submit(_post_to_parent")
    assert forks == 1, (
        f"{forks} copies of the sub-agent bridge fork in web_interface.py; there must be "
        "exactly one, in _bridge_or_push"
    )
