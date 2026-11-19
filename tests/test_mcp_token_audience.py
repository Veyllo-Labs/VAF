# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A stored MCP credential reaches only the origin it was stored for.

Measured before the change, with two loopback servers A and B and real `upsert_server` moves:
a registered tool built while the server was at A sent B's token to A after one move (1 of 1);
with a mover saving continuously, registered tools sent the other origin's token in 200 of 400
requests, discovery in 3 of 150, the raw `mcp_call` in 1 of 1500, and a second process (a CLI
chat) in 211 of 800. The token was read from the key ring by NAME at request time, with
nothing tying it to the URL the request went to. A snapshot of entry and token under the
in-process manifest lock left the second process at 246 of 800: a save writes the ring before
the manifest, and the lock does not reach another process. With the address stored in the same
ring value as the token: 0 of 800.

A request here goes to a stub session pool that records each `(url, headers)`; nothing leaves
the process. The key ring is per test (conftest).
"""
import inspect
import json
import random
import string
import time

import pytest

import vaf.core.mcp_registry as reg
from vaf.core import data_keyring, mcp_secrets

A = "https://a.example.invalid/mcp"
A2 = "https://a.example.invalid:443/v2/mcp"      # the same origin as A, another path
B = "https://b.example.invalid/mcp"
B2 = "https://B.example.invalid/other"            # the same origin as B
C = "http://a.example.invalid/mcp"                # A's host over another scheme: another origin
URLS = (A, A2, B, B2, C)
LABEL = {A: "a", A2: "a", B: "b", B2: "b", C: "c"}
RING = "mcp_server.svc.token"


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: path)
    # The address check of a save resolves the host; these hosts never resolve, and a test
    # must not wait on a resolver for that.
    import vaf.network.egress as egress
    monkeypatch.setattr(egress, "check_destination", lambda url, policy=None: None)

    def write(servers):
        path.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    def read():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("M", (), {"write": staticmethod(write), "read": staticmethod(read), "path": path})


class _Pool:
    """The remote session pool, recording what each request would carry."""

    def __init__(self):
        self.sent = []

    def call_tool(self, transport, url, tool, arguments=None, headers=None, timeout=None,
                  auth=None, egress=None):
        self.sent.append((url, dict(headers or {})))
        return "ok"

    def list_tools(self, transport, url, headers=None, timeout=None, auth=None, egress=None):
        self.sent.append((url, dict(headers or {})))
        return [{"name": "echo"}]

    def close(self, url=None, account=None):
        pass


@pytest.fixture
def pool(monkeypatch):
    import vaf.core.mcp_remote as mcp_remote
    stub = _Pool()
    monkeypatch.setattr(mcp_remote, "get_remote_pool", lambda: stub)
    return stub


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# -- 1. lifetime: a tool built before a move ---------------------------------------------------

def test_a_tool_built_before_a_move_sends_no_token_to_the_old_origin(manifest, pool):
    reg.upsert_server("svc", transport="http", url=A, token="TOKEN-A")
    tool = reg.make_mcp_tool("svc", manifest.read()["servers"]["svc"], {"name": "echo"})()
    assert tool.run(text="x") == "ok"
    assert pool.sent[-1] == (A, _bearer("TOKEN-A"))

    reg.upsert_server("svc", transport="http", url=B, token="TOKEN-B")
    assert tool.run(text="y") == "ok"
    assert pool.sent[-1] == (A, {}), "the old address got the new server's token"
    assert not any("TOKEN-B" in json.dumps(h) for url, h in pool.sent if url == A)


def test_an_mcp_call_that_looked_the_server_up_before_a_move_sends_no_token_there(
        manifest, pool, monkeypatch):
    from vaf.tools.mcp_client import get_mcp_client
    reg.upsert_server("svc", transport="http", url=A, token="TOKEN-A")
    real_lookup = reg.registered_server

    def lookup_then_move(server):
        found = real_lookup(server)
        reg.upsert_server("svc", transport="http", url=B, token="TOKEN-B")
        return found

    monkeypatch.setattr(reg, "registered_server", lookup_then_move)
    assert get_mcp_client().run(server="svc", tool_name="echo", arguments={}) == "ok"
    assert pool.sent == [(A, {})], pool.sent


# -- 2. discovery ------------------------------------------------------------------------------

def test_discovery_sends_no_token_to_an_origin_the_token_was_not_stored_for(manifest, pool):
    # The manifest discovery loaded names A; the ring already holds B's token.
    manifest.write({"svc": {"transport": "http", "url": A}})
    mcp_secrets.set_server_token("svc", "TOKEN-B", audience=B)
    tools, status = reg.discover_mcp_tools(timeout_seconds=5)
    assert status["svc"]["connected"] is True, status
    assert pool.sent == [(A, {})], pool.sent
    mcp_secrets.set_server_token("svc", "TOKEN-A", audience=A2)
    reg.discover_mcp_tools(timeout_seconds=5)
    assert pool.sent[-1] == (A, _bearer("TOKEN-A")), "the token's own origin still gets it"


# -- 3. the connection test --------------------------------------------------------------------

def test_the_probe_sends_the_stored_token_only_to_its_origin(manifest, pool):
    reg.upsert_server("svc", transport="http", url=A, token="TOKEN-A")
    assert reg.probe_mcp_server({"transport": "http", "url": A2}, 5, name="svc")["connected"]
    assert pool.sent[-1] == (A2, _bearer("TOKEN-A")), "the saved origin gets the stored token"
    reg.probe_mcp_server({"transport": "http", "url": B}, 5, name="svc")
    assert pool.sent[-1] == (B, {}), "another origin does not"
    for url in (A, B):
        reg.probe_mcp_server({"transport": "http", "url": url}, 5, name="svc", token="EDITOR")
        assert pool.sent[-1] == (url, _bearer("EDITOR")), "the editor's token goes where it is tested"


def test_the_probe_holds_no_snapshot_of_its_own():
    source = inspect.getsource(reg.probe_mcp_server)
    assert "_manifest_lock" not in source and "server_token" not in source


# -- 4. the stored format ----------------------------------------------------------------------

def test_a_saved_token_is_stored_with_its_address(manifest):
    reg.upsert_server("svc", transport="http", url=A, token="TOKEN-A")
    assert json.loads(data_keyring.peek_data_secret(RING)) == {"audience": A, "token": "TOKEN-A"}
    assert mcp_secrets.server_token("svc") == "TOKEN-A"
    shown = {s["name"]: s for s in reg.servers_for_display({})}["svc"]
    assert shown["token_set"] is True and "TOKEN-A" not in json.dumps(shown)


def test_a_hand_written_token_is_bound_to_the_entry_url(manifest):
    manifest.write({"svc": {"transport": "http", "url": B, "token": "HAND"},
                    "hdr": {"transport": "sse", "url": A, "headers": {"Authorization": "Bearer HDR"}}})
    reg.load_mcp_manifest()
    assert json.loads(data_keyring.peek_data_secret(RING)) == {"audience": B, "token": "HAND"}
    assert json.loads(data_keyring.peek_data_secret("mcp_server.hdr.token")) == {"audience": A, "token": "HDR"}


def test_a_save_binds_a_legacy_token(manifest):
    manifest.write({"svc": {"transport": "http", "url": A}})
    data_keyring.set_data_secret(RING, "OLD")                 # stored before the address was kept
    reg.upsert_server("svc", transport="http", url=A2, permission_level="read")
    assert json.loads(data_keyring.peek_data_secret(RING)) == {"audience": A2, "token": "OLD"}


# -- 5. a save from another process, after any read --------------------------------------------

def _foreign_save(path):
    """A move to B with a new token, written as another VAF process writes it: the ring value
    first, then the manifest file. Neither takes this process's lock."""
    data_keyring.set_data_secret(RING, json.dumps({"audience": B, "token": "TOKEN-B"}, sort_keys=True))
    path.write_text(json.dumps({"servers": {"svc": {"transport": "http", "url": B}}}), encoding="utf-8")


