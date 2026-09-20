# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one ingress decision every messaging bridge asks.

Two inputs, and they answer different questions: the CHANNEL switch says whether people
nobody has decided about are answered here at all, and the PERSON's own decision in the
contact book (allowed, denied, or nobody decided) holds on every channel they have. The
owner's own paired endpoint is a third thing and outranks both, because that is the owner
talking to their own agent.

What is gone and must not come back: the 72 hour reply window that admitted anyone the agent
had ever written to, and the two expert doors in config.json that said the same thing as the
contact's own decision in a place nobody looked.
"""
from vaf.core.channel_ingress_policy import (
    FRONT_OFFICE_CHANNELS,
    _SUPPORTED_CHANNELS,
    evaluate_ingress,
    normalize_policy,
    set_front_office,
)
from vaf.core.messaging_connections import ROUTABLE_CHANNELS


def test_the_owners_own_paired_endpoint_wins_over_every_decision():
    """The explicit pair is the owner's own number, not a third party, so it is answered
    whatever the book and the switch say. MUTATION: move the `denied` test above the
    explicit one and the second assertion goes red."""
    assert evaluate_ingress("whatsapp", None, explicit_match=True) == (True, "explicit_pair")
    assert evaluate_ingress("whatsapp", None, explicit_match=True, access="denied") == (True, "explicit_pair")


def test_a_denied_contact_is_refused_on_an_open_channel():
    """The veto the owner took by hand outranks the switch. MUTATION: check
    `open_to_new_senders` before the denial and this goes red with front_office_open."""
    opened = set_front_office(None, True, "whatsapp")
    assert evaluate_ingress("whatsapp", opened, explicit_match=False, access="denied") == (False, "contact_denied")
    assert evaluate_ingress("whatsapp", None, explicit_match=False, access="denied") == (False, "contact_denied")


def test_an_allowed_contact_is_answered_on_a_closed_channel():
    """The permission the owner gave about the PERSON holds on every channel they have, or
    allowing somebody by hand would mean nothing until a switch is thrown. MUTATION: require
    `open_to_new_senders` for the allowed branch and this goes red with not_paired."""
    assert evaluate_ingress("whatsapp", None, explicit_match=False, access="allowed") == (True, "contact_allowed")
    opened = set_front_office(None, True, "telegram")
    assert evaluate_ingress("telegram", opened, explicit_match=False, access="allowed") == (True, "contact_allowed")


def test_a_sender_nobody_decided_about_is_answered_only_while_the_channel_is_open():
    """The third state is the one the switch decides for. MUTATION: default `access` to
    "allowed" in the signature and the closed-channel assertions go red."""
    assert evaluate_ingress("whatsapp", None, explicit_match=False) == (False, "not_paired")
    assert evaluate_ingress("whatsapp", None, explicit_match=False, access=None) == (False, "not_paired")
    opened = set_front_office(None, True, "whatsapp")
    assert evaluate_ingress("whatsapp", opened, explicit_match=False) == (True, "front_office_open")
    assert evaluate_ingress("whatsapp", opened, explicit_match=False, access="") == (True, "front_office_open")


def test_the_seventy_two_hour_reply_window_admits_nobody_any_more():
    """Live incident: with Inbound closed the owner had the agent send one message, and from
    then on that person could write back for three days and be answered on a channel the
    owner had shut. No argument of this function admits them now; the message is still stored
    for the inbox, it is just not answered. MUTATION: add any second door for a messenger
    channel (a conversation match, a window) and one of these goes red."""
    for policy in (None, {"mode": "paired_only"}, {"mode": "permissive"},
                   {"mode": "paired_only", "whatsapp": {"allow_contact_fallback": True}}):
        assert evaluate_ingress("whatsapp", policy, explicit_match=False) == (False, "not_paired")
        # Even the mail-only anchor is refused on a messenger channel.
        assert evaluate_ingress("whatsapp", policy, explicit_match=False, case_reply=True) == (False, "not_paired")


def test_the_mail_case_anchor_is_mail_only_and_survives_a_closed_channel():
    """A reply carrying the anchor this agent minted into its own outgoing Message-ID is
    proof it answers a mail the agent sent, so a correspondence the owner started is not
    stranded by a switch. MUTATION: drop the `is_mail` condition and the messenger assertion
    in the test above goes red; drop the branch and this one does."""
    assert evaluate_ingress("email", None, explicit_match=False, case_reply=True) == (True, "open_conversation")
    # A denial still wins: the anchor says which correspondence this is, not who may write.
    assert evaluate_ingress("email", None, explicit_match=False, access="denied",
                            case_reply=True) == (False, "contact_denied")


def test_an_unknown_decision_word_is_read_as_nobody_decided():
    """A record with a word this version does not know must not become a grant. MUTATION:
    compare with `in` instead of equality and "allowed_sometimes" turns into a pass."""
    for word in ("allowed_sometimes", "yes", "true", "denied?", "block"):
        assert evaluate_ingress("whatsapp", None, explicit_match=False, access=word) == (False, "not_paired")
    # The two real words are read case- and space-insensitively.
    assert evaluate_ingress("whatsapp", None, explicit_match=False, access=" Allowed ") == (True, "contact_allowed")
    assert evaluate_ingress("whatsapp", None, explicit_match=False, access="DENIED") == (False, "contact_denied")


def test_the_legacy_expert_doors_are_read_once_and_never_honoured():
    """A config written before the doors were merged keeps working, and keeps nothing: the
    global `permissive` is coerced to the floor and the per-channel contact fallback is not
    a key of the normalized policy any more. MUTATION: keep `permissive` in _SUPPORTED_MODES
    and the first assertion goes red."""
    policy = normalize_policy({"mode": "permissive", "whatsapp": {"mode": "permissive",
                                                                 "allow_contact_fallback": True},
                               "email": {"opened_at": 1234}})
    assert policy["mode"] == "paired_only"
    assert policy["whatsapp"]["mode"] == "paired_only"
    assert "allow_contact_fallback" not in policy["whatsapp"]
    # Coerced rather than rejected, so mail's own stamp survives the read.
    assert policy["email"]["opened_at"] == 1234


def test_old_positional_callers_keep_meaning_what_they_meant():
    """Telegram and Discord call with the flag positionally."""
    assert evaluate_ingress("telegram", None, False) == (False, "not_paired")
    assert evaluate_ingress("telegram", None, True) == (True, "explicit_pair")


def test_supported_channels_untouched_by_the_third_state():
    assert set(_SUPPORTED_CHANNELS) == set(ROUTABLE_CHANNELS)
    assert set(normalize_policy(None)) >= set(ROUTABLE_CHANNELS)


def test_the_caller_may_hand_in_the_door_it_has_already_asked_for():
    """The flag in the policy is not the whole door. Telegram answers a stranger only while
    exactly one account is paired on the shared bot, and WhatsApp forwards nothing at all with
    `inbound_to_agent` off, so a surface that has asked `front_office_open` hands the answer in
    and the rule stays in one place instead of being written a second time beside it.

    MUTATION: ignore `door_open` and read the flag anyway, and the first two assertions go red.
    """
    opened = set_front_office(None, True, "telegram")
    assert evaluate_ingress("telegram", opened, explicit_match=False) == (True, "front_office_open")
    assert evaluate_ingress("telegram", opened, explicit_match=False, door_open=False) == (False, "not_paired")
    shut = set_front_office(None, False, "telegram")
    assert evaluate_ingress("telegram", shut, explicit_match=False, door_open=True) == (True, "front_office_open")
    # It is the DOOR, never the decision: a person's own word still outranks it both ways.
    assert evaluate_ingress("telegram", shut, explicit_match=False, access="allowed", door_open=False) == (True, "contact_allowed")
    assert evaluate_ingress("telegram", opened, explicit_match=False, access="denied", door_open=True) == (False, "contact_denied")
    # Left out, nothing changes for the callers that never pass it.
    assert evaluate_ingress("telegram", opened, explicit_match=False, door_open=None) == (True, "front_office_open")


def test_the_doors_are_read_once_for_a_whole_listing(monkeypatch):
    """`front_office_doors` answers for every Front Office channel in one call, because a
    surface listing people would otherwise ask per person AND per channel, rebuilding the
    Telegram owner set each time.

    MUTATION: drop a channel from the map and this goes red.
    """
    import vaf.core.config as cfg_mod
    from vaf.core.messaging_connections import front_office_doors
    state = {"channel_ingress_policy": set_front_office(None, True, "whatsapp"),
             "whatsapp_config": {"enabled": True},
             "telegram_config": {"whitelist": [{"telegram_user_id": "7", "vaf_username": "alice"}]}}
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    doors = front_office_doors(state["channel_ingress_policy"])
    assert set(doors) == set(FRONT_OFFICE_CHANNELS)
    assert doors["whatsapp"] is True and doors["telegram"] is False and doors["email"] is False
