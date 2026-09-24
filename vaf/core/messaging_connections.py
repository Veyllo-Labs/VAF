# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Resolve which messaging channels (the ones declared in vaf/core/channels.py) are available for
the current user and their preferred channel for proactive messages (main_messenger from
user_identity.json).

Used by the system prompt to inform the agent and by the per-channel send tools.

Also persists and resolves user -> telegram_chat_id for proactive Telegram sends
(messaging_endpoints.json under Platform.data_dir()).
"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vaf.core.config import Config, is_admin_account, is_local_admin_lane  # noqa: F401 (is_local_admin_lane re-exported)
from vaf.core.platform import Platform
from vaf.core.channel_secrets import channel_secret, has_channel_secret

# ── Channel registry ─────────────────────────────────────────────────────────
# Declared once in vaf/core/channels.py; these names stay importable from here for the
# callers that always read them here. ROUTABLE_CHANNELS are the channels
# send_to_main_messenger dispatches to: exactly the ones with a bridge.
from vaf.core.channels import CHANNEL_SEND_TOOLS, KNOWN_CHANNELS, MAIN_MESSENGERS  # noqa: F401 (re-exported)
from vaf.core.channels import CHAT_CHANNELS as ROUTABLE_CHANNELS  # noqa: F401 (re-exported)


# Reply window: a number the agent wrote to may answer for this long without being a
# contact. `whatsapp_config.reply_window_hours` overrides; 0 switches the window off.
WA_REPLY_WINDOW_HOURS_DEFAULT = 72.0


def reply_window_hours() -> float:
    """Configured reply window in hours (never negative; 0 = off)."""
    wc = Config.get("whatsapp_config") or {}
    raw = wc.get("reply_window_hours", WA_REPLY_WINDOW_HOURS_DEFAULT) if isinstance(wc, dict) else WA_REPLY_WINDOW_HOURS_DEFAULT
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        hours = WA_REPLY_WINDOW_HOURS_DEFAULT
    return max(0.0, hours)


def append_channel_activity(channel: str, entry: Dict[str, Any], keep: int) -> None:
    """Append one entry to a channel's dashboard timeline (`<channel>_config.chat_activity`,
    newest `keep` kept). Never raises: a timeline entry must not cost a message.

    The three bridges each carried this read-modify-write by hand, with two faults between
    them. It ran without the config lock, so an entry written around a disconnect could
    load the block before the disconnect removed it and save it back after, and it turned a
    missing block into an empty dict, so an entry arriving after the disconnect created a
    fresh `{"chat_activity": [...]}` block for a channel that is gone. Held under the lock
    from load to save, and a channel without a block gets no entry.
    """
    key = f"{channel}_config"
    try:
        with Config._locked():
            config = Config.load()
            block = config.get(key)
            if not isinstance(block, dict):
                return
            activity = list(block.get("chat_activity") or [])
            activity.append(entry)
            config[key] = {**block, "chat_activity": activity[-keep:]}
            Config.save(config)
    except Exception:
        pass


def whatsapp_inbound_to_agent() -> bool:
    """Does an accepted WhatsApp message reach the agent at all? `whatsapp_config.inbound_to_agent`,
    on unless it says False: the bridge stops EVERY sender before the policy when it is off, the
    owner included, and the account stays a place the agent can send to. Measured before this
    existed: five readers spelled the same isinstance-and-default line by hand (the bridge gate,
    two dashboard payloads, the inbox compose rule, the learning counter), and the Front Office
    switch read none of them."""
    wc = Config.get("whatsapp_config") or {}
    return not (isinstance(wc, dict) and wc.get("inbound_to_agent", True) is False)


