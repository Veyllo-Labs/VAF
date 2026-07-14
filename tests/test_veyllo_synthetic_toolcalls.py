# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Veyllo tool_call-id compatibility: synthetic-id stamping + outbound downgrade.

The provider rejects replayed tool_call ids it did not issue itself. VAF-minted
ids (text-recovered calls, id-less streams) therefore carry the call_synth_
prefix, and _prepare_messages folds such exchanges into plain text for veyllo
only. Pins the stamp, the id uniqueness fix, and the downgrade semantics.
"""
import re

from vaf.core.agent import (
    _SYNTHETIC_TC_ID_RE,
    _downgrade_synthetic_tool_exchanges,
    _synth_tool_call_id,
)
from vaf.core.tool_call_recovery import extract_xml_tool_call

GENUINE_ID = "call_00_a1b2c3d4e5f6a7b8c9d0e1f2"  # veyllo-issued shape (32 chars)


def test_synth_id_stamp_and_matcher():
    sid = _synth_tool_call_id()
    assert sid.startswith("call_synth_")
    assert _SYNTHETIC_TC_ID_RE.match(sid)
    # Legacy shapes still in persisted sessions are recognized too
    assert _SYNTHETIC_TC_ID_RE.match("extracted_1751000000")
    assert _SYNTHETIC_TC_ID_RE.match("call_a1b2c3d4")  # old 8-hex inline mint
    # Genuine ids must NEVER match
    assert not _SYNTHETIC_TC_ID_RE.match(GENUINE_ID)
    assert not _SYNTHETIC_TC_ID_RE.match("call_Ab3dEf9hIjKlMnOpQrStUvWx")  # OpenAI shape


def test_recovered_ids_are_stamped_and_unique_within_a_second():
    xml = '<invoke name="web_search"><parameter name="query">x</parameter></invoke>'
    a = extract_xml_tool_call(xml, ["web_search"])
    b = extract_xml_tool_call(xml, ["web_search"])
    assert a["id"].startswith("call_synth_")
    assert a["id"] != b["id"]  # the old extracted_<epoch> ids collided


def test_downgrade_folds_synthetic_pair_keeps_genuine():
    messages = [
        {"role": "user", "content": "check my mail"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": GENUINE_ID, "type": "function",
                         "function": {"name": "mail_inbox", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": GENUINE_ID, "name": "mail_inbox",
         "content": "2 mails"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_synth_ab12cd34", "type": "function",
                         "function": {"name": "list_email_accounts", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_synth_ab12cd34",
         "name": "list_email_accounts", "content": "gmail, work"},
    ]
    out = _downgrade_synthetic_tool_exchanges(messages)
    # Genuine exchange replays untouched, byte-identical id
    assert out[1]["tool_calls"][0]["id"] == GENUINE_ID
    assert out[2]["role"] == "tool"
    # Synthetic exchange: the empty assistant tc-message is dropped, call and
    # result fold into ONE system context message (assistant-role notes taught
    # the model to parrot "[Context: ...]" blocks as answers - live incident)
    assert len(out) == 4
    assert out[3]["role"] == "system"
    assert "list_email_accounts" in out[3]["content"]
    assert "gmail, work" in out[3]["content"]
    assert not any(m.get("role") == "tool" and m.get("tool_call_id") == "call_synth_ab12cd34"
                   for m in out)


def test_downgrade_mixed_batch_goes_whole():
    """One synthetic id in a parallel batch downgrades the WHOLE message -
    replaying half an exchange would leave the gateway's pairing broken."""
    messages = [
        {"role": "assistant", "content": "checking",
         "tool_calls": [
             {"id": GENUINE_ID, "type": "function",
              "function": {"name": "find_mail", "arguments": '{"q": "invoice"}'}},
             {"id": "extracted_1751000000", "type": "function",
              "function": {"name": "mail_inbox", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": GENUINE_ID, "name": "find_mail", "content": "hit"},
        {"role": "tool", "tool_call_id": "extracted_1751000000", "name": "mail_inbox",
         "content": "3 mails"},
    ]
    out = _downgrade_synthetic_tool_exchanges(messages)
    assert all("tool_calls" not in m for m in out)
    assert all(m.get("role") != "tool" for m in out)
    # Assistant keeps ONLY its own prose; calls+results become system context
    assert out[0] == {"role": "assistant", "content": "checking"}
    sys_text = " ".join(m["content"] for m in out if m["role"] == "system")
    assert "find_mail" in sys_text and "hit" in sys_text
    assert "mail_inbox" in sys_text and "3 mails" in sys_text
    # The parrot-prone assistant-note header must never come back
    assert not any("[Context: tools called this turn]" in str(m.get("content"))
                   for m in out)


def test_downgrade_leaves_plain_histories_alone():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert _downgrade_synthetic_tool_exchanges(messages) == messages


def test_all_agent_mint_sites_use_the_stamp():
    """CI guard: no inline f-string id mint may reappear in agent.py or the
    recovery module - every synthetic id must go through the stamped helpers."""
    import vaf.core.agent as agent_mod
    import vaf.core.tool_call_recovery as rec_mod
    for mod in (agent_mod, rec_mod):
        src = open(mod.__file__, encoding="utf-8").read()
        assert not re.search(r'f"call_\{os\.urandom', src), mod.__file__
        assert not re.search(r'f"extracted_\{int\(time', src), mod.__file__
