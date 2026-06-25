# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md

import asyncio
import os
import shutil
from pathlib import Path
from sqlalchemy import text
from vaf.memory.database import get_engine
from vaf.core.config import Config

async def clear_data():
    print("🚀 Starting full cleanup of user and RAG data...")
    
    # 1. Database Cleanup
    try:
        engine = await get_engine()
        async with engine.begin() as conn:
            print("  - Clearing database tables...")
            # Disable triggers/constraints temporarily for thorough cleanup if needed
            # but standard DELETE is usually fine for our schema
            await conn.execute(text("DELETE FROM user_sessions"))
            await conn.execute(text("DELETE FROM local_users"))
            await conn.execute(text("DELETE FROM connections"))
            await conn.execute(text("DELETE FROM chunks"))
            await conn.execute(text("DELETE FROM memories"))
            print("  ✅ Database tables cleared.")
    except Exception as e:
        print(f"  ❌ Database cleanup failed: {e}")

    # 2. Filesystem Cleanup (User Workspaces)
    try:
        users_dir = Config.APP_DIR / "users"
        if users_dir.exists():
            print(f"  - Deleting user workspaces in {users_dir}...")
            shutil.rmtree(users_dir)
            print("  ✅ User workspaces deleted.")
        else:
            print("  - No user workspaces found to delete.")
    except Exception as e:
        print(f"  ❌ Filesystem cleanup failed: {e}")

    print("\n✨ Cleanup complete! You can now restart VAF to begin the fresh setup.")

if __name__ == "__main__":
    asyncio.run(clear_data())
