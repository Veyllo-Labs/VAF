# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Front Office switch: the contact door of the ingress policy, read and written as one
state, and the card in Settings, Connections that shows it.

Before the switch, the door lived only in config.json: the default policy (paired_only, no
contact fallback) turned away every contact with "Can reach your assistant" while the
contact book's hint said the agent answers them. The framework half is two pure functions
in channel_ingress_policy; the harness half is a route, a security event and the card.
"""
import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core.channel_ingress_policy import (
    FRONT_OFFICE_CHANNELS,
    _SUPPORTED_CHANNELS,
    evaluate_ingress,
    front_office_state,
    normalize_policy,
    set_front_office,
)
from vaf.core.config import Config
from vaf.core.messaging_connections import ROUTABLE_CHANNELS
from vaf.core.platform import Platform

REPO = Path(__file__).resolve().parents[1]
SCOPE = "11111111-2222-3333-4444-555555555555"
TENANT = "22222222-3333-4444-5555-666666666666"


# ── the framework half: pure over the policy dict ──────────────────────────────────────

def test_the_default_policy_keeps_every_front_office_door_shut():
    state = front_office_state(None)
    assert state == {"enabled": False, "channels": {"whatsapp": False, "telegram": False},
                     "contacts_only": {"whatsapp": False, "telegram": False}}


def test_opening_one_channel_opens_only_that_door():
    policy = set_front_office(None, True, "whatsapp")
    assert front_office_state(policy) == {"enabled": True, "channels": {"whatsapp": True, "telegram": False},
                                          "contacts_only": {"whatsapp": False, "telegram": False}}
    # The door is the contact rule of evaluate_ingress, nothing else.
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=True) == (True, "contact_fallback_override")
    assert evaluate_ingress("telegram", policy, explicit_match=False, contact_match=True) == (False, "not_paired")
    # MUTATION: a setter that forgets the flag leaves the default state; this goes red.
    assert policy["whatsapp"]["allow_contact_fallback"] is True


def test_the_master_switch_opens_and_closes_every_front_office_channel():
    opened = set_front_office(None, True)
    assert front_office_state(opened)["channels"] == {"whatsapp": True, "telegram": True}
    closed = set_front_office(opened, False)
    assert front_office_state(closed) == front_office_state(None)


def test_opening_never_touches_the_modes_or_the_throttle():
    raw = {"mode": "paired_only", "throttle_seconds": 120, "telegram": {"mode": "paired_only"}}
    policy = set_front_office(raw, True)
    assert policy["mode"] == "paired_only", "permissive is the warned state; the switch must not write it"
    assert policy["throttle_seconds"] == 120
    assert policy["telegram"]["mode"] == "paired_only"
    assert raw == {"mode": "paired_only", "throttle_seconds": 120, "telegram": {"mode": "paired_only"}}, "pure: input untouched"


def test_closing_under_a_permissive_global_mode_really_closes():
    """The expert setting `mode: permissive` lets contacts in everywhere; switching a channel
    off must win over it, or the UI shows "off" while the bridge answers."""
    policy = set_front_office({"mode": "permissive"}, False, "whatsapp")
    assert front_office_state(policy)["contacts_only"] == {"whatsapp": False, "telegram": True}
    assert front_office_state(policy)["channels"] == {"whatsapp": False, "telegram": False}
    assert policy["whatsapp"] == {"mode": "paired_only", "allow_contact_fallback": False, "open_to_new_senders": False}
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=True) == (False, "not_paired")
    assert policy["mode"] == "permissive", "the global mode is the admin's: never rewritten"


def test_closing_under_a_channel_permissive_override_really_closes():
    policy = set_front_office({"telegram": {"mode": "permissive"}}, False, "telegram")
    assert front_office_state(policy)["channels"]["telegram"] is False
    assert evaluate_ingress("telegram", policy, explicit_match=False, contact_match=True) == (False, "not_paired")


def test_permissive_reads_as_contacts_only_never_as_an_open_channel():
    state = front_office_state({"mode": "permissive"})
    assert state["contacts_only"] == {"whatsapp": True, "telegram": True} and state["channels"] == {"whatsapp": False, "telegram": False}
    state = front_office_state({"whatsapp": {"mode": "permissive"}})
    assert state["contacts_only"] == {"whatsapp": True, "telegram": False}


def test_a_channel_without_a_contact_lane_is_refused():
    with pytest.raises(ValueError):
        set_front_office(None, True, "discord")
    with pytest.raises(ValueError):
        set_front_office(None, True, "signal")


def test_the_setter_returns_a_normalized_policy_and_leaves_discord_alone():
    policy = set_front_office({"discord": {"allow_contact_fallback": True}}, True)
    assert set(policy) == set(normalize_policy(None))
    assert policy["discord"] == {"mode": "inherit", "allow_contact_fallback": True, "open_to_new_senders": False}


def test_front_office_channels_are_a_strict_subset_of_the_routable_channels():
    assert set(FRONT_OFFICE_CHANNELS) < set(_SUPPORTED_CHANNELS) == set(ROUTABLE_CHANNELS)


# ── the tuple against the bridges ──────────────────────────────────────────────────────

_BRIDGES = {
    "whatsapp": REPO / "vaf" / "api" / "whatsapp_bridge.py",
    "telegram": REPO / "vaf" / "api" / "telegram_bridge.py",
    "discord": REPO / "vaf" / "api" / "discord_bridge.py",
}


def _bridge_reports_a_contact_match(path: Path) -> bool:
    """Whether any evaluate_ingress call in this bridge passes a contact_match that is not
    the literal False: the one way a contact can be let in on that channel."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if callee != "evaluate_ingress":
            continue
        for kw in node.keywords:
            if kw.arg == "contact_match":
                if not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                    return True
    return False


