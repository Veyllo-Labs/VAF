# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Auth database schema and session handling.

Reuses the existing PostgreSQL connection from vaf.memory.database.
Auth tables (local_users, user_sessions) are created on the same Base as memory.
"""

import asyncio
import logging
import threading
import time

from vaf.memory.database import get_engine, get_db
from vaf.memory.models import Base

logger = logging.getLogger(__name__)

get_auth_db = get_db


async def init_auth_db() -> None:
    """
    Ensure auth tables exist in the shared PostgreSQL database.

    Imports auth models so they are registered on Base, then creates all
    tables (memory + auth). Idempotent.
    The first admin is created only via the web UI onboarding (POST /api/auth/bootstrap).
    """
    from vaf.auth.models import LocalUser  # noqa: F401 - register on Base for create_all

    engine = await get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        # Non-main threads get throwaway NullPool engines from get_engine (never disposed by
        # it); dispose here - mirrors get_db - so a retry loop cannot pile up engine objects.
        if threading.current_thread() is not threading.main_thread():
            try:
                await engine.dispose()
            except Exception:
                pass

    logger.info("Auth schema initialized (shared DB)")


async def init_auth_db_with_retry(max_wait_seconds: float = 1800.0) -> bool:
    """
    Retry init_auth_db until the database accepts it.

    The Docker stack starts in a thread parallel to the web server, and on Windows
    (Rancher/WSL2) PostgreSQL can need minutes between "container started" and "accepting
    queries". Without this retry a lost race left the auth tables uncreated until the next
    restart - a fresh install then showed the login form instead of the first-run setup.

    Never gives up: the frontend polls needs-setup unbounded too, and a database that
    appears AFTER the budget must still heal the install without a process restart.
    max_wait_seconds only escalates logging (one loud ERROR) and slows the cadence to one
    attempt per 60s. Returns True once the schema is initialized.
    """
    start = time.monotonic()
    attempt = 0
    delay = 2.0
    next_log_at = 0.0
    over_budget = False
    while True:
        attempt += 1
        try:
            await init_auth_db()
            logger.info(
                "Auth schema initialized after %.0fs (attempt %d) - DB became ready",
                time.monotonic() - start, attempt,
            )
            return True
        except Exception as e:
            elapsed = time.monotonic() - start
            if not over_budget and elapsed >= max_wait_seconds:
                over_budget = True
                logger.error(
                    "Auth DB still not ready after %.0fs (%d attempts): %s - login/setup stay "
                    "unavailable; continuing to retry every 60s (is the Docker stack running?)",
                    elapsed, attempt, e,
                )
            elif elapsed >= next_log_at:
                next_log_at = elapsed + 60.0
                logger.warning(
                    "Auth DB not ready (attempt %d, %.0fs elapsed): %s - retrying",
                    attempt, elapsed, e,
                )
        await asyncio.sleep(delay)
        delay = 60.0 if over_budget else min(delay * 1.5, 15.0)
