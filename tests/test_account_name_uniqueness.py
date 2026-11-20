# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One name, one account: a new account name may not be another account's, "admin" or the
local admin's in any letter case or Unicode form.

THE DEFECT, measured on the code before this change: `username_taken` compared SQLite's
`lower()`, which folds ASCII only. Of the pairs Strasse/Straße, sam/ſam, weiss/Weiß, a composed
and a decomposed "josé" and ΣΟΦΟΣ/σοφος it called 0 of 5 the same name, while
`path_jail.portable_name_key` (NFC, casefold, NFC) called 5 of 5 the same. Each such pair is one
`users/<name>/` folder, and so one WhatsApp login, on Windows or macOS. Nothing reserved
"admin" or the owner's name for an ordinary account.

Real SQLite (`tests/auth_harness.py`), the real `create_local_user`; no keyring, no real
config, a scratch APP_DIR.
"""
import os
import random
import unicodedata
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from auth_harness import Harness
from vaf.auth import user_admin
from vaf.auth.models import LocalUser
from vaf.auth.user_admin import UserAdminError
from vaf.core import config as config_module
from vaf.core.config import Config
from vaf.core.path_jail import portable_name_key

PASSWORD = "correct-horse-battery-9"
OWNER = "Jos\u00e9"     # composed, as an owner types it


@pytest.fixture
def h(monkeypatch, tmp_path):
    harness = Harness(monkeypatch)
    state = {"local_admin_username": OWNER, "local_admin_scope_id": "ab12cd34-0000-4000-8000-0000000000aa"}
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "app" / "config.json")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))

    async def _cheap(p):
        return f"hash:{p}"
    monkeypatch.setattr(user_admin, "hash_password_async", _cheap)
    # SQLite stores the UUID columns with numeric affinity: a random id whose hex digits are
    # all decimal (one "e" at most, about 1 in 160,000) is stored as a REAL and cannot be read
    # back, which the fuzz's quarter million inserts hit. Ids with hex letters in them.
    from auth_harness import AsyncSessionShim
    import itertools
    counter = itertools.count(1)
    real_add = AsyncSessionShim.add

    def add(self, obj):
        if isinstance(obj, LocalUser):
            n = next(counter)
            obj.id = obj.id or uuid.UUID(f"ab12cd34-0000-4000-8000-{n:012x}")
            obj.user_scope_id = obj.user_scope_id or uuid.UUID(f"ab12cd34-0000-4000-9000-{n:012x}")
        return real_add(self, obj)
    monkeypatch.setattr(AsyncSessionShim, "add", add)
    harness.state = state
    return harness


def _create(h, name, **kw):
    async def go(db):
        return await user_admin.create_local_user(db, username=name, password=PASSWORD, **kw)
    return h.run_db(go)


def _names(h):
    with Session(h.engine) as s:
        return sorted(s.execute(select(LocalUser.username)).scalars().all())


PAIRS = [
    ("Strasse", "Straße"),
    ("sam", "ſam"),
    ("weiss", "Weiß"),
    ("ren\u00e9", "rene\u0301"),
    ("ΣΟΦΟΣ", "σοφος"),
    ("Alice", "alice"),
]


@pytest.mark.parametrize("first,second", PAIRS, ids=["ss-sharp-s", "long-s", "weiss", "nfc-nfd", "sigma", "ascii"])
def test_names_one_folder_may_open_are_taken(h, first, second):
    """MUTATION: back to `func.lower(...) == username.lower()` (every row but the ASCII one
    red); casefold without the NFC steps (the NFD row red)."""
    _create(h, first)
    with pytest.raises(UserAdminError) as refused:
        _create(h, second)
    assert refused.value.code == "username_taken" and refused.value.http_status == 409
    assert "capitals, accents or similar letters" in refused.value.message
    assert refused.value.message.endswith("Choose another user name.")
    assert _names(h) == [first]


def test_existing_twins_still_sign_in(h):
    """Rows created before the rule are left as they are and both still sign in (login
    compares `lower()`); only a third spelling is refused."""
    h.add_user("strasse", PASSWORD, no_second_factor=True)
    h.add_user("Straße", PASSWORD, no_second_factor=True)
    c = h.client(ip=None)
    for name in ("strasse", "Straße"):
        assert h.sign_in(c, name, PASSWORD, None), name
    with pytest.raises(UserAdminError) as refused:
        _create(h, "STRASSE")
    assert refused.value.code == "username_taken"
    assert _names(h) == ["Straße", "strasse"]


@pytest.mark.parametrize("name", ["admin", "ADMIN", " Admin ", OWNER, "JOS\u00c9", "jose\u0301", "JOSE\u0301"],
                         ids=["admin", "ADMIN", "padded", "owner", "owner-upper", "owner-nfd", "owner-nfd-upper"])
def test_owner_names_are_reserved(h, name):
    """No row carries these names, so only the reservation can refuse them. MUTATION: drop
    the reservation (all red); drop the key comparison in `is_local_admin_name` (the NFD
    rows red)."""
    with pytest.raises(UserAdminError) as refused:
        _create(h, name)
    assert refused.value.code == "username_reserved" and refused.value.http_status == 409
    assert refused.value.message.endswith("Choose another user name.")
    assert _names(h) == []


def test_the_first_admin_may_be_named_admin(h, monkeypatch):
    """`create_first_admin` (the browser setup and `vaf setup`) passes `first_admin`. MUTATION:
    reserve the names for the first admin too - red."""
    from vaf.auth import account_seal
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(user_admin, "_retire_setup_code", lambda: None)
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a: None)
    saved = {}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(saved)))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: saved.update(cfg)))
    h.state["local_admin_username"] = "admin"

    async def go(db):
        return await user_admin.create_first_admin(db, username="admin", password=PASSWORD)
    user = h.run_db(go)
    assert user.username == "admin" and user.role == "admin"
    assert saved["local_admin_username"] == "admin"
    # An ordinary account is still refused the name it now carries.
    with pytest.raises(UserAdminError) as refused:
        _create(h, "Admin")
    assert refused.value.code == "username_reserved"


def test_the_ordinary_route_answers_the_reservation(h):
    """`POST /api/users` carries the code to the user editor."""
    admin_id, _ = h.add_user("dana", PASSWORD, role="admin", no_second_factor=True)
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from vaf.api import user_routes

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = {"user_id": admin_id, "username": "dana", "role": "admin",
                              "user_scope_id": "ab12cd34-0000-4000-8000-0000000000dd"}
        return await call_next(request)
    app.include_router(user_routes.router)
    res = TestClient(app).post("/api/users", json={"username": "ADMIN", "password": PASSWORD})
    assert res.status_code == 409 and res.json()["code"] == "username_reserved", res.text


def test_account_name_for_scope_unchanged(monkeypatch):
    """The inline owner-name clause is gone; `is_local_admin_name` answers for it. A directory
    row whose name has the owner's key gets no folder name, any other does."""
    from vaf.core import tool_dispatch
    owner = "\u0390x"                       # folds to iota, diaeresis, acute (decomposed)
    twin = "\u03aa\u0301x"                  # folds to the precomposed iota with diaeresis + acute
    assert owner.casefold() != twin.casefold() and portable_name_key(owner) == portable_name_key(twin)
    state = {"local_admin_username": owner, "local_admin_scope_id": "ab12cd34-0000-4000-8000-0000000000aa"}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    rows = [{"username": unicodedata.normalize("NFC", twin), "user_scope_id": "ab12cd34-0000-4000-8000-0000000000b1",
             "active": True},
            {"username": "carol", "user_scope_id": "ab12cd34-0000-4000-8000-0000000000c1", "active": True}]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: rows)
    assert config_module.is_local_admin_name(rows[0]["username"]) is True
    assert config_module.account_name_for_scope("ab12cd34-0000-4000-8000-0000000000b1") is None
    assert config_module.account_name_for_scope("ab12cd34-0000-4000-8000-0000000000c1") == "carol"
    assert config_module.account_name_for_scope(state["local_admin_scope_id"]) == owner
    src = open(config_module.__file__, encoding="utf-8").read()
    body = src[src.index("def account_name_for_scope("):src.index("\ndef ", src.index("def account_name_for_scope(") + 1)]
    assert "portable_name_key(admin_name)" not in body, "the inline copy of the owner-name rule is back"