@pytest.mark.parametrize("start", ["legacy", "bound"])
@pytest.mark.parametrize("target", [A, B])
def test_a_save_from_another_process_at_any_read_sends_no_foreign_token(manifest, monkeypatch,
                                                                       start, target):
    own = {A: "TOKEN-A", B: "TOKEN-B"}[target]
    real_record, real_load = mcp_secrets.server_token_record, reg.load_mcp_manifest
    box = {"reads": 0, "at": 0, "fired": False}

    def after_read():
        box["reads"] += 1
        if box["reads"] == box["at"] and not box["fired"]:
            box["fired"] = True
            _foreign_save(manifest.path)

    def record(name):
        try:
            return real_record(name)
        finally:
            after_read()

    def load():
        try:
            return real_load()
        finally:
            after_read()

    monkeypatch.setattr(mcp_secrets, "server_token_record", record)
    monkeypatch.setattr(reg, "load_mcp_manifest", load)
    reached = []
    for at in range(0, 5):
        manifest.write({"svc": {"transport": "http", "url": A}})
        if start == "legacy":
            data_keyring.set_data_secret(RING, "TOKEN-A")
        else:
            data_keyring.set_data_secret(RING, json.dumps({"audience": A, "token": "TOKEN-A"}))
        box.update(reads=0, at=at, fired=False)
        if at == 0:
            box["fired"] = True
            _foreign_save(manifest.path)
        sent = reg.server_headers("svc", {"transport": "http", "url": target}).get("Authorization", "")
        assert sent in ("", f"Bearer {own}"), (start, target, at, sent)
        if box["fired"]:
            reached.append(at)
    # Every read the lookup makes had a save land after it: one read for a bound token, the
    # record, the entry and the record again for a legacy one.
    assert reached == ([0, 1, 2, 3] if start == "legacy" else [0, 1]), reached


