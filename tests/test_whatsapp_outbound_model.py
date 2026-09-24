# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WhatsApp as the agent's own number.

The linked account is the AGENT: its own chat is dropped, no whitelist entry is needed to
run it, and no user ever runs on another user's credentials. Who may write in is decided
per message (the registered main-user number, the owner's decision about the person, an
open channel); who may be replied to is decided by the same question through the same
function. Every test here fails when the old model comes back: the admin-creds fallback,
the self-chat note, the bare @lid pass, the whitelist-gated process list, and the 72 hour
reply window that answered a stranger on a channel the owner had closed.
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core import channel_message_store as store
from vaf.core import whatsapp_auth
from vaf.core.config import Config
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Scratch data dir (message store), scratch APP_DIR (creds), controlled Config."""
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    overrides = {"whatsapp_config": {"enabled": True, "inbound_to_agent": True}, "channel_ingress_policy": None}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: overrides.get(key, default)))
    # Config writes (chat_activity, lid map) must not touch any real file.
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"whatsapp_config": dict(overrides["whatsapp_config"])}))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: overrides.__setitem__("whatsapp_config", cfg.get("whatsapp_config") or {})))
    monkeypatch.setattr(wa, "_wa_pending", {})
    monkeypatch.setattr(wa, "_lid_mappings", {})
    return overrides


def _creds(username: str, phone_digits: str) -> Path:
    d = Config.APP_DIR / "users" / username / "whatsapp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "creds.json").write_text(json.dumps({"me": {"id": f"{phone_digits}:7@s.whatsapp.net", "name": "Agent"}}), encoding="utf-8")
    return d


def _dispatch(username, from_jid, body="hello", **extra):
    obj = {"from": from_jid, "senderJid": from_jid, "body": body, "chatType": "dm", "messageId": "m1"}
    obj.update(extra)
    wa._dispatch_bridge_event(username, SCOPE, "message", obj)
    rec = wa._wa_pending.get(f"{username}|{from_jid}")
    if rec and rec.get("timer") is not None:
        rec["timer"].cancel()
    return rec


# ── the linked account is the agent ───────────────────────────────────────────

def test_linked_phone_is_read_from_creds_me_id(isolated):
    _creds("alice", "491700000001")
    assert whatsapp_auth.get_linked_phone("alice") == "+491700000001"
    assert whatsapp_auth.get_linked_phone("nobody") is None
    assert whatsapp_auth.linked_usernames() == ["alice"]


def test_auth_dir_never_falls_back_to_another_users_credentials(isolated):
    _creds("admin", "491700000001")
    bob_dir = whatsapp_auth.get_whatsapp_auth_dir("bob")
    assert bob_dir == Config.APP_DIR / "users" / "bob" / "whatsapp"
    assert not whatsapp_auth.whatsapp_auth_exists("bob")


def test_users_to_run_are_the_linked_enabled_accounts_not_the_whitelist(isolated, monkeypatch):
    _creds("alice", "491700000001")
    _creds("carol", "491700000003")
    Path(Config.APP_DIR / "users" / "bob").mkdir(parents=True)          # account without a link
    scopes = {"alice": "scope-alice", "carol": "scope-carol"}
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: scopes.get(name))
    monkeypatch.setattr(wa, "channel_enabled_for_scope", lambda channel, scope: scope != "scope-carol")
    isolated["whatsapp_config"]["whitelist"] = [{"phone_number": "+491700000099", "vaf_username": "bob"}]

    users = wa._get_users_to_run()

    assert users == [("scope-alice", "alice", Config.APP_DIR / "users" / "alice" / "whatsapp")]
    # bob is whitelisted but not linked: no process (and no borrowed credentials); carol is
    # linked but switched off.


def test_self_chat_is_dropped_before_store_and_queue(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: (["+491700000009"], ["+491700000009"]))
    voice = Platform.data_dir() / "v.ogg"
    voice.parent.mkdir(parents=True, exist_ok=True)
    voice.write_bytes(b"OggS")
    rec = _dispatch("alice", "491700000001@s.whatsapp.net", body="<voice>", selfChat=True, voice_path=str(voice))
    assert rec is None
    assert store.last_message_ts("alice", "+491700000001", user_scope_id=SCOPE) is None
    assert not voice.exists()                                            # the temp file is cleaned up


def test_connected_event_removes_the_agents_own_number_from_the_whitelist(isolated, monkeypatch):
    isolated["whatsapp_config"]["whitelist"] = [
        {"phone_number": "+491700000001", "vaf_username": "alice", "user_scope_id": SCOPE},   # the linked number
        {"phone_number": "+491700000009", "vaf_username": "alice", "user_scope_id": SCOPE},   # the real owner number
    ]
    notes = []
    monkeypatch.setattr("vaf.core.user_notifications.append_notification", lambda *a, **k: notes.append(k))
    wa._dispatch_bridge_event("alice", SCOPE, "connected", {"selfJid": "491700000001:7@s.whatsapp.net"})
    assert wa._self_phone["alice"] == "+491700000001"
    assert [e["phone_number"] for e in isolated["whatsapp_config"]["whitelist"]] == ["+491700000009"]
    assert notes and "own number" in notes[0]["title"]


# ── who may write in ──────────────────────────────────────────────────────────

def test_owner_number_gets_the_full_chat(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: (["+491700000009"], ["+491700000009"]))
    saved = []
    monkeypatch.setattr(wa, "save_whatsapp_chat_jid", lambda scope, user, jid: saved.append(jid))
    rec = _dispatch("alice", "491700000009@s.whatsapp.net")
    assert rec is not None and rec["session_id"] == "whatsapp_alice_491700000009"
    assert rec["metadata"]["ingress_reason"] == "explicit_pair"
    assert "from_contact" not in rec["metadata"] and "chat_label" not in rec["metadata"]
    assert saved == ["491700000009@s.whatsapp.net"]                    # the owner endpoint


def test_an_allowed_contact_lands_in_front_office_on_a_closed_channel(isolated, monkeypatch):
    """The permission the owner gave about the PERSON needs no switch, and it does not make
    them the owner. MUTATION: require the open channel for the allowed branch of
    evaluate_ingress and no task is queued."""
    from vaf.core import contacts_store
    contacts_store.create_contact("Bob", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000005",
                                  assistant_access="allowed")
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], ["+491700000005"]))
    saved = []
    monkeypatch.setattr(wa, "save_whatsapp_chat_jid", lambda scope, user, jid: saved.append(jid))
    rec = _dispatch("alice", "491700000005@s.whatsapp.net", pushName="Bob")
    assert rec is not None and rec["metadata"]["from_contact"] is True
    assert rec["metadata"]["ingress_reason"] == "contact_allowed"
    assert rec["metadata"]["chat_label"] == "Bob", "the namespace label is the name the bridge knew"
    assert saved == []


def test_a_denied_contact_is_refused_even_on_an_open_channel(isolated, monkeypatch):
    """The veto the owner took by hand outranks the switch, and the message is still stored
    for the inbox. MUTATION: read the record with bool() instead of contact_access and the
    denial turns into a pass, because "denied" is a truthy string."""
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    contacts_store.create_contact("Mara", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000007",
                                  assistant_access="denied")
    isolated["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    assert _dispatch("alice", "491700000007@s.whatsapp.net", body="hello?") is None
    assert store.last_message_ts("alice", "+491700000007", direction="in", user_scope_id=SCOPE) is not None


def test_a_stranger_on_an_open_channel_is_answered_and_enrolled(isolated, monkeypatch):
    """The open channel is what answers a person nobody decided about, whether or not the
    agent wrote first. MUTATION: drop the enrolment after a front_office_open reason and the
    record assertion goes red."""
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    isolated["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    store.append_message("alice", "+491700000042", "Hi, do you have a table tonight?", direction="out", user_scope_id=SCOPE)
    rec = _dispatch("alice", "491700000042@s.whatsapp.net", body="Yes, 8pm works", pushName="Table")
    assert rec is not None and rec["metadata"]["from_contact"] is True
    assert rec["metadata"]["ingress_reason"] == "front_office_open"
    assert store.last_message_ts("alice", "+491700000042", direction="in", user_scope_id=SCOPE) is not None
    book = contacts_store.find_contact_by_channel("whatsapp", "+491700000042", "alice", SCOPE)
    assert book is not None and contacts_store.contact_access(book) is None, \
        "the record exists so the owner can decide; the channel is what answered"


def test_a_closed_channel_does_not_answer_the_person_the_agent_wrote_to(isolated, monkeypatch):
    """The live incident, end to end through the bridge: Inbound on "paired only", the owner
    has the agent send one message, and the answer comes back. It is STORED for the inbox and
    it is not handed to the agent, because the owner never opened that channel.

    MUTATION: accept `conversation_match` without the open-channel test in evaluate_ingress.
    """
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    store.append_message("alice", "+491700000042", "Gute Nacht!", direction="out", user_scope_id=SCOPE)
    assert _dispatch("alice", "491700000042@s.whatsapp.net", body="Danke, dir auch") is None
    assert store.last_message_ts("alice", "+491700000042", direction="in", user_scope_id=SCOPE) is not None


def test_reply_after_the_window_is_rejected(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    store.append_message("alice", "+491700000042", "old", direction="out", user_scope_id=SCOPE, ts=time.time() - 73 * 3600)
    assert _dispatch("alice", "491700000042@s.whatsapp.net") is None


def test_the_window_setting_no_longer_admits_anybody(isolated, monkeypatch):
    """`whatsapp_config.reply_window_hours` is a display value now (the WhatsApp window shows
    how long an owner-started conversation stays convenient to answer). Whatever it says, a
    stranger is not admitted by it. MUTATION: hand a conversation match back to
    evaluate_ingress and the open-channel case turns into "open_conversation" plus an answer
    on the closed one."""
    from vaf.core.channel_ingress_policy import set_front_office
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    for hours in (0, 72):
        isolated["whatsapp_config"]["reply_window_hours"] = hours
        store.append_message("alice", "+491700000043", "just now", direction="out", user_scope_id=SCOPE)
        assert _dispatch("alice", "491700000043@s.whatsapp.net") is None, hours
    isolated["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    assert _dispatch("alice", "491700000043@s.whatsapp.net")["metadata"]["ingress_reason"] == "front_office_open"


def test_an_inbound_alone_does_not_open_the_window(isolated, monkeypatch):
    # Only the agent's own outbound message opens the door; a stray accepted inbound
    # from some earlier rule does not keep a stranger in.
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    store.append_message("alice", "+491700000042", "hello?", direction="in", user_scope_id=SCOPE)
    assert _dispatch("alice", "491700000042@s.whatsapp.net") is None


def test_a_synced_contact_is_undecided_and_not_an_opt_out(isolated, monkeypatch):
    """The chat-list sync creates a record for every named chat and decides nothing
    (contacts_store.sync_channel_contacts), so that is the state of nearly every number the
    agent is asked to write to. Reading it as "switched off" would silence 161 people the
    owner never touched; reading it as a grant would answer them on a closed channel. It is
    the third state, and only the switch decides for it.
    MUTATION: make `contact_access` return ACCESS_DENIED for a record whose bool is False and
    the open-channel case goes red."""
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    contacts_store.sync_channel_contacts(
        "whatsapp", [{"endpoint": "+491700000042", "display_name": "Carol", "last_seen_ts": 1.0}], "alice", user_scope_id=SCOPE)
    rec = contacts_store.find_contact_by_channel("whatsapp", "+491700000042", "alice", SCOPE)
    assert rec is not None and contacts_store.contact_access(rec) is None, "the sync decides nothing"
    assert _dispatch("alice", "491700000042@s.whatsapp.net", body="Hi?", pushName="Carol") is None, \
        "a closed channel answers nobody the owner has not allowed"
    isolated["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    task = _dispatch("alice", "491700000042@s.whatsapp.net", body="Yes, 8pm works", pushName="Carol")
    assert task is not None and task["metadata"]["ingress_reason"] == "front_office_open"
    assert task["metadata"]["from_contact"] is True


# ── who may be replied to ─────────────────────────────────────────────────────

def test_the_reply_lane_asks_the_same_question_as_the_inbound_side(isolated, monkeypatch):
    """One function, both directions: the agent must not be free to answer somebody who is no
    longer free to write. MUTATION: let _is_reply_allowed fall back to the phone list of
    allowed_phones instead of the book and the denied assertion goes red."""
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: (["+491700000009"], ["+491700000009"]))
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: SCOPE)
    contacts_store.create_contact("Bob", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000005",
                                  assistant_access="allowed")
    contacts_store.create_contact("Mara", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000007",
                                  assistant_access="denied")
    assert wa._is_reply_allowed("alice", "491700000009@s.whatsapp.net", SCOPE)       # the owner
    assert wa._is_reply_allowed("alice", "491700000005@s.whatsapp.net", SCOPE)       # allowed
    assert not wa._is_reply_allowed("alice", "491700000007@s.whatsapp.net", SCOPE)   # denied
    assert not wa._is_reply_allowed("alice", "491700000042@s.whatsapp.net", SCOPE)   # undecided, channel shut
    store.append_message("alice", "+491700000042", "hi", direction="out", user_scope_id=SCOPE)
    assert not wa._is_reply_allowed("alice", "491700000042@s.whatsapp.net", SCOPE), \
        "writing to somebody does not make them answerable: the window is gone"
    isolated["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    assert wa._is_reply_allowed("alice", "491700000042@s.whatsapp.net", SCOPE)       # undecided, channel open
    assert not wa._is_reply_allowed("alice", "491700000007@s.whatsapp.net", SCOPE), "a denial holds on an open channel"


def test_reply_lane_never_passes_an_unresolved_lid(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: (["+491700000009"], ["+491700000009"]))
    assert not wa._is_reply_allowed("alice", "123456789012345@lid", SCOPE)
    # A resolved LID is judged by the number it stands for.
    isolated["whatsapp_config"]["lid_to_e164"] = {"123456789012345@lid": "+491700000009"}
    assert wa._is_reply_allowed("alice", "123456789012345@lid", SCOPE)


# ── the store query behind the window ────────────────────────────────────────

def test_last_message_ts_filters_by_direction(isolated):
    store.append_message("alice", "+491700000042", "a", direction="in", user_scope_id=SCOPE, ts=100.0)
    store.append_message("alice", "+491700000042", "b", direction="out", user_scope_id=SCOPE, ts=200.0)
    store.append_message("alice", "+491700000042", "c", direction="in", user_scope_id=SCOPE, ts=300.0)
    assert store.last_message_ts("alice", "+491700000042", user_scope_id=SCOPE) == 300.0
    assert store.last_message_ts("alice", "+491700000042", direction="out", user_scope_id=SCOPE) == 200.0
    assert store.last_message_ts("alice", "+491700000042", direction="in", user_scope_id=SCOPE) == 300.0
    assert store.last_message_ts("alice", "+491700000099", user_scope_id=SCOPE) is None
    assert store.last_message_ts("alice", "+491700000042", user_scope_id=SCOPE, channel="telegram") is None


# ── the framework surface the harness reads ──────────────────────────────────

def test_messaging_connections_distinguish_outbound_from_owner_reachable(isolated, monkeypatch):
    monkeypatch.setattr("vaf.core.whatsapp_auth.whatsapp_auth_exists", lambda u: u == "admin")
    from vaf.core.messaging_connections import get_messaging_connections
    conn = get_messaging_connections(username="admin", user_scope_id=None)
    assert "whatsapp" in conn["outbound"] and "whatsapp" not in conn["available"]
    isolated["whatsapp_config"]["whitelist"] = [{"phone_number": "+491700000009", "vaf_username": "admin"}]
    conn = get_messaging_connections(username="admin", user_scope_id=None)
    assert "whatsapp" in conn["outbound"] and "whatsapp" in conn["available"]


def test_agent_injects_the_send_tool_for_outbound_channels():
    import vaf.core.agent as agent_mod
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert 'conn.get("outbound")' in src, "agent.py no longer offers send_whatsapp to an outbound-only agent"


def test_security_finding_is_about_the_link_not_the_whitelist(isolated):
    from vaf.core.security_misconfig import collect_security_findings
    cfg = {"whatsapp_config": {"enabled": True, "whitelist": []}}
    codes = {f["code"] for f in collect_security_findings(cfg)}
    assert "whatsapp_enabled_without_link" in codes
    assert "whatsapp_enabled_without_pairing" not in codes          # outbound-only is a complete setup
    _creds("admin", "491700000001")
    codes = {f["code"] for f in collect_security_findings(cfg)}
    assert "whatsapp_enabled_without_link" not in codes


# ── the bridge installs its own Node dependencies ────────────────────────────

def _bridge_tree(tmp_path: Path, locked: str, installed: str | None) -> Path:
    node_dir = tmp_path / "whatsapp_node"
    node_dir.mkdir()
    (node_dir / "wa-bridge.js").write_text("// bridge\n", encoding="utf-8")
    (node_dir / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    (node_dir / "package-lock.json").write_text(json.dumps(
        {"packages": {"node_modules/@whiskeysockets/baileys": {"version": locked}}}), encoding="utf-8")
    if installed:
        pkg = node_dir / "node_modules" / "@whiskeysockets" / "baileys"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"version": installed}), encoding="utf-8")
    return node_dir


def test_bridge_deps_are_left_alone_when_they_match_the_lockfile(tmp_path, monkeypatch):
    node_dir = _bridge_tree(tmp_path, "6.7.24", "6.7.24")
    monkeypatch.setattr(wa, "_wa_bridge_path", lambda: node_dir / "wa-bridge.js")
    monkeypatch.setattr(wa.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("npm must not run")))
    ok, msg = wa.ensure_bridge_deps()
    assert ok and "6.7.24" in msg


@pytest.mark.parametrize("installed", [None, "6.7.23"])
def test_bridge_deps_are_installed_from_the_lockfile_when_missing_or_behind(tmp_path, monkeypatch, installed):
    node_dir = _bridge_tree(tmp_path, "6.7.24", installed)
    monkeypatch.setattr(wa, "_wa_bridge_path", lambda: node_dir / "wa-bridge.js")
    monkeypatch.setattr(wa.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        pkg = node_dir / "node_modules" / "@whiskeysockets" / "baileys"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "package.json").write_text(json.dumps({"version": "6.7.24"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    ok, msg = wa.ensure_bridge_deps()
    assert ok and "6.7.24" in msg
    assert calls == [["/usr/bin/npm", "ci", "--omit=dev", "--no-audit", "--no-fund"]]   # the lockfile, never rewritten


def test_bridge_deps_fall_back_to_npm_install_and_report_a_failure_honestly(tmp_path, monkeypatch):
    node_dir = _bridge_tree(tmp_path, "6.7.24", None)
    monkeypatch.setattr(wa, "_wa_bridge_path", lambda: node_dir / "wa-bridge.js")
    monkeypatch.setattr(wa.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    calls = []
    monkeypatch.setattr(wa.subprocess, "run", lambda cmd, **k: (calls.append(cmd), SimpleNamespace(returncode=1, stdout="", stderr="ENOTFOUND registry"))[1])
    ok, msg = wa.ensure_bridge_deps()
    assert not ok and "ENOTFOUND" in msg and "npm install" in msg
    assert [c[1] for c in calls] == ["ci", "install"]


def test_bridge_deps_without_npm_say_so(tmp_path, monkeypatch):
    node_dir = _bridge_tree(tmp_path, "6.7.24", None)
    monkeypatch.setattr(wa, "_wa_bridge_path", lambda: node_dir / "wa-bridge.js")
    monkeypatch.setattr(wa.shutil, "which", lambda name: None)
    ok, msg = wa.ensure_bridge_deps()
    assert not ok and "npm not found" in msg


def test_conversation_pane_reads_the_message_store_not_the_agent_session(isolated):
    """A chat that came in through the history sync has messages in the store and no
    agent session at all; the pane must show the store, oldest first, both directions."""
    import asyncio
    from vaf.api import whatsapp_routes as routes
    store.append_message("alice", "+491700000042", "hello there", direction="in", user_scope_id=SCOPE, ts=100.0)
    store.append_message("alice", "+491700000042", "hi, how can I help?", direction="out", user_scope_id=SCOPE, ts=200.0)
    request = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE, "username": "alice"}))
    out = asyncio.run(routes.get_whatsapp_chat_messages(request, chat_id="+491700000042"))
    assert [(m["role"], m["content"]) for m in out["messages"]] == [("user", "hello there"), ("assistant", "hi, how can I help?")]
    assert out["session_id"] == "whatsapp_alice_491700000042"
    assert "user_turn_count" not in out and "compaction_interval" not in out
    assert asyncio.run(routes.get_whatsapp_chat_messages(request, chat_id="+491700000099"))["messages"] == []


def test_learning_counter_travels_only_for_the_owners_answered_chat(isolated):
    """The pane's counter travels for the owner's own registered number while inbound
    messages reach the agent. A contact chat the agent answers learns into its own
    namespace, whose visible surface is the chat node on the Memory page, not this
    counter; a read-only chat learns nowhere."""
    import asyncio
    from vaf.api import whatsapp_routes as routes
    isolated["whatsapp_config"]["whitelist"] = [{"phone_number": "+491700000001", "vaf_username": "alice", "user_scope_id": SCOPE}]
    request = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE, "username": "alice"}))
    owner = asyncio.run(routes.get_whatsapp_chat_messages(request, chat_id="+491700000001"))
    assert owner["user_turn_count"] == 0 and owner["compaction_interval"] == 15 and owner["last_compaction_at_turn"] == 0
    stranger = asyncio.run(routes.get_whatsapp_chat_messages(request, chat_id="+491700000042"))
    assert "compaction_interval" not in stranger
    isolated["whatsapp_config"]["inbound_to_agent"] = False
    silent = asyncio.run(routes.get_whatsapp_chat_messages(request, chat_id="+491700000001"))
    assert "compaction_interval" not in silent


def test_oldest_message_is_the_cursor_for_an_on_demand_history_fetch(isolated):
    store.append_message("alice", "+491700000042", "newest", direction="in", user_scope_id=SCOPE, ts=300.0, message_id="C")
    store.append_message("alice", "+491700000042", "oldest real", direction="out", user_scope_id=SCOPE, ts=100.0, message_id="A")
    store.append_message("alice", "+491700000042", "no id, even older", direction="in", user_scope_id=SCOPE, ts=50.0)  # fallback key
    row = store.oldest_message("alice", "+491700000042", user_scope_id=SCOPE)
    assert (row["message_id"], row["direction"], row["ts"]) == ("A", "out", 100.0)   # the id-less row is skipped
    assert store.oldest_message("alice", "+491700000099", user_scope_id=SCOPE) is None


def test_fetch_older_messages_asks_the_node_and_waits_for_the_store_to_grow(isolated, monkeypatch):
    """The command carries the oldest stored key; success is measured on the store, because the
    phone answers through the ordinary history batch, not through the command's result."""
    import io
    store.append_message("alice", "+491700000042", "oldest", direction="out", user_scope_id=SCOPE, ts=100.0, message_id="A")
    written = io.StringIO()
    fake_proc = SimpleNamespace(stdin=written, poll=lambda: None)
    monkeypatch.setattr(wa, "_processes", {"alice": fake_proc})

    def on_write(orig_write):
        def _w(s):
            r = orig_write(s)
            cmd = json.loads(s)
            # The Node acknowledges, then the phone's batch lands in the store.
            wa._dispatch_bridge_event("alice", SCOPE, "fetch_history_result", {"req_id": cmd["req_id"], "success": True})
            store.append_message("alice", "+491700000042", "older one", direction="in", user_scope_id=SCOPE, ts=10.0, message_id="Z")
            return r
        return _w
    written.write = on_write(written.write)

    out = wa.fetch_older_messages("alice", "491700000042@s.whatsapp.net", "+491700000042", SCOPE, count=50, wait_timeout=3.0)
    assert out["ok"] and out["stored_before"] == 1 and out["stored_after"] == 2
    cmd = json.loads(written.getvalue().strip().splitlines()[-1])
    assert cmd["cmd"] == "fetchHistory" and cmd["jid"] == "491700000042@s.whatsapp.net"
    assert (cmd["oldestId"], cmd["oldestFromMe"], cmd["oldestTs"], cmd["count"]) == ("A", True, 100.0, 50)


