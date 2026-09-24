# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A channel's login token lives in the encrypted key ring, not in config.json.

Measured before the change on a live installation: the Telegram bot token sat in plaintext
in config.json, and `GET /api/config` handed the whole `telegram_config` block, token
included, to every admin's browser - the config API blanks only secret-named top-level keys,
and the token is a field one level down. The setup wizard even pre-filled it.

What must hold, each pinned below: a token pasted into config.json (the documented setup,
and what an older release left) moves into the ring on first read and leaves config.json; a
save that no longer carries the token, which after this change is every save, keeps it; an
empty field never removes one; the browser never receives one; a disconnect removes it for
good; and nothing ever invents one.
"""
import json

import pytest

from vaf.core import channel_secrets as cs
from vaf.core import data_keyring as dk
from vaf.core.channels import CHANNEL_SECRETS
from vaf.core.config import Config

TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A fresh config.json for this test; the ring is already per-test (conftest)."""
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)

    def seed(**blocks):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(blocks)
        path.write_text(json.dumps(data), encoding="utf-8")

    def on_disk():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("F", (), {"seed": staticmethod(seed), "on_disk": staticmethod(on_disk), "path": path})


def test_a_token_in_config_json_moves_into_the_ring_on_first_read(config_file):
    config_file.seed(telegram_config={"bot_token": TOKEN, "verified": True, "enabled": True})

    assert cs.channel_secret("telegram") == TOKEN
    assert dk.peek_data_secret(cs.ring_name("telegram", "bot_token")) == TOKEN
    block = config_file.on_disk()["telegram_config"]
    assert "bot_token" not in block, "the plaintext copy is gone from config.json"
    assert block["verified"] is True and block["enabled"] is True, "the rest of the block is untouched"
    assert TOKEN not in config_file.path.read_text(encoding="utf-8")
    assert cs.channel_secret("telegram") == TOKEN, "and the next read answers from the ring"


def test_the_move_writes_no_plaintext_backup_of_config_json(config_file):
    """The pre-keyring backup is a plaintext copy of config.json. Writing one for a token
    would put the token right back on disk next to the file it just left."""
    config_file.seed(telegram_config={"bot_token": TOKEN})
    cs.channel_secret("telegram")
    backups = [p for p in config_file.path.parent.iterdir() if p.name.startswith("config.json.") or p.suffix == ".bak"]
    assert all(TOKEN not in p.read_text(encoding="utf-8", errors="ignore") for p in backups), backups


def test_nothing_is_ever_invented(config_file):
    assert cs.channel_secret("telegram") == ""
    assert cs.has_channel_secret("discord") is False
    assert dk.peek_data_secret(cs.ring_name("telegram", "bot_token")) == "", "no value was minted"


def test_config_json_wins_so_a_hand_pasted_new_token_replaces_the_stored_one(config_file):
    cs.set_channel_secret("telegram", "bot_token", TOKEN)
    newer = "987654321:" + "B" * 35
    config_file.seed(telegram_config={"bot_token": newer})
    assert cs.channel_secret("telegram") == newer
    assert dk.peek_data_secret(cs.ring_name("telegram", "bot_token")) == newer
    assert "bot_token" not in config_file.on_disk()["telegram_config"]


def test_the_first_save_after_the_upgrade_does_not_lose_the_token(config_file):
    """THE dangerous case. config.json still holds the token (nothing has read it yet), and
    the browser, which no longer receives it, saves the block back without it. Replacing the
    block first and moving second would lose the token; absorb moves it before the save."""
    config_file.seed(telegram_config={"bot_token": TOKEN, "verified": True, "enabled": False})
    body = {"telegram_config": {"verified": True, "enabled": True, "bot_token": ""}}

    current = Config.load()
    from vaf.core.api_keys import absorb_config_keys
    merged = Config.merge_preserving_nonempty_sensitive(current, absorb_config_keys(body, is_admin=True))
    Config.save(merged)

    assert cs.channel_secret("telegram") == TOKEN
    assert config_file.on_disk()["telegram_config"]["enabled"] is True
    assert TOKEN not in config_file.path.read_text(encoding="utf-8")


def test_a_saved_token_goes_to_the_ring_and_an_empty_one_changes_nothing(config_file):
    from vaf.core.api_keys import absorb_config_keys
    cleaned = absorb_config_keys({"discord_config": {"bot_token": f"  {TOKEN}  ", "verified": True}}, is_admin=True)
    assert cleaned["discord_config"] == {"verified": True}, "the token never reaches config.json"
    assert cs.channel_secret("discord") == TOKEN

    cleaned = absorb_config_keys({"discord_config": {"bot_token": "", "verified": True}}, is_admin=True)
    assert "bot_token" not in cleaned["discord_config"]
    assert cs.channel_secret("discord") == TOKEN, "blank means not re-sent, never removed"


