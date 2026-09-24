# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Front Office switch: one channel opened to the people nobody has decided about, read
and written as one state, plus the card in Settings, Connections that shows it.

What the switch is NOT: a permission on anybody. It writes one field per channel and never
touches the contact book. A person the owner allowed is answered with the switch off, a
person they denied is answered on no channel at all, and the switch decides only the third
state, "nobody decided". The framework half is the pure functions in channel_ingress_policy;
the harness half is a route, a security event and the card.
"""
import ast
import json
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

_SHUT = {"whatsapp": False, "telegram": False, "discord": False, "email": False}


def test_the_default_policy_keeps_every_front_office_door_shut():
    state = front_office_state(None)
    assert state == {"enabled": False, "channels": dict(_SHUT),
                     "email_reply_mode": "draft", "email_opened_at": 0}
    assert "contacts_only" not in state, \
        "the narrower state is gone: allowing a person IS it, per person rather than per channel"


def test_opening_one_channel_opens_only_that_door():
    policy = set_front_office(None, True, "whatsapp")
    assert front_office_state(policy) == {"enabled": True, "channels": dict(_SHUT, whatsapp=True),
                                          "email_reply_mode": "draft", "email_opened_at": 0}
    # Open means: whoever writes here and has no decision against them is answered.
    assert evaluate_ingress("whatsapp", policy, explicit_match=False) == (True, "front_office_open")
    assert evaluate_ingress("telegram", policy, explicit_match=False) == (False, "not_paired")
    # MUTATION: a setter that forgets the flag leaves the default state; this goes red.
    assert policy["whatsapp"]["open_to_new_senders"] is True
    assert "allow_contact_fallback" not in policy["whatsapp"], "the second door is gone, not written as False"


def test_the_master_switch_opens_and_closes_every_front_office_channel():
    opened = set_front_office(None, True, now=1_700_000_000)
    assert front_office_state(opened)["channels"] == {"whatsapp": True, "telegram": True, "discord": True, "email": True}
    closed = set_front_office(opened, False)
    assert front_office_state(closed)["channels"] == front_office_state(None)["channels"]
    assert front_office_state(closed)["email_opened_at"] == 1_700_000_000, "the stamp survives a close"


def test_opening_never_touches_the_modes_or_the_throttle():
    raw = {"mode": "paired_only", "throttle_seconds": 120, "telegram": {"mode": "paired_only"}}
    policy = set_front_office(raw, True)
    assert policy["mode"] == "paired_only", "the mode is the floor and nobody's to write, the switch least of all"
    assert policy["throttle_seconds"] == 120
    assert policy["telegram"]["mode"] == "paired_only"
    assert raw == {"mode": "paired_only", "throttle_seconds": 120, "telegram": {"mode": "paired_only"}}, "pure: input untouched"


def test_a_config_written_before_the_doors_were_merged_opens_nothing():
    """`mode: permissive` and the per-channel `allow_contact_fallback` were a second way to say
    "let contacts in", in a place nobody looked. They are read once, coerced, and never
    honoured; the contact's own decision is the only version of that rule left. MUTATION: keep
    "permissive" in _SUPPORTED_MODES and the first assertion goes red."""
    for legacy in ({"mode": "permissive"},
                   {"telegram": {"mode": "permissive"}},
                   {"whatsapp": {"allow_contact_fallback": True}}):
        state = front_office_state(legacy)
        assert state["channels"] == dict(_SHUT), legacy
        assert normalize_policy(legacy)["mode"] == "paired_only"
        for channel in FRONT_OFFICE_CHANNELS:
            assert evaluate_ingress(channel, legacy, explicit_match=False) == (False, "not_paired")
    # And switching a channel off under such a config writes the one field it owns.
    policy = set_front_office({"mode": "permissive"}, False, "whatsapp")
    assert policy["whatsapp"] == {"mode": "inherit", "open_to_new_senders": False}
    assert front_office_state(policy)["channels"] == dict(_SHUT)


def test_a_channel_without_a_contact_lane_is_refused():
    with pytest.raises(ValueError):
        set_front_office(None, True, "signal")


def test_the_setter_returns_a_normalized_policy_and_switches_discord_with_the_rest():
    policy = set_front_office({"discord": {"allow_contact_fallback": True}}, True)
    assert set(policy) == set(normalize_policy(None))
    assert policy["discord"] == {"mode": "inherit", "open_to_new_senders": True}


def test_front_office_channels_are_the_messengers_with_a_contact_lane_plus_mail():
    """Every routable messenger has a contact lane now (Discord's is the local admin's, for
    direct messages), so the messenger Front Office channels are the routable channels; a
    messenger that arrives without a lane is listed in neither tuple until it has one. Mail
    is the one ingress-only channel (an answering lane in vaf/mail/inbound.py, no bridge, no
    send tool), so it is a Front Office channel without being routable."""
    from vaf.core.channel_ingress_policy import MAIL_CHANNEL, MESSENGER_FRONT_OFFICE_CHANNELS
    assert set(MESSENGER_FRONT_OFFICE_CHANNELS) == set(_SUPPORTED_CHANNELS) == set(ROUTABLE_CHANNELS)
    assert set(FRONT_OFFICE_CHANNELS) == set(MESSENGER_FRONT_OFFICE_CHANNELS) | {MAIL_CHANNEL}
    assert MAIL_CHANNEL not in ROUTABLE_CHANNELS


def test_mail_is_a_front_office_channel_because_the_answering_lane_subscribes_to_new_mail():
    """MUTATION: drop the on_new_mail registration from web_server.py, or the lane module,
    and this goes red: a Front Office switch for a channel nothing answers on would switch
    nothing."""
    lane = REPO / "vaf" / "mail" / "inbound.py"
    assert lane.exists() and "def handle_new_mail" in lane.read_text(encoding="utf-8")
    server = (REPO / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "on_new_mail(" in server and "inbound.handle_new_mail" in server


def test_the_mail_switch_stamps_the_moment_and_the_reply_mode_is_its_own_setting():
    from vaf.core.channel_ingress_policy import set_email_reply_mode
    policy = set_front_office(None, True, "email", now=1_700_000_000)
    assert policy["email"]["open_to_new_senders"] and policy["email"]["opened_at"] == 1_700_000_000
    assert front_office_state(policy)["channels"]["email"] and front_office_state(policy)["email_reply_mode"] == "draft"
    again = set_front_office(policy, True, "email", now=1_700_000_500)
    assert again["email"]["opened_at"] == 1_700_000_500, "re-enabling stamps again"
    off = set_front_office(again, False, "email")
    assert not off["email"]["open_to_new_senders"] and off["email"]["opened_at"] == 1_700_000_500
    sending = set_email_reply_mode(off, "send")
    assert front_office_state(sending)["email_reply_mode"] == "send"
    assert normalize_policy({"email": {"reply_mode": "guess", "opened_at": "x"}})["email"] == {
        "mode": "inherit", "open_to_new_senders": False, "reply_mode": "draft", "opened_at": 0}
    with pytest.raises(ValueError):
        set_email_reply_mode(None, "later")


# ── the tuple against the bridges ──────────────────────────────────────────────────────

_BRIDGES = {
    "whatsapp": REPO / "vaf" / "api" / "whatsapp_bridge.py",
    "telegram": REPO / "vaf" / "api" / "telegram_bridge.py",
    "discord": REPO / "vaf" / "api" / "discord_bridge.py",
}


def _bridge_reports_a_contact_match(path: Path) -> bool:
    """Whether this bridge looks the sender up in the contact book: it calls the shared
    admission (contacts_store.admit_front_office_sender) or hands evaluate_ingress an `access`
    that is not the literal None. The one way a person's own decision can be honoured on that
    channel, in either direction: without it a denial is invisible there too."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if callee == "admit_front_office_sender":
            return True
        if callee != "evaluate_ingress":
            continue
        for kw in node.keywords:
            if kw.arg == "access":
                if not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                    return True
    return False


