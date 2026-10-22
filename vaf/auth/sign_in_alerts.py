# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Messages about sign-ins a person should hear about while they happen.

WHY. The security log records failures and closures, and an admin sees it in the Logs window
when they look. Nobody looked at the moment it mattered: a sign-in from a device the owner
does not know, or somebody guessing at an account, was found days later if at all. Three
moments are now said at once, on the account's main messenger and in its notifications:

- `new_address`: a completed sign-in from an address that completed none for this account
  within the last 30 days (`signed_in_from`, asked before the new session row makes the
  address known). To the account only: it is the one who can tell its own device from a
  stranger's.
- `password_guessing`: the account window closed, so repeated wrong passwords came from
  addresses that never signed in to the account. To the account and to the local admin.
  The name is looked up in the background job, so the request does the same work for an
  existing name and an unknown one (an answer's timing must not say which names exist);
  for an unknown name nothing is sent.
- `second_factor_guessing`: the second-factor window closed. After a correct password (the
  sign-in, the password change) that means someone else has the password; at a door a
  signed-in session uses (a token for programs, an admin's step-up) it means a session of the
  account is guessing the code, and it says that instead. To the account and to the local
  admin.

Each fires once per closure or per new address by construction (the closure's first refusal,
the window's closing failure, the first sign-in from an address). The texts come from the
vocabulary book (`vaf/core/vocab/data/signin_*.json`), in the language of the person they go
to, in a neutral voice that fits the account and the admin alike.

Every delivery runs off the request path, in a daemon thread (`_spawn`, the test seam): a
messenger that hangs must not hold a sign-in. A failed messenger still leaves the
notification, and nothing here ever raises into the caller.

NOT SAID, named: a sign-in on the machine itself, the first-run setup, a token for programs
(it is its own security event, `program_token_created`), an unknown account name (the
security log has it), a new browser at an address already known (devices behind one router
or VPN share it).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

NEW_ADDRESS = "signin_new_address"
SESSION_CODE_GUESSING = "signin_session_code_guessing"
# The notification titles, short enough for a phone's bell: an ordered list per language,
# one per alert, in this order.
HEADLINES = "signin_headlines"
# The device in the reader's words: [0] "{browser} on {os}", [1] "an unknown device".
DEVICE = "signin_device"
PASSWORD_GUESSING = "signin_password_guessing"
SECOND_FACTOR_GUESSING = "signin_second_factor_guessing"
_HEADLINE_OF = {NEW_ADDRESS: 0, PASSWORD_GUESSING: 1, SECOND_FACTOR_GUESSING: 2,
                SESSION_CODE_GUESSING: 3}
# The doors that ask for the code right after a correct password (`check_second_factor`'s
# `where`): the sign-in passes none, the password change "change-password".
_AFTER_PASSWORD = frozenset({"", "change-password"})


def _spawn(fn: Callable[[], None]) -> None:
    """Run one delivery off the request path. The test seam."""
    threading.Thread(target=fn, name="vaf-sign-in-alert", daemon=True).start()


def _headline(key: str, lang: str, text: str, **fmt) -> str:
    """The notification's title, from the headline file; the text's start when there is none."""
    from vaf.core.vocab import phrasings
    forms = phrasings(HEADLINES, lang)
    index = _HEADLINE_OF.get(key)
    if index is not None and index < len(forms):
        try:
            return forms[index].format(**fmt)
        except (KeyError, IndexError, ValueError):
            pass
    return text[:60]


def _device_words(lang: str, browser: str, system: str) -> str:
    """"Firefox on Linux" in the reader's language; a browser alone when the system is unknown."""
    from vaf.core.vocab import phrasings
    forms = phrasings(DEVICE, lang)
    if not browser:
        return forms[1] if len(forms) > 1 else ""
    if not system or not forms:
        return browser
    return forms[0].format(browser=browser, os=system)


def _deliver(to_scope: str, to_username: str, key: str, status: str, *,
             device: tuple = None, **fmt) -> None:
    """One message to one person: the notification first, then the main messenger. `device` is
    (browser, system), worded in the reader's language. Never raises."""
    scope, username = to_scope, to_username
    try:
        from vaf.core.vocab import pick, resolve_user_language
        lang = resolve_user_language(scope, username)
        if device is not None:
            fmt["device"] = _device_words(lang, *device)
        text = pick(key, lang, scope=scope, **fmt)
    except Exception as e:
        logger.warning("Sign-in alert %s could not be worded: %s", key, e)
        return
    if not text:
        return
    try:
        from vaf.core.user_notifications import append_notification
        append_notification(user_scope_id=scope, kind="system", title=_headline(key, lang, text, **fmt),
                            status=status, summary=text)
    except Exception as e:
        logger.warning("Sign-in alert %s was not added to the notifications: %s", key, e)
    try:
        from vaf.core.messaging_connections import send_to_main_messenger
        send_to_main_messenger(scope, username, text)
    except Exception as e:
        logger.warning("Sign-in alert %s was not sent to the main messenger: %s", key, e)


def _with_admin(scope: str, username: str) -> Iterable[Tuple[str, str]]:
    """The account and the local admin, once when they are the same."""
    recipients = [(scope, username)]
    try:
        from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
        admin = (str(get_local_admin_scope_id()), get_local_admin_username())
        if admin[0] and admin[0] != scope:
            recipients.append(admin)
    except Exception as e:
        logger.debug("The local admin is not known for a sign-in alert: %s", e)
    return recipients


def new_address(user, ip: str, user_agent: str) -> None:
    """A completed sign-in from an address this account has not used within 30 days."""
    from vaf.auth.sessions import device_parts
    scope, username = str(getattr(user, "user_scope_id", "") or ""), str(getattr(user, "username", "") or "")
    if not scope:
        return
    device = device_parts(user_agent)
    _spawn(lambda: _deliver(scope, username, NEW_ADDRESS, "info", device=device,
                            username=username, address=ip))


def _account_of(name: str) -> Optional[Tuple[str, str]]:
    """(scope, username) of an active account by its name, or None. Runs in the job."""
    import asyncio

    async def _lookup():
        from vaf.auth.database import get_auth_db
        from vaf.auth.user_admin import find_user
        async with get_auth_db() as db:
            user = await find_user(db, name)
            if user is None or not user.is_active:
                return None
            return str(user.user_scope_id or ""), str(user.username or "")

    return asyncio.run(_lookup())


def password_guessing(username: str, minutes: int) -> None:
    """The account window closed for this name. Looked up in the job, sent only for an
    account that exists."""
    def _job():
        try:
            found = _account_of(username)
        except Exception as e:
            logger.warning("Sign-in alert: the account could not be looked up: %s", e)
            return
        if not found or not found[0]:
            return
        for scope, name in _with_admin(*found):
            _deliver(scope, name, PASSWORD_GUESSING, "error", username=found[1], minutes=minutes)

    _spawn(_job)


def second_factor_guessing(user, minutes: int, doors: Iterable[str] = ("",)) -> None:
    """The second-factor window closed. `doors` are where its wrong codes were entered
    (`check_second_factor`'s `where`). A wrong code right after a correct password means
    someone has the password, and that is said whenever one of the codes came from such a
    door: the window counts every door of the account, so a closure can mix them, and that
    warning must not be lost to the door of the last code. Only codes all entered at doors a
    signed-in session uses (a token for programs, an admin's step-up) say a session of the
    account is guessing the code."""
    scope, username = str(getattr(user, "user_scope_id", "") or ""), str(getattr(user, "username", "") or "")
    if not scope:
        return
    after_password = any(door in _AFTER_PASSWORD for door in doors)

    def _job():
        key = SECOND_FACTOR_GUESSING if after_password else SESSION_CODE_GUESSING
        for to_scope, name in _with_admin(scope, username):
            _deliver(to_scope, name, key, "error", username=username, minutes=minutes)

    _spawn(_job)
