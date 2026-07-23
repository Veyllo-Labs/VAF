# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""IMAP sync engine: RFC 4549 baseline, capability-gated accelerators.

The engine speaks to a duck-typed client (subset of the IMAPClient API:
capabilities, list_folders, select_folder, search, fetch, logout) so tests run
against an in-memory fake and production wraps a real IMAPClient (client
factory in imap_client.py). Phase 1 is strictly READ-ONLY: every SELECT is
readonly; no STORE/MOVE/APPEND exists in this module by design - server-side
writes arrive with the op-queue in phase 2 behind mail_engine_write_enabled.

Algorithm per folder (EMAIL_CLIENT.md, RFC 4549):
- cache key is (folder, UIDVALIDITY, UID): a UIDVALIDITY change wipes the
  folder cache and restarts bookkeeping;
- new mail: UID FETCH last_seen+1:* (initial sync bounded to the newest
  INITIAL_WINDOW messages via UID SEARCH);
- flag resync + expunge detection: windowed UID FETCH ... FLAGS; every cached
  UID absent from the response window is removed locally;
- CONDSTORE (HIGHESTMODSEQ/CHANGEDSINCE) is used as an accelerator when the
  server advertises it - never required (Office365/Yahoo/GMX/T-Online lack it).
- Gmail (X-GM-EXT-1): X-GM-MSGID/X-GM-THRID/X-GM-LABELS are captured; labels
  map to the category model the v1 tools already use.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vaf.mail.parser import parse_message
from vaf.mail.store import MailStore

logger = logging.getLogger("vaf.mail.sync")

INITIAL_WINDOW = 500        # newest messages fetched on the very first sync of a folder
NEW_FETCH_BATCH = 100       # RFC 4549 example 7: batch UID ranges so the UI stays responsive
FLAG_WINDOW = 1000          # uids per flag-resync FETCH window

# RFC 6154 special-use -> well-known localized names fallback (Office365 and
# T-Online advertise no SPECIAL-USE; German providers use localized folders).
SPECIAL_USE_FALLBACK = {
    "\\Sent": ("Sent", "Sent Items", "Sent Messages", "Gesendet", "Gesendete Elemente",
               "Gesendete Objekte", "[Gmail]/Sent Mail"),
    "\\Drafts": ("Drafts", "Entwürfe", "Entwuerfe", "[Gmail]/Drafts"),
    "\\Trash": ("Trash", "Deleted", "Deleted Items", "Papierkorb", "Gelöschte Elemente",
                "Geloeschte Elemente", "[Gmail]/Trash"),
    "\\Junk": ("Junk", "Spam", "Junk-E-Mail", "[Gmail]/Spam"),
    "\\Archive": ("Archive", "Archiv", "[Gmail]/All Mail"),
}

# Runtime strings carry ONE backslash (Gmail system labels over X-GM-LABELS,
# e.g. \Category/Promotions) - review caught an unmatchable double-backslash.
_GMAIL_CATEGORY_LABELS = {
    "\\Category/Promotions": "promotions",
    "CATEGORY_PROMOTIONS": "promotions",
    "\\Category/Social": "social",
    "CATEGORY_SOCIAL": "social",
}


def _b2s(v: Any) -> str:
    return v.decode("utf-8", errors="replace") if isinstance(v, (bytes, bytearray)) else str(v)


def _flags_to_strs(flags: Iterable[Any]) -> List[str]:
    return [_b2s(f) for f in (flags or [])]