@pytest.mark.parametrize("channel", sorted(ROUTABLE_CHANNELS))
def test_a_channel_is_a_front_office_channel_iff_its_bridge_reports_a_contact_match(channel):
    """MUTATION: drop "discord" from FRONT_OFFICE_CHANNELS and this goes red; take the
    admission out of the Discord bridge without delisting the channel and it goes red the
    other way."""
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
    assert state["channels"] == dict(_SHUT)
    assert state["channels_connected"] == {"whatsapp": True, "telegram": False, "discord": False, "email": False}, "the admin reads the global flags; no mail account, no mail"
    assert state["whatsapp_inbound_to_agent"] is True
    assert state["reply_window_hours"] == 48.0
    assert state["reachable_contacts"] == 1, "Dave has no flag, Erin is another book"
    assert state["admin"] is True

    tenant = asyncio.run(routes.get_front_office(_req("bob", TENANT, "user")))
    assert tenant["reachable_contacts"] == 1, "the tenant's own book, never the admin's"
    # The tenant's Telegram slider is on, but Telegram is one bot for the whole install and it
    # is off: nobody's lane is on then (messaging_connections.channel_enabled_for_scope).
    assert tenant["channels_connected"] == {"whatsapp": False, "telegram": False, "discord": False, "email": False}
    assert tenant["admin"] is False
    config["telegram_config"] = {**(config.get("telegram_config") or {}), "enabled": True}
    tenant = asyncio.run(routes.get_front_office(_req("bob", TENANT, "user")))
    assert tenant["channels_connected"] == {"whatsapp": False, "telegram": True, "discord": False, "email": False}, "a tenant reads their own sliders"