def single_telegram_owner() -> Optional[Tuple[Optional[str], str]]:
    """(scope, username) when exactly ONE account is paired on Telegram, else None.

    The Telegram bot is shared by every account on the install, so a sender who is nobody's
    paired endpoint cannot be attributed to an owner when there are several: whose contact
    would they be, whose memory would the turn read? A WhatsApp number belongs to one account
    and the Discord lane is the local admin's, so neither needs this.
    """
    tc = Config.get("telegram_config") or {}
    # Both lists: a relay entry belongs to an account too (it says whose relay the person
    # is), so a whitelist owner plus another account's relay is two accounts on one bot, and
    # a stranger cannot be attributed. The same account on both lists is still one owner.
    entries = ((tc.get("whitelist") or []) + (tc.get("relay_whitelist") or [])) if isinstance(tc, dict) else []
    # An entry without a Telegram id is a half-filled row, not a pairing (the security check
    # counts paired users the same way): it must neither stand in as the one owner nor make a
    # single-owner bot look like two.
    owners = {(str(e.get("user_scope_id") or ""), str(e.get("vaf_username") or "admin").strip())
              for e in entries
              if isinstance(e, dict) and str(e.get("telegram_user_id") or "").strip()}
    if len(owners) != 1:
        return None
    scope, uname = next(iter(owners))
    return (scope or None, uname)


def front_office_open(channel: str, raw_policy: Any = None) -> bool:
    """Does this channel's Inbound really answer somebody nobody has decided about?

    The policy flag is the switch; this is the switch AND whatever else that channel needs to
    act on it. Today that is Telegram's single owner (above) and WhatsApp's forwarding switch
    (`whatsapp_inbound_to_agent`): with it off the bridge enqueues nothing for anybody, so an
    open policy answers nobody there. The distinction matters because two different answers to
    one question is how a row ends up claiming the agent answers in a chat the bridge refuses:
    the bridges, the inbox rows and the channel windows all ask here.
    """
    from vaf.core.channel_ingress_policy import resolve_channel_policy
    name = str(channel or "").strip().lower()
    try:
        if not resolve_channel_policy(name, raw_policy if raw_policy is not None
                                      else Config.get("channel_ingress_policy"))["open_to_new_senders"]:
            return False
    except Exception:
        return False
    if name == "telegram":
        return single_telegram_owner() is not None
    if name == "whatsapp":
        return whatsapp_inbound_to_agent()
    return True


def front_office_doors(raw_policy: Any = None) -> Dict[str, bool]:
    """Every Front Office channel's real answer to "does Inbound answer a stranger here", in
    one dict. A surface that asks per contact would otherwise ask per contact AND per channel,
    and each ask rebuilds the Telegram owner set; the answer is the same for the whole listing.
    """
    from vaf.core.channel_ingress_policy import FRONT_OFFICE_CHANNELS
    return {ch: front_office_open(ch, raw_policy) for ch in FRONT_OFFICE_CHANNELS}


def _entry_is_mine(entry: Dict[str, Any], username: Optional[str], user_scope_id: Optional[str]) -> bool:
    """Whether a whitelist entry belongs to this identity: by scope when both carry one, by
    VAF username otherwise; the local admin sees every entry."""
    from vaf.core.contacts_store import is_local_admin_caller
    if is_local_admin_caller(username, user_scope_id):
        return True
    scope = str(user_scope_id or "").strip()
    if scope and str(entry.get("user_scope_id") or "").strip() == scope:
        return True
    uname = (username or "").strip()
    return bool(uname) and (entry.get("vaf_username") or "").strip() == uname


def owner_endpoints(channel: str, username: Optional[str], user_scope_id: Optional[str], *,
                    relay: bool = False) -> set:
    """The store keys of the endpoints that are THIS person's own on a channel: their
    registered WhatsApp numbers (E.164 with one leading plus), their Telegram user ids (the
    relay whitelist with `relay=True`), the Discord admin id. What the inbox calls the
    `owner` lane, read from the same whitelists the bridges pair against."""
    out: set = set()
    channel = (channel or "").strip().lower()
    if channel == "whatsapp":
        wc = Config.get("whatsapp_config") or {}
        for e in (wc.get("whitelist") or []) if isinstance(wc, dict) else []:
            if not isinstance(e, dict) or not _entry_is_mine(e, username, user_scope_id):
                continue
            phone = str(e.get("phone_number") or "").strip()
            if phone:
                # The key the store files the chat under (canonical digits behind one plus),
                # so a formatted, 00-prefixed or trunk-zero whitelist number still matches.
                from vaf.core.contacts_store import whatsapp_store_key
                key = whatsapp_store_key(phone)
                out.add(key or (phone if phone.startswith("+") else f"+{phone}"))
    elif channel == "telegram":
        tc = Config.get("telegram_config") or {}
        key = "relay_whitelist" if relay else "whitelist"
        for e in (tc.get(key) or []) if isinstance(tc, dict) else []:
            if not isinstance(e, dict) or not _entry_is_mine(e, username, user_scope_id):
                continue
            tid = str(e.get("telegram_user_id") or "").strip()
            if tid:
                out.add(tid)
    elif channel == "discord":
        dc = Config.get("discord_config") or {}
        admin_id = str((dc.get("admin_user_id") if isinstance(dc, dict) else "") or "").strip()
        if admin_id:
            out.add(admin_id)
    return out