def test_the_browser_never_receives_a_credential_field(config_file):
    """Every credential field the registry declares, for the admin view too, even while a
    copy still sits in config.json."""
    blocks = {f"{ch}_config": {f: TOKEN for f in fields} | {"verified": True}
              for ch, fields in CHANNEL_SECRETS.items()}
    config_file.seed(**blocks)
    for role in ("admin", "user"):
        view = Config.config_for_user(Config.load(), None, role)
        assert TOKEN not in json.dumps(view), f"{role} view carries a token"
    admin = Config.config_for_user(Config.load(), None, "admin")
    assert admin["telegram_config"]["bot_token"] == "" and admin["telegram_config"]["verified"] is True


def test_a_disconnect_removes_the_token_for_good(config_file):
    cs.set_channel_secret("telegram", "bot_token", TOKEN)
    config_file.seed(discord_config={"bot_token": TOKEN})

    assert cs.clear_channel_secrets("telegram") == {"bot_token": True}
    assert cs.clear_channel_secrets("discord") == {"bot_token": True}
    assert cs.channel_secret("telegram") == "" and cs.channel_secret("discord") == ""
    assert TOKEN not in config_file.path.read_text(encoding="utf-8")
    assert cs.clear_channel_secrets("telegram") == {"bot_token": False}


def test_a_failed_move_keeps_the_only_copy(config_file, monkeypatch):
    """The ring write fails: the token stays in config.json and the channel keeps working."""
    config_file.seed(telegram_config={"bot_token": TOKEN})

    def boom(name, value):
        raise RuntimeError("ring unavailable")

    monkeypatch.setattr(dk, "set_data_secret", boom)
    assert cs.channel_secret("telegram") == TOKEN
    assert config_file.on_disk()["telegram_config"]["bot_token"] == TOKEN


def test_an_empty_value_is_refused_and_an_unknown_field_is_an_error(config_file):
    with pytest.raises(ValueError):
        cs.set_channel_secret("telegram", "bot_token", "   ")
    with pytest.raises(ValueError):
        cs.channel_secret("telegram", "whitelist")
    with pytest.raises(ValueError):
        cs.channel_secret("whatsapp")


def test_the_ring_can_forget_a_named_secret(config_file):
    dk.set_data_secret("channel_test_token", "x" * 40)
    assert dk.delete_data_secret("channel_test_token") is True
    assert dk.peek_data_secret("channel_test_token") == ""
    assert dk.delete_data_secret("channel_test_token") is False


def test_no_module_reads_a_credential_field_out_of_a_config_block():
    """The class, not the instance: before this change twelve places read
    `<channel>_config.get("bot_token")`, and every one of them would have kept working
    against a copy nobody wrote any more. channel_secrets.py is the only reader."""
    import re
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    fields = sorted({f for fs in CHANNEL_SECRETS.values() for f in fs})
    pattern = re.compile(r"""(?:\.get\(\s*|\[\s*)["'](%s)["']""" % "|".join(map(re.escape, fields)))
    tracked = subprocess.run(["git", "ls-files", "-z", "vaf/"], cwd=repo, capture_output=True,
                             check=True).stdout.decode("utf-8", "ignore").split("\0")
    offenders = []
    for rel in tracked:
        if not rel.endswith(".py") or rel == "vaf/core/channel_secrets.py" or not (repo / rel).is_file():
            continue
        for n, line in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{rel}:{n}  {line.strip()}")
    assert not offenders, "read the credential through vaf.core.channel_secrets:\n  " + "\n  ".join(offenders)


def test_secure_status_names_a_token_still_in_config_json_until_it_moves(config_file):
    """`vaf secure status` reads ring_status: a token pasted by hand shows as plaintext
    left in config.json, and the next read that moves it clears the line."""
    config_file.seed(telegram_config={"bot_token": TOKEN})
    assert "telegram_config.bot_token" in dk.ring_status()["legacy_in_config"]
    cs.channel_secret("telegram")
    status = dk.ring_status()
    assert "telegram_config.bot_token" not in status["legacy_in_config"]
    assert cs.ring_name("telegram", "bot_token") in status["entries"], "listed by name, never by value"
    assert TOKEN not in json.dumps(status)