def test_fetch_older_messages_reports_a_refusal_instead_of_waiting(isolated, monkeypatch):
    import io
    store.append_message("alice", "+491700000042", "oldest", direction="out", user_scope_id=SCOPE, ts=100.0, message_id="A")  # the cursor
    written = io.StringIO()
    fake_proc = SimpleNamespace(stdin=written, poll=lambda: None)
    monkeypatch.setattr(wa, "_processes", {"alice": fake_proc})
    orig = written.write
    def _w(s):
        r = orig(s)
        wa._dispatch_bridge_event("alice", SCOPE, "fetch_history_result", {"req_id": json.loads(s)["req_id"], "success": False, "error": "WhatsApp not connected"})
        return r
    written.write = _w
    out = wa.fetch_older_messages("alice", "491700000042@s.whatsapp.net", "+491700000042", SCOPE, wait_timeout=3.0)
    assert not out["ok"] and "not connected" in out["error"]


def test_inbound_message_stores_the_name_whatsapp_shows(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: (["+491700000009"], ["+491700000009"]))
    monkeypatch.setattr(wa, "save_whatsapp_chat_jid", lambda *a: None)
    _dispatch("alice", "491700000009@s.whatsapp.net", body="hi", pushName="Alice Example")
    rows = store.list_chats_from_store("alice", user_scope_id=SCOPE)
    assert rows and rows[0]["chat_id"] == "+491700000009" and rows[0]["chat_name"] == "Alice Example"