def whatsapp_session_id(username: Optional[str], endpoint: str, *, fallback: str = "self") -> str:
    """The session, and therefore the memory namespace, of one WhatsApp chat:
    `whatsapp_<user>_<digits>`. The recipe was hand-rolled at eight sites, and a drifted
    copy means the Composer or the thinking lane looks for a chat under a name the bridge
    never wrote, with no error. `endpoint` is anything that names the other side: an E.164
    display (+49 170...), a JID (49170...:7@s.whatsapp.net), an unresolved @lid, or bare
    digits; the digits before "@" and ":" are the key. `fallback` stands in when there are
    none ("self" for the owner's own chat, "unknown" for an unresolved sender); with an
    empty fallback the result is "" instead, for callers that must not name a session."""
    local = (endpoint or "").split("@", 1)[0].split(":", 1)[0]
    digits = "".join(c for c in local if c.isdigit())
    key = digits or (fallback or "")
    if not key:
        return ""
    uname = (username or "admin").strip() or "admin"
    return f"whatsapp_{uname}_{key}"

_ENDPOINTS_LOCK = threading.Lock()
_ENDPOINTS_FILE = None


def _endpoints_path() -> Path:
    global _ENDPOINTS_FILE
    if _ENDPOINTS_FILE is None:
        _ENDPOINTS_FILE = Platform.data_dir() / "messaging_endpoints.json"
    return _ENDPOINTS_FILE


def _load_endpoints() -> Dict[str, Any]:
    path = _endpoints_path()
    if not path.exists():
        return {"by_scope": {}, "by_username": {}, "whatsapp_by_scope": {}, "whatsapp_by_username": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "by_scope": data.get("by_scope") or {},
            "by_username": data.get("by_username") or {},
            "whatsapp_by_scope": data.get("whatsapp_by_scope") or {},
            "whatsapp_by_username": data.get("whatsapp_by_username") or {},
        }
    except Exception:
        return {"by_scope": {}, "by_username": {}, "whatsapp_by_scope": {}, "whatsapp_by_username": {}}


def _save_endpoints(data: Dict[str, Any]) -> None:
    path = _endpoints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_telegram_chat_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
    chat_id: str,
) -> None:
    """Persist telegram_chat_id for this user (by scope and username). Called from Telegram bridge when a message is received."""
    if not chat_id:
        return
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            data["by_scope"][str(user_scope_id)] = chat_id
        uname = (username or "").strip() or "admin"
        data["by_username"][uname] = chat_id
        _save_endpoints(data)


def _telegram_whitelist_entry_matches(
    entry: Dict[str, Any],
    scope_str: Optional[str],
    vaf_username: str,
) -> bool:
    """True if this whitelist entry matches the request (loose: case-insensitive username, normalized scope)."""
    if not isinstance(entry, dict):
        return False
    entry_scope = entry.get("user_scope_id")
    entry_scope_str = str(entry_scope).strip() if entry_scope is not None else None
    entry_name = (entry.get("vaf_username") or "").strip() or "admin"
    scope_ok = (not scope_str and not entry_scope_str) or (
        scope_str and entry_scope_str and scope_str.strip() == entry_scope_str
    )
    name_ok = (vaf_username or "admin").lower() == (entry_name or "admin").lower()
    if scope_str and entry_scope_str:
        return scope_ok and name_ok
    return name_ok


