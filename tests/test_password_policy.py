# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The password rule: 12 characters, and not one of the most common passwords.

One rule (`validate_password`, vaf/auth/user_admin.py) for every door that sets a password,
checked here through each door: the first admin from the browser, an admin's new account,
the account's own change, the terminal reset and `vaf setup`. Passwords set before the rule
keep working: only a hash is stored, so their length is unknown.
"""
import hashlib
import importlib.util
from pathlib import Path

import pytest

from auth_harness import Harness

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "vaf" / "auth" / "data" / "common_passwords.txt"

ELEVEN = "a-quiet-key"            # 11 characters
COMMON = "Qwerty123456"           # on the list, in another case
GOOD = "a-quiet-key1"             # 12 characters, on no list


def _entries():
    return [line for line in LIST.read_bytes().decode("ascii").splitlines()
            if not line.startswith("#")]


# ── the five doors ─────────────────────────────────────────────────────────────

def _bootstrap(h, monkeypatch, password):
    from vaf.auth import account_seal, user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)
    r = h.client(None).post("/api/auth/bootstrap", json={"username": "owner", "password": password})
    return None if r.status_code == 200 else r.json().get("code")


def _admin_create(h, monkeypatch, password):
    """The admin's new account answers a refusal with its code, which the user editor words."""
    r = h.client(None).post("/api/users", json={"username": "carol", "password": password})
    if r.status_code == 201:
        return None
    assert r.json().get("detail")
    return r.json().get("code")


def _own_change(h, monkeypatch, password):
    h.add_user("dave", "dave-old-password-1", no_second_factor=True)
    token = h.sign_in(h.client("192.168.31.1"), "dave", "dave-old-password-1", None)
    r = h.client("192.168.31.1").post(
        "/api/auth/change-password", headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "dave-old-password-1", "new_password": password})
    return None if r.status_code == 200 else r.json().get("code")


def _terminal_reset(h, monkeypatch, password):
    from types import SimpleNamespace

    from vaf.auth.user_admin import UserAdminError, apply_terminal_reset
    try:
        apply_terminal_reset(SimpleNamespace(), password)
    except UserAdminError as e:
        return e.code
    return None


def _vaf_setup(h, monkeypatch, password):
    from vaf.auth.user_admin import UserAdminError, validate_password
    from vaf.cli.cmd import setup as setup_cmd

    class _Passed(Exception):
        pass

    said = []
    monkeypatch.setattr(setup_cmd.UI, "error", lambda text, *a, **k: said.append(text))

    def _stack():
        raise _Passed()

    monkeypatch.setattr(setup_cmd, "_start_service_stack", _stack)
    try:
        outcome = setup_cmd.run_first_account_setup(username="owner", password=password,
                                                    interactive=False)
    except _Passed:
        return None
    assert outcome == setup_cmd.SetupOutcome.ABORTED and said
    try:
        validate_password(password)
    except UserAdminError as e:
        assert said[-1] == e.message
        return e.code
    raise AssertionError("refused without the rule saying so")


DOORS = {"bootstrap": _bootstrap, "admin create": _admin_create, "own change": _own_change,
         "terminal reset": _terminal_reset, "vaf setup": _vaf_setup}


@pytest.mark.parametrize("door", sorted(DOORS))
@pytest.mark.parametrize("password,refusal", [(ELEVEN, "password_too_short"),
                                               (COMMON, "password_common"),
                                               (GOOD, None)])
def test_twelve_and_not_common(monkeypatch, door, password, refusal):
    """Every door refuses an 11-character password and a common one (in any case), and takes
    a 12-character one that is on no list. MUTATION: MIN_PASSWORD_LEN = 8; compare without
    .lower(); skip the rule in apply_terminal_reset - red."""
    h = Harness(monkeypatch)
    assert DOORS[door](h, monkeypatch, password) == refusal


def test_an_older_short_password_still_signs_in(monkeypatch):
    """Only a hash is stored, so the length of a password set before the rule is unknown: it
    keeps working, and the rule applies to every new one."""
    h = Harness(monkeypatch)
    h.add_user("erin", "eight888", no_second_factor=True)
    assert h.login(h.client("192.168.31.2"), "erin", "eight888").status_code == 200
    token = h.sign_in(h.client("192.168.31.3"), "erin", "eight888", None)
    r = h.client("192.168.31.3").get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["username"] == "erin"


def test_the_terminal_reset_prompt_prints_the_rule(monkeypatch):
    """`vaf users reset-password` asks for the new password and says the rule's own sentence
    when it refuses. MUTATION: check the length in _new_password again - red."""
    import vaf.cli.cmd.users as users_cmd
    from vaf.auth.user_admin import UserAdminError, validate_password
    answers = [COMMON, GOOD, GOOD]
    said = []
    monkeypatch.setattr(users_cmd.getpass, "getpass", lambda _p="": answers.pop(0))
    monkeypatch.setattr(users_cmd.UI, "error", lambda text, *a, **k: said.append(text))
    assert users_cmd._new_password() == GOOD
    with pytest.raises(UserAdminError) as e:
        validate_password(COMMON)
    assert said == [e.value.message]