def test_the_switch_writes_the_policy_and_records_one_event_per_changed_channel(config, monkeypatch):
    from vaf.api import front_office_routes as routes
    events = _events(monkeypatch)
    admin = {"username": "alice", "role": "admin"}

    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True), _req(), admin))
    assert out["enabled"] is True and out["channels"] == {"whatsapp": True, "telegram": True, "discord": True, "email": True}
    assert front_office_state(config["channel_ingress_policy"])["enabled"] is True, "saved through Config.save"
    # The detail says what changed and nothing more: the switch grants nobody, so a count of
    # granted contacts would be a promise it does not keep.
    assert sorted(events, key=lambda e: e[1]["channel"]) == [
        ("front_office_changed", {"channel": "discord", "username": "alice", "detail": "on"}),
        ("front_office_changed", {"channel": "email", "username": "alice", "detail": "on"}),
        ("front_office_changed", {"channel": "telegram", "username": "alice", "detail": "on"}),
        ("front_office_changed", {"channel": "whatsapp", "username": "alice", "detail": "on"}),
    ]
    assert config["channel_ingress_policy"]["email"]["opened_at"] > 0, "the mail switch stamps the moment"

    events.clear()
    asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True), _req(), admin))
    assert events == [], "a write that changes nothing records nothing"

    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=False, channel="telegram"), _req(), admin))
    assert out["channels"] == {"whatsapp": True, "telegram": False, "discord": True, "email": True}
    assert events == [("front_office_changed", {"channel": "telegram", "username": "alice", "detail": "off"})]
    assert config["channel_ingress_policy"]["mode"] == "paired_only", "the mode is not the switch's to write"


def test_a_channel_without_a_contact_lane_is_a_400(config, monkeypatch):
    from fastapi import HTTPException
    from vaf.api import front_office_routes as routes
    _events(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True, channel="signal"), _req(), {"role": "admin"}))
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
    assert "const CHANNEL_ROWS" in source and "id: 'discord'" in source and "frontOffice:" not in source, "every listed channel has a Front Office lane"
    assert "grid-cols-1 md:grid-cols-3" in source and "md:col-span-2" in source, "two columns on a desktop, one on a phone"
    assert "masterLabel" not in source, "no switch for the whole of Front Office: the channel is the unit"
    assert "api/front-office/profile" in source and "{ briefing }" in source and "use_general_memory: on" in source
    assert "api/front-office/knowledge" in source and "content_base64" in source
    assert "'DELETE'" in source and "setConfirm({ kind: 'remove', doc: k })" in source


def test_the_window_is_mounted_from_settings_and_the_contacts_hint_reads_the_door():
    """The window opens from Settings, and the contact book explains a person's state with the
    verdict the SERVER folded, never with the raw switch.

    The browser only ever had the policy flag, and the flag is not the whole door: a Telegram
    bot with two owners and a WhatsApp account with forwarding off answer nobody whatever it
    says. The book used to fetch that flag and fold it itself, which is how a hint promised an
    answer the bridge refuses.

    MUTATION: fold the door in the browser again (fetch `api/front-office` and read
    `channels[...]` per contact) and the last two assertions go red.
    """
    settings = _SETTINGS.read_text(encoding="utf-8")
    assert "showFrontOfficeDashboard" in settings
    assert "onOpenFrontOfficeDashboard={() => setShowFrontOfficeDashboard(true)}" in settings
    assert "<FrontOfficeDashboard" in settings
    index = (REPO / "web" / "components" / "connections" / "index.ts").read_text(encoding="utf-8")
    assert "export { default as FrontOfficeDashboard } from './FrontOfficeDashboard';" in index
    contacts = _CONTACTS.read_text(encoding="utf-8")
    # One line per reason, the words the bridge logs.
    for reason, key in (("contact_allowed", "reachHintOn"), ("contact_denied", "reachHintOff"),
                        ("front_office_open", "reachHintUndecided"),
                        ("", "reachHintUndecidedDoorClosed")):
        assert f"tc('{key}')" in contacts, key
        if reason:
            assert f"reach.reason === '{reason}'" in contacts, reason
    assert "contactReach(c)" in contacts and "c.assistant_answers" in contacts
    assert "api('api/front-office')" not in contacts, "the door is folded once, on the server"
    assert "frontOffice" not in contacts and "doorClosed" not in contacts


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