@pytest.mark.parametrize("channel", sorted(ROUTABLE_CHANNELS))
def test_a_channel_is_a_front_office_channel_iff_its_bridge_reports_a_contact_match(channel):
    """MUTATION: add "discord" to FRONT_OFFICE_CHANNELS and this goes red; give the Discord
    bridge a contact lookup without listing the channel and it goes red the other way."""
    assert (channel in FRONT_OFFICE_CHANNELS) == _bridge_reports_a_contact_match(_BRIDGES[channel]), channel


# ── the Telegram lookup reads the caller's book, scope included ────────────────────────

def test_the_telegram_contact_lookup_sees_a_book_saved_under_a_scope(monkeypatch, tmp_path):
    """A tenant's contacts are saved under scopes/<uuid>/contacts.json. The lookup used to
    read by username only, so those contacts were counted as reachable everywhere and never
    admitted on Telegram. MUTATION: drop the scope from the front_office_endpoints call in
    get_contact_whitelist_telegram_entry and this goes red."""
    from vaf.core import contacts_store
    from vaf.core.messaging_connections import get_contact_whitelist_telegram_entry

    state = {
        "local_admin_scope_id": SCOPE,
        "local_admin_username": "admin",
        "telegram_config": {"enabled": True, "whitelist": [{"telegram_user_id": "1", "user_scope_id": TENANT, "vaf_username": "bob"}]},
    }
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    contacts_store.create_contact("Carol", "bob", user_scope_id=TENANT, telegram_user_id="777", allow_as_assistant_user=True)
    contacts_store.create_contact("Dave", "bob", user_scope_id=TENANT, telegram_user_id="778", allow_as_assistant_user=False)
    assert (tmp_path / "data" / "scopes" / TENANT / "contacts.json").is_file()

    entry = get_contact_whitelist_telegram_entry("777")
    assert entry == {"user_scope_id": TENANT, "vaf_username": "bob", "telegram_user_id": "777", "from_contact": True}
    assert get_contact_whitelist_telegram_entry("778") is None, "the flag decides, not the record"
    assert get_contact_whitelist_telegram_entry("779") is None


# ── the route ──────────────────────────────────────────────────────────────────────────

def _req(username="alice", scope=SCOPE, role="admin"):
    return SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": scope, "username": username, "role": role}))


@pytest.fixture
def config(monkeypatch, tmp_path):
    """An in-memory config: the route reads through Config.get/load and writes through Config.save."""
    state = {
        "local_admin_scope_id": SCOPE,
        "local_admin_username": "alice",
        "whatsapp_config": {"enabled": True, "reply_window_hours": 48},
        "telegram_config": {"enabled": False},
        "connection_enabled_by_scope": {TENANT: {"telegram": True, "whatsapp": False}},
        # No memory store in a unit test: the knowledge list stays empty instead of probing a database.
        "memory_enabled": False,
    }
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.items()}))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: state.update(cfg)))
    return state