def get_telegram_chat_id_from_whitelist(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Resolve Telegram chat_id from the Telegram whitelist (connected bot config).
    For private chats (DM), chat_id equals telegram_user_id.
    Matching is loose: case-insensitive username, normalized scope, so the verified
    account owner (who linked their Telegram) is found even if session identity differs slightly.
    Returns telegram_user_id as string, or None if no matching whitelist entry.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict) or not telegram_config.get("whitelist"):
        return None
    whitelist = telegram_config.get("whitelist") or []
    scope_str = str(user_scope_id).strip() if user_scope_id is not None else None
    vaf_username = (username or "").strip() or "admin"
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if _telegram_whitelist_entry_matches(entry, scope_str, vaf_username):
            tid = entry.get("telegram_user_id")
            return str(tid) if tid is not None else None
    return None


def get_telegram_chat_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """Where VAF may reach this user on Telegram: their chat, while their Telegram lane is on
    (`channel_enabled_for_scope`). The delivery lanes ask here - `send_telegram`,
    `send_to_user` - so an account that switched Telegram off is not written to there, where
    the bot would not answer its reply. `telegram_chat_id_of` is the same chat without the
    switch, for the readers ("which chat is theirs")."""
    if not channel_enabled_for_scope("telegram", user_scope_id):
        return None
    return telegram_chat_id_of(user_scope_id, username)


def telegram_chat_id_of(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Return telegram_chat_id for this user, switched on or not.
    Lookup: 1) persisted endpoints (from past Telegram message), 2) Telegram whitelist
    (loose match: case-insensitive username, normalized scope). The verified account owner
    (who linked their Telegram with the bot) does not need to be manually re-added to the
    whitelist – the bot recognizes them by the existing link.
    """
    uname = (username or "").strip() or "admin"
    scope_str = str(user_scope_id).strip() if user_scope_id is not None else None

    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            cid = data["by_scope"].get(str(user_scope_id))
            if cid:
                return cid
        cid = data["by_username"].get(uname)
        if cid:
            return cid
        # Case-insensitive username lookup (session may send different casing)
        for key, val in (data.get("by_username") or {}).items():
            if (key or "").strip().lower() == uname.lower():
                return val
    # Fallback: Telegram whitelist (verified/linked user; loose match)
    chat_id = get_telegram_chat_id_from_whitelist(user_scope_id, username)
    if chat_id:
        save_telegram_chat_id(user_scope_id, username, chat_id)
        return chat_id
    # Single verified user: exactly one whitelist entry = the account that linked the bot;
    # they don't need to be manually on the whitelist – match them loosely.
    telegram_config = Config.get("telegram_config") or {}
    if isinstance(telegram_config, dict):
        whitelist = telegram_config.get("whitelist") or []
        if len(whitelist) == 1 and isinstance(whitelist[0], dict):
            entry = whitelist[0]
            if _telegram_whitelist_entry_matches(entry, scope_str, uname):
                tid = entry.get("telegram_user_id")
                chat_id = str(tid) if tid is not None else None
                if chat_id:
                    save_telegram_chat_id(user_scope_id, username, chat_id)
                    return chat_id
    return None


def save_whatsapp_chat_jid(
    user_scope_id: Optional[Any],
    username: Optional[str],
    chat_jid: str,
) -> None:
    """Persist WhatsApp chat JID for this user. Called from WhatsApp bridge when a message is received."""
    if not chat_jid:
        return
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            data["whatsapp_by_scope"][str(user_scope_id)] = chat_jid
        uname = (username or "").strip() or "admin"
        data["whatsapp_by_username"][uname] = chat_jid
        _save_endpoints(data)


def get_whatsapp_chat_jid_from_whitelist(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Resolve WhatsApp JID from the whitelist (phone_number in E.164 maps to user).
    For proactive sends we need the user's WhatsApp JID - typically obtained when they first message us.
    Returns JID string if whitelist has phone_number for this user (we construct JID from it), or None.
    Note: We prefer persisted endpoints from actual message; this is fallback when user hasn't messaged yet.
    """
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("whitelist"):
        return None
    whitelist = whatsapp_config.get("whitelist") or []
    scope_str = str(user_scope_id) if user_scope_id is not None else None
    vaf_username = (username or "").strip() or "admin"
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if scope_str and str(entry.get("user_scope_id")) == scope_str:
            phone = entry.get("phone_number")
            if phone:
                return _e164_to_jid(str(phone))
        if entry.get("vaf_username") == vaf_username:
            phone = entry.get("phone_number")
            if phone:
                return _e164_to_jid(str(phone))
    return None