@pytest.mark.parametrize("channel", sorted(CHANNEL_SECRETS))
def test_the_disconnect_route_stops_the_bridge_and_leaves_no_login_behind(config_file, monkeypatch, channel):
    """Discord's disconnect used to save `null`, which the merge keeps, so the token and the
    whole block survived it. The route removes both, after stopping the bridge."""
    import asyncio
    import importlib

    from vaf.api.config_routes import disconnect_channel

    stopped = []
    bridge = importlib.import_module(f"vaf.api.{channel}_bridge")
    monkeypatch.setattr(bridge, "stop_bridge", lambda: stopped.append(channel))
    config_file.seed(**{f"{channel}_config": {"bot_token": TOKEN, "verified": True, "enabled": True}})
    cs.channel_secret(channel)          # moved into the ring, as on any running install

    out = asyncio.run(disconnect_channel(channel, {"role": "admin"}))

    assert out["status"] == "disconnected" and stopped == [channel]
    assert cs.channel_secret(channel) == ""
    assert config_file.on_disk().get(f"{channel}_config") is None
    assert TOKEN not in config_file.path.read_text(encoding="utf-8")


def test_the_disconnect_route_refuses_a_channel_without_a_stored_login(config_file):
    import asyncio

    from fastapi import HTTPException

    from vaf.api.config_routes import disconnect_channel
    with pytest.raises(HTTPException) as err:
        asyncio.run(disconnect_channel("whatsapp", {"role": "admin"}))
    assert err.value.status_code == 400


def test_a_save_that_cannot_move_the_token_is_refused_and_config_json_keeps_it(config_file, monkeypatch):
    """The first save after the upgrade, with the ring unwritable. The forgiving read would
    log the failed move and carry on, and the save would then replace the block with one
    that has no token: gone from config.json and never in the ring. The save must fail
    instead and leave config.json holding the only copy."""
    config_file.seed(telegram_config={"bot_token": TOKEN, "verified": True, "enabled": False})

    def boom(name, value):
        raise RuntimeError("ring unavailable")

    monkeypatch.setattr(dk, "set_data_secret", boom)
    from vaf.core.api_keys import absorb_config_keys
    with pytest.raises(RuntimeError):
        absorb_config_keys({"telegram_config": {"verified": True, "enabled": True, "bot_token": ""}}, is_admin=True)
    assert config_file.on_disk()["telegram_config"]["bot_token"] == TOKEN
    assert config_file.on_disk()["telegram_config"]["enabled"] is False, "nothing of the save landed"


@pytest.mark.parametrize("channel", ["discord", "telegram", "whatsapp"])
def test_a_timeline_entry_after_a_disconnect_does_not_bring_the_channel_back(config_file, channel):
    """The bridges' activity writer turned a missing block into an empty dict, so an entry
    arriving after a disconnect created `{"chat_activity": [...]}` for a channel that is gone."""
    from vaf.core.messaging_connections import append_channel_activity
    config_file.seed(**{f"{channel}_config": None})
    append_channel_activity(channel, {"chat_id": "1", "ts": 1.0, "direction": "in"}, keep=100)
    assert config_file.on_disk().get(f"{channel}_config") is None


def test_a_timeline_entry_keeps_the_block_and_the_newest_entries(config_file):
    from vaf.core.messaging_connections import append_channel_activity
    config_file.seed(discord_config={"verified": True, "admin_user_id": "42",
                                     "chat_activity": [{"n": i} for i in range(20)]})
    append_channel_activity("discord", {"n": 20}, keep=20)
    block = config_file.on_disk()["discord_config"]
    assert block["verified"] is True and block["admin_user_id"] == "42"
    assert [e["n"] for e in block["chat_activity"]] == list(range(1, 21))


def test_a_timeline_entry_loads_and_saves_inside_one_config_lock(config_file, monkeypatch):
    """Without the lock, an entry that loaded the block before a disconnect and saved after
    it wrote the whole old block back."""
    import contextlib

    from vaf.core.messaging_connections import append_channel_activity
    events = []
    real_load, real_save = Config.load.__func__, Config.save.__func__

    @contextlib.contextmanager
    def spy_lock(cls):
        events.append("lock")
        yield
        events.append("unlock")

    monkeypatch.setattr(Config, "_locked", classmethod(spy_lock))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: (events.append("load"), real_load(cls))[1]))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, c: (events.append("save"), real_save(cls, c))[1]))
    config_file.seed(telegram_config={"verified": True})
    events.clear()
    append_channel_activity("telegram", {"chat_id": "1"}, keep=100)
    # Config.save takes the (reentrant) lock again itself, so count the nesting: every
    # load and save must happen while the OUTER lock is held, released only at the end.
    depth, seen = 0, []
    for e in events:
        depth += {"lock": 1, "unlock": -1}.get(e, 0)
        if e in ("load", "save"):
            seen.append((e, depth))
    assert events[0] == "lock" and events[-1] == "unlock", events
    assert seen[0] == ("load", 1) and ("save", 1) in seen, seen
    assert all(d >= 1 for _, d in seen), seen


