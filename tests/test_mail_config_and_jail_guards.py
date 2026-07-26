# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Mail config drift guards + send_mail attachment jail (CLAUDE.md Rule 2/4).

Guards:
- every email-related DEFAULTS key has a row in docs/setup/CONFIG_SCHEMA.md
  (wildcard rows like `email_oauth_*_client_id` count),
- the phishing-filter keys and the SSRF toggle are admin-write-only,
- compute_user_jail leaves an admin unjailed and confines everyone else
  (the role half of "admin" is pinned in tests/test_admin_identity_is_role_aware.py),
- SendMailTool cannot attach files outside a non-admin user's own data
  (the attachment resolution installs the shared per-user jail).
"""
import re
from pathlib import Path

from vaf.core.config import Config, get_local_admin_scope_id

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = _REPO_ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md"

# Synthetic non-admin scope (public-repo hygiene: never a real scope UUID).
_USER_SCOPE = "12345678-1234-1234-1234-123456789abc"


def _schema_email_key_patterns(doc: str):
    """Backticked tokens from the schema doc with '*' as a wildcard
    (the doc uses rows like `email_config*` and `email_oauth_*_client_id`)."""
    pats = []
    for tok in re.findall(r"`([A-Za-z0-9_*]+)`", doc):
        if "mail" in tok:  # matches email_* and mail_* rows
            pats.append(re.compile("^" + re.escape(tok).replace(r"\*", ".*") + "$"))
    return pats


def test_every_email_default_has_a_schema_row():
    doc = _SCHEMA.read_text(encoding="utf-8")
    pats = _schema_email_key_patterns(doc)
    keys = [k for k in Config.DEFAULTS if k.startswith(("email_", "mail_"))]
    missing = [k for k in keys if not any(p.match(k) for p in pats)]
    assert not missing, f"CONFIG_SCHEMA.md is missing rows for: {missing}"


def test_phishing_and_ssrf_keys_are_registered_and_admin_only():
    for key in (
        "email_allow_private_hosts",
        "email_agent_phishing_filter_enabled",
        "email_agent_phishing_score_threshold",
        "email_agent_trusted_sender_domains",
        "mail_engine_write_enabled",
        "mail_body_retention_days",
    ):
        assert key in Config.DEFAULTS, f"{key} must be registered in DEFAULTS"
        assert Config.is_global_config_key(key), f"{key} must be admin-write-only"
    # The store encryption key must be protected (never overwritten from the UI)
    # and redacted for non-admin reads.
    assert "mail_store_encryption_key" in Config.PROTECTED_KEYS
    assert Config.is_secret_config_key("mail_store_encryption_key")


def test_compute_user_jail_admin_and_user():
    from vaf.tools.filesystem import compute_user_jail

    assert compute_user_jail(None)["is_admin"] is True
    admin_scope = get_local_admin_scope_id()
    if admin_scope:
        assert compute_user_jail(admin_scope)["is_admin"] is True
    info = compute_user_jail(_USER_SCOPE)
    assert info["is_admin"] is False
    assert info["uid8"] == "12345678"
    assert info["allowed_roots"], "non-admin jail must carry the user's own root"


def test_send_mail_attachment_outside_user_data_is_refused(monkeypatch, tmp_path):
    """A non-admin scope must not be able to attach (= exfiltrate) files outside
    its own VAF_Projects tree. /tmp passes the static BLOCKED_DIRS list, so a
    denial here can only come from the per-user jail."""
    import vaf.tools.send_mail as sm

    outside_file = tmp_path / "report.txt"
    outside_file.write_text("not yours")

    calls = {"n": 0}
    monkeypatch.setattr(sm, "list_accounts_for_user", lambda *a, **k: ["user@example.com"])
    monkeypatch.setattr(sm, "get_account", lambda *a, **k: {"provider": "imap", "email": "user@example.com"})
    monkeypatch.setattr(sm.sender, "send",
                        lambda msg: calls.__setitem__("n", calls["n"] + 1) or sm.sender.SendResult(True, "ok"))

    out = sm.SendMailTool().run(
        to="rcpt@example.com",
        subject="Report",
        body="see attachment",
        attachment_paths=[str(outside_file)],
        username="alice",
        user_scope_id=_USER_SCOPE,
    )
    assert "outside your own data" in out
    assert calls["n"] == 0, "the sender must not be reached with a jailed path"


def test_send_mail_attachment_inside_own_root_is_allowed(monkeypatch, tmp_path):
    """Inside the caller's own VAF_Projects/<uid8> root the jail must allow the
    attachment (documents_dir is redirected into tmp_path - no real dirs touched)."""
    import vaf.tools.send_mail as sm
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "documents_dir", classmethod(lambda cls: tmp_path))
    own = tmp_path / "VAF_Projects" / "12345678"
    own.mkdir(parents=True)
    attachment = own / "invoice.pdf"
    attachment.write_bytes(b"%PDF-1.4")

    sent = {}

    def _snd(msg):
        sent["attachments"] = msg.attachments      # {path, filename} - delegate tail only
        sent["raw"] = msg.raw_bytes                # native path: bytes read INSIDE the jail
        return sm.sender.SendResult(True, "ok")

    monkeypatch.setattr(sm, "list_accounts_for_user", lambda *a, **k: ["user@example.com"])
    monkeypatch.setattr(sm, "get_account", lambda *a, **k: {"provider": "imap", "email": "user@example.com"})
    monkeypatch.setattr(sm.sender, "send", _snd)

    out = sm.SendMailTool().run(
        to="rcpt@example.com",
        subject="Report",
        body="see attachment",
        attachment_paths=[str(attachment)],
        username="alice",
        user_scope_id=_USER_SCOPE,
    )
    assert "sent to rcpt@example.com" in out
    assert sent["attachments"] == [{"path": str(attachment), "filename": "invoice.pdf"}]
    assert b"invoice.pdf" in sent["raw"]           # attachment embedded in the delivered MIME


def test_mail_tool_registry_copies_stay_in_sync():
    """CLAUDE.md Rule 2 guard: the mail-tool kwargs-injection tuples in
    agent.py and workflows/engine.py must list the SAME tools, every mail tool
    module must be covered, outbound mail tools must be stripped from thinking
    runs, and the destructive/outbound v2 tools must NOT be in the
    front-office allow-list (deliberate exclusion)."""
    import re as _re
    root = _REPO_ROOT
    agent_src = (root / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    engine_src = (root / "vaf" / "workflows" / "engine.py").read_text(encoding="utf-8")

    def _tuple(src):
        m = _re.search(r'\("mail_inbox", "read_mail"[^)]*\)', src)
        assert m, "mail kwargs-injection tuple not found"
        return set(_re.findall(r'"([a-z_]+)"', m.group(0)))

    agent_tools, engine_tools = _tuple(agent_src), _tuple(engine_src)
    assert agent_tools == engine_tools, (
        f"agent.py vs engine.py mail tuple drift: {agent_tools ^ engine_tools}")
    for tool in ("reply_mail", "forward_mail", "archive_mail", "delete_mail"):
        assert tool in agent_tools, f"{tool} missing from kwargs injection"

    thinking_src = (root / "vaf" / "core" / "thinking_mode.py").read_text(encoding="utf-8")
    m = _re.search(r"_SENT_TOOLS = \{[^}]*\}", thinking_src)
    assert m and "reply_mail" in m.group(0) and "forward_mail" in m.group(0), (
        "outbound mail tools must be stripped from thinking runs (_SENT_TOOLS)")

    fo_src = (root / "vaf" / "core" / "front_office_tools.py").read_text(encoding="utf-8")
    for tool in ("reply_mail", "forward_mail", "archive_mail", "delete_mail"):
        assert tool not in fo_src, (
            f"{tool} must NOT be in the front-office allow-list (deliberate exclusion)")