def test_avatar_is_fetched_once_and_cached_in_both_directions(isolated, monkeypatch):
    import base64, io
    written = io.StringIO()
    fake_proc = SimpleNamespace(stdin=written, poll=lambda: None)
    monkeypatch.setattr(wa, "_processes", {"alice": fake_proc})
    answers = {"491700000009@s.whatsapp.net": {"success": True, "found": True, "mime": "image/jpeg", "b64": base64.b64encode(b"JPEGDATA").decode()},
               "491700000005@s.whatsapp.net": {"success": True, "found": False}}
    asked = []
    orig = written.write
    def _w(s):
        r = orig(s)
        cmd = json.loads(s)
        asked.append(cmd["jid"])
        wa._dispatch_bridge_event("alice", SCOPE, "avatar", {"req_id": cmd["req_id"], **answers[cmd["jid"]]})
        return r
    written.write = _w
    assert wa.get_avatar("alice", "491700000009@s.whatsapp.net", wait_timeout=2.0) == (b"JPEGDATA", "image/jpeg")
    assert wa.get_avatar("alice", "491700000009@s.whatsapp.net", wait_timeout=2.0) == (b"JPEGDATA", "image/jpeg")   # disk cache
    assert wa.get_avatar("alice", "491700000005@s.whatsapp.net", wait_timeout=2.0) is None
    assert wa.get_avatar("alice", "491700000005@s.whatsapp.net", wait_timeout=2.0) is None                          # negative cache
    assert asked == ["491700000009@s.whatsapp.net", "491700000005@s.whatsapp.net"]
    cache = wa.avatar_cache_dir("alice")
    assert (cache / "491700000009.jpg").is_file() and (cache / "491700000005.none").is_file()
    assert cache.parent == Config.APP_DIR / "users" / "alice"                        # per user, beside the credentials