def test_no_bridge_writes_its_timeline_by_hand():
    """Three hand copies of the same read-modify-write carried the same two faults."""
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    for bridge in ("discord", "telegram", "whatsapp"):
        src = (repo / "vaf" / "api" / f"{bridge}_bridge.py").read_text(encoding="utf-8")
        assert f'append_channel_activity("{bridge}"' in src, bridge
        assert '["chat_activity"] = activity' not in src, f"{bridge} writes chat_activity by hand"


# ── a non-admin never writes a channel's login or its block ──────────────────

NON_ADMIN = {"username": "mallory", "role": "user", "user_scope_id": "22222222-2222-2222-2222-222222222222"}


def _patch(body, user):
    import asyncio

    from vaf.api.config_routes import patch_config

    class _Req:
        state = type("S", (), {})()

    return asyncio.run(patch_config(body, _Req(), user))


@pytest.mark.parametrize("extra", [{}, {"enabled": False}], ids=["no-toggle", "with-toggle"])
def test_a_non_admin_save_cannot_replace_the_bot_token_or_the_whitelist(config_file, extra):
    """Measured before the fix, on this code and on the release before it: a non-admin PATCH
    whose block carried no `enabled` passed the connection filter untouched, replaced the
    Telegram whitelist with their own entry (the bot's owner) and replaced the bot token."""
    owner = [{"telegram_user_id": "1", "vaf_username": "owner"}]
    config_file.seed(telegram_config={"verified": True, "enabled": True, "whitelist": owner})
    cs.set_channel_secret("telegram", "bot_token", TOKEN)

    attacker = "999999999:" + "Z" * 35
    _patch({"telegram_config": {"bot_token": attacker, "verified": True,
                                "whitelist": [{"telegram_user_id": "666", "vaf_username": "mallory"}], **extra}},
           NON_ADMIN)

    assert cs.channel_secret("telegram") == TOKEN
    assert config_file.on_disk()["telegram_config"]["whitelist"] == owner
    if extra:
        by_scope = config_file.on_disk()["connection_enabled_by_scope"]
        assert by_scope[NON_ADMIN["user_scope_id"]] == {"telegram": False}, "their own toggle is all they set"


def test_a_non_admin_without_a_scope_cannot_write_a_block_either(config_file):
    owner = [{"telegram_user_id": "1"}]
    config_file.seed(discord_config={"verified": True, "admin_user_id": "1"}, telegram_config={"whitelist": owner})
    _patch({"discord_config": {"admin_user_id": "666"}, "telegram_config": {"whitelist": []}},
           {"username": "x", "role": "user", "user_scope_id": ""})
    disk = config_file.on_disk()
    assert disk["discord_config"]["admin_user_id"] == "1" and disk["telegram_config"]["whitelist"] == owner


def test_the_absorb_step_itself_refuses_a_non_admin(config_file):
    """The second lock on the door: even a block that reached it is not stored for a non-admin."""
    config_file.seed(telegram_config={"bot_token": TOKEN})
    from vaf.core.api_keys import absorb_config_keys
    cleaned = absorb_config_keys({"telegram_config": {"bot_token": "999999999:" + "Z" * 35}}, is_admin=False)
    assert cleaned["telegram_config"] == {}
    assert dk.peek_data_secret(cs.ring_name("telegram", "bot_token")) == "", "nothing stored, nothing moved"
    assert config_file.on_disk()["telegram_config"]["bot_token"] == TOKEN


