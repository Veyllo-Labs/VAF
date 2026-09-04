# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one ingress decision every messaging bridge asks.

Three match kinds: the owner's paired endpoint (explicit), a contact with "Can reach
your assistant" (contact) and, new, an open conversation the agent itself started
(conversation). The third is accepted in EVERY mode: the door was opened by the agent's
own outbound message, not by a stranger, and the reply lands in Front Office.
"""
from vaf.core.channel_ingress_policy import (
    _SUPPORTED_CHANNELS,
    evaluate_ingress,
    normalize_policy,
)
from vaf.core.messaging_connections import ROUTABLE_CHANNELS


def test_explicit_pair_wins_over_everything():
    ok, reason = evaluate_ingress("whatsapp", None, explicit_match=True, contact_match=True, conversation_match=True)
    assert (ok, reason) == (True, "explicit_pair")


def test_open_conversation_is_accepted_in_paired_only_mode():
    # Default policy: paired_only, no contact fallback. A contact alone is refused ...
    assert evaluate_ingress("whatsapp", None, explicit_match=False, contact_match=True) == (False, "not_paired")
    # ... an open conversation is not: the agent wrote first.
    ok, reason = evaluate_ingress("whatsapp", None, explicit_match=False, contact_match=False, conversation_match=True)
    assert (ok, reason) == (True, "open_conversation")


def test_open_conversation_is_accepted_in_permissive_mode_and_named_as_such():
    policy = {"mode": "permissive"}
    ok, reason = evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=True, conversation_match=True)
    # The conversation is the stronger, more specific reason; the contact rule follows it.
    assert (ok, reason) == (True, "open_conversation")
    ok, reason = evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=True, conversation_match=False)
    assert (ok, reason) == (True, "contact_fallback")


def test_contact_fallback_override_still_works_without_conversation():
    policy = {"mode": "paired_only", "whatsapp": {"allow_contact_fallback": True}}
    ok, reason = evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=True)
    assert (ok, reason) == (True, "contact_fallback_override")


def test_nothing_matched_is_rejected_in_every_mode():
    for policy in (None, {"mode": "permissive"}, {"mode": "paired_only", "whatsapp": {"allow_contact_fallback": True}}):
        assert evaluate_ingress("whatsapp", policy, explicit_match=False, contact_match=False, conversation_match=False) == (False, "not_paired")


def test_conversation_match_defaults_to_false_for_old_callers():
    # Telegram and Discord call with two positional flags; they must keep meaning what they meant.
    assert evaluate_ingress("telegram", None, False, False) == (False, "not_paired")
    assert evaluate_ingress("discord", {"mode": "permissive"}, False, True) == (True, "contact_fallback")


def test_supported_channels_untouched_by_the_third_match_kind():
    assert set(_SUPPORTED_CHANNELS) == set(ROUTABLE_CHANNELS)
    assert set(normalize_policy(None)) >= set(ROUTABLE_CHANNELS)