# -- fuzz ---------------------------------------------------------------------------------------

ALPHABET = ["a", "s", "A", "S", "\u00df", "\u1e9e", "\u017f", "\u00e9", "e\u0301", "\u03a3", "\u03c3",
            "\u03c2", "K", "\u212a", "\u0130", "\u0131", "i", "\ufb01", "f"]


def _draw_name(rng):
    name = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(2, 6)))
    if rng.random() < 0.2:
        name = " " + name
    if rng.random() < 0.2:
        name = name + " "
    return name


def test_fuzz_accepted_names_never_share_a_key(h):
    """A seeded classifier fuzz: sequences of 12 names through `create_local_user` on the real
    SQLite, with an owner and a stored account seeded. Oracle over the generator's own record:
    the accepted names are pairwise distinct under `portable_name_key` and none has a reserved
    key, and every refusal collides with an accepted, seeded or reserved key (no false
    refusal). `VAF_FUZZ_DRAWS` raises the draw count outside the suite."""
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "300"))
    rng = random.Random(453_001)
    owner = "Sas"
    h.state["local_admin_username"] = owner
    reserved = {portable_name_key("admin"), portable_name_key(owner)}
    mismatches = []
    for draw in range(draws):
        with Session(h.engine) as s:
            s.execute(delete(LocalUser))
            s.commit()
        seeded = "\u017fa\u017f"
        with Session(h.engine) as s:
            s.add(LocalUser(username=seeded, password_hash="x", role="user",
                            id=uuid.UUID("ab12cd34-0000-4000-a000-0000000000ff"),
                            user_scope_id=uuid.UUID("ab12cd34-0000-4000-b000-0000000000ff")))
            s.commit()
        taken = {portable_name_key(seeded)}
        for _ in range(12):
            name = _draw_name(rng)
            cleaned = name.strip()
            key = portable_name_key(cleaned)
            try:
                _create(h, name)
                outcome = "ok"
            except UserAdminError as e:
                outcome = e.code
            if outcome == "username_invalid":
                if len(cleaned) >= 2:
                    mismatches.append((draw, name, outcome))
                continue
            if outcome == "ok":
                if key in taken or key in reserved:
                    mismatches.append((draw, name, "accepted a colliding name"))
                taken.add(key)
            elif outcome == "username_reserved":
                if key not in reserved:
                    mismatches.append((draw, name, "reserved without a reserved key"))
            elif outcome == "username_taken":
                if key not in taken:
                    mismatches.append((draw, name, "taken without a colliding key"))
            else:
                mismatches.append((draw, name, outcome))
    assert not mismatches, mismatches[:10]