def test_both_save_paths_say_who_is_saving():
    """is_admin is a required argument; the two callers must pass the caller's role."""
    import inspect
    from pathlib import Path

    from vaf.core.api_keys import absorb_config_keys
    assert inspect.signature(absorb_config_keys).parameters["is_admin"].default is inspect.Parameter.empty
    repo = Path(__file__).resolve().parent.parent
    routes = (repo / "vaf" / "api" / "config_routes.py").read_text(encoding="utf-8")
    ws = (repo / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert 'absorb_config_keys(body, is_admin=_user.get("role") == "admin")' in routes
    assert "absorb_config_keys(new_config, is_admin=is_admin)" in ws


def test_a_refused_settings_save_answers_on_the_socket_and_keeps_it_open(config_file, monkeypatch):
    """The Settings page saves over the WebSocket. When the save is refused (the ring cannot
    take the token, the test above) the exception used to end the whole socket: the page lost
    its connection and kept showing the change as if it had been saved. It hears "error" now,
    nothing is written, and the same socket goes on answering."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.web_server import app

    stored = {"bot_token": TOKEN, "verified": True, "enabled": False}
    config_file.seed(telegram_config=dict(stored))

    def boom(name, value):
        raise RuntimeError("ring unavailable")

    monkeypatch.setattr(dk, "set_data_secret", boom)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "1", "user_scope_id": str(get_local_admin_scope_id()),
                        "username": "admin", "role": "admin"}, "s" * 32, algorithm="HS256")

    def next_of(ws, kind, wait=20):
        # Read on a helper thread: a handler that died without closing leaves the test
        # client's receive waiting forever, and that has to fail here, not hang the suite.
        import threading
        box = {}

        def pump():
            try:
                for _ in range(100):
                    msg = ws.receive_json()
                    if msg.get("type") == kind:
                        box["msg"] = msg
                        return
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(wait)
        assert "msg" in box, f"no {kind} within {wait}s, the socket stopped answering ({box.get('error')!r})"
        return box["msg"]

    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "save_config",
                                 "config": {"telegram_config": {"verified": True, "enabled": True}}}))
        saved = next_of(ws, "config_saved")
        ws.send_text(json.dumps({"type": "get_config"}))
        shown = next_of(ws, "config_update")["config"]

    assert saved["status"] == "error"
    assert shown["telegram_config"]["enabled"] is False, "the page gets the stored settings back"
    assert config_file.on_disk()["telegram_config"] == stored


@pytest.mark.parametrize("action", ["start", "stop"])
@pytest.mark.parametrize("channel", ["telegram", "discord"])
def test_only_an_admin_starts_or_stops_the_bot_of_the_whole_installation(config_file, monkeypatch, channel, action):
    """Measured before the fix: both routes answered any signed-in user, and the Connections
    switch called them, so a non-admin switching their own Telegram off stopped the bot for
    everybody. Their switch is their own lane now; the bot is an admin's to run."""
    import importlib

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    routes = importlib.import_module(f"vaf.api.{channel}_routes")
    bridge = importlib.import_module(f"vaf.api.{channel}_bridge")
    calls = []
    monkeypatch.setattr(bridge, "start_bridge", lambda: calls.append("start") or True)
    monkeypatch.setattr(bridge, "stop_bridge", lambda: calls.append("stop"))
    monkeypatch.setattr(bridge, "is_bridge_running", lambda: action == "stop")
    config_file.seed(**{f"{channel}_config": {"verified": True, "enabled": True, "admin_user_id": "1"}})
    cs.set_channel_secret(channel, "bot_token", TOKEN)

    who = {"user": NON_ADMIN}
    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = who["user"]
        return await call_next(request)

    app.include_router(routes.router)
    client = TestClient(app)

    assert client.post(f"/api/{channel}/{action}").status_code == 403
    assert calls == [], "a non-admin reached the bot"
    who["user"] = {"username": "admin", "role": "admin", "user_scope_id": "11111111-1111-1111-1111-111111111111"}
    assert client.post(f"/api/{channel}/{action}").status_code == 200
    assert calls == [action]


def test_the_connections_panel_leaves_the_bot_to_an_admin():
    """The page half of the rule above: a non-admin's switch and Disconnect button save only
    their own lane and never call the admin's routes (a 403 read as "could not be
    disconnected, its login is still stored")."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "web" / "components" / "connections"
           / "ConnectionsPanel.tsx").read_text(encoding="utf-8")
    toggle = src[src.index("const handleToggleConnection"):src.index("const handleDisconnect")]
    for channel in ("discord", "telegram"):
        block = toggle[toggle.index(f"if (appId === '{channel}') {{"):]
        assert block.index("if (!isAdmin) return;") < block.index(f"api/{channel}/start")
    disconnect = src[src.index("const handleDisconnect"):]
    assert disconnect.index("!isAdmin") < disconnect.index("/credentials")
