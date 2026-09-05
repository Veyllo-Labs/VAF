# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
The VAF calendar: one SQLite store per user scope holding every appointment the user has,
whether typed in VAF, booked by the agent, attached to a contact, or mirrored from a
connected Google or Microsoft calendar (vaf/core/calendar_sync.py keeps those in step,
both ways).

Shape and rules, modelled on vaf/mail/store.py:
  * `CalendarStore(user_scope_id)` is fail-closed: an explicit scope or a ValueError; the
    local admin resolves their scope with get_local_admin_scope_id() like everyone else. The
    file is data_dir/scopes/<scope>/calendar.db, owner-only.
  * `schema_meta.schema_version` versions the file; a file newer than this build is refused.
  * `CalendarStore.exists(scope)` answers "is there anything?" without creating a database.
  * Times are UTC instants (`start_ts`, `end_ts`) plus the IANA zone they were entered in;
    an all-day event additionally carries its dates and sits at midnight in that zone.
  * A mirrored event remembers its provider identity (`account_id`, `external_id`,
    `external_updated`, `etag`); `sync_state` says what the sync still owes the provider.
  * Contact events live here (`contact_ids`); the first store for a scope moves the events
    that older records kept inside contacts.json, once.
Nothing here talks to a provider or a browser: writes mark `pending_push`, the sync pushes,
the routes signal.
"""
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("vaf.core.calendar_store")

SCHEMA_VERSION = 1
DB_NAME = "calendar.db"
DEFAULT_REMINDER_MINUTES = 15
REMINDER_GRACE_SECONDS = 6 * 3600
PUSH_MAX_ATTEMPTS = 5

SYNC_STATES = ("local_only", "synced", "pending_push", "pending_delete", "push_failed")
_UNSET: Any = object()


def _now() -> float:
    return time.time()


def _zone(tz_name: Optional[str]):
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _midnight_ts(date_str: str, tz_name: Optional[str]) -> float:
    d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    return d.replace(tzinfo=_zone(tz_name)).timestamp()


def _date_in_zone(ts: float, tz_name: Optional[str]) -> str:
    return datetime.fromtimestamp(float(ts), tz=_zone(tz_name)).strftime("%Y-%m-%d")


def _iso_to_ts(s: Optional[str], tz_name: Optional[str]) -> Optional[float]:
    """A provider timestamp (RFC 3339, with or without offset; Graph's seven fractional
    digits) to a unix instant; a naive value is read in `tz_name` (UTC when none)."""
    from vaf.core.calendar_client import _parse_iso
    dt = _parse_iso(s)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name))
    return dt.timestamp()


class CalendarStore:
    """One instance per user scope. Connections are per thread; SQLite WAL keeps readers and
    writers sane across the API worker threads and the scheduler thread."""

    def __init__(self, user_scope_id: str, base_dir: Optional[Path] = None):
        scope = str(user_scope_id or "").strip()
        if not scope:
            raise ValueError("CalendarStore requires an explicit user_scope_id (fail-closed; "
                             "resolve the local admin scope via get_local_admin_scope_id())")
        self.user_scope_id = scope
        if base_dir is None:
            from vaf.core.platform import Platform
            base_dir = Platform.data_dir()
        self.db_path = Path(base_dir) / "scopes" / scope / DB_NAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from vaf.core.secure_store import harden_dir, harden_path
            harden_dir(self.db_path.parent)
        except Exception:
            harden_path = None
        self._local = threading.local()
        self.ensure_schema()
        if harden_path is not None:
            try:
                harden_path(self.db_path)
            except Exception:
                pass
        self._migrate_contact_events()

    @staticmethod
    def exists(user_scope_id: str, base_dir: Optional[Path] = None) -> bool:
        """Whether this scope has a calendar on disk. Constructing the store creates the file
        and its schema; a glance that only wants to know "any appointments?" asks here first."""
        scope = str(user_scope_id or "").strip()
        if not scope:
            return False
        if base_dir is None:
            from vaf.core.platform import Platform
            base_dir = Platform.data_dir()
        return (Path(base_dir) / "scopes" / scope / DB_NAME).exists()

    # ── connection / schema ─────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def ensure_schema(self) -> None:
        conn = self._conn()
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'")
        if cur.fetchone() is None:
            self._create_schema(conn)
            return
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"calendar.db schema {version} is newer than this build ({SCHEMA_VERSION})")
        # future migrations: if version < SCHEMA_VERSION: migrate stepwise here

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE events (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          location TEXT NOT NULL DEFAULT '',
          start_ts REAL NOT NULL,
          end_ts REAL NOT NULL,
          all_day INTEGER NOT NULL DEFAULT 0,
          start_date TEXT,
          end_date TEXT,
          tz TEXT,
          status TEXT NOT NULL DEFAULT 'confirmed',
          source TEXT NOT NULL DEFAULT 'vaf',
          account_id TEXT,
          external_calendar_id TEXT,
          external_id TEXT,
          external_updated REAL,
          etag TEXT,
          recurring_master_id TEXT,
          sync_state TEXT NOT NULL DEFAULT 'local_only',
          push_attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          contact_ids TEXT NOT NULL DEFAULT '[]',
          created_by TEXT NOT NULL DEFAULT 'user',
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          reminder_minutes INTEGER,
          reminder_fired_at REAL,
          reminder_missed_at REAL,
          link TEXT,
          legacy_id TEXT
        );
        CREATE INDEX idx_events_start ON events(start_ts);
        CREATE INDEX idx_events_external ON events(account_id, external_id);
        CREATE INDEX idx_events_sync ON events(sync_state);
        CREATE TABLE accounts (
          account_id TEXT PRIMARY KEY,
          enabled INTEGER NOT NULL DEFAULT 1,
          last_sync_at REAL,
          last_error TEXT,
          needs_reconsent INTEGER NOT NULL DEFAULT 0,
          sync_state TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('created_at', ?)", (datetime.now(timezone.utc).isoformat(),))
        conn.commit()

    def _meta(self, key: str) -> Optional[str]:
        row = self._conn().execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        conn = self._conn()
        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", (key, value))
        conn.commit()

    # ── rows ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            ids = json.loads(d.get("contact_ids") or "[]")
        except (TypeError, ValueError):
            ids = []
        d["contact_ids"] = [str(i) for i in ids if i]
        d["all_day"] = bool(d.get("all_day"))
        rm = d.get("reminder_minutes")
        d["reminder_at"] = (float(d["start_ts"]) - int(rm) * 60) if rm is not None else None
        return d

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM events WHERE id=?", (str(event_id or ""),)).fetchone()
        return self._row(row) if row else None

    def _bounds(self, start_ts: float, end_ts: Optional[float], all_day: bool, tz: Optional[str],
                start_date: Optional[str], end_date: Optional[str]):
        """Consistent (start_ts, end_ts, start_date, end_date) for a timed or an all-day event.
        An all-day event's dates rule; its instants are midnight in the zone and the end is
        the exclusive next day, the way both providers count."""
        if all_day:
            sd = (start_date or _date_in_zone(start_ts, tz))[:10]
            ed = (end_date or "")[:10]
            if not ed or ed <= sd:
                ed = (datetime.strptime(sd, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            return _midnight_ts(sd, tz), _midnight_ts(ed, tz), sd, ed
        s = float(start_ts)
        e = float(end_ts) if end_ts is not None else s + 3600.0
        if e < s:
            e = s
        return s, e, None, None

    def add_event(
        self,
        *,
        title: str,
        start_ts: float,
        end_ts: Optional[float] = None,
        all_day: bool = False,
        tz: Optional[str] = None,
        description: str = "",
        location: str = "",
        contact_ids: Optional[Iterable[str]] = None,
        created_by: str = "user",
        reminder_minutes: Any = _UNSET,
        account_id: Optional[str] = None,
        source: str = "vaf",
        status: str = "confirmed",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        external_id: Optional[str] = None,
        external_calendar_id: Optional[str] = None,
        external_updated: Optional[float] = None,
        etag: Optional[str] = None,
        recurring_master_id: Optional[str] = None,
        link: Optional[str] = None,
        legacy_id: Optional[str] = None,
        created_at: Optional[float] = None,
        sync_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert one event. `reminder_minutes` left unset takes the user's default (a
        mirrored event gets none unless asked); an event with an account and no explicit
        sync_state is owed to the provider (`pending_push`), one without stays local."""
        now = _now()
        s, e, sd, ed = self._bounds(start_ts, end_ts, bool(all_day), tz, start_date, end_date)
        if reminder_minutes is _UNSET:
            reminder_minutes = self.settings()["default_reminder_minutes"] if (source == "vaf" and not external_id) else None
        rm = int(reminder_minutes) if reminder_minutes not in (None, 0, "0", "") else None
        if sync_state is None:
            sync_state = "pending_push" if (account_id and not external_id) else ("synced" if external_id else "local_only")
        if sync_state not in SYNC_STATES:
            sync_state = "local_only"
        ids = [str(i) for i in (contact_ids or []) if i]
        event_id = str(uuid.uuid4())
        conn = self._conn()
        conn.execute(
            """INSERT INTO events (id, title, description, location, start_ts, end_ts, all_day, start_date, end_date, tz,
               status, source, account_id, external_calendar_id, external_id, external_updated, etag, recurring_master_id,
               sync_state, contact_ids, created_by, created_at, updated_at, reminder_minutes, link, legacy_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, (title or "").strip()[:500], (description or "")[:4000], (location or "").strip()[:500], s, e,
             1 if all_day else 0, sd, ed, tz, status if status in ("confirmed", "cancelled") else "confirmed",
             (source or "vaf").strip().lower(), account_id, external_calendar_id, external_id, external_updated, etag,
             recurring_master_id, sync_state, json.dumps(ids), (created_by or "user").strip()[:40],
             float(created_at) if created_at else now, now, rm, link, legacy_id),
        )
        conn.commit()
        return self.get_event(event_id)  # type: ignore[return-value]

    _UPDATABLE = ("title", "description", "location", "start_ts", "end_ts", "all_day", "tz", "contact_ids",
                  "reminder_minutes", "status", "account_id", "start_date", "end_date")

    def update_event(self, event_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Change the given fields. A change to a synced or pending event is owed to the
        provider again (`pending_push`); a moved start arms its reminder anew."""
        cur = self.get_event(event_id)
        if not cur:
            return None
        allowed = {k: v for k, v in fields.items() if k in self._UPDATABLE}
        if not allowed:
            return cur
        merged = dict(cur)
        merged.update(allowed)
        all_day = bool(merged.get("all_day"))
        s, e, sd, ed = self._bounds(float(merged["start_ts"]), merged.get("end_ts"), all_day, merged.get("tz"),
                                    merged.get("start_date") if "start_date" in allowed or "start_ts" not in allowed else None,
                                    merged.get("end_date") if "end_date" in allowed or "end_ts" not in allowed else None)
        rm_raw = merged.get("reminder_minutes")
        rm = int(rm_raw) if rm_raw not in (None, 0, "0", "") else None
        ids = [str(i) for i in (merged.get("contact_ids") or []) if i]
        status = merged.get("status") if merged.get("status") in ("confirmed", "cancelled") else "confirmed"
        now = _now()
        sync_state = cur["sync_state"]
        if cur.get("account_id") or allowed.get("account_id"):
            if sync_state in ("synced", "local_only", "push_failed"):
                sync_state = "pending_push"
        moved = abs(s - float(cur["start_ts"])) > 1e-6
        conn = self._conn()
        conn.execute(
            """UPDATE events SET title=?, description=?, location=?, start_ts=?, end_ts=?, all_day=?, start_date=?, end_date=?,
               tz=?, status=?, account_id=?, sync_state=?, contact_ids=?, reminder_minutes=?, updated_at=?,
               reminder_fired_at=CASE WHEN ? THEN NULL ELSE reminder_fired_at END,
               reminder_missed_at=CASE WHEN ? THEN NULL ELSE reminder_missed_at END
               WHERE id=?""",
            ((merged.get("title") or "").strip()[:500], (merged.get("description") or "")[:4000],
             (merged.get("location") or "").strip()[:500], s, e, 1 if all_day else 0, sd, ed, merged.get("tz"), status,
             merged.get("account_id"), sync_state, json.dumps(ids), rm, now, 1 if moved else 0, 1 if moved else 0, cur["id"]),
        )
        conn.commit()
        return self.get_event(cur["id"])

    def delete_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Remove an event. A mirrored one is only marked `pending_delete`: the row stays until
        the provider has been told (purge_event), so a failed push can retry. Returns the
        event as it was, or None when there was none."""
        cur = self.get_event(event_id)
        if not cur:
            return None
        conn = self._conn()
        if cur.get("external_id") and cur.get("account_id"):
            conn.execute("UPDATE events SET sync_state='pending_delete', updated_at=? WHERE id=?", (_now(), cur["id"]))
        else:
            conn.execute("DELETE FROM events WHERE id=?", (cur["id"],))
        conn.commit()
        return cur

    def purge_event(self, event_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM events WHERE id=?", (str(event_id or ""),))
        conn.commit()

    def list_events(
        self,
        start_ts: float,
        end_ts: float,
        *,
        include_cancelled: bool = False,
        contact_id: Optional[str] = None,
        include_pending_delete: bool = False,
    ) -> List[Dict[str, Any]]:
        """Every event overlapping [start_ts, end_ts), oldest first."""
        clauses = ["end_ts > ?", "start_ts < ?"]
        params: List[Any] = [float(start_ts), float(end_ts)]
        if not include_cancelled:
            clauses.append("status != 'cancelled'")
        if not include_pending_delete:
            clauses.append("sync_state != 'pending_delete'")
        if contact_id:
            clauses.append("instr(contact_ids, ?) > 0")
            params.append(json.dumps(str(contact_id)))
        rows = self._conn().execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY start_ts ASC, id ASC", params).fetchall()
        out = [self._row(r) for r in rows]
        if contact_id:
            out = [e for e in out if str(contact_id) in e["contact_ids"]]
        return out

    def events_for_contact(self, contact_id: str, start_ts: Optional[float] = None,
                           end_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        """The events linked to one contact, optionally within a window, oldest first."""
        cid = str(contact_id or "")
        if not cid:
            return []
        clauses = ["instr(contact_ids, ?) > 0", "status != 'cancelled'", "sync_state != 'pending_delete'"]
        params: List[Any] = [json.dumps(cid)]
        if start_ts is not None:
            clauses.append("end_ts > ?")
            params.append(float(start_ts))
        if end_ts is not None:
            clauses.append("start_ts < ?")
            params.append(float(end_ts))
        rows = self._conn().execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY start_ts ASC, id ASC", params).fetchall()
        return [e for e in (self._row(r) for r in rows) if cid in e["contact_ids"]]

    def search_events(self, needles: Iterable[str], start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
        """Events in the window whose title or description mentions one of the needles
        (case-insensitive substring): the match the contact book runs on a person's name and
        addresses."""
        terms = [str(n).strip().lower() for n in needles if str(n or "").strip()]
        if not terms:
            return []
        clauses = ["end_ts > ?", "start_ts < ?", "status != 'cancelled'", "sync_state != 'pending_delete'"]
        params: List[Any] = [float(start_ts), float(end_ts)]
        ors = []
        for t in terms:
            ors.append("(lower(title) LIKE ? OR lower(description) LIKE ?)")
            params.extend([f"%{t}%", f"%{t}%"])
        clauses.append("(" + " OR ".join(ors) + ")")
        rows = self._conn().execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY start_ts ASC, id ASC", params).fetchall()
        return [self._row(r) for r in rows]

    # ── mirrored events (called by the sync) ─────────────────────────────────

    def find_external(self, account_id: str, external_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM events WHERE account_id=? AND external_id=?",
                                   (account_id, external_id)).fetchone()
        return self._row(row) if row else None

    def upsert_external(self, account_id: str, ev: Dict[str, Any], *, source: str,
                        calendar_id: Optional[str] = None) -> str:
        """Fold one normalised provider event in. Returns "created", "updated", "cancelled",
        "kept_local" (a local edit still waiting to be pushed is newer than the provider's
        copy, so the newer change wins and the push goes ahead) or "unchanged"."""
        ext_id = str(ev.get("id") or "").strip()
        if not ext_id:
            return "unchanged"
        tz = ev.get("tz") or None
        all_day = bool(ev.get("all_day"))
        if all_day:
            sd = str(ev.get("start") or "")[:10]
            ed = str(ev.get("end") or "")[:10]
            if not sd:
                return "unchanged"
            s = _midnight_ts(sd, tz)
            e = _midnight_ts(ed, tz) if ed else s + 86400.0
        else:
            s = _iso_to_ts(ev.get("start"), tz)
            e = _iso_to_ts(ev.get("end"), tz)
            sd = ed = None
            if s is None:
                return "unchanged"
            if e is None or e < s:
                e = s + 3600.0
        ext_updated = float(ev.get("updated") or 0) or None
        status = "cancelled" if (ev.get("status") or "") == "cancelled" else "confirmed"
        cur = self.find_external(account_id, ext_id)
        now = _now()
        conn = self._conn()
        if cur is None:
            if status == "cancelled":
                return "unchanged"
            self.add_event(title=ev.get("summary") or "", start_ts=s, end_ts=e, all_day=all_day, tz=tz,
                           description=ev.get("description") or "", location=ev.get("location") or "",
                           created_by="sync", reminder_minutes=None, account_id=account_id, source=source,
                           status=status, start_date=sd, end_date=ed, external_id=ext_id,
                           external_calendar_id=calendar_id, external_updated=ext_updated, etag=ev.get("etag"),
                           recurring_master_id=ev.get("recurring_event_id"), link=ev.get("link"), sync_state="synced")
            return "created"
        if cur["sync_state"] in ("pending_push", "pending_delete", "push_failed"):
            local_newer = ext_updated is None or float(cur["updated_at"]) >= ext_updated
            if local_newer:
                return "kept_local"
        if status == "cancelled":
            if cur["status"] == "cancelled":
                return "unchanged"
            conn.execute("UPDATE events SET status='cancelled', sync_state='synced', external_updated=?, etag=?, updated_at=? WHERE id=?",
                         (ext_updated, ev.get("etag"), now, cur["id"]))
            conn.commit()
            return "cancelled"
        same = (
            cur["title"] == (ev.get("summary") or "") and cur["description"] == (ev.get("description") or "")
            and cur["location"] == (ev.get("location") or "") and abs(float(cur["start_ts"]) - s) < 1e-6
            and abs(float(cur["end_ts"]) - e) < 1e-6 and bool(cur["all_day"]) == all_day and cur["status"] == status
            and (cur.get("external_updated") or None) == ext_updated
        )
        if same and cur["sync_state"] == "synced":
            return "unchanged"
        moved = abs(float(cur["start_ts"]) - s) > 1e-6
        conn.execute(
            """UPDATE events SET title=?, description=?, location=?, start_ts=?, end_ts=?, all_day=?, start_date=?, end_date=?,
               tz=COALESCE(?, tz), status=?, sync_state='synced', external_updated=?, etag=?, recurring_master_id=?, link=?,
               push_attempts=0, last_error=NULL, updated_at=?,
               reminder_fired_at=CASE WHEN ? THEN NULL ELSE reminder_fired_at END,
               reminder_missed_at=CASE WHEN ? THEN NULL ELSE reminder_missed_at END
               WHERE id=?""",
            ((ev.get("summary") or "")[:500], (ev.get("description") or "")[:4000], (ev.get("location") or "")[:500],
             s, e, 1 if all_day else 0, sd, ed, tz, status, ext_updated, ev.get("etag"), ev.get("recurring_event_id"),
             ev.get("link"), now, 1 if moved else 0, 1 if moved else 0, cur["id"]),
        )
        conn.commit()
        return "updated"

    def mark_missing_external(self, account_id: str, seen_external_ids: Iterable[str],
                              start_ts: float, end_ts: float) -> int:
        """Drop the mirrored events of this account inside the window that the provider no
        longer returned: they were deleted there. Rows with a change still to push are kept
        (the push decides). Returns the number removed."""
        seen = {str(i) for i in seen_external_ids if i}
        rows = self._conn().execute(
            "SELECT id, external_id FROM events WHERE account_id=? AND external_id IS NOT NULL AND sync_state='synced' "
            "AND start_ts >= ? AND start_ts < ?", (account_id, float(start_ts), float(end_ts))).fetchall()
        gone = [r["id"] for r in rows if r["external_id"] not in seen]
        if gone:
            conn = self._conn()
            conn.executemany("DELETE FROM events WHERE id=?", [(i,) for i in gone])
            conn.commit()
        return len(gone)

    def pending_pushes(self, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Events owed to the provider (pending_push, pending_delete), oldest change first."""
        clauses = ["sync_state IN ('pending_push', 'pending_delete')"]
        params: List[Any] = []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        rows = self._conn().execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY updated_at ASC", params).fetchall()
        return [self._row(r) for r in rows]

    def mark_pushed(self, event_id: str, *, external_id: str, external_updated: Optional[float],
                    etag: Optional[str], link: Optional[str], external_calendar_id: Optional[str] = None) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE events SET sync_state='synced', external_id=?, external_updated=?, etag=?, link=COALESCE(?, link), "
            "external_calendar_id=COALESCE(?, external_calendar_id), push_attempts=0, last_error=NULL WHERE id=?",
            (external_id, external_updated, etag, link, external_calendar_id, str(event_id)))
        conn.commit()

    def mark_push_failed(self, event_id: str, error: str, *, cap: int = PUSH_MAX_ATTEMPTS) -> str:
        """Count a failed push; after `cap` attempts the event is parked as push_failed
        (visible, retried only after the next local edit). Returns the resulting state."""
        cur = self.get_event(event_id)
        if not cur:
            return "gone"
        attempts = int(cur.get("push_attempts") or 0) + 1
        state = cur["sync_state"] if attempts < cap else "push_failed"
        conn = self._conn()
        conn.execute("UPDATE events SET push_attempts=?, last_error=?, sync_state=? WHERE id=?",
                     (attempts, (error or "")[:500], state, cur["id"]))
        conn.commit()
        return state

    def detach_account(self, account_id: str) -> int:
        """An account that is gone: its mirrored events stay, read-only and local; pending
        deletes are honoured locally; its state row goes; a push target pointing at it is
        cleared. Returns the number of events kept."""
        conn = self._conn()
        conn.execute("DELETE FROM events WHERE account_id=? AND sync_state='pending_delete'", (account_id,))
        cur = conn.execute("SELECT COUNT(*) AS n FROM events WHERE account_id=?", (account_id,))
        kept = int(dict(cur.fetchone() or {}).get("n") or 0)
        conn.execute("UPDATE events SET account_id=NULL, external_id=NULL, external_calendar_id=NULL, external_updated=NULL, "
                     "etag=NULL, sync_state='local_only', push_attempts=0, last_error=? WHERE account_id=?",
                     ("account removed", account_id))
        conn.execute("DELETE FROM accounts WHERE account_id=?", (account_id,))
        conn.commit()
        if self.settings().get("push_target") == account_id:
            self.set_settings(push_target="")
        return kept

    # ── accounts and settings ────────────────────────────────────────────────

    def account_state(self, account_id: str) -> Dict[str, Any]:
        row = self._conn().execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if not row:
            return {"account_id": account_id, "enabled": True, "last_sync_at": None, "last_error": None,
                    "needs_reconsent": False, "sync_state": {}}
        d = dict(row)
        d["enabled"] = bool(d.get("enabled", 1))
        d["needs_reconsent"] = bool(d.get("needs_reconsent"))
        try:
            d["sync_state"] = json.loads(d.get("sync_state") or "{}")
        except (TypeError, ValueError):
            d["sync_state"] = {}
        return d

    def list_account_states(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute("SELECT account_id FROM accounts ORDER BY account_id").fetchall()
        return [self.account_state(r["account_id"]) for r in rows]

    def _ensure_account(self, account_id: str) -> None:
        self._conn().execute("INSERT OR IGNORE INTO accounts(account_id) VALUES (?)", (account_id,))

    def set_account_enabled(self, account_id: str, enabled: bool) -> None:
        conn = self._conn()
        self._ensure_account(account_id)
        conn.execute("UPDATE accounts SET enabled=? WHERE account_id=?", (1 if enabled else 0, account_id))
        conn.commit()

    def mark_account_synced(self, account_id: str, *, ts: Optional[float] = None, error: Optional[str] = None,
                            needs_reconsent: Optional[bool] = None, sync_state: Optional[Dict[str, Any]] = None) -> None:
        conn = self._conn()
        self._ensure_account(account_id)
        sets, params = ["last_error=?"], [(error or "")[:500] or None]
        if error is None:
            sets.append("last_sync_at=?")
            params.append(float(ts if ts is not None else _now()))
        if needs_reconsent is not None:
            sets.append("needs_reconsent=?")
            params.append(1 if needs_reconsent else 0)
        if sync_state is not None:
            sets.append("sync_state=?")
            params.append(json.dumps(sync_state))
        params.append(account_id)
        conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE account_id=?", params)
        conn.commit()

    def settings(self) -> Dict[str, Any]:
        rows = self._conn().execute("SELECT key, value FROM settings").fetchall()
        raw = {r["key"]: r["value"] for r in rows}
        try:
            default_rm = int(raw.get("default_reminder_minutes", DEFAULT_REMINDER_MINUTES))
        except (TypeError, ValueError):
            default_rm = DEFAULT_REMINDER_MINUTES
        return {"push_target": (raw.get("push_target") or "").strip() or None,
                "default_reminder_minutes": max(0, default_rm)}

    def set_settings(self, *, push_target: Any = _UNSET, default_reminder_minutes: Any = _UNSET) -> Dict[str, Any]:
        conn = self._conn()
        if push_target is not _UNSET:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('push_target', ?)", ((push_target or "").strip(),))
        if default_reminder_minutes is not _UNSET:
            try:
                rm = max(0, int(default_reminder_minutes or 0))
            except (TypeError, ValueError):
                rm = DEFAULT_REMINDER_MINUTES
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('default_reminder_minutes', ?)", (str(rm),))
        conn.commit()
        return self.settings()

    # ── reminders (read by the scheduler tick) ───────────────────────────────

    def due_reminders(self, now_ts: float) -> List[Dict[str, Any]]:
        """Confirmed events whose reminder time has come and that were neither fired nor
        marked missed. The caller decides fired versus missed by its grace window."""
        rows = self._conn().execute(
            "SELECT * FROM events WHERE reminder_minutes IS NOT NULL AND status='confirmed' "
            "AND sync_state != 'pending_delete' AND reminder_fired_at IS NULL AND reminder_missed_at IS NULL "
            "AND (start_ts - reminder_minutes * 60) <= ? ORDER BY start_ts ASC", (float(now_ts),)).fetchall()
        return [self._row(r) for r in rows]

    def mark_reminder(self, event_id: str, outcome: str, ts: Optional[float] = None) -> None:
        col = "reminder_fired_at" if outcome == "fired" else "reminder_missed_at"
        conn = self._conn()
        conn.execute(f"UPDATE events SET {col}=? WHERE id=?", (float(ts if ts is not None else _now()), str(event_id)))
        conn.commit()

    # ── migration of the contact-book events ─────────────────────────────────

    def _migrate_contact_events(self) -> None:
        """Move the events older contact records kept inside contacts.json into this
        calendar, once per store. Each becomes a VAF event linked to its contact and keeps
        the old id in legacy_id, so a rerun finds them; the record's list is emptied and
        saved only when something moved. A failure leaves the marker unset and is retried
        on the next construction."""
        if self._meta("contacts_migrated_at"):
            return
        try:
            from vaf.core import contacts_store as cs
            contacts = cs._load_all(None, self.user_scope_id)
            moved = 0
            conn = self._conn()
            for c in contacts:
                events = c.get("events") if isinstance(c.get("events"), list) else []
                if not events:
                    continue
                for e in events:
                    if not isinstance(e, dict):
                        continue
                    legacy_id = str(e.get("id") or "")
                    if legacy_id and conn.execute("SELECT 1 FROM events WHERE legacy_id=?", (legacy_id,)).fetchone():
                        continue
                    try:
                        when = float(e.get("when_ts") or 0)
                    except (TypeError, ValueError):
                        when = 0.0
                    if not when:
                        continue
                    self.add_event(title=str(e.get("title") or ""), start_ts=when, end_ts=when + 3600.0,
                                   description=str(e.get("note") or ""), contact_ids=[c.get("id")],
                                   created_by=str(e.get("source") or "user"), reminder_minutes=None,
                                   created_at=float(e.get("ts") or when), legacy_id=legacy_id or None,
                                   sync_state="local_only")
                c["events"] = []
                moved += 1
            if moved:
                cs._save_all(contacts, None, self.user_scope_id)
            self._set_meta("contacts_migrated_at", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.warning("calendar_store: contact-event migration skipped for now: %s", e)


# ── conveniences for callers that hold an identity pair ─────────────────────────

def scope_for(username: Optional[str], user_scope_id: Optional[str]) -> str:
    """The scope a store is keyed by: the given one, else the local admin's (the only
    identity that may reach a store without naming a scope)."""
    scope = str(user_scope_id or "").strip()
    if scope:
        return scope
    from vaf.core.config import get_local_admin_scope_id
    return get_local_admin_scope_id()


def store_for(username: Optional[str], user_scope_id: Optional[str]) -> CalendarStore:
    return CalendarStore(scope_for(username, user_scope_id))


def store_exists(username: Optional[str], user_scope_id: Optional[str]) -> bool:
    return CalendarStore.exists(scope_for(username, user_scope_id))


def push_enabled() -> bool:
    """The admin's kill switch for outbound writes (calendar_sync_push_enabled)."""
    try:
        from vaf.core.config import Config
        return bool(Config.get("calendar_sync_push_enabled", True))
    except Exception:
        return True
