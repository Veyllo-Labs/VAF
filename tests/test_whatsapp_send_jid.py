# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One number path for every outbound WhatsApp lane: `contacts_store.whatsapp_send_jid`.

Measured on the two hand copies this replaced (`whatsapp_bridge._e164_to_jid`,
`messaging_connections._e164_to_jid`): a digit of another script landed in the JID, digits were
joined around text (``+491761234567 ext 99`` dialled ``49176123456799``), a national number
went to ``01761234567`` or ``1761234567``, and the LID map matched by suffix in both
directions, so ``+34917612345`` reached the LID of ``+4917612345``. Pure functions; no bridge.

MUTATIONS, each turns the named test red:
- ``isdigit()`` instead of `shown_text.phone_digits` -> test_table;
- the suffix match on the LID map restored -> test_table;
- the `phone_digits_canonical` call dropped -> test_table, test_the_registered_number;
- a local copy re-added in `messaging_connections` -> test_one_conversion.
"""
import ast
from pathlib import Path

import pytest

from vaf.core import contacts_store
from vaf.core.contacts_store import whatsapp_send_jid

ROOT = Path(__file__).resolve().parents[1]
LID_MAP = {"111@lid": "+4917612345"}


@pytest.mark.parametrize("value, jid", [
    ("0176 1234567", "491761234567@s.whatsapp.net"),
    ("0049 176 1234567", "491761234567@s.whatsapp.net"),
    ("+49 176 1234567", "491761234567@s.whatsapp.net"),
    ("+49 (176) 123-45.67", "491761234567@s.whatsapp.net"),
    ("+491761234567 ext 99", ""),
    ("+49 176 123456٧", ""),
    ("４９176 1234567", ""),
    ("12345", ""),
    ("", ""),
    ("+4917612345", "111@lid"),
    ("004917612345", "111@lid"),
    ("+34917612345", "34917612345@s.whatsapp.net"),
    ("17612345", "17612345@s.whatsapp.net"),
])
def test_table(value, jid):
    assert whatsapp_send_jid(value, lid_map=LID_MAP) == jid


def test_one_conversion():
    """No second number conversion on the send side: every former caller imports the one
    function, and no module defines its own `_e164_to_jid`."""
    for path in (ROOT / "vaf").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "_e164_to_jid" not in names, path
    routes = ast.parse((ROOT / "vaf" / "api" / "whatsapp_routes.py").read_text(encoding="utf-8"))
    assert "_normalize_phone" not in {n.name for n in ast.walk(routes)
                                      if isinstance(n, ast.FunctionDef)}
    for rel in (("vaf", "tools", "send_whatsapp.py"), ("vaf", "api", "whatsapp_routes.py"),
                ("vaf", "api", "whatsapp_bridge.py"), ("vaf", "core", "messaging_connections.py")):
        src = ROOT.joinpath(*rel).read_text(encoding="utf-8")
        assert "from vaf.core.contacts_store import whatsapp_send_jid" in src, rel
        assert "whatsapp_send_jid(" in src.split("import whatsapp_send_jid", 1)[1], rel


def test_the_registered_number(monkeypatch):
    """The main-user number registered as ``0176 ...`` is reached at 49..., not at 176...;
    an unusable number gives None, as a missing one does."""
    from vaf.core import messaging_connections as mc
    cfg = {"whatsapp_config": {"whitelist": [
        {"vaf_username": "alice", "phone_number": "0176 1234567"},
        {"vaf_username": "bob", "phone_number": "+49 176 123456٧"},
    ]}}
    monkeypatch.setattr(mc.Config, "get", staticmethod(lambda k, d=None: cfg.get(k, d)))
    assert mc.get_whatsapp_chat_jid_from_whitelist(None, "alice") == "491761234567@s.whatsapp.net"
    assert mc.get_whatsapp_chat_jid_from_whitelist(None, "bob") is None
    assert contacts_store.whatsapp_send_jid("0176 1234567", lid_map={}) == \
        "491761234567@s.whatsapp.net"
