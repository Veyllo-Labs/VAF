# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""MailService: the fail-closed facade routes and tools talk to.

Scoping rule (EMAIL_CLIENT.md): every entry point requires an explicit
user_scope_id; there is no default and no admin fallback. HTML mail is
sanitized HERE, at the trust boundary, with nh3 (Rust/ammonia): scripts,
event handlers and dangerous URL schemes are stripped; remote images are
BLOCKED by default (tracking protection) and reported via blocked_remote so
the UI can offer an explicit opt-in; cid: inline images are rewritten to the
authenticated attachment endpoint. The UI must still render the result inside
a sandboxed iframe with CSP script-src 'none' (defense in layers)."""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from vaf.mail.parser import parse_message
from vaf.mail.store import MailStore

logger = logging.getLogger("vaf.mail.service")

_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "cite", "code",
    "col", "colgroup", "dd", "div", "dl", "dt", "em", "figcaption", "figure",
    "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol",
    "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ALLOWED_ATTRS = {
    "*": {"style", "align", "valign", "width", "height", "dir", "lang"},
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "table": {"cellpadding", "cellspacing", "border"},
    "font": {"color", "face", "size"},
    "col": {"span"},
}
_REMOTE_URL = re.compile(r"^\s*(https?:)?//", re.IGNORECASE)
# Any inline style that can reference an external resource. Three lessons are
# baked in here, each of them a hole this filter had:
#   1. `image-set()` / `-webkit-image-set()` / `src()` fetch a URL with no `url(`
#      token at all, so matching only `url(` misses them entirely.
#   2. A SINGLE backslash is a CSS escape (`u\72 l(` renders as `url(`), so the
#      backslash alternative must be `\\` (one literal backslash). It used to be
#      `\\\\`, which requires TWO - the escape the comment claimed to catch
#      sailed straight through.
#   3. Anything matched here is dropped AND counted, so the reader sees the
#      "external content blocked" banner instead of a silently gutted mail.
_STYLE_URL = re.compile(
    r"url\s*\(|image-set\s*\(|src\s*\(|expression\s*\(|@import|\\",
    re.IGNORECASE)


def _agent_row(m: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy row shape the agent mail tools consume. Single source (P3.2,
    moved off tool_bridge); the field set matches email_sync_store exactly so the
    tools' output stays byte-identical when they repoint to MailService."""
    from datetime import datetime, timezone
    ts = m.get("date_ts") or m.get("internaldate_ts")
    iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None
    return {
        "account_id": m.get("acct") or "",
        "folder": m.get("folder_name") or "INBOX",
        "message_id": m.get("message_id") or f"pk-{m.get('id')}",
        "category": m.get("category") or "primary",
        "provider_message_id": m.get("gm_msgid") or "",
        "subject": m.get("subject") or "",
        "from": m.get("from_addr") or "",
        "date": iso or "",
        "message_date_iso": iso,
        "body_snippet": m.get("snippet") or "",
        "synced_at": m.get("created_at") or "",
        "answered_at": (m.get("answered_at") or "").strip() if m.get("answered_at") else "",
    }


