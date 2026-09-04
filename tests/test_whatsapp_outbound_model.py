# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WhatsApp as the agent's own number.

The linked account is the AGENT: its own chat is dropped, no whitelist entry is needed to
run it, and no user ever runs on another user's credentials. Who may write in is decided
per message (registered main-user number, Front Office contact, open conversation); who
may be replied to is decided by the same three answers. Every test here fails when the
old model comes back: the admin-creds fallback, the self-chat note, the bare @lid pass,
the whitelist-gated process list.
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
    monkeypatch.setattr(wa, "whatsapp_enabled_for_scope", lambda scope: scope != "scope-carol")
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
    assert "from_contact" not in rec["metadata"]
    assert saved == ["491700000009@s.whatsapp.net"]                    # the owner endpoint


def test_contact_lands_in_front_office_and_does_not_become_the_owner_endpoint(isolated, monkeypatch):
    isolated["channel_ingress_policy"] = {"mode": "permissive"}
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], ["+491700000005"]))
    saved = []
    monkeypatch.setattr(wa, "save_whatsapp_chat_jid", lambda scope, user, jid: saved.append(jid))
    rec = _dispatch("alice", "491700000005@s.whatsapp.net")
    assert rec is not None and rec["metadata"]["from_contact"] is True
    assert rec["metadata"]["ingress_reason"] == "contact_fallback"
    assert saved == []


def test_reply_inside_the_window_is_front_office_with_reason_open_conversation(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    store.append_message("alice", "+491700000042", "Hi, do you have a table tonight?", direction="out", user_scope_id=SCOPE)
    rec = _dispatch("alice", "491700000042@s.whatsapp.net", body="Yes, 8pm works")
    assert rec is not None and rec["metadata"]["from_contact"] is True
    assert rec["metadata"]["ingress_reason"] == "open_conversation"
    # The accepted reply is stored, so the conversation stays open for the reply lane.
    assert store.last_message_ts("alice", "+491700000042", direction="in", user_scope_id=SCOPE) is not None


def test_reply_after_the_window_is_rejected(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    store.append_message("alice", "+491700000042", "old", direction="out", user_scope_id=SCOPE, ts=time.time() - 73 * 3600)
    assert _dispatch("alice", "491700000042@s.whatsapp.net") is None


def test_window_of_zero_switches_the_rule_off(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    isolated["whatsapp_config"]["reply_window_hours"] = 0
    store.append_message("alice", "+491700000042", "just now", direction="out", user_scope_id=SCOPE)
    assert _dispatch("alice", "491700000042@s.whatsapp.net") is None


def test_an_inbound_alone_does_not_open_the_window(isolated, monkeypatch):
    # Only the agent's own outbound message opens the door; a stray accepted inbound
    # from some earlier rule does not keep a stranger in.
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: ([], []))
    store.append_message("alice", "+491700000042", "hello?", direction="in", user_scope_id=SCOPE)
    assert _dispatch("alice", "491700000042@s.whatsapp.net") is None


# ── who may be replied to ─────────────────────────────────────────────────────

def test_reply_lane_answers_owner_contact_and_open_conversation_only(isolated, monkeypatch):
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user", lambda u, s: (["+491700000009"], ["+491700000009", "+491700000005"]))
    assert wa._is_reply_allowed("alice", "491700000009@s.whatsapp.net", SCOPE)      # owner
    assert wa._is_reply_allowed("alice", "491700000005@s.whatsapp.net", SCOPE)      # contact
    assert not wa._is_reply_allowed("alice", "491700000042@s.whatsapp.net", SCOPE)  # stranger
    store.append_message("alice", "+491700000042", "hi", direction="out", user_scope_id=SCOPE)
    assert wa._is_reply_allowed("alice", "491700000042@s.whatsapp.net", SCOPE)      # open conversation


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


def test_whitelist_add_refuses_the_agents_own_number(isolated, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from vaf.api import whatsapp_routes as routes
    _creds("alice", "491700000001")
    request = SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": SCOPE, "username": "alice"}))
    monkeypatch.setattr(routes, "_is_whatsapp_admin", lambda req: False)
    body = routes.WhitelistAddRequest(phone_number="+491700000001")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.add_whitelist_entry(request, body))
    assert exc.value.status_code == 400 and "agent's own" in exc.value.detail
    # The number the user chats from is accepted.
    out = asyncio.run(routes.add_whitelist_entry(request, routes.WhitelistAddRequest(phone_number="+491700000009")))
    assert out["status"] in ("added", "updated")