def test_a_stored_value_too_deep_to_read_is_sent_nowhere(manifest):
    """Found by the fuzz below, as a question to the model: how deep a value the decoder
    reads depends on the interpreter, so the one place the answer matters is pinned here. A
    value in the stored format that cannot be read is damage: no token, for any address, and
    not the whole text as a bare token to the address the entry names now."""
    manifest.write({"svc": {"transport": "http", "url": A}})
    deep = "[" * 100000 + "]" * 100000
    data_keyring.set_data_secret(RING, '{"audience": "%s", "token": "TOKEN-A", "x": %s}' % (A, deep))
    assert mcp_secrets.server_token_record("svc") == ("", "")
    assert all(reg.stored_token("svc", url) == "" for url in URLS)
    readable = '{"audience": "%s", "token": "TOKEN-A", "x": %s}' % (B, "[" * 50 + "]" * 50)
    data_keyring.set_data_secret(RING, readable)
    assert [reg.stored_token("svc", url) for url in URLS] == ["", "", "TOKEN-A", "TOKEN-A", ""]


# -- 6. a token stored before the address was kept ---------------------------------------------

def test_a_token_stored_before_this_release_still_reaches_its_server_and_no_other(manifest):
    manifest.write({"svc": {"transport": "http", "url": A}})
    data_keyring.set_data_secret(RING, "OLD")
    assert reg.server_headers("svc", {"transport": "http", "url": A2}) == _bearer("OLD")
    for url in (B, C):
        assert reg.server_headers("svc", {"transport": "http", "url": url}) == {}
    manifest.write({"svc": {"transport": "http", "url": A, "auth": "oauth"}})
    assert reg.stored_token("svc", A) == "", "a server with a sign-in per account takes none"
    manifest.write({"svc": {"command": "srv"}})
    assert reg.stored_token("svc", A) == "", "nor does a local one"