# ── the contact book learns from the channel ─────────────────────────────────

def test_channel_sync_creates_named_people_and_links_known_numbers_without_touching_trust(isolated):
    from vaf.core import contacts_store as cs
    bob = cs.create_contact("Bob Builder", "alice", user_scope_id=SCOPE, whatsapp_phone="0170 1234567", allow_as_assistant_user=True)
    numbered = cs.create_contact("+49 171 7654321", "alice", user_scope_id=SCOPE, whatsapp_phone="+491717654321")
    result = cs.sync_channel_contacts("whatsapp", [
        {"endpoint": "+491701234567", "display_name": "Bobby (WA)", "last_seen_ts": 1000.0},   # known: Bob keeps his name
        {"endpoint": "+491717654321", "display_name": "Carla Client", "last_seen_ts": 2000.0},  # known by number only: gets the name
        {"endpoint": "+491700000042", "display_name": "Dana New", "last_seen_ts": 3000.0},      # unknown: created
        {"endpoint": "+491700000043", "display_name": "", "last_seen_ts": 4000.0},              # number only: not a contact
        {"endpoint": "+491700000044", "display_name": "+49 170 0000044"},                       # a number posing as a name
    ], "alice", user_scope_id=SCOPE)
    assert result == {"created": 1, "linked": 2, "skipped": 2}
    all_contacts = {c["name"]: c for c in cs.list_contacts("alice", user_scope_id=SCOPE)}
    assert set(all_contacts) == {"Bob Builder", "Carla Client", "Dana New"}
    assert all_contacts["Bob Builder"]["allow_as_assistant_user"] is True            # trust untouched
    assert all_contacts["Bob Builder"]["links"]["whatsapp"]["display_name"] == "Bobby (WA)"
    assert all_contacts["Carla Client"]["id"] == numbered["id"]                       # renamed in place, not duplicated
    assert all_contacts["Dana New"]["allow_as_assistant_user"] is False              # created without trust
    assert all_contacts["Dana New"]["channels"] == [{"type": "whatsapp", "value": "+491700000042"}]
    assert cs.find_contact_by_phone("+491700000042", "alice", user_scope_id=SCOPE)["id"] == all_contacts["Dana New"]["id"]
    # A second round with nothing new writes nothing and creates nothing.
    again = cs.sync_channel_contacts("whatsapp", [{"endpoint": "+491700000042", "display_name": "Dana New", "last_seen_ts": 3000.0}], "alice", user_scope_id=SCOPE)
    assert again == {"created": 0, "linked": 0, "skipped": 0}
    assert len(cs.list_contacts("alice", user_scope_id=SCOPE)) == 3