def _events(monkeypatch):
    from vaf.api import front_office_routes as routes
    events = []
    monkeypatch.setattr(routes, "log_security_event", lambda kind, **f: events.append((kind, f)))
    return events


def test_the_state_carries_the_doors_the_other_door_and_the_callers_own_book(config, monkeypatch):
    from vaf.api import front_office_routes as routes
    from vaf.core import contacts_store
    contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000001", allow_as_assistant_user=True)
    contacts_store.create_contact("Dave", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000002")
    contacts_store.create_contact("Erin", "bob", user_scope_id=TENANT, whatsapp_phone="+491700000003", allow_as_assistant_user=True)

    state = asyncio.run(routes.get_front_office(_req()))
    assert state["enabled"] is False
    assert state["channels"] == {"whatsapp": False, "telegram": False}
    assert state["channels_connected"] == {"whatsapp": True, "telegram": False}, "the admin reads the global flags"
    assert state["whatsapp_inbound_to_agent"] is True
    assert state["reply_window_hours"] == 48.0
    assert state["reachable_contacts"] == 1, "Dave has no flag, Erin is another book"
    assert state["admin"] is True

    tenant = asyncio.run(routes.get_front_office(_req("bob", TENANT, "user")))
    assert tenant["reachable_contacts"] == 1, "the tenant's own book, never the admin's"
    assert tenant["channels_connected"] == {"whatsapp": False, "telegram": True}, "a tenant reads their own sliders"
    assert tenant["admin"] is False


def test_the_switch_writes_the_policy_and_records_one_event_per_changed_channel(config, monkeypatch):
    from vaf.api import front_office_routes as routes
    events = _events(monkeypatch)
    admin = {"username": "alice", "role": "admin"}

    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True), _req(), admin))
    assert out["enabled"] is True and out["channels"] == {"whatsapp": True, "telegram": True}
    assert front_office_state(config["channel_ingress_policy"])["enabled"] is True, "saved through Config.save"
    assert sorted(events, key=lambda e: e[1]["channel"]) == [
        ("front_office_changed", {"channel": "telegram", "username": "alice", "detail": "on, 0 contacts granted"}),
        ("front_office_changed", {"channel": "whatsapp", "username": "alice", "detail": "on, 0 contacts granted"}),
    ]

    events.clear()
    asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True), _req(), admin))
    assert events == [], "a write that changes nothing records nothing"

    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=False, channel="telegram"), _req(), admin))
    assert out["channels"] == {"whatsapp": True, "telegram": False}
    assert events == [("front_office_changed", {"channel": "telegram", "username": "alice", "detail": "off"})]
    assert config["channel_ingress_policy"]["mode"] == "paired_only", "the mode is not the switch's to write"


def test_a_channel_without_a_contact_lane_is_a_400(config, monkeypatch):
    from fastapi import HTTPException
    from vaf.api import front_office_routes as routes
    _events(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True, channel="discord"), _req(), {"role": "admin"}))
    assert exc.value.status_code == 400
    assert "channel_ingress_policy" not in config


def test_the_write_is_behind_the_admin_dependency():
    """The policy is an instance-wide key; the route declares the same floor every admin
    endpoint uses instead of checking a role by hand."""
    import inspect
    from vaf.api import front_office_routes as routes
    from vaf.api.user_routes import require_admin
    params = inspect.signature(routes.put_front_office).parameters
    deps = [p.default.dependency for p in params.values() if getattr(p.default, "dependency", None)]
    assert require_admin in deps
    assert not [p for p in inspect.signature(routes.get_front_office).parameters.values() if getattr(p.default, "dependency", None)], \
        "reading is every signed-in user's: the contact book and its hint are per user"


