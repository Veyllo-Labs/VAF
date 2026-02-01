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
    """
    from vaf.auth.models import LocalUser
    from vaf.auth.crypto import hash_password
    from sqlalchemy import select

    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Bootstrap default admin if no admin exists
    try:
        async with get_db() as db:
            result = await db.execute(select(LocalUser).where(LocalUser.role == "admin"))
            if not result.scalar_one_or_none():
                logger.info("No admin user found. Bootstrapping default 'admin' account...")
                admin = LocalUser(
                    username="admin",
                    password_hash=hash_password("vaf_admin_secret"),
                    role="admin",
                    requires_2fa_setup=True,
                    is_active=True
                )
                db.add(admin)
                await db.commit()
                logger.info("Default 'admin' account created. Please change password and setup 2FA on login.")
    except Exception as e:
        logger.warning(f"Auth bootstrap check failed: {e}")

    logger.info("Auth schema initialized (shared DB)")