def test_chat_list_feeds_the_contact_book_but_groups_newsletters_and_bare_numbers_do_not(isolated, monkeypatch):
    from vaf.core import contacts_store as cs
    monkeypatch.setattr(wa, "_contact_sync_last", {})
    chats = [
        {"jid": "491700000042@s.whatsapp.net", "name": "Dana New", "phone": "+491700000042", "is_group": False, "last_ts": 5},
        {"jid": "491700000043@s.whatsapp.net", "name": "+491700000043", "phone": "+491700000043", "is_group": False, "last_ts": 5},
        {"jid": "120363000000000000@g.us", "name": "Family group", "phone": "120363000000000000@g.us", "is_group": True, "last_ts": 5},
        {"jid": "120363000000000001@newsletter", "name": "Daily news", "phone": "120363000000000001@newsletter", "is_group": False, "last_ts": 5},
        {"jid": "225790843207825@lid", "name": "Someone", "phone": "225790843207825@lid", "is_group": False, "last_ts": 5},
    ]
    wa._dispatch_bridge_event("alice", SCOPE, "chats", {"chats": chats})
    names = sorted(c["name"] for c in cs.list_contacts("alice", user_scope_id=SCOPE))
    assert names == ["Dana New"]
    # Throttled: the same list a second later does not even hit the store.
    calls = []
    monkeypatch.setattr(cs, "sync_channel_contacts", lambda *a, **k: calls.append(1) or {"created": 0, "linked": 0, "skipped": 0})
    wa._dispatch_bridge_event("alice", SCOPE, "chats", {"chats": chats})
    assert calls == []