def test_the_listing_reads_the_callers_lane_and_a_missing_scope_reads_only_null_scoped_rows(monkeypatch, tmp_path):
    """The knowledge listing is one SELECT on memories. With a scope it selects that scope's
    rows. Without one (a caller whose scope is not a UUID, whose learn writes rows with
    user_scope_id NULL) it selects exactly the NULL-scoped rows: on the default install the
    data connection is the owner role, which bypasses RLS, so an unfiltered SELECT returned
    every user's Front Office titles. MUTATION: drop the `else` branch in `_knowledge_rows`
    and the IS NULL assertion goes red."""
    from uuid import UUID
    from sqlalchemy.dialects import postgresql
    from vaf.api import front_office_routes as routes
    import vaf.memory.database as database
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    seen = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        async def execute(self, stmt):
            compiled = stmt.compile(dialect=postgresql.dialect())
            seen.append((str(compiled).split("WHERE", 1)[1], dict(compiled.params)))
            return _Result()

    class _Db:
        def __init__(self, user_scope_id=None):
            seen.append(("get_db", user_scope_id))

        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(database, "get_db", _Db)

    assert asyncio.run(routes._knowledge_rows({"username": "alice", "user_scope_id": SCOPE, "is_admin": True})) == []
    (_, scope), (where, params) = seen
    assert scope == UUID(SCOPE) and params["user_scope_id_1"] == UUID(SCOPE) and "IS NULL" not in where

    seen.clear()
    assert asyncio.run(routes._knowledge_rows({"username": "admin", "user_scope_id": None, "is_admin": True})) == []
    (_, scope), (where, params) = seen
    assert scope is None and "memories.user_scope_id IS NULL" in where, "a None scope must not read every scope's lane"
    assert "front_office" in params.values() and "document_index" in params.values()


# ── an open channel: every sender answered, one person kept out, new ones enrolled ──────

def test_switching_a_channel_on_opens_it_to_the_undecided_and_to_nobody_else():
    """The switch writes ONE field and decides ONE of the three states. MUTATION: let the
    `denied` branch fall through to the open channel and the second assertion goes red; make
    the allowed branch depend on the switch and the fourth does."""
    policy = set_front_office(None, True, "whatsapp")
    assert front_office_state(policy)["channels"] == dict(_SHUT, whatsapp=True)
    assert policy["whatsapp"] == {"mode": "inherit", "open_to_new_senders": True}
    assert evaluate_ingress("whatsapp", policy, explicit_match=False) == (True, "front_office_open")
    assert evaluate_ingress("whatsapp", policy, explicit_match=False, access="denied") == (False, "contact_denied")
    assert evaluate_ingress("telegram", policy, explicit_match=False) == (False, "not_paired")
    assert evaluate_ingress("telegram", policy, explicit_match=False, access="allowed") == (True, "contact_allowed"), \
        "the person's own permission needs no switch"
    assert evaluate_ingress("whatsapp", policy, explicit_match=True)[1] == "explicit_pair"
    closed = set_front_office(policy, False, "whatsapp")
    assert closed["whatsapp"] == {"mode": "inherit", "open_to_new_senders": False}


def test_the_open_door_never_applies_outside_the_front_office_channels():
    """Every policy channel is a Front Office channel now, so the FRONT_OFFICE_CHANNELS guard
    in resolve_channel_policy is belt and braces; what this pins is that a channel the policy
    does not know stays shut whatever its entry says, and that the open door reads for a
    listed one."""
    policy = {"signal": {"open_to_new_senders": True}, "whatsapp": {"open_to_new_senders": True}}
    assert evaluate_ingress("signal", policy, explicit_match=False) == (False, "not_paired")
    assert evaluate_ingress("whatsapp", policy, explicit_match=False) == (True, "front_office_open")