def test_the_router_is_mounted():
    source = (REPO / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "from vaf.api.front_office_routes import router as front_office_router" in source
    assert "app.include_router(front_office_router)" in source


# ── the card and the window ────────────────────────────────────────────────────────────

_PANEL = REPO / "web" / "components" / "connections" / "ConnectionsPanel.tsx"
_WINDOW = REPO / "web" / "components" / "connections" / "FrontOfficeDashboard.tsx"
_CONTACTS = REPO / "web" / "components" / "connections" / "ContactsDashboard.tsx"
_SETTINGS = REPO / "web" / "components" / "SettingsModal.tsx"


def test_the_card_sits_under_contacts_and_is_special_cased_everywhere_contacts_is():
    """The panel decides a card's look by id in five places; a new id that is missing from one
    of them renders as an unconfigured app with a dead Connect button. The card carries no
    switch of its own (the window does); it reads the state for its status line."""
    source = _PANEL.read_text(encoding="utf-8")
    contacts_at = source.index("id: 'contacts'")
    card_at = source.index("id: 'front_office'")
    assert contacts_at < card_at < source.index("id: 'discord'"), "right under Contacts, in the contacts category"
    assert "category: 'contacts'" in source[card_at:card_at + 400]
    assert re.search(r"if \(app\.id === 'front_office'\) return true;\s*$", source[source.index("const isConfigured"):source.index("const isEnabled")], re.M)
    assert "if (app.id === 'front_office') return frontOffice.enabled;" in source
    assert "if (app.id === 'front_office') onOpenFrontOfficeDashboard?.();" in source
    assert "!['contacts', 'front_office'].includes(app.id)" in source, "no Connected/Disconnected pill"
    assert "app.id === 'front_office' ? (" in source, "its own right-hand arm: the gear"
    assert "role?: string" in source, "the card is the admin's: the panel needs the role"
    assert "currentUser?.role === 'admin'" in source
    assert "api('api/front-office')" in source, "the status line reads the state"
    assert "method: 'PUT'" not in source[source.index("fetchFrontOffice"):source.index("const getAppsByCategory")], "the card writes nothing; the switch is in the window"
    assert "ConfirmDialog" not in source


def test_the_window_owns_the_switch_the_instructions_and_the_knowledge():
    source = _WINDOW.read_text(encoding="utf-8")
    assert "useEscapeLayer({ active: isOpen" in source
    assert "level: 60" in source, "a z-[60] window answers at 60"
    assert "addEventListener('keydown'" not in source, "hand-rolled listeners only shrink"
    assert "ConfirmDialog" in source and 'zIndexClass="z-[70]"' in source and "escapeLevel={70}" in source
    assert "useTranslations('settings.frontOffice')" in source
    assert "max-md:h-[100dvh]" in source, "the full-screen sheet on a phone (MOBILE_UI.md)"
    assert "setConfirm({ kind: 'open', channel: row.id as FrontOfficeChannel })" in source, "switching a channel on asks once"
    assert "setChannel(row.id as FrontOfficeChannel, false)" in source, "switching it off does not"
    assert "const CHANNEL_ROWS" in source and "frontOffice: false" in source, "every channel is listed, with or without a Front Office lane"
    assert "grid-cols-1 md:grid-cols-3" in source and "md:col-span-2" in source, "two columns on a desktop, one on a phone"
    assert "masterLabel" not in source, "no switch for the whole of Front Office: the channel is the unit"
    assert "api/front-office/profile" in source and "{ briefing }" in source and "use_general_memory: on" in source
    assert "api/front-office/knowledge" in source and "content_base64" in source
    assert "'DELETE'" in source and "setConfirm({ kind: 'remove', doc: k })" in source


def test_the_window_is_mounted_from_settings_and_the_contacts_hint_reads_the_door():
    settings = _SETTINGS.read_text(encoding="utf-8")
    assert "showFrontOfficeDashboard" in settings
    assert "onOpenFrontOfficeDashboard={() => setShowFrontOfficeDashboard(true)}" in settings
    assert "<FrontOfficeDashboard" in settings
    index = (REPO / "web" / "components" / "connections" / "index.ts").read_text(encoding="utf-8")
    assert "export { default as FrontOfficeDashboard } from './FrontOfficeDashboard';" in index
    contacts = _CONTACTS.read_text(encoding="utf-8")
    assert "api('api/front-office')" in contacts
    assert "tc('reachHintOnDoorClosed')" in contacts


# ── the profile and the knowledge behind the window ─────────────────────────────────────

def test_the_state_carries_the_callers_profile_and_knowledge(config, monkeypatch, tmp_path):
    from vaf.api import front_office_routes as routes
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    rows = [{"doc_tag": "fo-prices", "title": "Prices", "status": "complete", "sections": 4,
             "learned_pages": 2, "total_pages": 2, "learned_at": "2026-09-17T10:00:00",
             "batches_done": 0, "batches_total": 0, "error": None}]

    async def _rows(caller):
        return rows
    monkeypatch.setattr(routes, "_knowledge_rows", _rows)
    config["memory_enabled"] = True

    state = asyncio.run(routes.get_front_office(_req()))
    assert state["profile"] == {"briefing": "", "use_general_memory": False}
    assert state["knowledge"] == rows and state["knowledge_error"] is None
    assert state["memory_enabled"] is True and state["briefing_max_chars"] == 8000

    out = asyncio.run(routes.put_front_office_profile(routes.FrontOfficeProfileUpdate(briefing="Be brief."), _req()))
    assert out["profile"]["briefing"] == "Be brief."
    out = asyncio.run(routes.put_front_office_profile(routes.FrontOfficeProfileUpdate(use_general_memory=True), _req()))
    assert out["profile"] == {"briefing": "Be brief.", "use_general_memory": True}
    assert (tmp_path / "app" / "users" / "alice" / "front_office.json").is_file(), "the caller's own file"
    assert asyncio.run(routes.get_front_office(_req("bob", TENANT, "user")))["profile"]["briefing"] == "", "per user"

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.put_front_office_profile(routes.FrontOfficeProfileUpdate(), _req()))
    assert exc.value.status_code == 400