class MailService:
    def __init__(self, user_scope_id: str):
        scope = str(user_scope_id or "").strip()
        if not scope:
            raise ValueError("MailService requires an explicit user_scope_id (fail-closed)")
        self.user_scope_id = scope
        self.store = MailStore(scope)

    # ── listing / search (thin store passthrough) ──────────────────────────

    def list_threads(self, **kw) -> List[Dict[str, Any]]:
        return self.store.list_threads(**kw)

    def list_messages(self, **kw) -> List[Dict[str, Any]]:
        return self.store.list_messages(**kw)

    def thread_messages(self, thread_id: int) -> List[Dict[str, Any]]:
        return self.store.thread_messages(thread_id)

    def search(self, query: str, **kw) -> List[Dict[str, Any]]:
        return self.store.search(query, **kw)

    def annotate_visibility(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Add suspicious_for_agent / suspicious_reasons to UI rows using the SSOT
        phishing scorer (field shim: v2 from_addr->from, snippet->body_snippet). The
        reader surfaces this as a warning banner; the agent tools hide these mails
        entirely - re-surfacing it here is the safety layer MailDashboard had (P5.1)."""
        from vaf.tools.mail_utils import annotate_messages_with_agent_visibility
        shimmed = [{"from": r.get("from_addr") or r.get("from") or "",
                    "subject": r.get("subject") or "",
                    "body_snippet": r.get("snippet") or r.get("body_snippet") or "",
                    "category": r.get("category") or ""} for r in rows]
        for r, a in zip(rows, annotate_messages_with_agent_visibility(shimmed)):
            r["suspicious_for_agent"] = a.get("suspicious_for_agent", False)
            r["suspicious_reasons"] = a.get("suspicious_reasons", [])
            # the score is what the Overview security panel ranks by; the scorer
            # produces it, so pass it through rather than let that panel guess
            r["suspicious_score"] = a.get("suspicious_score", 0)
        return rows

    def counts(self, **kw) -> Dict[str, int]:
        return self.store.counts(**kw)

    def folders(self, account_id: str) -> List[Dict[str, Any]]:
        apk = self.store.account_pk(account_id)
        return self.store.list_folders(apk) if apk else []

    # ── agent-facing API (P3.2): legacy-row lists, on-demand body, metadata.
    #    The tools repoint onto these in P3.3-P3.5; shipped unused here. ──

    def list_for_agent(self, account_id: Optional[str] = None, folder: Optional[str] = None,
                       category: Optional[str] = None, limit: int = 50,
                       offset: int = 0) -> List[Dict[str, Any]]:
        cat = None if (category or "").strip() in ("", "all") else category
        rows = self.store.list_messages(account_id=account_id or None, folder=folder or None,
                                        category=cat, limit=limit, offset=offset)
        return [_agent_row(m) for m in rows]

    def search_for_agent(self, query: str, account_id: Optional[str] = None,
                         limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.store.search(query, account_id=account_id or None, limit=limit)
        return [_agent_row(m) for m in rows]

    def find_pk_by_message_id(self, message_id: str, account_id: Optional[str] = None) -> Optional[int]:
        return self.store.pk_by_message_id(message_id, account_id=account_id)

    def message_from_addr(self, account_id: str, message_id: str) -> Optional[str]:
        pk = self.store.pk_by_message_id(message_id, account_id=account_id)
        return (self.store.get_message(pk) or {}).get("from_addr") if pk else None

    def set_category(self, account_id: str, message_id: str, category: str) -> bool:
        pk = self.store.pk_by_message_id(message_id, account_id=account_id)
        if pk is None:
            return False
        self.store.set_category(pk, category)
        return True

    def relabel(self, message_pk: int, category: str) -> Optional[str]:
        """Local-only category relabel by pk (the UI has the message pk). Returns
        the normalized category, or None if the message does not exist. Category is
        a local classification (Gmail-style tabs); nothing is written to the server,
        so this is NOT gated by mail_engine_write_enabled."""
        cat = re.sub(r"\s+", "_", str(category or "").strip().lower())[:64] or "primary"
        if self.store.get_message(message_pk) is None:
            return None
        self.store.set_category(message_pk, cat)
        return cat

    def apply_sender_rules_backfill(self, username: Optional[str] = None) -> int:
        """Re-apply the sender->category rules (config blob, SSOT) to EVERY stored
        message; returns the count whose category changed. This is the backfill the
        classic dashboard ran so a relabel reaches existing mail of the same sender."""
        from vaf.core.email_accounts import apply_sender_rules_to_category
        updated = 0
        for row in self.store.list_for_relabel():
            cur = row.get("category") or "primary"
            new = apply_sender_rules_to_category(
                row.get("from_addr") or "", cur, username, self.user_scope_id)
            new = re.sub(r"\s+", "_", str(new or "primary").strip().lower())[:64] or "primary"
            if new != cur:
                self.store.set_category(int(row["pk"]), new)
                updated += 1
        return updated

    def relabel_and_learn(self, message_pk: int, category: str,
                          username: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Relabel one message, then (legacy parity, deliberate) add a
        sender rule for its From address and backfill every mail from that sender.
        Returns {category, updated} or None if the message is unknown."""
        from vaf.core.email_accounts import pattern_from_from_addr, upsert_sender_rule
        cat = self.relabel(message_pk, category)
        if cat is None:
            return None
        msg = self.store.get_message(message_pk) or {}
        updated = 1
        pattern = pattern_from_from_addr(msg.get("from_addr") or "")
        if pattern:
            upsert_sender_rule(pattern, cat, username=username, user_scope_id=self.user_scope_id)
            updated += self.apply_sender_rules_backfill(username=username)
        return {"category": cat, "updated": updated}

    def mark_answered(self, account_id: str, message_id: str, at: Optional[str] = None) -> bool:
        pk = self.store.pk_by_message_id(message_id, account_id=account_id)
        if pk is None:
            return False
        self.store.set_answered(pk, at)
        return True

    def body_text(self, message_id: str, account_id: Optional[str] = None,
                  cred_username: Optional[str] = None) -> Optional[str]:
        """Plain-text body by Message-ID: served from the cached raw, else fetched
        on demand from the server. None when the message is unknown locally."""
        pk = self.store.pk_by_message_id(message_id, account_id=account_id)
        return self.ensure_body(pk, cred_username=cred_username) if pk else None

    def ensure_body(self, pk: int, cred_username: Optional[str] = None) -> Optional[str]:
        """The message's plain-text body, fetching + caching the raw from the server
        if it is not cached yet (UID FETCH BODY.PEEK[])."""
        raw = self.store.get_raw(pk)
        if raw is None:
            raw = self._fetch_raw_on_demand(pk, cred_username)
        return (parse_message(raw).body_text or None) if raw else None

    def _fetch_raw_on_demand(self, pk: int, cred_username: Optional[str] = None) -> Optional[bytes]:
        acct, folder, uid = self.store.message_location(pk)
        if not acct or not folder or not uid:
            return None
        from vaf.core.email_accounts import get_account
        from vaf.mail.imap_client import _safe_logout, build_imap_client
        acc = get_account(acct, cred_username, user_scope_id=self.user_scope_id)
        if not acc:
            return None
        client = None
        try:
            client = build_imap_client(acc, cred_username, self.user_scope_id)
            client.select_folder(folder, readonly=True)
            data = client.fetch([int(uid)], ["BODY.PEEK[]"]).get(int(uid)) or {}
            raw = data.get(b"BODY[]") or data.get("BODY[]")
            if raw:
                self.store.cache_raw(pk, bytes(raw))
                return bytes(raw)
            return None
        except Exception as e:
            logger.warning("on-demand body fetch failed for pk=%s: %s", pk, e)
            return None
        finally:
            if client is not None:
                _safe_logout(client)

    # ── body rendering ─────────────────────────────────────────────────────

    def get_body(self, message_pk: int, allow_remote: bool = False) -> Optional[Dict[str, Any]]:
        """Sanitized body for the UI. Returns {html?, text, blocked_remote,
        attachments, cached}. html is nh3-sanitized with remote content
        blocked; None when the message has no HTML part or no cached raw."""
        msg = self.store.get_message(message_pk)
        if msg is None:
            return None
        raw = self.store.get_raw(message_pk)
        if raw is None:
            return {"html": None, "text": msg.get("snippet") or "", "blocked_remote": 0,
                    "attachments": self.store.list_attachments(message_pk),
                    "cached": False, "body_state": msg.get("body_state")}
        parsed = parse_message(raw)
        html, blocked = (None, 0)
        if parsed.body_html:
            html, blocked = self._sanitize_html(parsed.body_html, message_pk,
                                                allow_remote=allow_remote)
        return {"html": html, "text": parsed.body_text, "blocked_remote": blocked,
                "attachments": self.store.list_attachments(message_pk),
                "cached": True, "body_state": msg.get("body_state")}

    def _sanitize_html(self, dirty: str, message_pk: int,
                       allow_remote: bool = False) -> Tuple[str, int]:
        import nh3
        blocked = {"n": 0}

        def _attr_filter(element: str, attribute: str, value: str):
            if attribute == "style":
                # Inline styles may not reference external resources. Counting the
                # drop matters: a mail whose only tracker sits in CSS used to come
                # back with blocked_remote == 0, so the client showed no banner and
                # the reader had no idea anything had been removed. There is no
                # opt-in path for CSS URLs (allow_remote only rewrites img@src), so
                # this count can legitimately stay > 0 after loading images - the
                # client already tolerates a residual count.
                if _STYLE_URL.search(value or ""):
                    blocked["n"] += 1
                    return None
                return value
            if element == "img" and attribute == "src":
                v = (value or "").strip()
                if v.lower().startswith("cid:"):
                    # cid is attacker-controlled: strict charset + URL-encoding,
                    # otherwise a crafted cid ("../..", "?", "#") turns the img
                    # into an authenticated GET against an arbitrary API path.
                    cid = v[4:].strip("<>")
                    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,256}", cid):
                        blocked["n"] += 1
                        return None
                    from urllib.parse import quote
                    return f"/api/mail/messages/{int(message_pk)}/parts/{quote(cid, safe='')}"
                if _REMOTE_URL.match(v) or v.lower().startswith("data:"):
                    if v.lower().startswith("data:image/"):
                        return v  # small inline data images are self-contained
                    if v.lower().startswith("data:"):
                        blocked["n"] += 1
                        return None
                    if allow_remote:
                        # Explicit opt-in: remote images ride the server-side proxy
                        # (SSRF-guarded, image-only), which strips the reader's
                        # browser identity - no Referer, cookies, User-Agent or
                        # Accept-Language reach the sender.
                        # It does NOT make the load anonymous, and this comment
                        # used to claim it did. The backend runs on the reader's
                        # own machine, so the sender sees the same egress IP it
                        # would have seen from the browser; and the tracking URL is
                        # forwarded verbatim, so a per-recipient token still
                        # reports "this person opened it, now". See the tracking
                        # section in docs/integrations/EMAIL_CLIENT.md.
                        from urllib.parse import quote
                        u = v if v.lower().startswith("http") else f"https:{v}"
                        return f"/api/mail/image-proxy?url={quote(u, safe='')}"
                    blocked["n"] += 1
                    return None
                blocked["n"] += 1
                return None
            if element == "a" and attribute == "href":
                v = (value or "").strip()
                if v.lower().startswith(("javascript:", "vbscript:", "data:")):
                    return None
                return v
            return value

        clean = nh3.clean(
            dirty,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            attribute_filter=_attr_filter,
            link_rel="noopener noreferrer nofollow",
            # "data" must pass the scheme gate so the attribute filter can keep
            # data:image/* (self-contained) while dropping every other data: use.
            url_schemes={"http", "https", "mailto", "cid", "data"},
        )
        return clean, blocked["n"]

    # ── phase 2: local-first verbs + outbox ────────────────────────────────

    def _msg_ctx(self, message_pk: int):
        msg = self.store.get_message(message_pk)
        if not msg:
            return None, None, None
        conn = self.store._conn()
        folder = conn.execute("SELECT * FROM folders WHERE id=?",
                              (msg["folder_id"],)).fetchone()
        account = conn.execute("SELECT * FROM accounts WHERE id=?",
                               (msg["account_id"],)).fetchone()
        return msg, (dict(folder) if folder else None), (dict(account) if account else None)

    def _enqueue_flag_op(self, msg, folder, add=(), remove=()) -> None:
        if not folder:
            return
        # Carry the stable local pk so the replay resolves the CURRENT server
        # coordinates even if the row moved since enqueue. Right after a local
        # move the uid is NULL; the op then defers until a sync adopts a uid,
        # instead of being silently dropped (the flag intent is not lost).
        self.store.enqueue_op(int(msg["account_id"]), "flags", {
            "folder": folder["name"],
            "message_pk": int(msg["id"]),
            "uid": int(msg["uid"]) if msg.get("uid") is not None else None,
            "uidvalidity": folder.get("uidvalidity"),
            "add": list(add), "remove": list(remove)})

    def mark_read(self, message_pk: int, read: bool = True) -> Optional[List[str]]:
        msg, folder, _ = self._msg_ctx(message_pk)
        if not msg:
            return None
        flags = self.store.set_local_flags(
            message_pk, add=["\\Seen"] if read else (), remove=() if read else ["\\Seen"])
        self._enqueue_flag_op(msg, folder,
                              add=["\\Seen"] if read else (),
                              remove=() if read else ["\\Seen"])
        return flags

    def set_star(self, message_pk: int, starred: bool = True) -> Optional[List[str]]:
        msg, folder, _ = self._msg_ctx(message_pk)
        if not msg:
            return None
        flags = self.store.set_local_flags(
            message_pk, add=["\\Flagged"] if starred else (),
            remove=() if starred else ["\\Flagged"])
        self._enqueue_flag_op(msg, folder,
                              add=["\\Flagged"] if starred else (),
                              remove=() if starred else ["\\Flagged"])
        return flags

    def _move_to_special(self, message_pk: int, special_use: str) -> Dict[str, Any]:
        """Local-first move to a special folder + queued server MOVE. Delete
        semantics are trash-only by design (EXPUNGE exists only behind
        'Empty Trash', which phase 2 does not expose)."""
        msg, folder, account = self._msg_ctx(message_pk)
        if not msg or not folder:
            return {"ok": False, "error": "message not found"}
        dest = self.store.find_special_folder(int(msg["account_id"]), special_use)
        if not dest:
            return {"ok": False, "error": f"no {special_use} folder discovered yet - run a sync"}
        if dest["id"] == folder["id"]:
            return {"ok": True, "noop": True}
        uid = msg.get("uid")
        self.store.move_message_local(message_pk, int(dest["id"]))
        if uid is not None:
            # Pin the source folder's UIDVALIDITY so the replay can detect a
            # server-side rotation between enqueue and replay (uid would then
            # denote a DIFFERENT message).
            self.store.enqueue_op(int(msg["account_id"]), "move", {
                "folder": folder["name"], "dest": dest["name"], "uid": int(uid),
                "uidvalidity": folder.get("uidvalidity")})
        return {"ok": True, "dest": dest["name"]}

    def archive(self, message_pk: int) -> Dict[str, Any]:
        out = self._move_to_special(message_pk, "\\Archive")
        if not out.get("ok"):
            # Gmail: Archive = out of INBOX into All Mail
            out = self._move_to_special(message_pk, "\\All")
        return out

    def trash(self, message_pk: int) -> Dict[str, Any]:
        return self._move_to_special(message_pk, "\\Trash")

    def reply_prefill(self, message_pk: int, reply_all: bool = False,
                      own_addresses: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Composer prefill for reply/reply-all: recipients, subject, quoted
        body, threading headers."""
        from vaf.mail import compose
        msg, _, account = self._msg_ctx(message_pk)
        if not msg:
            return None
        body = self.get_body(message_pk) or {}
        refs_row = [r["ref_id"] for r in self.store._conn().execute(
            "SELECT ref_id FROM msg_refs WHERE message_pk=? AND ref_id != ?",
            (message_pk, msg.get("message_id") or "")).fetchall()]
        own = list(own_addresses or [])
        if account:
            own.append(account.get("email") or account.get("account_id") or "")
        rcpt = compose.reply_recipients(msg["from_addr"], msg["to_addrs"], msg["cc_addrs"],
                                        None, own, reply_all)
        headers = compose.reply_reference_headers(msg.get("message_id") or "", refs_row)
        quoted = compose.quote_reply(msg["from_addr"], msg.get("date_ts"),
                                     body.get("text") or msg.get("snippet") or "")
        return {"account_id": (account or {}).get("account_id"),
                "to": rcpt["to"], "cc": rcpt["cc"],
                "subject": compose.reply_subject(msg["subject"]),
                "body": f"\n\n{quoted}", **headers}

    def forward_prefill(self, message_pk: int) -> Optional[Dict[str, Any]]:
        from vaf.mail import compose
        msg, _, account = self._msg_ctx(message_pk)
        if not msg:
            return None
        body = self.get_body(message_pk) or {}
        atts = [a for a in (body.get("attachments") or []) if not a.get("is_inline")]
        note = ""
        if atts:
            names = ", ".join(a.get("filename") or "attachment" for a in atts[:5])
            note = f"\n[Original attachments not included: {names}]"
        block = compose.forward_block(msg["from_addr"], msg["to_addrs"], msg.get("date_ts"),
                                      msg["subject"], (body.get("text") or "") + note)
        return {"account_id": (account or {}).get("account_id"), "to": "", "cc": "",
                "subject": compose.forward_subject(msg["subject"]),
                "body": f"\n\n{block}", "in_reply_to": "", "references": ""}

    def queue_send(self, account_id: str, to: str, subject: str, body: str,
                   cc: str = "", bcc: str = "", in_reply_to: str = "",
                   references: str = "", undo_seconds: int = 15) -> Dict[str, Any]:
        """Undo-send outbox (client-delay model): the op becomes runnable after
        undo_seconds; until then cancel_send withdraws it. Survives restarts -
        the supervisor sweep delivers held ops whose delay passed."""
        from datetime import datetime, timezone
        from vaf.mail import compose
        apk = self.store.account_pk(account_id)
        if apk is None:
            apk = self.store.upsert_account(account_id, "imap", account_id)
        # From = the account's real address (not the account_id identifier); the
        # Bcc goes into the stored Sent copy so the sender keeps the record.
        acc = next((a for a in self.store.list_accounts()
                    if a.get("account_id") == account_id), None)
        from_addr = (acc or {}).get("email") or account_id
        msg = compose.build_message(from_addr, to, subject, body, cc=cc or None,
                                    bcc=bcc or None,
                                    in_reply_to=in_reply_to or None,
                                    references=references or None)
        # Carry the compose Message-ID so the DELIVERED mail is sent with this
        # exact id (transport message_id=), making the delivered mail and the
        # Sent copy one RFC822 entity - replies then thread correctly.
        message_id = msg["Message-ID"]
        import base64 as _b64
        not_before = int(datetime.now(timezone.utc).timestamp()) + max(0, int(undo_seconds))
        op_id = self.store.enqueue_op(apk, "send", {
            "account_id": account_id, "to": to, "cc": cc, "bcc": bcc,
            "subject": subject, "body": body,
            "in_reply_to": in_reply_to, "references": references,
            "message_id": message_id,
            "raw_b64": _b64.b64encode(bytes(msg)).decode("ascii"),
        }, not_before_ts=not_before)
        # undo_seconds is the DURATION the client counts down (server-relative);
        # the client uses it instead of (undo_until_ts - client_now) so a skewed
        # browser clock cannot make the undo snackbar vanish early or linger.
        return {"ok": True, "op_id": op_id, "undo_until_ts": not_before,
                "undo_seconds": max(0, int(undo_seconds))}

    def cancel_send(self, op_id: int) -> bool:
        op = self.store.get_op(int(op_id))
        if not op or op.get("kind") != "send":
            return False
        return self.store.cancel_op(int(op_id))

    # ── attachments ────────────────────────────────────────────────────────

    def get_attachment(self, message_pk: int, part_ref: str) -> Optional[Tuple[str, str, bytes]]:
        """(filename, content_type, payload) by part_id or Content-ID. Served
        from the cached raw message only - no live fetch here."""
        raw = self.store.get_raw(message_pk)
        if raw is None:
            return None
        from email import policy
        from email.parser import BytesParser
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception:
            return None
        index = 0
        for part in msg.walk():
            index += 1
            if part.is_multipart():
                continue
            cid = (part.get("Content-ID") or "").strip().strip("<>")
            if str(index) == part_ref or (cid and cid == part_ref):
                payload = part.get_payload(decode=True) or b""
                filename = part.get_filename() or f"part-{index}"
                ctype = part.get_content_type() or "application/octet-stream"
                return filename, ctype, payload
        return None