def test_avatar_lookups_skip_newsletters_and_remember_whatsapps_refusals(isolated, monkeypatch):
    import io
    written = io.StringIO()
    fake_proc = SimpleNamespace(stdin=written, poll=lambda: None)
    monkeypatch.setattr(wa, "_processes", {"alice": fake_proc})
    asked = []
    orig = written.write
    def _w(s):
        r = orig(s)
        cmd = json.loads(s)
        asked.append(cmd["jid"])
        wa._dispatch_bridge_event("alice", SCOPE, "avatar", {"req_id": cmd["req_id"], "success": False, "error": "item-not-found"})
        return r
    written.write = _w
    # Newsletters, groups and the status broadcast are never asked: each such query would
    # hold the one-at-a-time lock for the whole timeout (live: minutes of queue).
    for jid in ("120363000000000001@newsletter", "120363000000000000@g.us", "status@broadcast"):
        assert wa.get_avatar("alice", jid, wait_timeout=1.0) is None
    assert asked == []
    # WhatsApp's own refusal (item-not-found / not-authorized) is a "no picture", cached
    # like an empty answer, not a transport error to retry on every render.
    assert wa.get_avatar("alice", "491700000009@s.whatsapp.net", wait_timeout=1.0) is None
    assert wa.get_avatar("alice", "491700000009@s.whatsapp.net", wait_timeout=1.0) is None
    assert asked == ["491700000009@s.whatsapp.net"]
    assert (wa.avatar_cache_dir("alice") / "491700000009.none").is_file()


def test_resync_contacts_asks_the_node_and_clears_the_sync_throttle(isolated, monkeypatch):
    import io
    written = io.StringIO()
    fake_proc = SimpleNamespace(stdin=written, poll=lambda: None)
    monkeypatch.setattr(wa, "_processes", {"alice": fake_proc})
    monkeypatch.setattr(wa, "_contact_sync_last", {"alice": time.time()})
    orig = written.write
    def _w(s):
        r = orig(s)
        cmd = json.loads(s)
        assert cmd["cmd"] == "resyncContacts"
        wa._dispatch_bridge_event("alice", SCOPE, "resync_contacts_result", {"req_id": cmd["req_id"], "success": True, "names": 12})
        return r
    written.write = _w
    assert wa.resync_contacts("alice", wait_timeout=2.0) == (True, "")
    assert "alice" not in wa._contact_sync_last


def test_named_numbers_from_the_node_name_the_chats_and_fill_the_contact_book(isolated, monkeypatch):
    from vaf.core import contacts_store as cs
    monkeypatch.setattr(wa, "_contact_names", {})
    wa._dispatch_bridge_event("alice", SCOPE, "contacts", {"contacts": [
        {"jid": "491700000042@s.whatsapp.net", "e164": "+491700000042", "name": "Dana New", "source": "addressbook"},
        {"jid": "491700000043@s.whatsapp.net", "e164": "+491700000043", "name": "", "source": "push"},
    ]})
    assert wa._contact_names["alice"]["491700000042"]["name"] == "Dana New"
    assert "491700000043" not in wa._contact_names["alice"]
    assert [c["name"] for c in cs.list_contacts("alice", user_scope_id=SCOPE)] == ["Dana New"]
    # Without a running bridge the getter answers from the last event.
    monkeypatch.setattr(wa, "is_bridge_running", lambda: False)
    assert wa.get_contact_names("alice")["491700000042"]["source"] == "addressbook"


def test_status_updates_and_newsletters_are_not_rejected_senders(isolated, monkeypatch):
    """They are not people who could be paired, so they never reach the ingress decision:
    no REJECT line, no activity row, no stored message."""
    import vaf.core.channel_ingress_policy as policy_mod
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    policy_mod._log_last.clear()
    lines = []
    monkeypatch.setattr("vaf.core.log_helper.log_channel_inbound", lambda ch, msg, always=False: lines.append((ch, msg, always)))
    monkeypatch.setattr("vaf.core.security_events.log_security_event",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a refused sender is not a security event")))
    for jid in ("status@broadcast", "120363195908196684@newsletter", "123@broadcast"):
        assert _dispatch("alice", jid, body="post") is None
    assert [l for l in lines if "REJECT" in l[1]] == []
    assert store.list_chats_from_store("alice", user_scope_id=SCOPE) == []
    # A real stranger is still rejected and recorded, in the channel's own lane, with the
    # line that survives debug logging being off.
    assert _dispatch("alice", "491700000099@s.whatsapp.net", body="hi") is None
    rejects = [l for l in lines if l[1].startswith("REJECT ") and "reason=not_paired" in l[1]]
    assert len(rejects) == 1 and rejects[0][0] == "whatsapp" and rejects[0][2] is True
    assert "from=491700000099@s.whatsapp.net" in rejects[0][1]
    # The line names the decision the book held about them, so a refusal can be told apart
    # from a veto when somebody asks why a message was not answered.
    assert "access=undecided" in rejects[0][1]
    # A LID no number is known for gets ONE line too, with the hint on it, not a second one.
    assert _dispatch("alice", "12345678901234@lid", body="hi") is None
    lid_lines = [l for l in lines if "12345678901234@lid" in l[1] and "REJECT" in l[1]]
    assert len(lid_lines) == 1 and "unresolved @lid" in lid_lines[0][1] and lid_lines[0][2] is True


def test_whitelist_add_refuses_the_agents_own_number(isolated, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from vaf.api import whatsapp_routes as routes
    _creds("alice", "491700000001")
    request = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE, "username": "alice"}))
    monkeypatch.setattr(routes, "caller_is_admin", lambda req: False)
    body = routes.WhitelistAddRequest(phone_number="+491700000001")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.add_whitelist_entry(request, body))
    assert exc.value.status_code == 400 and "agent's own" in exc.value.detail
    # The number the user chats from is accepted.
    out = asyncio.run(routes.add_whitelist_entry(request, routes.WhitelistAddRequest(phone_number="+491700000009")))
    assert out["status"] in ("added", "updated")