def test_a_document_is_stored_under_the_caller_and_learned_into_the_front_office_lane(config, monkeypatch, tmp_path):
    import base64
    from vaf.api import front_office_routes as routes
    from vaf.tools import learn_job as lj
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    config["memory_enabled"] = True
    started = []
    monkeypatch.setattr(lj, "start_background_learn", lambda spec, *, user_scope_id, on_done=None: (started.append((spec, user_scope_id)) or True))
    monkeypatch.setattr(lj, "background_learn_running", lambda tag, scope: False)

    body = routes.KnowledgeUpload(filename="Price List.pdf", content_base64=base64.b64encode(b"%PDF-1.4 fake").decode())
    out = asyncio.run(routes.add_front_office_knowledge(body, _req()))
    assert out == {"doc_tag": "fo-price-list", "title": "Price List", "started": True}
    spec, scope = started[0]
    assert spec.source == "front_office" and spec.doc_tag == "fo-price-list" and spec.document_title == "Price List"
    assert str(scope) == SCOPE
    stored = Path(spec.path)
    assert stored.read_bytes() == b"%PDF-1.4 fake"
    assert stored.parent == tmp_path / "app" / "users" / "alice" / "front_office" / "knowledge"

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.add_front_office_knowledge(
            routes.KnowledgeUpload(filename="slides.pptx", content_base64=base64.b64encode(b"x").decode()), _req()))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.add_front_office_knowledge(routes.KnowledgeUpload(filename="a.txt", content_base64="***"), _req()))
    assert exc.value.status_code == 400
    out = asyncio.run(routes.add_front_office_knowledge(
        routes.KnowledgeUpload(filename="../../evil.md", content_base64=base64.b64encode(b"x").decode()), _req()))
    assert out["doc_tag"] == "fo-evil"
    assert Path(started[-1][0].path).parent == stored.parent, "a traversal in the name lands in the caller's folder"
    assert not (tmp_path / "evil.md").exists() and not (tmp_path / "app" / "evil.md").exists()


def test_removing_a_document_stops_its_learn_and_forgets_the_lane_rows(config, monkeypatch, tmp_path):
    from vaf.api import front_office_routes as routes
    from vaf.tools import learn_job as lj
    import vaf.memory.database as database
    import vaf.memory.rag as ragmod
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    cancelled = []
    monkeypatch.setattr(lj, "cancel_background_learn", lambda tag, scope: cancelled.append(tag) or True)
    deleted = []

    class _Pipeline:
        def __init__(self, db):
            pass

        async def delete_by_tag(self, tag, soft=True, user_scope_id=None):
            deleted.append((tag, soft, str(user_scope_id)))
            return 5

    class _Db:
        def __init__(self, user_scope_id=None):
            pass

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(database, "get_db", _Db)
    monkeypatch.setattr(ragmod, "RagPipeline", _Pipeline)

    out = asyncio.run(routes.remove_front_office_knowledge("fo-prices", _req()))
    assert out == {"doc_tag": "fo-prices", "removed": 5}
    assert cancelled == ["fo-prices"] and deleted == [("fo-prices", True, SCOPE)]

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.remove_front_office_knowledge("doc-book", _req()))
    assert exc.value.status_code == 400, "the owner's own documents are not this window's to delete"
    assert deleted == [("fo-prices", True, SCOPE)]


