# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Auth database schema and session handling.

Reuses the existing PostgreSQL connection from vaf.memory.database.
Auth tables (local_users, user_sessions) are created on the same Base as memory.
"""

import logging

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Auth schema initialized (shared DB)")
