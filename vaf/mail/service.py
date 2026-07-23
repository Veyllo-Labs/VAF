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
# backslash catches CSS escapes like u\72 l( that dodge the literal patterns
_STYLE_URL = re.compile(r"url\s*\(|expression\s*\(|@import|\\\\", re.IGNORECASE)


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

    def counts(self, **kw) -> Dict[str, int]:
        return self.store.counts(**kw)

    def folders(self, account_id: str) -> List[Dict[str, Any]]:
        apk = self.store.account_pk(account_id)
        return self.store.list_folders(apk) if apk else []

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
                # inline styles may not reference external resources
                return None if _STYLE_URL.search(value or "") else value
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
                        # explicit opt-in: remote images ride the server-side
                        # proxy (SSRF-guarded, image-only) so the sender never
                        # sees the reader's IP and trackers die at the proxy
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
        if msg.get("uid") is None or not folder:
            return  # server coordinates unknown (e.g. just moved locally); the
            # regular resync reconciles once the move replayed
        self.store.enqueue_op(int(msg["account_id"]), "flags", {
            "folder": folder["name"], "uid": int(msg["uid"]),
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
            self.store.enqueue_op(int(msg["account_id"]), "move", {
                "folder": folder["name"], "dest": dest["name"], "uid": int(uid)})
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
        msg = compose.build_message(account_id, to, subject, body, cc=cc or None,
                                    in_reply_to=in_reply_to or None,
                                    references=references or None)
        import base64 as _b64
        not_before = int(datetime.now(timezone.utc).timestamp()) + max(0, int(undo_seconds))
        op_id = self.store.enqueue_op(apk, "send", {
            "account_id": account_id, "to": to, "cc": cc, "bcc": bcc,
            "subject": subject, "body": body,
            "in_reply_to": in_reply_to, "references": references,
            "raw_b64": _b64.b64encode(bytes(msg)).decode("ascii"),
        }, not_before_ts=not_before)
        return {"ok": True, "op_id": op_id, "undo_until_ts": not_before}

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