# -- 7. the sign-in per account ----------------------------------------------------------------

def test_a_sign_in_made_at_the_new_address_is_not_used_by_an_old_tool(manifest, pool, monkeypatch):
    from vaf.core import mcp_oauth
    monkeypatch.setattr(mcp_oauth, "forget_server", lambda *a, **k: 0)
    reg.upsert_server("svc", transport="http", url=A, auth="oauth")
    tool = reg.make_mcp_tool("svc", manifest.read()["servers"]["svc"], {"name": "echo"})()
    reg.upsert_server("svc", transport="http", url=B, auth="oauth")
    account = mcp_oauth.account_key("scope-alice")
    mcp_secrets.set_oauth_record("svc", account, {
        "tokens": {"access_token": "TOKEN-B", "token_type": "Bearer"}, "audience": B,
        "redirect_uri": "http://localhost:8001/api/mcp/oauth/callback"})
    out = tool.run(user_scope_id="scope-alice")
    assert out.startswith("Error:") and "not signed in" in out, out
    assert pool.sent == [], "a request was made for the old address"
    new_tool = reg.make_mcp_tool("svc", manifest.read()["servers"]["svc"], {"name": "echo"})()
    assert new_tool.run(user_scope_id="scope-alice") == "ok", "the new address uses it"


def test_the_session_storage_hands_a_sign_in_only_to_its_own_address(manifest):
    import asyncio

    from vaf.core import mcp_oauth
    mcp_secrets.set_oauth_record("svc", "acct", {
        "tokens": {"access_token": "TOKEN-B", "token_type": "Bearer"}, "audience": B})
    old = mcp_oauth._RingStorage("svc", "acct", {"transport": "http", "url": A, "auth": "oauth"}, "r")
    new = mcp_oauth._RingStorage("svc", "acct", {"transport": "http", "url": B2, "auth": "oauth"}, "r")
    assert asyncio.run(old.get_tokens()) is None
    assert asyncio.run(new.get_tokens()).access_token == "TOKEN-B"


def test_a_sign_in_of_another_origin_is_not_listed_or_used_for_discovery_or_the_probe(
        manifest, pool):
    """A hand edit moved the entry to A; the only record was signed in at B. Discovery does not
    list with it, the account's status does not call it signed in, and the connection test,
    which never raises, tests without it."""
    from vaf.core import mcp_oauth
    from vaf.core.mcp_remote import McpSignInRequired
    cfg = {"transport": "http", "url": A, "auth": "oauth"}
    manifest.write({"svc": cfg})
    account = mcp_oauth.account_key("scope-alice")
    mcp_secrets.set_oauth_record("svc", account, {
        "tokens": {"access_token": "TOKEN-B", "token_type": "Bearer"}, "audience": B})
    with pytest.raises(McpSignInRequired):
        mcp_oauth.discovery_auth("svc", cfg)
    # The local admin's record is of the other origin; another account's fits: the list is
    # read with that one, rather than refused for the admin's.
    mcp_secrets.set_oauth_record("svc", mcp_oauth.account_key(None), {
        "tokens": {"access_token": "ADMIN-B", "token_type": "Bearer"}, "audience": B})
    mcp_secrets.set_oauth_record("svc", "scope-bob", {
        "tokens": {"access_token": "BOB-A", "token_type": "Bearer"}, "audience": A2})
    assert mcp_oauth.discovery_auth("svc", cfg).account == mcp_oauth._pool_account("svc", "scope-bob")
    status = {s["name"]: s for s in mcp_oauth.sign_in_status("scope-alice")}["svc"]
    assert status["signed_in"] is False and status["signed_in_at"] is None
    out = reg.probe_mcp_server(cfg, 5, name="svc", user_scope_id="scope-alice")
    assert out["connected"] is True and pool.sent == [(A, {})], (out, pool.sent)