# ── the shipped list ───────────────────────────────────────────────────────────

def _hygiene_guard():
    spec = importlib.util.spec_from_file_location(
        "_hygiene_guard", ROOT / "tests" / "test_public_repo_hygiene.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_list_is_well_formed():
    """10,000 entries, lowercased, at least 12 characters, no duplicates, none that the
    public-repo hygiene guard flags, under a header that names where they come from.
    MUTATION: add an entry with the shape of a session id to the file - red."""
    from vaf.auth.user_admin import MIN_PASSWORD_LEN
    raw = LIST.read_bytes()
    raw.decode("ascii")
    header = [line for line in raw.decode("ascii").splitlines() if line.startswith("#")]
    text = "\n".join(header)
    for needle in ("https://github.com/danielmiessler/SecLists",
                   "commit c205c36a445bff37f8e58a9ec829105cd4975c58",
                   "sha256 424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51",
                   "MIT, Copyright 2018 Daniel Miessler"):
        assert needle in text, needle
    entries = _entries()
    assert len(entries) == 10_000
    assert len(set(entries)) == len(entries)
    assert all(e == e.lower() and len(e) >= MIN_PASSWORD_LEN and e.strip() == e for e in entries)
    guard = _hygiene_guard()
    flagged = [e for e in entries if guard._session_id_hits(e) or guard._home_path_hits(e)
               or guard._attribution_hits(e)]
    assert flagged == []
    assert "qwerty123456" in entries
    assert (ROOT / "licenses" / "SecLists-MIT.txt").read_bytes().startswith(b"MIT License")
    assert "vaf/auth/data/common_passwords.txt" in (ROOT / "docs" / "legal" / "THIRD_PARTY.md"
                                                    ).read_bytes().decode("utf-8")


def test_the_generator_writes_this_file():
    """The script that rebuilds the file from the pinned source writes exactly these bytes
    when it is handed these entries: the header, the order and the filter are its own.
    MUTATION: change a header line in the script - red."""
    spec = importlib.util.spec_from_file_location(
        "build_common_passwords", ROOT / "scripts" / "build_common_passwords.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    entries = _entries()
    # The source's own case and some noise the filter must drop: a short one and a duplicate.
    source = "\n".join(["short", entries[0].upper()] + entries + [entries[5]]).encode("ascii")
    digest = hashlib.sha256(source).hexdigest()
    assert script.build(source, expected=digest).encode("ascii") == LIST.read_bytes()
    with pytest.raises(SystemExit):
        script.build(source + b"\nchanged", expected=digest)


def test_a_missing_list_still_checks_the_length(monkeypatch, caplog):
    """A packaging error must not block every password change: the length still applies,
    and the missing file is logged once."""
    from vaf.auth import user_admin
    monkeypatch.setattr(user_admin, "_COMMON_PASSWORDS_FILE", ROOT / "no-such-file.txt")
    monkeypatch.setattr(user_admin, "_common", None)
    with caplog.at_level("ERROR"):
        user_admin.validate_password(COMMON)
        user_admin.validate_password(GOOD)
    assert len([r for r in caplog.records if "common-password list" in r.getMessage()]) == 1
    with pytest.raises(user_admin.UserAdminError):
        user_admin.validate_password(ELEVEN)


def test_the_list_is_read_once(monkeypatch):
    from vaf.auth import user_admin
    monkeypatch.setattr(user_admin, "_common", None)
    reads = []
    real = Path.read_bytes

    def counting(self):
        if self == user_admin._COMMON_PASSWORDS_FILE:
            reads.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    for _ in range(3):
        with pytest.raises(user_admin.UserAdminError):
            user_admin.validate_password(COMMON)
    assert len(reads) == 1


def test_a_crlf_checkout_of_the_list_still_refuses(monkeypatch, tmp_path):
    """What a Windows checkout without the line-end rule would ship: the loader reads the same
    set from a CRLF copy, so the rule only keeps the byte-for-byte test honest there."""
    from vaf.auth import user_admin
    crlf = tmp_path / "common_passwords.txt"
    crlf.write_bytes(LIST.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(user_admin, "_COMMON_PASSWORDS_FILE", crlf)
    monkeypatch.setattr(user_admin, "_common", None)
    assert user_admin._common_passwords() == frozenset(_entries())
    with pytest.raises(user_admin.UserAdminError):
        user_admin.validate_password(COMMON)
