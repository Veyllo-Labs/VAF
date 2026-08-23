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
    _close_assistant_after_tool_result,
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


def test_downgrade_carries_reasoning_content_when_present():
    """MUTATION: the fold must hand reasoning back, dynamically.

    The thinking contract is symmetric: no reasoning produced -> no field sent
    and none demanded; reasoning produced -> it MUST ride the folded assistant
    message. The fold used to rebuild from content alone - the field the
    DeepSeek-family repair had just restored was dropped, and Veyllo (stateful,
    it knew the exchange had reasoning) answered 400 "The reasoning_content in
    the thinking mode must be passed back" (live incident 2026-08-21: a
    text-recovered web_search inside a thinking turn).
    """
    messages = [
        {"role": "user", "content": "wetter morgen?"},
        {"role": "assistant", "content": "checking the forecast",
         "reasoning_content": "user wants tomorrow's weather, search first",
         "tool_calls": [{"id": "call_synth_ab12cd34", "type": "function",
                         "function": {"name": "web_search", "arguments": '{"q": "wetter"}'}}]},
        {"role": "tool", "tool_call_id": "call_synth_ab12cd34",
         "name": "web_search", "content": "rain, 20 C"},
    ]
    out = _downgrade_synthetic_tool_exchanges(messages)
    asst = [m for m in out if m["role"] == "assistant"][0]
    assert asst["reasoning_content"] == "user wants tomorrow's weather, search first"
    assert asst["content"] == "checking the forecast"
    assert "tool_calls" not in asst