def test_signed_in_and_the_settings_count_only_a_sign_in_made_at_the_entry_origin(manifest):
    """The yes of `signed_in` and the count on the Settings card are the ones `auth_for`
    gives: a record made at another address is no sign-in for this entry."""
    from vaf.core import mcp_oauth
    manifest.write({"svc": {"transport": "http", "url": A, "auth": "oauth"}})
    tokens = {"access_token": "X", "token_type": "Bearer"}
    mcp_secrets.set_oauth_record("svc", "scope-here", {"tokens": tokens, "audience": A2})
    mcp_secrets.set_oauth_record("svc", "scope-there", {"tokens": tokens, "audience": B})
    mcp_secrets.set_oauth_record("svc", "scope-old", {"tokens": tokens})     # made before the field
    assert mcp_oauth.signed_in("svc", "scope-here") is True
    assert mcp_oauth.signed_in("svc", "scope-old") is True
    assert mcp_oauth.signed_in("svc", "scope-there") is False
    assert mcp_oauth.signed_in("svc", "scope-there", {"transport": "http", "url": B2, "auth": "oauth"}) is True
    shown = {s["name"]: s for s in reg.servers_for_display({})}["svc"]
    assert shown["accounts_signed_in"] == 2, shown


def test_a_token_stored_for_another_address_is_not_shown_as_set(manifest):
    """After a hand edit moved the URL, the stored token is never sent to the new address, so
    the editor must not say "a token is stored, leave the field empty to keep it"."""
    manifest.write({"svc": {"transport": "http", "url": A}})
    for value, shown_set in ((json.dumps({"audience": A2, "token": "T"}), True),
                             (json.dumps({"audience": B, "token": "T"}), False),
                             ("BARE", True)):
        data_keyring.set_data_secret(RING, value)
        shown = {s["name"]: s for s in reg.servers_for_display({})}["svc"]
        assert shown["token_set"] is shown_set, (value, shown)


# -- fuzz: the stored format and the decision, against a model ---------------------------------

_SEED = 0x397A
_SEQUENCES = 150


def _bare(rng):
    """A value stored before the address was kept: text that is not a JSON object."""
    kind = rng.randrange(4)
    if kind == 0:
        return rng.choice(["123", '"abc"', "[1, 2]", "null", "true", "[" * 50 + "]" * 50])
    alphabet = string.ascii_letters + string.digits + "-._~+/=: \"[]}"
    text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 64 if kind < 3 else 4096)))
    return text if not text.lstrip().startswith("{") else "x" + text


def _raw(rng):
    """A ring value from the generator, with what the model expects of it:
    ("bound", token, label-or-None) | ("legacy", token) | None (no token)."""
    choice = rng.randrange(8)
    if choice == 0:
        value = _bare(rng)
        return value, ("legacy", value)
    if choice == 1:
        return "", None
    token = "T" + "".join(rng.choice(string.ascii_letters) for _ in range(rng.randrange(1, 12)))
    # The user part hides the real host: that address is B's origin, not A's.
    hidden = "https://a.example.invalid@b.example.invalid/"
    audiences = {**LABEL, "": None, "not a url": None, "https://": None, hidden: "b"}
    audience = rng.choice(sorted(audiences))
    obj = {"token": token, "audience": audience}
    expected = ("bound", token, audiences[audience])
    if choice == 2:
        obj.pop("audience")
        expected = ("bound", token, None)
    elif choice == 3:
        obj["audience"] = rng.choice([5, None, [A], {"url": A}, True])
        expected = ("bound", token, None)
    elif choice == 4:
        obj["token"] = rng.choice(["", 7, None, [token], {"token": token}])
        expected = None
    elif choice == 5:
        obj.pop("token")
        expected = None
    elif choice == 6:
        obj["extra"] = {"nested": [[[["x" * rng.randrange(0, 2000)]]]]}
    else:
        # Nested within any decoder's limit, or near it: how deep the decoder reads depends on
        # the interpreter (3.13 reads 2000 levels), so a value there is either read and decided
        # by its own audience, or refused as damage and sent nowhere. Both are safe; nothing
        # else is. 2000 levels make a value of about 4 KB.
        depth = rng.choice([rng.randrange(1, 100), 2000])
        value = json.dumps(obj)[:-1] + ', "deep": ' + "[" * depth + "]" * depth + "}"
        if depth >= 100 and expected is not None:
            expected = ("deep",) + expected[1:]
        return value, expected
    return json.dumps(obj, sort_keys=rng.random() < 0.5), expected