def _e164_to_jid(phone: str) -> str:
    """Convert E.164 phone number to WhatsApp JID (e.g. 49123456789@s.whatsapp.net)."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = digits[1:]
    return f"{digits}@s.whatsapp.net"


def get_whatsapp_chat_jid(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    Return WhatsApp chat JID for this user. Used by send_whatsapp tool.
    Lookup order: 1) persisted endpoints, 2) whitelist (phone_number -> JID).
    """
    with _ENDPOINTS_LOCK:
        data = _load_endpoints()
        if user_scope_id is not None:
            jid = data["whatsapp_by_scope"].get(str(user_scope_id))
            if jid:
                return jid
        uname = (username or "").strip() or "admin"
        jid = data["whatsapp_by_username"].get(uname)
        if jid:
            return jid
    jid = get_whatsapp_chat_jid_from_whitelist(user_scope_id, username)
    if jid:
        save_whatsapp_chat_jid(user_scope_id, username, jid)
    return jid


def get_discord_user_id(
    user_scope_id: Optional[Any],
    username: Optional[str],
) -> Optional[str]:
    """
    The Discord user id this identity is reached on, or None. The Discord lane is the local
    admin's: the bot answers the one Discord account that verified it, and that account is
    the machine owner's. Every other identity gets None, and every Discord lane asks here:
    `send_to_user` with Discord as main messenger, `send_discord`, `read_discord_chat`'s
    default chat and the availability list. It used to return the admin's id for anybody,
    so another account's agent sent its messages to the admin's Discord and read the
    admin's Discord conversation.
    """
    if not channel_enabled_for_scope("discord", user_scope_id):
        return None
    discord_config = Config.get("discord_config") or {}
    if not isinstance(discord_config, dict) or not discord_config.get("verified"):
        return None
    return (discord_config.get("admin_user_id") or "").strip() or None


def channel_enabled_for_scope(channel: str, user_scope_id: Optional[Any], *,
                              admin: Optional[bool] = None) -> bool:
    """Whether one account's lane on a messenger is switched on: the one rule every place
    that acts on the switch asks (the bridges, the delivery lanes, the availability check
    below, the API routes and the Front Office). What the switch is depends on whose lane
    the channel is (`channels.Channel.accounts`):

    - the local admin (`config.is_local_admin_lane`) owns the global `<channel>_config.enabled`;
    - "each" (WhatsApp): everybody else has a switch of their own under
      `connection_enabled_by_scope[<scope>][<channel>]`, off until they turn it on;
    - "shared" (Telegram): nobody's lane is on while the BOT is off; an admin account rides
      the bot, and every other account needs its own switch on as well. It used to be the
      switch shown and stored while the bot answered every paired account regardless, and
      a check by scope alone would have cut a second admin off, whose saves write the bot's
      switch and never one of their own;
    - "owner" (Discord): nobody but the local admin has a lane.

    `admin` is the caller's admin answer when the caller already has it (a request, whose
    role was authenticated); without it the account directory is asked
    (`config.is_admin_account`), and only when nothing cheaper decided.
    """
    from vaf.core.channels import CHANNEL_ACCOUNTS
    name = str(channel or "").strip().lower()
    block = Config.get(f"{name}_config") or {}
    bot_on = bool(isinstance(block, dict) and block.get("enabled", False))
    if is_local_admin_lane(user_scope_id):
        return bot_on
    kind = CHANNEL_ACCOUNTS.get(name, "each")
    if kind == "owner":
        return False
    by_scope = Config.get("connection_enabled_by_scope") or {}
    toggles = by_scope.get(str(user_scope_id).strip(), {}) if isinstance(by_scope, dict) else {}
    own = bool(isinstance(toggles, dict) and toggles.get(name, False))
    if kind == "each":
        return own
    return bot_on and (own or (admin if admin is not None else is_admin_account(user_scope_id)))