def test_downgrade_keeps_a_reasoning_only_assistant_turn():
    """The think block was the message's WHOLE content: after the repair moved
    it into reasoning_content, content is empty - and the old `if text:` guard
    made the assistant turn vanish outright. It must survive with an empty
    content string (the API requires content to be a non-null string)."""
    messages = [
        {"role": "assistant", "content": "",
         "reasoning_content": "thinking only, then straight to the tool",
         "tool_calls": [{"id": "call_synth_ab12cd34", "type": "function",
                         "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_synth_ab12cd34",
         "name": "web_search", "content": "result"},
    ]
    out = _downgrade_synthetic_tool_exchanges(messages)
    assts = [m for m in out if m["role"] == "assistant"]
    assert len(assts) == 1
    assert assts[0]["content"] == ""
    assert assts[0]["reasoning_content"] == "thinking only, then straight to the tool"


def test_downgrade_sends_no_reasoning_field_when_none_exists():
    """The dynamic other half: a turn that never reasoned must not grow the
    field, and an empty tc-message without reasoning stays dropped."""
    messages = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_synth_ab12cd34", "type": "function",
                         "function": {"name": "mail_inbox", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_synth_ab12cd34",
         "name": "mail_inbox", "content": "2 mails"},
    ]
    out = _downgrade_synthetic_tool_exchanges(messages)
    assert not any(m.get("role") == "assistant" for m in out)
    assert not any("reasoning_content" in m for m in out)


def test_prepare_messages_repair_and_fold_compose_for_veyllo():
    """The WIRING, end to end: an inline <think> assistant message with a
    synthetic tool call goes through _prepare_messages and comes out as ONE
    assistant message carrying reasoning_content, no tool_calls - the repair
    runs first, the fold second, and the fold must not undo the repair."""
    from vaf.core.agent import Agent

    class _Stub:
        _thinking_reply_context = None
        filename = "api"
        model_display_name = ""
        config = {}
        provider = "veyllo"

        def _consolidate_system_messages(self, messages):
            return messages

    out = Agent._prepare_messages(_Stub(), [
        {"role": "user", "content": "wetter morgen?"},
        {"role": "assistant", "content": "<think>search first</think>",
         "tool_calls": [{"id": "call_synth_ab12cd34", "type": "function",
                         "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_synth_ab12cd34",
         "name": "web_search", "content": "rain"},
    ])
    assts = [m for m in out if m["role"] == "assistant"]
    assert len(assts) == 1
    assert assts[0].get("reasoning_content") == "search first"
    assert assts[0]["content"] == ""
    assert "tool_calls" not in assts[0]
    assert not any(m.get("role") == "tool" for m in out)


# ── an assistant turn after a tool result must be closed by a user turn ───────
#
# Measured against the live gateway with an id it had just issued, one variable
# at a time: the loop step (ends on tool) and a post-tool system nudge both pass,
# an assistant turn appended after the tool result fails, and a user turn behind
# it makes the identical history pass again. The gateway reports that as "The
# reasoning_content in the thinking mode must be passed back", which is
# misleading - the field was present in every failing probe.

def _tool_turn():
    return [
        {"role": "user", "content": "what time is it?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": GENUINE_ID, "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": GENUINE_ID, "content": "11:45"},
    ]


def test_a_correction_after_a_tool_result_travels_as_a_user_turn():
    """The live shape: a self-correction retry appends the reply it just got
    plus an instruction to redo it. Without the conversion the gateway 400s."""
    out = _close_assistant_after_tool_result(_tool_turn() + [
        {"role": "assistant", "content": "It is 11:45."},
        {"role": "system", "content": "CORRECTION NEEDED: restate it."},
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant", "user"]
    assert out[-1]["content"] == "CORRECTION NEEDED: restate it."


def test_the_loop_step_and_a_post_tool_nudge_are_left_alone():
    """Both measured at 200. A guard that also rewrote these would turn a
    working request into a user turn nobody wrote."""
    loop_step = _tool_turn()
    assert _close_assistant_after_tool_result(list(loop_step)) == loop_step
    nudged = _tool_turn() + [{"role": "system", "content": "answer now"}]
    assert _close_assistant_after_tool_result(list(nudged)) == nudged


def test_a_history_already_closed_by_a_user_turn_is_untouched():
    """The system nudge here comes AFTER the user turn, so the request already
    ends the way the gateway wants. Converting it anyway would rewrite an
    instruction into a message the person never sent, on a request that was
    fine - which is why the case carries both a user turn and a system one."""
    closed = _tool_turn() + [
        {"role": "assistant", "content": "It is 11:45."},
        {"role": "user", "content": "and tomorrow?"},
        {"role": "system", "content": "keep it short"},
    ]
    assert _close_assistant_after_tool_result(list(closed)) == closed


def test_a_history_without_a_tool_result_is_untouched():
    plain = [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"},
             {"role": "system", "content": "be brief"}]
    assert _close_assistant_after_tool_result(list(plain)) == plain


def test_the_named_boundary_invents_no_user_turn():
    """Nothing behind the assistant turn means nothing to convert. Inventing a
    user message would put words in the person's mouth; the gateway's own 400
    is the signal if a lane ever produces this."""
    bare = _tool_turn() + [{"role": "assistant", "content": "It is 11:45."}]
    assert _close_assistant_after_tool_result(list(bare)) == bare


def test_prepare_messages_closes_the_correction_for_veyllo_only():
    """The wiring, and its gate: the conversion is a veyllo wire requirement,
    so another API provider's history must come back byte-identical."""
    from vaf.core.agent import Agent

    class _Stub:
        _thinking_reply_context = None
        filename = "api"
        model_display_name = ""
        config = {}
        provider = "veyllo"

        def _consolidate_system_messages(self, messages):
            return messages

    history = _tool_turn() + [
        {"role": "assistant", "content": "<think>t</think>It is 11:45."},
        {"role": "system", "content": "CORRECTION NEEDED: restate it."},
    ]
    out = Agent._prepare_messages(_Stub(), [dict(m) for m in history])
    assert out[-1]["role"] == "user", "the correction still travels as a system turn"

    other = _Stub()
    other.provider = "openai"
    out2 = Agent._prepare_messages(other, [dict(m) for m in history])
    assert out2[-1]["role"] == "system", "the veyllo-only guard leaked to another provider"


def test_the_deepseek_family_gets_reasoning_content_passed_back():
    """MUTATION: restore reasoning_content for provider "deepseek" only.

    Veyllo speaks DeepSeek's thinking dialect: an assistant message whose think
    block is stored inline as <think>...</think> must go back to the API with a
    separate reasoning_content field, or the backend answers 400 "The
    `reasoning_content` in the thinking mode must be passed back to the API".
    That 400 fired live on veyllo during the empty-response retry - the first
    lane that REBUILDS and resends such a history instead of only appending.
    """
    from vaf.core.agent import Agent

    class _Stub:
        _thinking_reply_context = None
        filename = "api"
        model_display_name = ""
        config = {}          # the vision-capability check reads the active model

        def _consolidate_system_messages(self, messages):
            return messages

    for provider in ("deepseek", "veyllo"):
        stub = _Stub()
        stub.provider = provider
        out = Agent._prepare_messages(stub, [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "<think>weighing it</think>done"},
        ])
        asst = [m for m in out if m["role"] == "assistant"][0]
        assert asst.get("reasoning_content") == "weighing it", provider
        assert asst["content"] == "done", provider