def test_dashboard_marks_a_number_the_contact_book_already_knows(isolated, monkeypatch):
    """A chat whose number is in the contact book (Front Office flag or not) carries the
    contact id, so the dashboard offers "add as contact" only to a number the book does
    not know yet; the WhatsApp sync creates records without the flag, and those must
    not look new."""
    import asyncio
    from vaf.api import whatsapp_routes as routes
    from vaf.core import contacts_store as cs
    _creds("alice", "491700000001")
    cs.create_contact("Dana New", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000042")
    monkeypatch.setattr(routes, "caller_is_admin", lambda req: False)
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda *a, **k: [
        {"jid": "491700000042@s.whatsapp.net", "phone": "+491700000042", "name": "Dana", "last_ts": 10},
        {"jid": "491700000077@s.whatsapp.net", "phone": "+491700000077", "name": "Stranger", "last_ts": 9},
    ])
    monkeypatch.setattr(wa, "is_bridge_running", lambda: False)
    monkeypatch.setattr(wa, "get_connection_status", lambda *a, **k: False)
    monkeypatch.setattr(wa, "get_lid_mappings", lambda *a, **k: [])
    request = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE, "username": "alice"}))
    out = asyncio.run(routes.get_whatsapp_dashboard(request))
    by_id = {s["chat_id"]: s for s in out["sessions"]}
    known, unknown = by_id["+491700000042"], by_id["+491700000077"]
    assert known["contact_id"] and known["contact_name"] == "Dana New" and known["type"] == "unknown"
    assert unknown["contact_id"] is None and unknown["contact_name"] is None


# ── a rejected sender's message is the owner's mail ───────────────────────────

def test_a_rejected_senders_message_is_kept_for_the_owner_but_not_answered(isolated, monkeypatch):
    """The ingress policy decides whether the AGENT reacts, not whether the OWNER may read
    the mail of their own number. Live incident: two contacts wrote, neither had "Can
    reach your assistant", and the dashboard showed nothing at all, because the reject
    path returned before the store. Now the message is stored like any other inbound (the
    dashboard lists the chat as read-only), nothing is queued, and no reply window opens."""
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    activity = []
    monkeypatch.setattr(wa, "_append_chat_activity", lambda chat_id, scope, direction="in": activity.append((chat_id, scope, direction)))
    assert _dispatch("alice", "491700000042@s.whatsapp.net", body="hello?", pushName="Dana") is None
    rows = store.get_chat_messages("alice", "+491700000042", user_scope_id=SCOPE)
    assert [(r["body"], r["direction"], r["message_id"]) for r in rows] == [("hello?", "in", "m1")]
    listed = store.list_chats_from_store("alice", user_scope_id=SCOPE)
    assert listed and listed[0]["chat_id"] == "+491700000042" and listed[0]["chat_name"] == "Dana"
    assert activity == [("+491700000042", SCOPE, "in")]                       # under the phone, with the scope
    assert wa._is_reply_allowed("alice", "491700000042@s.whatsapp.net", SCOPE) is False
    # a resolved @lid lands under the phone number too, never under the bare LID
    assert _dispatch("alice", "173642054922259@lid", body="second", fromE164="+491700000042", messageId="m2") is None
    assert [r["body"] for r in store.get_chat_messages("alice", "+491700000042", user_scope_id=SCOPE)] == ["second", "hello?"]
    # and the tenant boundary holds: another scope sees none of it
    assert store.get_chat_messages("bob", "+491700000042", user_scope_id="66666666-7777-8888-9999-000000000000") == []


def test_the_reply_window_is_not_a_door_on_any_lane(isolated, monkeypatch):
    """The bridge's own copy of the window rule is gone with the door it served: whether the
    agent may answer is `evaluate_ingress`, on both lanes, and the window's timestamp is the
    WhatsApp window's display value (vaf/core/inbox.reply_window_until, one implementation).
    MUTATION: give _is_reply_allowed a window fallback and every assertion here goes red."""
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: SCOPE)
    assert not hasattr(wa, "conversation_open_until") and not hasattr(wa, "_conversation_is_open")
    now = time.time()
    # The agent wrote an hour ago and the person answered: no lane opens for them.
    store.append_message("alice", "+491700000043", "hello", direction="out", user_scope_id=SCOPE, ts=now - 3600)
    store.append_message("alice", "+491700000043", "yes", direction="in", user_scope_id=SCOPE, ts=now - 60)
    assert wa._is_reply_allowed("alice", "491700000043@s.whatsapp.net", SCOPE) is False
    assert _dispatch("alice", "491700000043@s.whatsapp.net", body="and you?") is None
    # The store's own query that the display value is built on is untouched: an inbound
    # inside the window is found, a later one does not shadow it.
    assert store.last_message_ts("alice", "+491700000043", direction="in", user_scope_id=SCOPE,
                                 until_ts=now) == pytest.approx(now - 60)


def test_fetch_older_messages_on_an_empty_chat_says_so_instead_of_asking(isolated, monkeypatch):
    """Baileys pages backwards from a stored key; with nothing stored there is none, and a
    request with an empty key only ever came back with nothing. The window turns
    no_cursor into its own note instead of "nothing older arrived"."""
    import io
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"alice": SimpleNamespace(stdin=written, poll=lambda: None)})
    out = wa.fetch_older_messages("alice", "491700000042@s.whatsapp.net", "+491700000042", SCOPE, wait_timeout=1.0)
    assert out["ok"] is True and out["no_cursor"] is True and out["stored_before"] == 0
    assert written.getvalue() == ""                                                       # nothing was asked of the Node