class ImapSyncEngine:
    def __init__(self, store: MailStore, account_id: str, provider: str, email: str, client):
        self.store = store
        self.account_id = account_id
        self.provider = provider
        self.email = email
        self.client = client
        self.account_pk = store.upsert_account(account_id, provider, email)
        caps = set()
        try:
            caps = {_b2s(c).upper() for c in (client.capabilities() or [])}
        except Exception as e:
            logger.warning("capabilities probe failed: %s", e)
        self.caps = caps
        self.is_gmail = "X-GM-EXT-1" in caps
        self.has_condstore = "CONDSTORE" in caps

    # ── folders ─────────────────────────────────────────────────────────────

    def discover_folders(self) -> List[Dict[str, Any]]:
        """LIST all folders; special-use from RFC 6154 flags when advertised,
        else from the localized well-known-name fallback table. INBOX is always
        tier 'eager'; other special folders 'headers'; the rest 'lazy'."""
        out = []
        try:
            listing = self.client.list_folders()
        except Exception as e:
            logger.warning("list_folders failed for %s: %s", self.provider, e)
            listing = [((), b"/", "INBOX")]
        for flags, _delim, name in listing:
            fl = {_b2s(f) for f in (flags or ())}
            if "\\Noselect" in fl:
                continue
            special = next((s for s in ("\\Sent", "\\Drafts", "\\Trash", "\\Junk",
                                        "\\Archive", "\\All", "\\Flagged") if s in fl), None)
            if special is None and name.upper() != "INBOX":
                for su, names in SPECIAL_USE_FALLBACK.items():
                    if name in names:
                        special = su
                        break
            if name.upper() == "INBOX":
                special, tier = "\\Inbox", "eager"
            elif special == "\\All":
                # Gmail All Mail duplicates every message; without X-GM-MSGID
                # dedup at ingest (phase 2+) syncing it doubles the store.
                tier = "lazy"
            elif special in ("\\Sent", "\\Drafts", "\\Archive"):
                tier = "headers"
            else:
                tier = "lazy"
            fpk = self.store.upsert_folder(self.account_pk, name, special_use=special,
                                           sync_tier=tier)
            out.append({"pk": fpk, "name": name, "special_use": special, "tier": tier})
        return out

    # ── folder sync (RFC 4549) ─────────────────────────────────────────────

    def sync_folder(self, name: str, fetch_bodies: bool = True,
                    max_new: Optional[int] = None) -> Dict[str, int]:
        stats = {"new": 0, "flag_updates": 0, "vanished": 0, "reset": 0, "errors": 0}
        info = self.client.select_folder(name, readonly=True)
        uidvalidity = int(info.get(b"UIDVALIDITY") or info.get("UIDVALIDITY") or 0)
        uidnext = int(info.get(b"UIDNEXT") or info.get("UIDNEXT") or 0)
        modseq_raw = info.get(b"HIGHESTMODSEQ") or info.get("HIGHESTMODSEQ")
        fpk = self.store.upsert_folder(self.account_pk, name)
        folder = self.store.get_folder(self.account_pk, name) or {}

        stored_uv = folder.get("uidvalidity")
        if stored_uv and uidvalidity and int(stored_uv) != uidvalidity:
            stats["reset"] = self.store.reset_folder(fpk, uidvalidity)
            folder = self.store.get_folder(self.account_pk, name) or {}
        last_seen = int(folder.get("last_seen_uid") or 0)

        # 1) new mail. The watermark may only advance over the CONTIGUOUS
        # successfully-fetched prefix: a failed batch aborts the new-mail loop
        # so its UIDs are retried next sync (review finding: continuing let a
        # later successful batch advance last_seen past up to 100 lost mails).
        # A failed single-message INGEST still advances (deliberate malformed-
        # mail policy, pinned by test_broken_message_does_not_abort_sync).
        new_uids = self._new_uids(last_seen, max_new or INITIAL_WINDOW)
        max_uid = last_seen
        for batch_start in range(0, len(new_uids), NEW_FETCH_BATCH):
            batch = new_uids[batch_start:batch_start + NEW_FETCH_BATCH]
            try:
                fetched = self._fetch_new(batch, fetch_bodies)
            except Exception as e:
                logger.warning("fetch batch failed in %s (will retry from uid %s): %s",
                               name, batch[0], e)
                stats["errors"] += 1
                break
            for uid, item in sorted(fetched.items()):
                try:
                    self._ingest(fpk, int(uid), item, fetch_bodies)
                    stats["new"] += 1
                except Exception as e:
                    # one broken message must never abort the folder sync
                    logger.warning("ingest failed for uid %s in %s: %s", uid, name, e)
                    stats["errors"] += 1
                max_uid = max(max_uid, int(uid))

        # 2) flag resync + expunge detection over the previously-known range
        flags_complete = True
        if last_seen > 0:
            changed, vanished, flags_complete = self._resync_flags(fpk, last_seen, folder)
            stats["flag_updates"], stats["vanished"] = changed, vanished

        # HIGHESTMODSEQ may only be persisted when every flag window succeeded,
        # otherwise the CONDSTORE fast path would skip the missed changes forever.
        self.store.set_folder_state(
            fpk, uidvalidity=uidvalidity or None, uidnext=uidnext or None,
            highestmodseq=int(modseq_raw) if (modseq_raw and flags_complete) else None,
            last_seen_uid=max_uid)
        self.store.set_account_synced(self.account_pk)
        return stats

    def _new_uids(self, last_seen: int, window: int) -> List[int]:
        """UIDs of new mail. Initial sync (last_seen 0) is bounded to the newest
        `window` messages; incremental uses last_seen+1:* (guarding the IMAP
        quirk that m:* with m > max returns the highest-UID message)."""
        try:
            if last_seen <= 0:
                uids = sorted(int(u) for u in self.client.search("ALL"))
                return uids[-window:]
            uids = sorted(int(u) for u in self.client.search(["UID", f"{last_seen + 1}:*"]))
            return [u for u in uids if u > last_seen][:window * 4]
        except Exception as e:
            logger.warning("uid discovery failed: %s", e)
            return []

    def _fetch_new(self, uids: List[int], fetch_bodies: bool) -> Dict[int, Dict[Any, Any]]:
        items = ["FLAGS", "INTERNALDATE", "RFC822.SIZE"]
        items.append("BODY.PEEK[]" if fetch_bodies else "BODY.PEEK[HEADER]")
        if self.is_gmail:
            items += ["X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS"]
        return self.client.fetch(uids, items)

    def _ingest(self, fpk: int, uid: int, item: Dict[Any, Any], fetched_body: bool) -> None:
        raw = item.get(b"BODY[]") or item.get("BODY[]")
        header = item.get(b"BODY[HEADER]") or item.get("BODY[HEADER]")
        blob = raw or header or b""
        parsed = parse_message(bytes(blob))
        flags = _flags_to_strs(item.get(b"FLAGS") or item.get("FLAGS") or [])
        internal = item.get(b"INTERNALDATE") or item.get("INTERNALDATE")
        internal_ts = int(internal.timestamp()) if hasattr(internal, "timestamp") else None
        size = item.get(b"RFC822.SIZE") or item.get("RFC822.SIZE")
        gm_msgid = item.get(b"X-GM-MSGID") or item.get("X-GM-MSGID")
        gm_thrid = item.get(b"X-GM-THRID") or item.get("X-GM-THRID")
        category = ""
        if self.is_gmail:
            labels = [_b2s(x) for x in (item.get(b"X-GM-LABELS") or item.get("X-GM-LABELS") or [])]
            category = next((v for k, v in _GMAIL_CATEGORY_LABELS.items() if k in labels), "primary")
        self.store.ingest_message(
            self.account_pk, fpk, uid, parsed,
            raw=bytes(raw) if (raw and fetched_body) else None,
            server_flags=flags, internaldate_ts=internal_ts,
            size_bytes=int(size) if size else None,
            gm_msgid=str(gm_msgid) if gm_msgid else None,
            gm_thrid=str(gm_thrid) if gm_thrid else None,
            category=category)

    def _resync_flags(self, fpk: int, last_seen: int,
                      folder: Dict[str, Any]) -> Tuple[int, int, bool]:
        """Windowed FLAGS refetch over 1:last_seen. With CONDSTORE we narrow the
        flag UPDATE query via CHANGEDSINCE, but presence (expunge detection)
        always uses the full response window (RFC 4549 4.3.1). Returns
        (changed, vanished, complete): complete=False on any failed window so
        the caller never persists a HIGHESTMODSEQ that would skip the miss."""
        changed = 0
        present: List[int] = []
        stored_modseq = folder.get("highestmodseq")
        use_changedsince = bool(self.has_condstore and stored_modseq)
        for start in range(1, last_seen + 1, FLAG_WINDOW):
            end = min(start + FLAG_WINDOW - 1, last_seen)
            rng = f"{start}:{end}"
            try:
                resp = self.client.fetch(rng, ["FLAGS"])
            except Exception as e:
                logger.warning("flag window %s failed: %s", rng, e)
                return changed, 0, False  # do NOT expunge on a failed window - fail safe
            window_flags: Dict[int, List[str]] = {}
            for uid, item in resp.items():
                u = int(uid)
                if u > last_seen:
                    continue
                present.append(u)
                window_flags[u] = _flags_to_strs(item.get(b"FLAGS") or item.get("FLAGS") or [])
            if use_changedsince:
                # CONDSTORE narrows which rows we diff, presence list stays full
                try:
                    resp2 = self.client.fetch(rng, ["FLAGS"],
                                              modifiers=[f"CHANGEDSINCE {int(stored_modseq)}"])
                    window_flags = {int(u): _flags_to_strs(i.get(b"FLAGS") or i.get("FLAGS") or [])
                                    for u, i in resp2.items() if int(u) <= last_seen}
                except Exception:
                    pass  # accelerator only - full window diff already in hand
            changed += self.store.apply_server_flags(fpk, window_flags)
        # bound expunge detection to the resynced range: uids above last_seen
        # (e.g. ingested seconds ago) are outside the presence window
        vanished = self.store.remove_vanished(fpk, present_uids=present, max_uid=last_seen)
        return changed, vanished, True

    # ── account-level convenience ──────────────────────────────────────────

    def sync_account(self, max_new: Optional[int] = None) -> Dict[str, Any]:
        """Discover folders, then sync by tier: INBOX eagerly with bodies,
        'headers' tiers without bodies. 'lazy' folders are skipped here (synced
        on open via sync_folder)."""
        results: Dict[str, Any] = {}
        for f in self.discover_folders():
            if f["tier"] == "eager":
                results[f["name"]] = self.sync_folder(f["name"], fetch_bodies=True,
                                                      max_new=max_new)
            elif f["tier"] == "headers":
                results[f["name"]] = self.sync_folder(f["name"], fetch_bodies=False,
                                                      max_new=max_new)
        return results