def get_messaging_connections(
    username: Optional[str] = None,
    user_scope_id: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Return available messaging channels and the user's preferred channel for proactive messages.

    Args:
        username: Current user's username (for user_identity.main_messenger and Telegram whitelist match).
        user_scope_id: Current user's scope ID (for Telegram whitelist match).

    Returns:
        {
            "available": ["telegram", "discord"],  # channels on which the OWNER is reachable
            "outbound": ["whatsapp"],              # channels on which the agent reaches THIRD PARTIES
            "main_messenger": "telegram" | None     # from user_identity.main_messenger if valid
        }

    "available" feeds main_messenger / send_to_user: the user has an endpoint of their own
    there. "outbound" is wider: WhatsApp counts as soon as the user linked an account (that
    account is the agent's own number), whether or not they registered a number to be
    reached on; the per-channel send tool is offered for both, the owner lane only for
    "available".
    """
    available: List[str] = []
    outbound: List[str] = []
    main_messenger: Optional[str] = None

    # Telegram: this account's lane is on (the bot too) + verified + a whitelist entry
    telegram_config = Config.get("telegram_config") or {}
    if isinstance(telegram_config, dict):
        if (telegram_config.get("verified") and has_channel_secret("telegram")
                and channel_enabled_for_scope("telegram", user_scope_id)):
            whitelist = telegram_config.get("whitelist") or []
            scope_str = str(user_scope_id) if user_scope_id is not None else None
            vaf_username = (username or "").strip() or "admin"
            for entry in whitelist:
                if not isinstance(entry, dict):
                    continue
                if scope_str and str(entry.get("user_scope_id")) == scope_str:
                    available.append("telegram")
                    break
                if entry.get("vaf_username") == vaf_username:
                    available.append("telegram")
                    break

    # Discord: only where it can deliver, which is the local admin's lane
    if get_discord_user_id(user_scope_id, username):
        available.append("discord")

    # WhatsApp: switched on + the user linked an account (the agent's number) -> outbound;
    # additionally a registered main-user number (whitelist entry) -> the owner is reachable.
    whatsapp_config = Config.get("whatsapp_config") or {}
    if isinstance(whatsapp_config, dict) and channel_enabled_for_scope("whatsapp", user_scope_id):
        try:
            from vaf.core.whatsapp_auth import whatsapp_auth_exists
            vaf_username = (username or "").strip() or "admin"
            if whatsapp_auth_exists(vaf_username):
                outbound.append("whatsapp")
                whitelist = whatsapp_config.get("whitelist") or []
                scope_str = str(user_scope_id) if user_scope_id is not None else None
                for entry in whitelist:
                    if not isinstance(entry, dict):
                        continue
                    if scope_str and str(entry.get("user_scope_id")) == scope_str:
                        available.append("whatsapp")
                        break
                    if entry.get("vaf_username") == vaf_username:
                        available.append("whatsapp")
                        break
        except Exception:
            pass

    # Slack: not yet configured in config; placeholder for future
    # if Config.get("slack_config", {}).get("enabled"): available.append("slack")

    # Deduplicate and keep order
    seen = set()
    ordered: List[str] = []
    for ch in available:
        if ch not in seen:
            seen.add(ch)
            ordered.append(ch)
    available = ordered

    # main_messenger from user_identity
    if username:
        try:
            from vaf.auth.user_workspace import get_user_workspace
            ws = get_user_workspace(username)
            ui = ws.get_user_identity()
            val = (ui.get("main_messenger") or "").strip().lower()
            if val in MAIN_MESSENGERS:
                main_messenger = val
        except Exception:
            pass

    return {"available": available, "outbound": outbound, "main_messenger": main_messenger}


def _record_outbound(
    channel: str,
    endpoint: str,
    text: str,
    username: Optional[str],
    user_scope_id: Optional[Any],
    file_path: Optional[str] = None,
    kind: Optional[str] = None,
) -> None:
    """Mirror a router-delivered outbound message into the channel session history
    (and, where the bridge does not do it itself, the channel message store).

    The per-platform send TOOLS record their own sends; the router path
    (automation result push, send_to_user, ...) previously delivered without any
    trace, so the channel main agent lacked its own last message when the user
    replied to it (live 2026-07-14: the agent could not know which "Timer" the
    user meant and confabulated). Best-effort: never raises, never blocks a send.

    `kind` is the proactive-bubble tag persisted on the message ("thinking",
    "nudge", ...), see `Session.append_background_message`. The append goes
    through that primitive so the agent holding this session in memory learns
    that its file grew - a message it cannot see in its own context is the
    incident above in a second form.
    """
    uname = (username or "admin").strip() or "admin"
    if channel == "whatsapp":
        session_id = whatsapp_session_id(uname, endpoint)
    else:
        session_id = f"{channel}_{endpoint}"
    try:
        from vaf.core.session import SessionManager
        # create=True: an outbound-FIRST session (automation message before the
        # user ever wrote inbound) is built here and stamped with its owner scope,
        # like the inbound lane (headless_runner) does. A scopeless session is
        # admin-only under the ownership gates - without the stamp its real owner
        # could see it in the sidebar but never open its workspace.
        SessionManager().append_background_message(
            session_id, text, kind=kind, create=True,
            name=f"{channel.capitalize()} {endpoint}", user_scope_id=user_scope_id,
        )
    except Exception:
        pass
    # Channel store: the WhatsApp bridge already records every outbound send
    # itself (whatsapp_bridge sender loop) - recording here again would duplicate
    # entries for read_/find_ tools.
    if channel != "whatsapp":
        try:
            from vaf.core.channel_message_store import append_message
            append_message(
                username=uname, chat_id=str(endpoint), body=text, direction="out",
                content_type=("document" if file_path else "text"),
                channel=channel, user_scope_id=user_scope_id,
            )
        except Exception:
            pass


def send_to_main_messenger(
    user_scope_id: Optional[Any],
    username: Optional[str],
    text: str,
    file_path: Optional[str] = None,
    record: bool = True,
    kind: Optional[str] = None,
) -> "tuple[bool, Optional[str]]":
    """Send ``text`` to the user's configured ``main_messenger`` (Telegram/WhatsApp/Discord).

    Single source of truth for "reach the user on their main channel", reused by the thinking-mode
    nudge, the proactive-question delivery AND proactive automation results. Returns ``(sent, channel)``:
      * ``(True, "telegram"|"whatsapp"|"discord")`` on success,
      * ``(False, None)`` when no main_messenger is configured, the channel id is missing, or the
        send fails.
    Never raises. (E-mail is intentionally NOT a valid main_messenger here.)

    ``file_path`` (optional): when given and the file exists, the text is sent as a normal message
    and the file is delivered as a *separate* attachment with a short caption. Sending the file
    separately (rather than as the text's caption) avoids the per-channel caption length limit
    (Telegram 1024 chars) so the full text AND the file always arrive. The attachment is
    best-effort: overall success is decided by the text send, so a too-large/failed attachment
    never reports the whole delivery as failed (the Web UI still carries the file link).

    ``text`` may be EMPTY when ``file_path`` names an existing file: the delivery is then
    attachment-only - the document goes out as the only message (filename caption) and its
    send decides success. Used by the automation result lane to hand over a produced file
    when an in-run send already delivered the text.

    ``record`` (default True): mirror the delivered text into the channel session history
    so the channel main agent has context when the user replies to it. ``kind`` tags that
    mirrored message (`Message.kind`: "thinking" for a background question, "nudge" for
    its reminder) so the chat renders it as the proactive bubble it was. Thinking mode
    used to pass ``record=False`` on the theory that its waiting latch would reconstruct
    the question at reply time and a session append would show it twice; the latch is
    one scope-keyed slot that any turn on the scope can consume, and when it went the
    question was nowhere (live 2026-09-02: the user answered a background question on
    Telegram and the agent had no record of ever asking). The transcript is the record;
    the latch adds the proposal and the findings on top of it.
    """
    text = (text or "").strip()
    import os as _os
    attach = file_path if (file_path and _os.path.isfile(file_path)) else None
    caption = ("\U0001F4CE " + _os.path.basename(attach)) if attach else ""
    if not text and not attach:
        return False, None

    try:
        conn = get_messaging_connections(
            username=(username or "admin").strip() or "admin", user_scope_id=user_scope_id
        )
        main = (conn.get("main_messenger") or "").strip().lower()
        if main == "telegram":
            chat_id = get_telegram_chat_id(user_scope_id, username)
            if chat_id:
                from vaf.core.telegram_reply import send_telegram_reply
                if text:
                    delivered = bool(send_telegram_reply(chat_id, text))
                    if delivered and attach:
                        try:
                            send_telegram_reply(chat_id, caption, file_path=attach)
                        except Exception:
                            pass
                else:
                    # Attachment-only: the document IS the message, so its send decides success.
                    delivered = bool(send_telegram_reply(chat_id, caption, file_path=attach))
                if delivered:
                    if record:
                        _record_outbound("telegram", str(chat_id), text or caption, username, user_scope_id, attach, kind=kind)
                    return True, "telegram"
        elif main == "whatsapp":
            jid = get_whatsapp_chat_jid(user_scope_id, username)
            if jid:
                from vaf.core.whatsapp_reply import send_whatsapp_reply
                # send_whatsapp_reply returns False when the bridge is down (callback unset), so a dead
                # bridge correctly degrades to (False, None) -> the caller falls back to the Web UI,
                # instead of falsely reporting success and silently swallowing the message.
                if text:
                    delivered = bool(send_whatsapp_reply((username or "admin"), jid, text, user_scope_id=user_scope_id))
                    if delivered and attach:
                        try:
                            from vaf.api.whatsapp_bridge import send_whatsapp_with_confirmation
                            send_whatsapp_with_confirmation(
                                (username or "admin"), jid, caption, document_path=attach
                            )
                        except Exception:
                            pass
                else:
                    # Attachment-only: the bridge returns a prose result string, never raises the
                    # outcome - only its three "... sent via WhatsApp." forms count as success.
                    from vaf.api.whatsapp_bridge import send_whatsapp_with_confirmation
                    _wa_result = send_whatsapp_with_confirmation(
                        (username or "admin"), jid, caption, document_path=attach
                    )
                    delivered = "sent via whatsapp" in str(_wa_result or "").lower()
                if delivered:
                    if record:
                        _record_outbound("whatsapp", str(jid), text or caption, username, user_scope_id, attach, kind=kind)
                    return True, "whatsapp"
        elif main == "discord":
            user_id = get_discord_user_id(user_scope_id, username)
            if user_id:
                bot_token = channel_secret("discord")
                if bot_token:
                    from vaf.core.discord_send import send_discord_dm
                    if text:
                        delivered = bool(send_discord_dm(bot_token, user_id, text, chunk=True))
                        if delivered and attach:
                            try:
                                send_discord_dm(bot_token, user_id, caption, file_path=attach)
                            except Exception:
                                pass
                    else:
                        # Attachment-only: the document IS the message, so its send decides success.
                        delivered = bool(send_discord_dm(bot_token, user_id, caption, file_path=attach))
                    if delivered:
                        if record:
                            _record_outbound("discord", str(user_id), text or caption, username, user_scope_id, attach, kind=kind)
                        return True, "discord"
    except Exception:
        pass
    return False, None


def get_contact_whitelist_telegram_entry(telegram_user_id: str) -> Optional[Dict[str, Any]]:
    """
    If telegram_user_id is in any user's contact list with allow_as_assistant_user=True,
    return an entry dict like Telegram whitelist: user_scope_id, vaf_username, telegram_user_id.
    Used by Telegram bridge to allow contacts as assistant users.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    whitelist = telegram_config.get("whitelist") or []
    seen: set = set()
    try:
        from vaf.core.contacts_store import front_office_endpoints
    except Exception:
        return None
    wanted = str(telegram_user_id or "").strip()
    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("user_scope_id")
        uname = (entry.get("vaf_username") or "admin").strip()
        key = (str(scope), uname)
        if key in seen:
            continue
        seen.add(key)
        # The owner's book by username AND scope, the keys the WhatsApp bridge and the
        # dashboards read: a book saved under scopes/<uuid> was invisible to the
        # username-only lookup this replaced, so a tenant's contacts were counted as
        # reachable everywhere and never admitted here.
        if wanted and wanted in front_office_endpoints(uname, scope or None, "telegram"):
            return {
                "user_scope_id": scope,
                "vaf_username": uname,
                "telegram_user_id": wanted,
                "from_contact": True,  # So bridge/headless can treat as front-office (not the account owner)
            }
    return None