def test_a_message_the_person_sent_from_the_dashboard_is_not_the_agent_writing(isolated, monkeypatch):
    """An outbound row the PERSON sent (the compose box under the chat, stored under
    OWNER_SENDER) leaves the number without the agent writing anything: the contact's answer
    stays a read-only message, and the store's own "the agent wrote" query says so. MUTATION:
    drop the exclude_sender argument and the first assertion goes red."""
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: SCOPE)
    now = time.time()
    store.append_message("alice", "+491700000050", "hi, it's me", direction="out",
                         sender_jid=store.OWNER_SENDER, user_scope_id=SCOPE, ts=now - 60)
    assert store.last_message_ts("alice", "+491700000050", direction="out", user_scope_id=SCOPE,
                                 exclude_sender=store.OWNER_SENDER) is None
    store.append_message("alice", "+491700000050", "hello back", direction="in", user_scope_id=SCOPE, ts=now - 30)
    assert wa._is_reply_allowed("alice", "491700000050@s.whatsapp.net", SCOPE) is False
    # the agent's own message counts as the agent's, and the row is visible to the pane either way
    store.append_message("alice", "+491700000050", "agent here", direction="out", user_scope_id=SCOPE, ts=now - 10)
    assert store.last_message_ts("alice", "+491700000050", direction="out", user_scope_id=SCOPE,
                                 exclude_sender=store.OWNER_SENDER) == pytest.approx(now - 10)
    assert len(store.get_chat_messages("alice", "+491700000050", user_scope_id=SCOPE)) == 3


def test_the_owner_origin_travels_through_both_send_paths(isolated, monkeypatch, tmp_path):
    """The sender loop stores the outbound row, so the origin has to reach it: as the
    eighth queue element in-process, as the `origin` field over the file IPC."""
    import queue as _queue
    q = _queue.Queue()
    monkeypatch.setattr(wa, "_outgoing_queue", q)
    monkeypatch.setattr(wa, "_processes", {"alice": SimpleNamespace(poll=lambda: None)})
    monkeypatch.setattr(wa, "_pending_sends", {})
    out = wa.send_whatsapp_with_confirmation("alice", "491700000051@s.whatsapp.net", "hi",
                                             timeout=0.05, allow_contact_send=True, origin="owner")
    item = q.get_nowait()
    assert len(item) == 8 and item[7] == "owner" and item[2] == "hi"
    assert "No delivery confirmation" in out                    # nobody answered the fake process
    # the external path: the request file carries origin, and the dequeue hands it back
    monkeypatch.setattr(wa, "_outgoing_queue", None)
    monkeypatch.setattr(wa, "_processes", {})
    wa._write_json_atomic(wa._ipc_state_path(), {"running": True, "usernames": ["alice"], "updated_at": time.time()})
    monkeypatch.setattr(wa, "_wait_for_external_send_result", lambda *a, **k: "Message sent via WhatsApp.")
    assert wa.send_whatsapp_with_confirmation("alice", "491700000051@s.whatsapp.net", "hi",
                                              allow_contact_send=True, origin="owner").startswith("Message sent")
    dequeued = wa._dequeue_external_send_request()
    assert dequeued is not None and dequeued[7] == "owner" and dequeued[0] == "alice"
    # an agent send carries no origin on either path
    wa.send_whatsapp_with_confirmation("alice", "491700000051@s.whatsapp.net", "hi", allow_contact_send=True)
    assert wa._dequeue_external_send_request()[7] is None


def test_forwarding_off_enrols_nobody_through_a_door_that_answers_nobody(isolated, monkeypatch):
    """`inbound_to_agent` off stops every sender before the agent, so an open Inbound answers
    nobody on WhatsApp. The enrolment read the policy FLAG, not the door, and wrote a contact
    record plus a security event saying the open Front Office had admitted a person the agent
    would never answer, while the row beside it said they were not answered.

    The message is still stored and still logged as accepted: that is what happened, and the
    owner reads it in the inbox.

    MEASURED BOUNDARY, so the next reader is not surprised: a record still appears in the book
    either way, because an older lane links every inbound sender to one (`source` "whatsapp").
    What the door decides is the Front Office ENROLMENT on top of it, which stamps `source`
    "front_office" and writes the event; that is what must not happen where nobody is answered.

    MUTATION: drop the `front_office_open` condition on the enrolment and the two assertions
    about the first dispatch go red.
    """
    from vaf.core import contacts_store
    from vaf.core.channel_ingress_policy import set_front_office
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    isolated["channel_ingress_policy"] = set_front_office(None, True, "whatsapp")
    isolated["whatsapp_config"] = {"enabled": True, "inbound_to_agent": False}

    assert _dispatch("alice", "491700000042@s.whatsapp.net", body="Hallo?", pushName="Mara") is None, \
        "forwarding off hands nothing to the agent"
    assert store.last_message_ts("alice", "+491700000042", direction="in", user_scope_id=SCOPE) is not None, \
        "the message is still stored for the owner"
    linked = contacts_store.find_contact_by_channel("whatsapp", "+491700000042", "alice", SCOPE)
    assert linked is not None and linked.get("source") != "front_office", \
        "the linking lane records the sender; the closed door enrols nobody"
    assert [k for k, _ in events if k == "contact_access_changed"] == []

    # With forwarding back on, a sender the door answers IS enrolled: the gate is the door,
    # not a new rule.
    isolated["whatsapp_config"] = {"enabled": True, "inbound_to_agent": True}
    assert _dispatch("alice", "491700000050@s.whatsapp.net", body="Hallo?", pushName="Nora") is not None
    book = contacts_store.find_contact_by_channel("whatsapp", "+491700000050", "alice", SCOPE)
    assert book is not None and book.get("source") == "front_office"
    assert contacts_store.contact_access(book) is None
    assert [k for k, _ in events if k == "contact_access_changed"] == ["contact_access_changed"]
