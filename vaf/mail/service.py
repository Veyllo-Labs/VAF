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

#: What a held draft is told when its last attempt handed the mail to the server and never
#: heard back. The ledger's word for that is `ambiguous`, and it is final for the draft: SMTP
#: has no idempotency key, so nobody may send it again on the person's behalf.
AMBIGUOUS_DRAFT = ("The last attempt was interrupted after the mail was handed to the server, so "
                   "it may already have been delivered. It is not sent again: check the Sent "
                   "folder, drop the draft if it arrived, and ask for it again if it did not.")

#: What a held draft is told when its account is not in the mail configuration any more.
#: Nothing can deliver it in that state, the sweep included, so it stays held rather than
#: becoming a pending op nobody drains.
NO_ACCOUNT_FOR_DRAFT = ("This draft was written for a mail account that is not set up any more. "
                        "Add the account again to send it, or discard the draft.")

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
        # The verdict summary rides along so the agent-facing tools and the phishing
        # filter can read it; a row from a store without verdicts carries an empty dict.
        "auth": dict(m.get("auth") or {}),
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
        self.attach_auth(rows)
        shimmed = [{"from": r.get("from_addr") or r.get("from") or "",
                    "subject": r.get("subject") or "",
                    "body_snippet": r.get("snippet") or r.get("body_snippet") or "",
                    "category": r.get("category") or "",
                    "auth": r.get("auth") or {}} for r in rows]
        for r, a in zip(rows, annotate_messages_with_agent_visibility(shimmed)):
            r["suspicious_for_agent"] = a.get("suspicious_for_agent", False)
            r["suspicious_reasons"] = a.get("suspicious_reasons", [])
            # the score is what the Overview security panel ranks by; the scorer
            # produces it, so pass it through rather than let that panel guess
            r["suspicious_score"] = a.get("suspicious_score", 0)
        return rows

    def attach_auth(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """`auth` on every row: the verdict summary (vaf.mail.verification.summary) of the
        message, or of a thread row's newest message. One query for the whole list; a
        message never assessed reads as state unknown, machine kind empty."""
        from vaf.mail.verification import summary
        pks: List[int] = []
        for r in rows:
            pk = r.get("newest_pk") if "thread_id" in r and "newest_pk" in r else r.get("id")
            try:
                pks.append(int(pk))
            except (TypeError, ValueError):
                continue
        verdicts = self.store.message_auth(pks) if pks else {}
        for r in rows:
            pk = r.get("newest_pk") if "thread_id" in r and "newest_pk" in r else r.get("id")
            try:
                r["auth"] = summary(verdicts.get(int(pk)))
            except (TypeError, ValueError):
                r["auth"] = summary(None)
        return rows

    def message_verdict(self, message_pk: int) -> Optional[Dict[str, Any]]:
        """The full stored verdict of one message (flags, reasons, the header snapshot)."""
        return self.store.message_auth([int(message_pk)]).get(int(message_pk))

    # ── verification: learning the provider, recomputing verdicts ──────────

    def learn_provider(self, account_id: str, *, automatic: bool = False) -> Dict[str, Any]:
        """What this account's inbox says about the provider's Authentication-Results
        header (`verification.learn_provider`): the majority authserv-id or the Microsoft
        profile, with the sample counts. Reads the stored verdicts only, never the server.
        `automatic` asks for the stricter evidence of a learn nobody watches."""
        from vaf.mail.verification import AUTO_LEARN_MIN_DOMAINS, AUTO_LEARN_MIN_SAMPLES, learn_provider
        apk = self.store.account_pk(account_id)
        if apk is None:
            return {"authserv_id": "", "profile": "rfc8601", "count": 0, "total": 0, "domains": 0}
        if automatic:
            return learn_provider(self.store.inbox_auth_samples(apk),
                                  min_samples=AUTO_LEARN_MIN_SAMPLES, min_domains=AUTO_LEARN_MIN_DOMAINS)
        return learn_provider(self.store.inbox_auth_samples(apk))

    def learn_sender_check(self, account: Dict[str, Any], username: Optional[str], *,
                           automatic: bool = False) -> Dict[str, Any]:
        """Set up sender verification for one account: learn the provider's id from the
        mailbox, save it on the account entry and re-assess every stored verdict under
        it. The one path for the account panel's button and for the learn after a sync.
        Returns {learned, saved, backfilled}; with too little evidence nothing is saved
        and `learned` carries the counts that say why."""
        from datetime import datetime, timezone

        from vaf.core.email_accounts import get_account, patch_account
        account_id = str(account.get("account_id") or account.get("email") or "")
        learned = self.learn_provider(account_id, automatic=automatic)
        saved, backfilled = False, 0
        if learned.get("authserv_id") or learned.get("profile") == "microsoft":
            fields = {
                "trusted_authserv_id": learned.get("authserv_id") or "",
                "auth_profile": learned.get("profile") or "rfc8601",
                "authserv_source": "mailbox",
                "authserv_learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "authserv_samples": int(learned.get("count") or 0),
            }
            saved = bool(patch_account(account_id, fields, username, user_scope_id=self.user_scope_id))
            if saved:
                backfilled = self.reassess(get_account(account_id, username, user_scope_id=self.user_scope_id))
        return {"learned": learned, "saved": saved, "backfilled": backfilled}

    def reassess(self, account: Optional[Dict[str, Any]]) -> int:
        """Recompute the account's verdicts that were computed under another policy than
        its current one. Never raises: a pass that fails leaves the old verdicts, and the
        next sync retries."""
        if not account:
            return 0
        try:
            from vaf.mail.verification import auth_policy_for_account
            aid = str(account.get("account_id") or account.get("email") or "")
            return self.backfill_verification(aid, auth_policy_for_account(account))
        except Exception as e:
            logger.warning("verification backfill failed for %s: %s",
                           str(account.get("account_id") or "")[:3] + "***", e)
            return 0

    def settle_verification(self, account: Dict[str, Any], username: Optional[str]) -> None:
        """After a sync: sender verification is on by default. An account with no
        trusted id yet learns it from its own inbox as soon as the evidence is strong
        enough (`learn_sender_check(automatic=True)`); either way, verdicts computed
        under an older policy (a learned id, a provider id VAF now knows, a changed rule)
        are recomputed. Nothing is learned where the policy already has its answer: an id
        set by hand or learned before, a provider id VAF knows, the Microsoft profile, or
        the profile "none" (the provider writes no header at all). Never raises."""
        try:
            from vaf.mail.verification import auth_policy_for_account
            policy = auth_policy_for_account(account)
            wants_learning = not policy["trusted_authserv_id"] and policy["auth_profile"] == "rfc8601"
            if wants_learning and self.learn_sender_check(account, username, automatic=True)["saved"]:
                return
            self.reassess(account)
        except Exception as e:
            logger.warning("sender verification pass failed for %s: %s",
                           str(account.get("account_id") or "")[:3] + "***", e)

    def backfill_verification(self, account_id: str, policy: Dict[str, Any], *, limit: int = 5000) -> int:
        """Recompute the verdicts of every message of the account whose verdict is missing
        or was computed under another policy (a newly learned authserv-id). Reads the
        stored header snapshot; a message never assessed is re-parsed from its cached raw
        bytes, and one without either keeps state unknown. Returns the number of rows
        written."""
        from vaf.mail.parser import parse_message
        from vaf.mail.verification import assess, parsed_from_snapshot, policy_key
        apk = self.store.account_pk(account_id)
        if apk is None:
            return 0
        key = policy_key(policy)
        done = 0
        for row in self.store.messages_for_verification(apk, policy_key=key, limit=limit):
            pk = int(row["id"])
            snapshot = row.get("headers") or {}
            parsed = None
            if snapshot:
                parsed = parsed_from_snapshot(snapshot, from_addr=row.get("from_addr") or "",
                                              message_id=row.get("message_id") or "",
                                              subject=row.get("subject") or "")
            else:
                raw = self.store.get_raw(pk)
                if raw:
                    parsed = parse_message(raw)
            if parsed is None:
                parsed = parsed_from_snapshot({}, from_addr=row.get("from_addr") or "",
                                              message_id=row.get("message_id") or "",
                                              subject=row.get("subject") or "")
            verdict = assess(parsed, policy=policy,
                             is_own_message_id=lambda mid, _a=apk: self.store.is_sent_id(_a, mid),
                             category=row.get("category") or "")
            self.store.write_message_auth(pk, verdict)
            done += 1
        return done

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
        return [_agent_row(m) for m in self.attach_auth(rows)]

    def search_for_agent(self, query: str, account_id: Optional[str] = None,
                         limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.store.search(query, account_id=account_id or None, limit=limit)
        return [_agent_row(m) for m in self.attach_auth(rows)]

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
        from vaf.core.email_accounts import NO_RULE, apply_sender_rules_to_category, get_sender_rules
        rules = get_sender_rules(username, user_scope_id=self.user_scope_id)
        updated = 0
        for row in self.store.list_for_relabel():
            # A rule's answer is stored even when it says primary (the inbox reads an explicit
            # primary as the person's word); a message no rule matches keeps what it has.
            ruled = apply_sender_rules_to_category(row.get("from_addr") or "", NO_RULE, rules=rules)
            if ruled == NO_RULE:
                continue
            cur = str(row.get("category") or "")
            new = re.sub(r"\s+", "_", str(ruled or "primary").strip().lower())[:64] or "primary"
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
                   references: str = "", undo_seconds: int = 15, *,
                   case_id: str = "", sent_by: str = "owner", hold: bool = False,
                   agent_written: bool = False, root_anchor: str = "",
                   attachments: Optional[List[Dict[str, Any]]] = None,
                   attachment_meta: Optional[List[Dict[str, str]]] = None,
                   reply_to_pk: Optional[int] = None,
                   thread_id: Optional[int] = None,
                   chat_session_id: str = "") -> Dict[str, Any]:
        """The one send funnel (EMAIL_CLIENT.md, "Native send"): every lane, the compose
        window, the agent's send/reply/forward tools and the Front Office answers, queues
        here and delivers through the outbox, so every sent mail has a Sent copy, a
        `sent_ids` row and a delivery stamp.

        Undo-send outbox (client-delay model): the op becomes runnable after undo_seconds;
        until then cancel_send withdraws it. Survives restarts - the supervisor sweep
        delivers queued ops whose delay passed. `hold` parks the mail as a draft for the
        person's approval (state `held`; approve_draft / discard_draft), which is how a
        Front Office answer waits in the inbox. `case_id` stamps the case anchor as the
        Message-ID (vaf/mail/case_token.py) so a reply is attributable with certainty;
        `agent_written` marks a mail the agent wrote on its own (RFC 3834 headers);
        `sent_by` records whose word it is (owner, agent, front_office); `reply_to_pk`
        is the inbound message this answers, marked answered when the mail leaves.
        """
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
        message_id = None
        if case_id:
            from vaf.mail.case_token import mint_message_id
            message_id = mint_message_id(self.user_scope_id, account_id, case_id,
                                         compose.message_id_domain(from_addr))
        msg = compose.build_message(from_addr, to, subject, body, cc=cc or None,
                                    bcc=bcc or None,
                                    in_reply_to=in_reply_to or None,
                                    references=references or None,
                                    attachments=attachments or None,
                                    message_id=message_id, agent_written=agent_written,
                                    root_anchor=root_anchor or None)
        # Carry the compose Message-ID so the DELIVERED mail is sent with this
        # exact id (transport message_id=), making the delivered mail and the
        # Sent copy one RFC822 entity - replies then thread correctly.
        message_id = msg["Message-ID"]
        import base64 as _b64
        not_before = int(datetime.now(timezone.utc).timestamp()) + max(0, int(undo_seconds))
        op_id = self.store.enqueue_op(apk, "send", {
            "account_id": account_id, "to": to, "cc": cc, "bcc": bcc,
            "subject": subject, "body": body,
            # The References as built (the case's root anchor in front), so the payload
            # says what the wire says.
            "in_reply_to": in_reply_to, "references": str(msg.get("References") or references or ""),
            "message_id": message_id,
            "raw_b64": _b64.b64encode(bytes(msg)).decode("ascii"),
            "case_id": case_id or "", "sent_by": sent_by or "owner",
            "agent_written": bool(agent_written),
            "attachments": list(attachment_meta or []),
            "reply_to_pk": int(reply_to_pk) if reply_to_pk is not None else None,
            "thread_id": int(thread_id) if thread_id is not None else None,
            # Which chat asked for this draft, when a chat did (vaf/core/outbound_hold.py).
            # The card in that conversation shows only its own drafts: a message being written
            # in one chat must never appear in another, and a Front Office draft (no chat at
            # all) belongs to the inbox rather than to any conversation.
            "chat_session_id": str(chat_session_id or ""),
        }, not_before_ts=not_before, state="held" if hold else "pending")
        self.store.record_sent_id(apk, message_id, case_id=case_id or "", to_addrs=to,
                                  sent_by=sent_by or "owner", in_reply_to=in_reply_to or "",
                                  op_id=op_id, delivery="held" if hold else "queued")
        # undo_seconds is the DURATION the client counts down (server-relative);
        # the client uses it instead of (undo_until_ts - client_now) so a skewed
        # browser clock cannot make the undo snackbar vanish early or linger.
        return {"ok": True, "op_id": op_id, "message_id": message_id, "undo_until_ts": not_before,
                "undo_seconds": max(0, int(undo_seconds)), "held": bool(hold)}

    def send_outcome(self, op_id: int) -> Dict[str, Any]:
        """What became of a queued send, for a caller that delivered right away: the op
        state (done, pending, failed, held, cancelled, discarded), the ledger's delivery
        stamp (sent, ambiguous, failed, queued, held) and the last error text. The
        delivery stamp is the word on an ambiguous send: handed to the server and not
        confirmed, which must never be re-sent."""
        op = self.store.get_op(int(op_id)) or {}
        payload = op.get("payload") or {}
        delivery = ""
        try:
            row = self.store.sent_id(int(op["account_id"]), payload.get("message_id") or "") if op else None
            delivery = str((row or {}).get("delivery") or "")
        except Exception:
            delivery = ""
        return {"state": str(op.get("state") or ""), "delivery": delivery,
                "error": str(payload.get("last_error") or ""), "message_id": payload.get("message_id") or ""}

    def draft_state(self, op: Dict[str, Any]) -> Tuple[str, str]:
        """(state, error) of one held op, the words the card and the terminal use: `held`
        while it waits, `failed` when its last attempt answered and the mail did not leave
        (the reason rides along), `ambiguous` when that attempt handed the mail to the server
        and never heard back (the ledger's stamp). The one reader of those two facts, so the
        listing, the approval and the release cannot disagree about which draft may be sent."""
        payload = op.get("payload") or {}
        delivery = ""
        try:
            row = self.store.sent_id(int(op["account_id"]), payload.get("message_id") or "")
            delivery = str((row or {}).get("delivery") or "")
        except Exception:
            delivery = ""
        error = str(payload.get("last_error") or "")
        if delivery == "ambiguous":
            return "ambiguous", error
        return ("failed" if error else "held"), error

    def approve_draft(self, op_id: int) -> bool:
        """A held answer leaves: pending now, delivered by the next drain. Refused for a draft
        whose last attempt may already have delivered it (`draft_state` ambiguous): every
        surface that can release a draft passes through here, so the one click that could be
        a second delivery is not handed out anywhere."""
        op = self.store.get_op(int(op_id))
        if not op or op.get("kind") != "send":
            return False
        if self.draft_state(op)[0] == "ambiguous":
            return False
        ok = self.store.approve_op(int(op_id))
        if ok:
            apk = int(op["account_id"])
            self.store.mark_sent_delivery(apk, op["payload"].get("message_id") or "", "queued")
        return ok

    def discard_draft(self, op_id: int, *, replaced_by: str = "") -> bool:
        """A held answer is dropped; its sent-id row records the discard. `replaced_by` is the
        newer draft that took its place, when that is why it goes (`MailStore.discard_op`)."""
        op = self.store.get_op(int(op_id))
        if not op or op.get("kind") != "send":
            return False
        ok = self.store.discard_op(int(op_id), replaced_by=replaced_by)
        if ok:
            self.store.mark_sent_delivery(int(op["account_id"]), op["payload"].get("message_id") or "", "discarded")
        return ok

    def revise_draft(self, op_id: int, *, subject: Optional[str] = None,
                     body: Optional[str] = None) -> bool:
        """New words for a held draft, before anybody sent it: False when it is not waiting.

        The stored message is edited in place (`compose.revise_message`), so what the person
        approves afterwards is still byte for byte what leaves, with the same recipients,
        attachments and Message-ID. Refused for a draft whose last attempt may already have
        delivered it (`draft_state` ambiguous): that one can only be dropped, and new words on
        it would be words nobody can send."""
        op = self.store.get_op(int(op_id))
        if not op or op.get("kind") != "send" or op.get("state") != "held":
            return False
        if self.draft_state(op)[0] == "ambiguous":
            return False
        payload = op.get("payload") or {}
        # An empty subject goes out as "(No subject)" (`compose` writes that header), so the
        # payload the card and an API sender read says the same thing as the bytes.
        if subject is not None and not str(subject).strip():
            subject = "(No subject)"
        new_subject = str(payload.get("subject") or "") if subject is None else str(subject)
        new_body = str(payload.get("body") or "") if body is None else str(body)
        import base64 as _b64
        from vaf.mail import compose
        raw = _b64.b64decode(payload.get("raw_b64") or "")
        revised = compose.revise_message(raw, subject=subject, body_text=body)
        return self.store.revise_held_op(int(op_id), subject=new_subject, body=new_body,
                                         raw_b64=_b64.b64encode(revised).decode("ascii"))

    def chat_draft(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """One send a chat asked for, in the words the chat card uses, whatever its state.

        `held`, `failed` and `ambiguous` are `draft_state`'s own answer for a draft that still
        waits. A released op that has not been delivered yet (`pending` for the outbox run,
        `sending` in the transport) is `sending`: it left the person's hands, which is what
        `release_held_draft` answers ok for, but "sent" would claim a delivery nobody has seen.
        Only `done` is `sent`. A parked `failed` op keeps the ledger's word for it
        (`draft_state`), and one parked by `reclaim_stale_ops` is `ambiguous`: it was handed to
        the transport and nobody heard back. A discard is `replaced` when a newer draft took its
        place, else `discarded`; a cancelled send is a discard."""
        from vaf.mail.store import INTERRUPTED_SEND
        p = op.get("payload") or {}
        raw_state = str(op.get("state") or "")
        error = ""
        if raw_state == "held":
            state, error = self.draft_state(op)
        elif raw_state in ("pending", "sending"):
            state = "sending"
        elif raw_state == "done":
            state = "sent"
        elif raw_state == "failed":
            state, error = self.draft_state(op)
            if state == "held":
                state = "failed"
            if str(p.get("last_error") or "") == INTERRUPTED_SEND:
                state = "ambiguous"
        elif raw_state == "discarded" and p.get("replaced_by"):
            state = "replaced"
        else:
            state = "discarded"
        return {
            "op_id": int(op["id"]), "account_id": p.get("account_id") or "",
            "to": p.get("to") or "", "cc": p.get("cc") or "", "bcc": p.get("bcc") or "",
            "subject": p.get("subject") or "", "body": p.get("body") or "",
            "attachments": [str(a.get("filename") or a.get("path") or "")
                            for a in (p.get("attachments") or []) if isinstance(a, dict)],
            "created_at": op.get("created_at") or "", "decided_at": op.get("updated_at") or "",
            "chat_session_id": p.get("chat_session_id") or "",
            "state": state, "error": error, "edited": bool(p.get("edited")),
            "replaced_by": str(p.get("replaced_by") or ""),
        }

    def list_chat_drafts(self, chat_session_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """The mail one chat asked for, newest first, in `chat_draft`'s shape: every draft
        still waiting, plus the newest `limit` of the rest (`MailStore.chat_send_ops`)."""
        return [self.chat_draft(op) for op in self.store.chat_send_ops(chat_session_id, limit=limit)]

    def get_chat_draft(self, op_id: int) -> Optional[Dict[str, Any]]:
        """One send by its op id in `chat_draft`'s shape, or None when it is no send."""
        op = self.store.get_op(int(op_id))
        if not op or op.get("kind") != "send":
            return None
        return self.chat_draft(op)

    def list_drafts(self, *, account_id: Optional[str] = None, thread_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Held answers awaiting approval: op id, account, thread, every recipient (to, cc,
        bcc), subject, body, the attachment names, who wrote it, when, and the draft's state
        with the reason of a failed attempt (`draft_state`). Newest first. The whole address
        list and the files are on the row because what the person approves is byte for byte
        what leaves: a card that showed the To line alone let a Bcc or a document go out
        unseen."""
        apk = self.store.account_pk(account_id) if account_id else None
        if account_id and apk is None:
            return []
        out = []
        for op in self.store.held_ops(apk, thread_id=thread_id):
            p = op.get("payload") or {}
            state, error = self.draft_state(op)
            out.append({
                "op_id": int(op["id"]), "account_id": p.get("account_id") or "",
                "thread_id": p.get("thread_id"), "reply_to_pk": p.get("reply_to_pk"),
                "to": p.get("to") or "", "cc": p.get("cc") or "", "bcc": p.get("bcc") or "",
                "subject": p.get("subject") or "",
                "body": p.get("body") or "", "sent_by": p.get("sent_by") or "", "case_id": p.get("case_id") or "",
                "attachments": [str(a.get("filename") or a.get("path") or "")
                                for a in (p.get("attachments") or []) if isinstance(a, dict)],
                "message_id": p.get("message_id") or "", "created_at": op.get("created_at") or "",
                "chat_session_id": p.get("chat_session_id") or "",
                "state": state, "error": error,
            })
        return out

    def cancel_send(self, op_id: int) -> bool:
        op = self.store.get_op(int(op_id))
        if not op or op.get("kind") != "send":
            return False
        return self.store.cancel_op(int(op_id))

    # ── attachments ────────────────────────────────────────────────────────

    def get_attachment(self, message_pk: int, part_ref: str) -> Optional[Tuple[str, str, bytes]]:
        """(filename, content_type, payload) by part_id or Content-ID. Served
        from the cached raw message only - no live fetch here.

        Returns None when the part's bytes are on the machine's known-bad list. The
        gate sits HERE, on the fetch, rather than on the sync: mail arrives whether we
        like it or not, and the raw message is stored as received, so the only thing
        that can actually be refused is handing the bytes onward - to the browser, to
        an agent tool, to a save-to-disk. One choke point covers all three.
        """
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
                from vaf.core.threat_db import refuse_known_bad
                if refuse_known_bad(payload, filename=filename, origin="mail"):
                    return None
                return filename, ctype, payload
        return None


class _NoImap:
    """Null client for send-only op processing when no IMAP session exists."""

    def has_capability(self, cap):
        return False

    def select_folder(self, *a, **k):
        raise RuntimeError("no imap session")

    def append(self, *a, **k):
        raise RuntimeError("no imap session")


def deliver_queued_sends(scope: str, account: Dict[str, Any], cred_username: Optional[str],
                         account_id: Optional[str] = None, *, service: Optional["MailService"] = None) -> Dict[str, int]:
    """Drain the account's queued SENDS now (the compose window's fast path after the
    undo window, and the agent's tools right after they queued): an IMAP session when
    one can be opened, so the Sent copy is filed, else the null client, so the mail
    still leaves. Other op kinds are left for the sweep, which has a real session.
    Never raises; a failure leaves the op for the sweep."""
    from vaf.core.config import Config
    from vaf.mail.imap_client import MailAuthError, _safe_logout, build_imap_client
    from vaf.mail.writeback import OpExecutor
    aid = account_id or account.get("account_id") or account.get("email") or ""
    svc = service or MailService(scope)
    apk = svc.store.account_pk(aid)
    if apk is None:
        return {"done": 0, "failed": 0, "deferred": 0}
    client = None
    try:
        try:
            client = build_imap_client(account, cred_username, scope)
        except (MailAuthError, ValueError, Exception):
            client = None  # send still works; Sent-APPEND is skipped
        return OpExecutor(svc.store, apk, client or _NoImap(), account, scope,
                          cred_username=cred_username).process(
            write_enabled=bool(Config.get("mail_engine_write_enabled", False)) and client is not None,
            allowed_kinds={"send"})
    except Exception as e:
        logger.warning("send delivery failed (the sweep retries): %s", e)
        return {"done": 0, "failed": 0, "deferred": 0}
    finally:
        if client is not None:
            try:
                _safe_logout(client)
            except Exception:
                pass


def release_held_draft(scope: str, username: str, op_id: int,
                       service: Optional["MailService"] = None) -> Dict[str, Any]:
    """Send one held draft NOW: {"ok", "state", "error"}.

    What "Send" means for a mail draft, in one place, because it is two acts: the op is
    released (`approve_draft`) and the account is drained (`deliver_queued_sends`), and only
    the second one puts the mail on the wire. The card and `vaf outbox send` both call this;
    when the card released and the terminal only released, the two surfaces disagreed about
    what the button did and the terminal's "Sent." was a mail still sitting in the outbox.
    A draft that is not waiting answers with state "" and ok False rather than raising: both
    callers turn that into "no draft with that id".

    The outcome is read BY STATE, and a draft is used up only by a send that left. `done` is
    delivered and `pending` is released to the sweep. `failed` means the transport answered and
    the mail did not leave: the op goes back to `held` with the reason on it, so the card and
    the terminal keep the draft the way the parked-call lane keeps one (a failed op is parked
    for the ops API, which is not where the person is looking, and the card lost the draft
    with nothing said). A `failed` op whose ledger stamp is `ambiguous` was handed to the
    server and never confirmed: it goes back to the person too, as `ambiguous`, which the
    approval refuses and only a discard can end.

    The ACCOUNT is resolved before the draft leaves the held state. Nothing but a configured
    account can deliver this mail: the immediate drain needs it and so does the sweep, so a
    draft released for an account the config no longer holds becomes a pending op nobody will
    ever drain, while the person was told the next run would take it. It stays held and says
    so instead.
    """
    svc = service or MailService(scope)
    op = svc.store.get_op(int(op_id))
    if not op or op.get("kind") != "send" or op.get("state") != "held":
        return {"ok": False, "state": "", "error": "not waiting"}
    if svc.draft_state(op)[0] == "ambiguous":
        return {"ok": False, "state": "ambiguous", "error": AMBIGUOUS_DRAFT}
    account_id = str((op.get("payload") or {}).get("account_id") or "")
    try:
        from vaf.core.email_accounts import get_email_config
        from vaf.tools.mail_utils import cred_username_from_kwargs
        ec = get_email_config(username or "admin", user_scope_id=scope)
        acc = next((a for a in (ec.get("accounts") or [])
                    if (a.get("account_id") or a.get("email") or "").lower() == account_id.lower()), None)
    except Exception as exc:                                   # noqa: BLE001
        # A config that cannot be read is not permission to release: the draft is worth more
        # than the click, so it waits and the person tries again.
        logger.warning("held draft %s: the account list could not be read: %s", op_id, exc)
        acc = None
    if acc is None:
        return {"ok": False, "state": "held", "error": NO_ACCOUNT_FOR_DRAFT}
    if not svc.approve_draft(int(op_id)):
        return {"ok": False, "state": "", "error": "not waiting"}
    try:
        deliver_queued_sends(scope, acc, cred_username_from_kwargs({"username": username}),
                             account_id, service=svc)
    except Exception as exc:                                   # noqa: BLE001
        # The op stays released, so the sweep delivers it; only the immediate drain failed.
        logger.warning("held draft %s: the immediate drain failed, the sweep takes it: %s", op_id, exc)
    outcome = svc.send_outcome(int(op_id))
    # `done` is delivered, `pending` is released and waiting for the sweep (the immediate drain
    # could not run: no IMAP session, no matching account, a deferred op). Both are a send that
    # left the person's hands, so both answer ok; `state` keeps the difference for a caller that
    # wants to say which one it was. Reporting `pending` as a failure told the person the send
    # had not worked over a mail that was already on its way.
    state = str(outcome.get("state") or "")
    delivery = str(outcome.get("delivery") or "")
    error = str(outcome.get("error") or "")
    if state == "failed":
        # Back to the person, with the reason: the transport answered and the mail did not
        # leave (or, stamped ambiguous, may have). `expect_state` keeps a sweep that raced this
        # from being overwritten; `last_error` stays in the payload for `draft_state`.
        svc.store.mark_op(int(op_id), "held", expect_state="failed")
        if delivery == "ambiguous":
            # What the person needs is what to DO, and it must read the same here as on the
            # second attempt (which refuses before it ever reaches the transport). The
            # transport's own words stay on the op for `draft_state` and go to the log.
            logger.warning("held draft %s was handed to the server and not confirmed: %s",
                           op_id, error or "no reason given")
            state, error = "ambiguous", AMBIGUOUS_DRAFT
    return {"ok": state in ("done", "pending"), "state": state, "delivery": delivery,
            "error": error}