def test_the_switch_grants_nobody_and_the_window_counts_the_three_states(config, monkeypatch):
    """The switch used to write "allowed" into every contact of that channel in every book on
    the instance, which outlived switching it off again and turned 161 synced records into
    standing permissions. It writes the policy only now, and the window shows what the book
    actually says. MUTATION: grant the channel's contacts in put_front_office and the
    untouched-record assertions go red."""
    from vaf.api import front_office_routes as routes
    from vaf.core import contacts_store
    events = _events(monkeypatch)
    carol = contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000001")
    dave = contacts_store.create_contact("Dave", "alice", user_scope_id=SCOPE, telegram_user_id="777")
    erin = contacts_store.create_contact("Erin", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000002",
                                         assistant_access="allowed")
    frank = contacts_store.create_contact("Frank", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000004",
                                          assistant_access="denied")
    contacts_store.create_contact("Gina", "bob", user_scope_id=TENANT, whatsapp_phone="+491700000003")

    state = asyncio.run(routes.get_front_office(_req()))
    assert state["channel_contacts"] == {
        "whatsapp": {"total": 3, "allowed": 1, "denied": 1, "undecided": 1},
        "telegram": {"total": 1, "allowed": 0, "denied": 0, "undecided": 1},
        "discord": {"total": 0, "allowed": 0, "denied": 0, "undecided": 0},
        "email": {"total": 0, "allowed": 0, "denied": 0, "undecided": 0}}
    assert state["reachable_contacts"] == 1, "reachable means allowed, never everybody undenied"

    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=True, channel="whatsapp"), _req(), {"role": "admin"}))
    assert out["channels"] == dict(_SHUT, whatsapp=True)
    assert out["channel_contacts"] == state["channel_contacts"], "the switch changed no decision"
    assert events == [("front_office_changed", {"channel": "whatsapp", "username": "alice", "detail": "on"})]
    for record in (carol, dave, frank):
        stored = contacts_store.get_contact_by_id(record["id"], "alice", user_scope_id=SCOPE)
        assert stored.get("assistant_access") == record.get("assistant_access")
        assert stored["allow_as_assistant_user"] is bool(record["allow_as_assistant_user"])
    assert contacts_store.contact_access(contacts_store.list_contacts("bob", user_scope_id=TENANT)[0]) is None, \
        "another book on the instance is not the switch's to write either"

    events.clear()
    out = asyncio.run(routes.put_front_office(routes.FrontOfficeUpdate(enabled=False, channel="whatsapp"), _req(), {"role": "admin"}))
    assert out["channel_contacts"] == state["channel_contacts"], "and off leaves the book alone too"
    assert events == [("front_office_changed", {"channel": "whatsapp", "username": "alice", "detail": "off"})]
    assert contacts_store.contact_access(
        contacts_store.get_contact_by_id(erin["id"], "alice", user_scope_id=SCOPE)) == "allowed"


def test_the_store_finds_a_sender_regardless_of_the_decision_and_enrols_a_new_one_once(config):
    """MUTATION: enrol with `assistant_access="allowed"` and the enrolment assertion goes red.
    A record created because an open channel let somebody in must carry no decision, or
    closing the channel again would leave a standing permission behind."""
    from vaf.core import contacts_store
    contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+49 170 0000001")
    rec = contacts_store.find_contact_by_channel("whatsapp", "491700000001@s.whatsapp.net", "alice", SCOPE)
    assert rec and rec["name"] == "Carol" and contacts_store.contact_access(rec) is None, \
        "the lookup finds the person; the decision is read separately and there is none"
    assert contacts_store.find_contact_by_channel("telegram", "777", "alice", SCOPE) is None
    # A LID is an opaque id, not a number: mapped in `whatsapp_config.lid_to_e164` it finds the
    # person behind the number, unmapped it finds NOBODY. It used to read the LID's digits as a
    # phone, a key no record carries, so the runner could not pin a contact who wrote from a
    # LID chat the dashboard had already assigned. MUTATION: drop the `@lid` branch and both
    # assertions go red (the mapped LID no longer resolves, and the unmapped one builds a
    # phone key out of an id).
    config["whatsapp_config"] = dict(config["whatsapp_config"], lid_to_e164={"123456789012345@lid": "+491700000001"})
    assert contacts_store.find_contact_by_channel("whatsapp", "123456789012345@lid", "alice", SCOPE)["name"] == "Carol"
    assert contacts_store.find_contact_by_channel("whatsapp", "999999999999999@lid", "alice", SCOPE) is None
    contacts_store.create_contact("Lid Digits", "alice", user_scope_id=SCOPE, whatsapp_phone="+999999999999999")
    assert contacts_store.find_contact_by_channel("whatsapp", "999999999999999@lid", "alice", SCOPE) is None, \
        "the digits of an id are not a phone number, whatever record happens to carry them"

    new = contacts_store.enrol_front_office_contact("whatsapp", "+491700000009", "Grace", "alice", SCOPE)
    assert contacts_store.contact_access(new) is None and new["allow_as_assistant_user"] is False
    assert new["source"] == "front_office" and new["name"] == "Grace"
    assert [ch["value"] for ch in new["channels"]] == ["+491700000009"]
    again = contacts_store.enrol_front_office_contact("whatsapp", "491700000009@s.whatsapp.net", "Grace again", "alice", SCOPE)
    assert again["id"] == new["id"], "enrolment is idempotent"
    nameless = contacts_store.enrol_front_office_contact("telegram", "778", "", "alice", SCOPE)
    assert nameless["name"] == "778"
    # Mail addresses match whatever their spelling: the book lowercases them, so must the lookup and the enrolment.
    contacts_store.create_contact("Hans", "alice", user_scope_id=SCOPE, email="Hans@Example.org")
    assert contacts_store.find_contact_by_channel("email", "HANS@example.org", "alice", SCOPE)["name"] == "Hans"
    mailed = contacts_store.enrol_front_office_contact("email", "New@Example.org", "", "alice", SCOPE)
    assert [ch["value"] for ch in mailed["channels"]] == ["new@example.org"] and mailed["name"] == "new@example.org"
    assert contacts_store.enrol_front_office_contact("email", "NEW@EXAMPLE.ORG", "again", "alice", SCOPE)["id"] == mailed["id"]