# ── an open channel: every sender answered, one person kept out, new ones enrolled ──────

def test_switching_a_channel_on_opens_it_to_new_senders_and_implies_the_contact_door():
    policy = set_front_office(None, True, "whatsapp")
    state = front_office_state(policy)
    assert state["channels"] == {"whatsapp": True, "telegram": False}
    assert state["contacts_only"] == {"whatsapp": False, "telegram": False}
    assert policy["whatsapp"] == {"mode": "inherit", "allow_contact_fallback": True, "open_to_new_senders": True}
    # A stranger is answered as a Front Office contact; a person switched off is not.
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=False) == (True, "front_office_open")
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=False, sender_opted_out=True) == (False, "not_paired")
    assert evaluate_ingress("telegram", policy, explicit_match=False, contact_match=False) == (False, "not_paired")
    # The owner's own pairing and the reply window keep their reasons.
    assert evaluate_ingress("whatsapp", policy, explicit_match=True, contact_match=False)[1] == "explicit_pair"
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=False, conversation_match=True)[1] == "open_conversation"
    closed = set_front_office(policy, False, "whatsapp")
    assert closed["whatsapp"]["open_to_new_senders"] is False and closed["whatsapp"]["allow_contact_fallback"] is False


def test_the_expert_contact_door_alone_reads_as_contacts_only_and_never_opens_the_channel():
    policy = {"whatsapp": {"allow_contact_fallback": True}}
    state = front_office_state(policy)
    assert state["channels"]["whatsapp"] is False and state["contacts_only"]["whatsapp"] is True
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=False) == (False, "not_paired")


def test_the_open_door_never_applies_outside_the_front_office_channels():
    """MUTATION: drop the FRONT_OFFICE_CHANNELS guard in resolve_channel_policy and Discord,
    whose bridge treats every accepted sender as the admin, would admit strangers."""
    policy = {"discord": {"open_to_new_senders": True}, "whatsapp": {"open_to_new_senders": True}}
    assert evaluate_ingress("discord", policy, explicit_match=False, contact_match=False) == (False, "not_paired")
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=False) == (True, "front_office_open")


def test_switching_a_channel_on_grants_every_contact_of_that_channel_in_the_callers_book(config, monkeypatch):
    from vaf.api import front_office_routes as routes
    from vaf.core import contacts_store
    events = _events(monkeypatch)
    carol = contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000001")
    dave = contacts_store.create_contact("Dave", "alice", user_scope_id=SCOPE, telegram_user_id="777")
    erin = contacts_store.create_contact("Erin", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000002", allow_as_assistant_user=True)
    contacts_store.create_contact("Frank", "bob", user_scope_id=TENANT, whatsapp_phone="+491700000003")

    state = asyncio.run(routes.get_front_office(_req()))
    assert state["channel_contacts"] == {"whatsapp": {"total": 2, "allowed": 1}, "telegram": {"total": 1, "allowed": 0}}

    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True, channel="whatsapp"), _req(), {"role": "admin"}))
    assert out["channels"] == {"whatsapp": True, "telegram": False}
    assert out["channel_contacts"]["whatsapp"] == {"total": 2, "allowed": 2}, "Carol was granted, Erin already was"
    assert out["channel_contacts"]["telegram"] == {"total": 1, "allowed": 0}, "Dave has no WhatsApp key"
    assert contacts_store.get_contact_by_id(carol["id"], "alice", user_scope_id=SCOPE)["allow_as_assistant_user"] is True
    assert contacts_store.get_contact_by_id(dave["id"], "alice", user_scope_id=SCOPE)["allow_as_assistant_user"] is False
    assert contacts_store.list_contacts("bob", user_scope_id=TENANT)[0]["allow_as_assistant_user"] is False, "another book is not the admin's to grant"
    assert events == [("front_office_changed", {"channel": "whatsapp", "username": "alice", "detail": "on, 1 contacts granted"})]

    # The owner switches Carol off; switching the channel off and on again does not undo
    # that on its own, and off leaves every flag alone.
    contacts_store.update_contact(carol["id"], "alice", user_scope_id=SCOPE, allow_as_assistant_user=False)
    events.clear()
    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=False, channel="whatsapp"), _req(), {"role": "admin"}))
    assert out["channel_contacts"]["whatsapp"] == {"total": 2, "allowed": 1}
    assert events == [("front_office_changed", {"channel": "whatsapp", "username": "alice", "detail": "off"})]
    assert erin["id"] and contacts_store.get_contact_by_id(erin["id"], "alice", user_scope_id=SCOPE)["allow_as_assistant_user"] is True


