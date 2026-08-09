# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Re-encrypt the memory store from an OLD encryption key to the CURRENT one.

Recovery path for a rotated `memory_encryption_key`: rows written before the
rotation are unreadable with the current key but perfectly intact - decrypting
them with the old key and re-encrypting with the current one restores the
whole store. Exposed as `vaf memory rekey`; the loop is idempotent (rows the
current key already opens are skipped), unreadable rows are counted and NEVER
overwritten, and every rewrite is verified against the plaintext before it is
written.

Runs on the OWNER engine: the app role is RLS-restricted (an unscoped app
session sees ZERO rows since the role cutover), so a rekey through `get_db()`
would report "0 rows, success" while fixing nothing. The connected role is
probed for superuser/BYPASSRLS before any work.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

_BATCH_COMMIT_EVERY = 200


@dataclass
class RekeyCounts:
    rekeyed: int = 0
    skipped_current: int = 0   # already readable with the current key
    failed: int = 0            # neither key opens the row - left untouched


@dataclass
class RekeyReport:
    memories: RekeyCounts = field(default_factory=RekeyCounts)
    chunks: RekeyCounts = field(default_factory=RekeyCounts)
    profile_cache_deleted: int = 0
    caches_cleared: bool = False
    dry_run: bool = False
    failed_ids: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        mode = " (dry-run: nothing written)" if self.dry_run else ""
        out = [
            f"Memories: {self.memories.rekeyed} rekeyed, "
            f"{self.memories.skipped_current} already current, "
            f"{self.memories.failed} unreadable{mode}",
            f"Chunks:   {self.chunks.rekeyed} rekeyed, "
            f"{self.chunks.skipped_current} already current, "
            f"{self.chunks.failed} unreadable{mode}",
        ]
        if self.profile_cache_deleted:
            out.append(f"Profile cache: {self.profile_cache_deleted} file(s) deleted "
                       "(regenerates on next use)")
        if self.caches_cleared:
            out.append("Redis caches cleared (cached snippets carried old plaintext)")
        if self.failed_ids:
            out.append("Unreadable row ids (left untouched): "
                       + ", ".join(self.failed_ids[:10])
                       + (" ..." if len(self.failed_ids) > 10 else ""))
        return out


def _old_cipher(old_key_b64: str):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        key = base64.b64decode(old_key_b64.strip(), validate=True)
    except Exception as e:
        raise RuntimeError("The old key is not valid Base64.") from e
    if len(key) != 32:
        raise RuntimeError(f"The old key decodes to {len(key)} bytes, expected 32.")
    return AESGCM(key)


async def _assert_owner_visibility(db) -> None:
    """The connected role must bypass RLS, or the loop silently sees a
    fraction of the store and reports a hollow success."""
    from sqlalchemy import text
    row = (await db.execute(text(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ))).scalar()
    if not bool(row):
        raise RuntimeError(
            "The connected database role is RLS-restricted and cannot see the "
            "whole store. Set memory_db_owner_url to the owner DSN and retry - "
            "a restricted rekey would 'succeed' on a fraction of the rows."
        )


async def rekey_store(old_key_b64: str, *, dry_run: bool = False) -> RekeyReport:
    """Decrypt every row the current key cannot open with the OLD key and
    re-encrypt it with the CURRENT key. Returns honest per-table counts."""
    from sqlalchemy import text
    from vaf.core.config import Config
    from vaf.memory.crypto import (FIELD_PREFIX, decrypt_field, encrypt_field,
                                   get_crypto)
    from vaf.memory.database import get_owner_db

    old = _old_cipher(old_key_b64)
    current = get_crypto()  # raises on unreadable config / corrupt key
    report = RekeyReport(dry_run=dry_run)

    async with get_owner_db() as db:
        await _assert_owner_visibility(db)

        # ── memories: encrypted_content + nonce columns (soft-deleted rows
        # included - they are restorable and must survive a later restore) ──
        rows = (await db.execute(text(
            "SELECT id, encrypted_content, nonce FROM memories"
        ))).all()
        pending = 0
        for mid, content, nonce in rows:
            if content is None or nonce is None:
                continue
            try:
                current.decrypt(bytes(content), bytes(nonce))
                report.memories.skipped_current += 1
                continue
            except Exception:
                pass
            try:
                plaintext = old.decrypt(bytes(nonce), bytes(content), None).decode("utf-8")
            except Exception:
                report.memories.failed += 1
                report.failed_ids.append(f"memory:{mid}")
                continue
            new_ct, new_nonce = current.encrypt(plaintext)
            if current.decrypt(new_ct, new_nonce) != plaintext:  # verify before write
                report.memories.failed += 1
                report.failed_ids.append(f"memory:{mid}")
                continue
            if not dry_run:
                await db.execute(
                    text("UPDATE memories SET encrypted_content = :c, nonce = :n "
                         "WHERE id = :i"),
                    {"c": new_ct, "n": new_nonce, "i": mid})
                pending += 1
                if pending >= _BATCH_COMMIT_EVERY:
                    await db.commit()
                    pending = 0
            report.memories.rekeyed += 1

        # ── chunks: enc:gcm: field format inside the text column ──────────
        rows = (await db.execute(text(
            "SELECT id, text FROM chunks WHERE text LIKE :p"
        ), {"p": FIELD_PREFIX + "%"})).all()
        for cid, value in rows:
            if decrypt_field(value) != "[Decryption failed]":
                report.chunks.skipped_current += 1
                continue
            try:
                rest = value[len(FIELD_PREFIX):]
                nonce_b64, _, ct_b64 = rest.partition(":")
                plaintext = old.decrypt(base64.b64decode(nonce_b64),
                                        base64.b64decode(ct_b64), None).decode("utf-8")
            except Exception:
                report.chunks.failed += 1
                report.failed_ids.append(f"chunk:{cid}")
                continue
            new_value = encrypt_field(plaintext)
            if decrypt_field(new_value) != plaintext:  # verify before write
                report.chunks.failed += 1
                report.failed_ids.append(f"chunk:{cid}")
                continue
            if not dry_run:
                await db.execute(text("UPDATE chunks SET text = :t WHERE id = :i"),
                                 {"t": new_value, "i": cid})
                pending += 1
                if pending >= _BATCH_COMMIT_EVERY:
                    await db.commit()
                    pending = 0
            report.chunks.rekeyed += 1
        # remaining pending rows commit at the context exit

    # ── profile cache: derived data encrypted with the old key - delete,
    # it regenerates on the next profile refresh ──────────────────────────
    cache_dir = Path(Config.APP_DIR) / "user_profile_cache"
    if cache_dir.is_dir():
        for f in cache_dir.glob("*.txt"):
            report.profile_cache_deleted += 1
            if not dry_run:
                try:
                    f.unlink()
                except OSError:
                    pass

    # ── Redis caches hold decrypted snippets keyed per scope - clear them ──
    if not dry_run:
        try:
            from vaf.memory.cache import get_cache
            report.caches_cleared = bool(await get_cache().clear_all())
        except Exception as e:
            logger.debug(f"Cache clear skipped: {e}")

    return report