def _expected(model, target):
    """The values a lookup may return."""
    ring = model["ring"]
    if ring is None:
        return {""}
    if ring[0] in ("bound", "deep"):
        due = ring[1] if ring[2] is not None and ring[2] == LABEL[target] else ""
        return {due, ""} if ring[0] == "deep" else {due}
    # A bare token follows the current entry, as it always did (named in stored_token).
    return {ring[1] if LABEL[model["entry"]] == LABEL[target] else ""}


def _run_sequence(rng, manifest_obj, checks):
    url = rng.choice(URLS)
    manifest_obj.write({"svc": {"transport": "http", "url": url}})
    data_keyring.delete_data_secret(RING)
    model = {"entry": url, "ring": None}
    for _ in range(rng.randrange(10, 21)):
        op = rng.randrange(6)
        if op == 0:                                         # a Settings save
            new_url = rng.choice(URLS)
            token = rng.choice([None, "S" + str(rng.randrange(10 ** 6))])
            reg.upsert_server("svc", transport="http", url=new_url, token=token)
            ring = model["ring"]
            if LABEL[model["entry"]] != LABEL[new_url] and not token:
                ring = None                                 # moved without a token: none kept
            elif token:
                ring = ("bound", token, LABEL[new_url])
            elif ring is not None and ring[0] == "legacy" and ring[1].strip():
                ring = ("bound", ring[1].strip(), LABEL[new_url])   # bound by the save
            model.update(entry=new_url, ring=ring)
        elif op == 1:                                       # a hand edit of the URL
            new_url = rng.choice(URLS)
            manifest_obj.write({"svc": {"transport": "http", "url": new_url}})
            model["entry"] = new_url
        elif op == 2:                                       # a bare token from an older release
            value = _bare(rng)
            data_keyring.set_data_secret(RING, value)
            model["ring"] = ("legacy", value)
        elif op == 3:                                       # any ring value at all
            value, expected = _raw(rng)
            data_keyring.set_data_secret(RING, value)
            model["ring"] = expected
            mcp_secrets.server_token_record("svc")          # never raises
        else:                                               # a lookup for every address
            for target in URLS:
                got, want = reg.stored_token("svc", target), _expected(model, target)
                checks.append((model["ring"], model["entry"], target, got, want))


def _fuzz(manifest_obj, seed, sequences):
    rng = random.Random(seed)
    checks = []
    for _ in range(sequences):
        _run_sequence(rng, manifest_obj, checks)
    return checks


def test_fuzz_a_token_goes_only_where_the_model_says(manifest):
    """Model-based, seeded: random saves, hand edits, bare tokens and arbitrary ring values,
    then a lookup for each of five addresses in three origins. The model knows, for every
    token, the origin label it was stored for; no URL is parsed on the test's side."""
    started = time.monotonic()
    checks = _fuzz(manifest, _SEED, _SEQUENCES)
    wrong = [c for c in checks if c[3] not in c[4]]
    assert not wrong, wrong[:3]
    assert len(checks) > 1000
    assert sum(1 for c in checks if c[3]) > 100, "too few lookups where a token is due"
    assert time.monotonic() - started < 30
