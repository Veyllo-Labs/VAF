"""
One-time migration: copy user data from users/<username>/ to scopes/<user_scope_id>/.

Run after Phase 1-2 of the UUID migration (docs/platform/UUID.md). Reads local_users to get
username -> user_scope_id, then copies data_dir/users/<username> to data_dir/scopes/<user_scope_id>.
Does not delete users/ so you can verify and remove manually. Optionally migrates
email_config_by_user to email_config_by_scope.

Usage:
  python -m scripts.migrate_users_to_scopes [--dry-run] [--config-only]
"""

import argparse
import asyncio
import shutil
from pathlib import Path

from sqlalchemy import text

from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.memory.database import get_engine


async def get_username_scope_mapping():
    """Return list of (username, user_scope_id_str) from local_users."""
    engine = await get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT username, user_scope_id FROM local_users WHERE user_scope_id IS NOT NULL")
        )
        rows = result.fetchall()
    return [(r[0], str(r[1])) for r in rows]


def migrate_filesystem(data_dir: Path, mapping: list, dry_run: bool) -> int:
    """Copy users/<username> to scopes/<user_scope_id>. Returns number of dirs copied."""
    users_dir = data_dir / "users"
    scopes_dir = data_dir / "scopes"
    count = 0
    for username, scope_id in mapping:
        src = users_dir / username
        if not src.is_dir():
            continue
        dst = scopes_dir / scope_id
        if dst.exists():
            continue
        if dry_run:
            print(f"  [dry-run] would copy {src} -> {dst}")
        else:
            shutil.copytree(src, dst)
            print(f"  copied {src} -> {dst}")
        count += 1
    return count


def migrate_config_email_by_scope(mapping: list, dry_run: bool) -> bool:
    """Copy email_config_by_user entries to email_config_by_scope using mapping. Returns True if config was updated."""
    by_user = Config.get("email_config_by_user") or {}
    if not isinstance(by_user, dict) or not by_user:
        return False
    by_scope = Config.get("email_config_by_scope") or {}
    if not isinstance(by_scope, dict):
        by_scope = {}
    username_to_scope = {u: s for u, s in mapping}
    changed = False
    for uname, ec in by_user.items():
        if not uname or not isinstance(ec, dict):
            continue
        scope_id = username_to_scope.get(uname)
        if not scope_id or scope_id in by_scope:
            continue
        if dry_run:
            print(f"  [dry-run] would set email_config_by_scope[{scope_id}] from user {uname}")
        else:
            by_scope[scope_id] = ec
            changed = True
    if changed and not dry_run:
        config = Config.load()
        config["email_config_by_scope"] = by_scope
        Config.save(config)
        print("  updated config: email_config_by_scope")
    return changed


async def main():
    parser = argparse.ArgumentParser(description="Migrate user data from users/<username> to scopes/<user_scope_id>")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be done")
    parser.add_argument("--config-only", action="store_true", help="Only migrate email_config_by_user -> email_config_by_scope")
    args = parser.parse_args()

    print("UUID migration: users/ -> scopes/")
    mapping = await get_username_scope_mapping()
    if not mapping:
        print("  No local_users with user_scope_id found (or DB not available). Nothing to migrate.")
        return
    print(f"  Found {len(mapping)} user(s): {[m[0] for m in mapping]}")

    if not args.config_only:
        data_dir = Platform.data_dir()
        n = migrate_filesystem(data_dir, mapping, args.dry_run)
        print(f"  Migrated {n} user directory(ies). Old users/ left in place; remove manually after verification.")
    migrate_config_email_by_scope(mapping, args.dry_run)

    if args.dry_run:
        print("  [dry-run] No changes written.")
    else:
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