def test_the_bridges_read_the_decision_and_enrol_who_the_open_door_let_in():
    wa = (REPO / "vaf" / "api" / "whatsapp_bridge.py").read_text(encoding="utf-8")
    assert "access=access," in wa and "contact_access(" in wa
    assert 'find_contact_by_channel("whatsapp", chat_id, username, user_scope_id)' in wa
    assert 'policy_reason == "front_office_open"' in wa and "enrol_front_office_contact(" in wa
    # And the enrolment asks the DOOR, not the flag the reason came from: with
    # `inbound_to_agent` off this bridge hands nothing to the agent, so nobody is admitted
    # there however the policy reads, and a record plus a security event saying the open Front
    # Office let them in would be two lies.
    # MUTATION: drop the condition and this goes red.
    assert 'and front_office_open("whatsapp", ingress_policy)):' in wa
    tg = (REPO / "vaf" / "api" / "telegram_bridge.py").read_text(encoding="utf-8")
    assert "def _open_front_office_entry" in tg and "admit_front_office_sender(" in tg, "the stranger path is the shared admission"
    # The single-owner condition moved into the framework, where the inbox rows and the channel
    # windows read it too: a shared bot with several owners cannot attribute a stranger, and a
    # row that claimed otherwise would say the agent answers in a chat the bridge refuses.
    assert "front_office_open(" in tg and "single_telegram_owner()" in tg
    inbox = (REPO / "vaf" / "core" / "inbox.py").read_text(encoding="utf-8")
    assert "front_office_open(channel)" in inbox, "the rows ask the same function"
    dc = (REPO / "vaf" / "api" / "discord_bridge.py").read_text(encoding="utf-8")
    assert "def _admit_sender" in dc and "admit_front_office_sender(" in dc and "local_admin_identity()" in dc, \
        "Discord admits through the shared admission, in the local admin's book"
    assert "if not is_dm:" in dc, "a guild message is never answered"
    assert dc.count("metadata.update(extra_meta or {})") == 2 and "metadata.update(fo_meta)" in dc, \
        "a contact's text, image and document all run in Front Office mode"
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
    assert rec and rec["name"] == "Grace Hopper" and rec["source"] == "front_office"
    assert contacts_store.contact_access(rec) is None, "the record, not a permission"
    assert events == [("contact_access_changed", {"channel": "telegram", "username": "alice", "path": rec["id"],
                                                  "detail": "added by the open Front Office: Grace Hopper"})]
    events.clear()
    assert tg._resolve_telegram_user("555", sender)[0] == entry and events == [], "enrolled once"

    # Taking the decision back is not a veto: the channel is still open, so she is still
    # answered. Only a denial keeps her out. MUTATION: read the record with bool() and the
    # last assertion goes red, because "denied" is a truthy string.
    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, assistant_access="undecided")
    assert tg._resolve_telegram_user("555", sender)[0] == entry
    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, assistant_access="denied")
    assert tg._resolve_telegram_user("555", sender) == (None, False), "denied in the book: kept out"

    state["telegram_config"]["whitelist"].append({"telegram_user_id": "2", "user_scope_id": TENANT, "vaf_username": "bob"})
    assert tg._resolve_telegram_user("556", sender) == (None, False), "two owners: a stranger cannot be attributed"


