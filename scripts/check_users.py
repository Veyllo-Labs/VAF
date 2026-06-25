# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import asyncio
from vaf.auth.database import get_auth_db
from vaf.auth.models import LocalUser
from sqlalchemy import select

async def check_users():
    async with get_auth_db() as db:
        result = await db.execute(select(LocalUser))
        users = result.scalars().all()
        print(f"Found {len(users)} users:")
        for u in users:
            print(f"  - Username: {u.username}, Role: {u.role}, Active: {u.is_active}")

if __name__ == "__main__":
    asyncio.run(check_users())
