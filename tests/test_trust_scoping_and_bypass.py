# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Standing grants belong to a user, and the admin bypass is visible.

The trust store used to be one machine-global file: an "always" granted while
serving one tenant armed that tool for every tenant of a LAN instance, and
unobservably, because a standing grant short-circuits the gate BEFORE any event
is emitted (docs/EMBEDDING.md listed this under "hard limits you must respect",
while the LAN harness violates its stated precondition of one tenant per
process).

The hands-off switch added here is the same decision made deliberately:
`tool_confirmation_bypass_admins` lets an admin skip the dialog - but it is
admin-writable only, it skips only the QUESTION (admin_only, the account
allowlist and an authorizer's explicit ask() are decided earlier in the funnel),
and every bypass emits `gate_bypassed`, so hands-off never means unobserved.
"""
import json
from pathlib import Path

import pytest

from vaf.core import trust
from vaf.core.config import Config
from vaf.core.tool_dispatch import resolve_confirmation_gate

SCOPE_A = "aaaaaaaa-1111-2222-3333-444444444444"
SCOPE_B = "bbbbbbbb-5555-6666-7777-888888888888"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real per-scope store on disk, so the file layout is exercised too."""
    monkeypatch.setattr(trust.Platform, "config_dir", staticmethod(lambda: tmp_path))
    return tmp_path


def test_one_tenants_always_does_not_answer_for_another(store):
    trust.set_tool_policy("host_bash", "allow", SCOPE_A)
    assert trust.get_tool_policy("host_bash", SCOPE_A) == "allow"
    assert trust.get_tool_policy("host_bash", SCOPE_B) == "ask", \
        "a standing grant leaked across tenants again"
    assert trust.get_tool_policy("host_bash", None) == "ask", \
        "a tenant's grant must not arm the local admin either"


def test_a_trusted_directory_is_trusted_for_its_owner_only(store, tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    trust.mark_trusted_dir(d, SCOPE_A)
    assert trust.is_trusted_dir(d, SCOPE_A) is True
    assert trust.is_trusted_dir(d, SCOPE_B) is False


def test_each_scope_gets_its_own_tagged_file(store):
    trust.set_tool_policy("bash", "allow", SCOPE_A)
    written = list((store / "trust").glob("*.json"))
    assert [p.name for p in written] == [f"{SCOPE_A}.json"]
    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert data["format"] == "trust-2-b17c4e", "the persisted format tag changed"


def test_the_old_machine_global_store_is_retired_not_inherited(store):
    """Deliberate: the old entries were granted under a store that could not
    tell tenants apart, so everyone confirms once more."""
    legacy = store / "trust.json"
    legacy.write_text(json.dumps({"trusted_dirs": ["/srv"],
                                  "tool_policies": {"host_bash": "allow"}}), encoding="utf-8")
    assert trust.get_tool_policy("host_bash", None) == "ask"
    assert not legacy.exists(), "the legacy store is still read"
    assert (store / "trust.json.pre-scope").exists(), "the legacy store was deleted, not kept"


# ── the admin bypass ────────────────────────────────────────────────────────

def _gate(monkeypatch, *, role=None, scope=None, bypass=False, events=None,
          policy="ask", trusted=False):
    monkeypatch.setattr("vaf.core.trust.get_tool_policy",
                        lambda name, user_scope_id=None: policy)
    monkeypatch.setattr("vaf.core.trust.is_trusted_dir",
                        lambda p, user_scope_id=None: trusted)
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    bypass if k == "tool_confirmation_bypass_admins" else d))
    return resolve_confirmation_gate(
        "host_bash", reason="runs a shell", args={"command": "ls"},
        trust_dir=Path("/tmp/p"), allow_once=set(), interactive=False,
        emit=(events.append if events is not None else None),
        user_scope_id=scope, user_role=role)


def test_the_bypass_is_off_by_default(monkeypatch):
    assert _gate(monkeypatch, role="admin") is not None, \
        "an admin skipped the dialog without the switch being on"


def test_an_admin_with_the_switch_on_is_not_asked(monkeypatch):
    assert _gate(monkeypatch, role="admin", bypass=True) is None


def test_a_bypass_is_announced_rather_than_silent(monkeypatch):
    events = []
    _gate(monkeypatch, role="admin", bypass=True, events=events)
    assert [e["type"] for e in events] == ["gate_bypassed"]
    assert events[0]["why"] == "admin_bypass"
    assert events[0]["tool"] == "host_bash"


def test_a_non_admin_never_gets_the_bypass(monkeypatch):
    assert _gate(monkeypatch, role="user", scope=SCOPE_B, bypass=True) is not None


def test_an_explicit_ask_still_wins_over_the_bypass(monkeypatch):
    """An authorizer's ask() means "put this to a person"; a convenience switch
    must not be able to answer it."""
    monkeypatch.setattr("vaf.core.trust.get_tool_policy",
                        lambda name, user_scope_id=None: "allow")
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    True if k == "tool_confirmation_bypass_admins" else d))
    out = resolve_confirmation_gate(
        "host_bash", reason="authorizer asked", args={}, trust_dir=Path("/tmp/p"),
        allow_once=set(), interactive=False, ignore_standing_grants=True,
        user_scope_id=None, user_role="admin")
    assert out is not None, "ask() was overruled by the bypass switch"


def test_the_switch_is_admin_write_only():
    assert Config.DEFAULTS["tool_confirmation_bypass_admins"] is False
    assert Config.is_global_config_key("tool_confirmation_bypass_admins")
    assert Config.filter_for_non_admin({"tool_confirmation_bypass_admins": True}) == {}