def test_the_store_finds_a_sender_regardless_of_the_flag_and_enrols_a_new_one_once(config):
    from vaf.core import contacts_store
    contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+49 170 0000001")
    rec = contacts_store.find_contact_by_channel("whatsapp", "491700000001@s.whatsapp.net", "alice", SCOPE)
    assert rec and rec["name"] == "Carol" and rec["allow_as_assistant_user"] is False, "the opt-out question sees the flag OFF"
    assert contacts_store.find_contact_by_channel("telegram", "777", "alice", SCOPE) is None

    new = contacts_store.enrol_front_office_contact("whatsapp", "+491700000009", "Grace", "alice", SCOPE)
    assert new["allow_as_assistant_user"] is True and new["source"] == "front_office" and new["name"] == "Grace"
    assert [ch["value"] for ch in new["channels"]] == ["+491700000009"]
    again = contacts_store.enrol_front_office_contact("whatsapp", "491700000009@s.whatsapp.net", "Grace again", "alice", SCOPE)
    assert again["id"] == new["id"], "enrolment is idempotent"
    nameless = contacts_store.enrol_front_office_contact("telegram", "778", "", "alice", SCOPE)
    assert nameless["name"] == "778"


def test_the_bridges_ask_the_opt_out_question_and_enrol_who_the_open_door_let_in():
    wa = (REPO / "vaf" / "api" / "whatsapp_bridge.py").read_text(encoding="utf-8")
    assert "sender_opted_out=opted_out" in wa
    assert 'find_contact_by_channel("whatsapp", chat_id, username, user_scope_id)' in wa
    assert 'if policy_reason == "front_office_open"' in wa and "enrol_front_office_contact(" in wa
    tg = (REPO / "vaf" / "api" / "telegram_bridge.py").read_text(encoding="utf-8")
    assert "def _open_front_office_entry" in tg and "sender_opted_out=opted_out" in tg
    assert "len(owners) != 1" in tg, "a shared bot with several owners cannot attribute a stranger"
    assert tg.count("_resolve_telegram_user(telegram_user_id, user)") == 6, "every handler hands the sender over"


def test_a_stranger_on_telegram_is_enrolled_for_the_one_owner_and_kept_out_when_switched_off(monkeypatch, tmp_path):
    from vaf.api import telegram_bridge as tg
    from vaf.core import contacts_store
    state = {
        "local_admin_scope_id": SCOPE,
        "local_admin_username": "alice",
        "channel_ingress_policy": set_front_office(None, True, "telegram"),
        "telegram_config": {"enabled": True, "whitelist": [{"telegram_user_id": "1", "user_scope_id": SCOPE, "vaf_username": "alice"}]},
    }
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    sender = SimpleNamespace(id=555, full_name="Grace Hopper", username="grace")

    entry, relay = tg._resolve_telegram_user("555", sender)
    assert entry == {"user_scope_id": SCOPE, "vaf_username": "alice", "telegram_user_id": "555", "from_contact": True} and relay is False
    rec = contacts_store.find_contact_by_channel("telegram", "555", "alice", SCOPE)
    assert rec and rec["name"] == "Grace Hopper" and rec["allow_as_assistant_user"] is True and rec["source"] == "front_office"
    assert events == [("contact_access_changed", {"channel": "telegram", "username": "alice", "path": rec["id"],
                                                  "detail": "granted by the open Front Office: Grace Hopper"})]
    events.clear()
    assert tg._resolve_telegram_user("555", sender)[0] == entry and events == [], "enrolled once"

    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, allow_as_assistant_user=False)
    assert tg._resolve_telegram_user("555", sender) == (None, False), "switched off in the book: kept out"

    state["telegram_config"]["whitelist"].append({"telegram_user_id": "2", "user_scope_id": TENANT, "vaf_username": "bob"})
    assert tg._resolve_telegram_user("556", sender) == (None, False), "two owners: a stranger cannot be attributed"