# ── the inbox and the Inbound window point at each other ────────────────────────────────

def test_the_inbox_and_the_inbound_window_are_linked_both_ways():
    """The inbox shows what Inbound answers; Inbound decides how. Each opens the other the
    way a chat jump does: the inbox closes first, Settings opens on Connections."""
    page = (REPO / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "setSettingsChatJump({ channel: 'front_office' });" in page
    assert "onOpenInbox={() => { handleSettingsClose(); setIsInboxOpen(true); }}" in page
    settings = _SETTINGS.read_text(encoding="utf-8")
    assert "| { channel: 'front_office' };" in settings
    assert "if (initialChatJump.channel === 'front_office') {" in settings
    assert "onOpenInbox={onOpenInbox ? () => { setShowFrontOfficeDashboard(false); onOpenInbox(); } : undefined}" in settings
    inbox = (REPO / "web" / "components" / "inbox" / "InboxWindow.tsx").read_text(encoding="utf-8")
    assert "onClick={() => { onClose(); onOpenInbound(); }}" in inbox, "the inbox closes before Settings opens"
    assert "t('rail.inbound')" in inbox
    window = _WINDOW.read_text(encoding="utf-8")
    assert "t('openInbox')" in window and "onOpenInbox" in window


def test_the_contacts_control_tracks_every_decision_in_flight():
    """One slot for "the contact being written" cleared the guard of the SECOND contact when
    the first PATCH answered: switch records mid-flight, click, and a second click on the new
    record went through while its own PATCH was still on the wire. A set holds every id in
    flight, each removed by its own request.

    MUTATION: put the single string slot back and this goes red.
    """
    src = (REPO / "web" / "components" / "connections" / "ContactsDashboard.tsx").read_text(encoding="utf-8")
    assert "useState<Set<string>>(() => new Set())" in src
    assert "if (accessBusy.has(id)) return;" in src
    assert "setAccessBusy(prev => new Set(prev).add(id));" in src
    assert "next.delete(id); return next;" in src
    assert "disabled={accessBusy.has(c.id)}" in src
    assert "accessBusy === " not in src and "setAccessBusy(null)" not in src


# ── the one verdict the switch shows ────────────────────────────────────────────────────

def test_a_person_is_answered_or_not_and_the_reason_says_which(config):
    """`assistant_reach` is the whole question the switch asks: does the agent answer this
    person right now. The word the owner wrote holds on every channel; where they wrote none,
    the channel's Inbound answers, and Inbound is more than its flag.

    MUTATION: read `resolve_channel_policy(...)["open_to_new_senders"]` instead of the door
    handed in, and the Telegram assertions go red: the bot is shared, so with two accounts
    paired it answers nobody whatever the flag says.
    """
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    config["channel_ingress_policy"] = set_front_office(set_front_office(None, True, "whatsapp"), True, "telegram")
    config["telegram_config"] = {"whitelist": [
        {"telegram_user_id": "7", "vaf_username": "alice", "user_scope_id": SCOPE},
        {"telegram_user_id": "8", "vaf_username": "bob", "user_scope_id": TENANT}]}
    policy = config["channel_ingress_policy"]

    wa = contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000001")
    tg = contacts_store.create_contact("Dave", "alice", user_scope_id=SCOPE, telegram_user_id="777")

    # Nobody decided: the open WhatsApp door answers, the shared Telegram bot does not.
    assert contacts_store.assistant_reach(wa, raw_policy=policy) == {
        "answers": True, "reason": "front_office_open", "channels": ["whatsapp"]}
    assert contacts_store.assistant_reach(tg, raw_policy=policy) == {
        "answers": False, "reason": "not_paired", "channels": ["telegram"]}
    # WhatsApp forwards nothing at all: the flag still says open, the channel answers nobody.
    config["whatsapp_config"] = {"enabled": True, "inbound_to_agent": False}
    assert contacts_store.assistant_reach(wa, raw_policy=policy)["answers"] is False
    config["whatsapp_config"] = {"enabled": True}

    # The owner's own word outranks both doors, in both directions.
    contacts_store.apply_contact_access(tg, "allowed")
    assert contacts_store.assistant_reach(tg, raw_policy=policy) == {
        "answers": True, "reason": "contact_allowed", "channels": ["telegram"]}
    contacts_store.apply_contact_access(wa, "denied")
    assert contacts_store.assistant_reach(wa, raw_policy=policy) == {
        "answers": False, "reason": "contact_denied", "channels": ["whatsapp"]}
    # A blocked person with no address anywhere still reads as blocked, not as a shut channel.
    lonely = contacts_store.create_contact("Erin", "alice", user_scope_id=SCOPE)
    contacts_store.apply_contact_access(lonely, "denied")
    assert contacts_store.assistant_reach(lonely, raw_policy=policy) == {
        "answers": False, "reason": "contact_denied", "channels": []}
    # And a channel window asks about its own lane only.
    assert contacts_store.assistant_reach(tg, channels=["whatsapp"], raw_policy=policy)["reason"] == "contact_allowed"


def test_the_rows_the_windows_read_carry_the_verdict(config, monkeypatch):
    """The contact book and the WhatsApp window both draw a switch, so both rows carry the
    answer and the reason. Folding it in the browser is what this replaces: the browser has
    the flag, not the door.

    MUTATION: drop `_with_reach` from the contacts route and the first block goes red; drop
    `assistant_reach` from the WhatsApp row and the second does.
    """
    from types import SimpleNamespace

    from vaf.api import contact_routes
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    config["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    contacts_store.create_contact("Carol", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000001")
    blocked = contacts_store.create_contact("Dave", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000002")
    contacts_store.update_contact(blocked["id"], "alice", user_scope_id=SCOPE, assistant_access="denied")

    request = SimpleNamespace(state=SimpleNamespace(user={"username": "alice", "user_scope_id": SCOPE}))
    rows = {r["name"]: r for r in asyncio.run(contact_routes.get_contacts_list(request))}
    assert (rows["Carol"]["assistant_answers"], rows["Carol"]["assistant_reason"]) == (True, "front_office_open")
    assert (rows["Dave"]["assistant_answers"], rows["Dave"]["assistant_reason"]) == (False, "contact_denied")
    one = asyncio.run(contact_routes.get_contact(rows["Carol"]["id"], request))
    assert one["assistant_answers"] is True

    src = (REPO / "vaf" / "api" / "whatsapp_routes.py").read_text(encoding="utf-8")
    assert 'reach = assistant_reach(book, channels=["whatsapp"],' in src
    assert 'rec["assistant_answers"] = bool(reach["answers"])' in src
    assert "front_office_doors(_ingress_policy)" in src, "the doors once per listing, not per chat"


def test_the_contact_book_and_the_channel_window_show_one_switch_not_three_positions():
    """Two answers, because that is the question: does the agent answer this person. The third
    position said "the channel decides", which is a fact about the channel rather than an
    answer about the person, and it made the owner read two controls to learn one thing.
    Switching off writes a refusal: with the channel open, writing "no decision" would spring
    the switch straight back on.

    MUTATION: bring the three-button group back in either window and this goes red.
    """
    for name in ("ContactsDashboard.tsx", "WhatsAppDashboard.tsx"):
        src = (REPO / "web" / "components" / "connections" / name).read_text(encoding="utf-8")
        assert "from '@/components/ui/Switch'" in src, name
        assert "<Switch on=" in src, name
        assert "accessUndecided" not in src and "accessAllowed" not in src and "accessDenied" not in src, name
        assert "'allowed', 'undecided', 'denied'" not in src, name
        assert "'denied')" in src, f"{name}: switching off is a refusal, not a cleared decision"
        # A word on EACH side, the knob pointing at the one that holds: a single word after
        # the switch reads as what pressing it would do, so "No" beside a switch that was
        # already off said the opposite of the truth.
        assert "switchWord" in src, name
        no_at, yes_at = src.index("tcm('no')"), src.index("tcm('yes')")
        assert no_at < src.index("<Switch on=") < yes_at, f"{name}: no on the left, yes on the right"
    # The word is gone from every catalogue too, so nothing renders "the channel decides".
    for loc in ("de", "en", "tr", "zh", "ja", "ko", "th"):
        block = json.loads((REPO / "web" / "messages" / f"{loc}.json").read_text(encoding="utf-8"))
        assert "accessUndecided" not in block["settings"]["whatsappDashboard"], loc
        assert "inContacts" in block["settings"]["whatsappDashboard"], loc
    # One switch in the product, not one per window.
    fo = (REPO / "web" / "components" / "connections" / "FrontOfficeDashboard.tsx").read_text(encoding="utf-8")
    assert "function Switch(" not in fo and "from '@/components/ui/Switch'" in fo
