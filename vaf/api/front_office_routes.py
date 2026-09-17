# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Front Office in Settings, Connections: the switch, the owner's briefing, the knowledge.

Three things live behind the "Front Office" card under Contacts:

- the switch per channel: "Front Office on for WhatsApp" opens that channel
  (`channel_ingress_policy.set_front_office`): every sender is answered in Front Office
  mode, the people already in the book get "Can reach your assistant" switched on at that
  moment, a new sender is enrolled as a contact by the bridge, and the owner keeps one
  person out by switching them off in the WhatsApp window or the contact book. The policy
  used to be reachable only by editing `channel_ingress_policy` in config.json: a contact
  with the flag was turned away under the default policy while the contact book said the
  agent answers them. Writing it is the admin's (an instance-wide key).
- the profile (`vaf/core/front_office_profile.py`): the owner's own instructions for those
  turns and whether they may read the owner's general memory. Per user, the owner's own.
- the knowledge: documents learned into the Front Office lane of the memory store
  (`vaf.memory.lanes.FRONT_OFFICE_SOURCE`), which only a contact's turn reads. A learn
  started here belongs to no chat, so it runs on a thread of this process
  (`learn_job.start_background_learn`); progress is the ledger the status reads.

Reading is open to every signed-in user (the count, the profile and the knowledge are the
caller's own, and the policy is already visible in GET /api/config).

Named boundary, the CLI: no switch of the Connections family (a channel's on/off,
WhatsApp's inbound-to-agent, the reply window) has a CLI command today, and these follow
them; the policy stays editable in config.json and the profile in
`users/<name>/front_office.json` for a headless install.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vaf.api.config_routes import get_current_user_or_local_admin
from vaf.api.user_routes import require_admin
from vaf.core.channel_ingress_policy import FRONT_OFFICE_CHANNELS, front_office_state, set_front_office
from vaf.core.config import Config, get_local_admin_scope_id
from vaf.core.front_office_profile import (
    BRIEFING_MAX_CHARS,
    load_front_office_profile,
    save_front_office_profile,
)
from vaf.core.security_events import log_security_event

logger = logging.getLogger("vaf.api.front_office")

router = APIRouter(prefix="/api/front-office", tags=["front-office"])

KNOWLEDGE_EXTENSIONS = (".pdf", ".txt", ".md")
KNOWLEDGE_MAX_BYTES = 40 * 1024 * 1024
KNOWLEDGE_TAG_PREFIX = "fo-"


class FrontOfficeUpdate(BaseModel):
    enabled: bool
    # One of FRONT_OFFICE_CHANNELS, or omitted for every channel at once.
    channel: Optional[str] = None


class FrontOfficeMailUpdate(BaseModel):
    # draft: the agent's answer is held in the mail outbox for the owner's approval;
    # send: it leaves at once.
    reply_mode: str


class FrontOfficeProfileUpdate(BaseModel):
    briefing: Optional[str] = None
    use_general_memory: Optional[bool] = None


class KnowledgeUpload(BaseModel):
    filename: str
    content_base64: str


def _caller(request: Request) -> Dict[str, Any]:
    """username, scope and whether this is the local admin, for the per-user parts of the
    state (the caller's own contact book, profile, knowledge and channel toggles)."""
    user = get_current_user_or_local_admin(request)
    scope = str(user.get("user_scope_id") or "").strip() or None
    is_admin = scope is None or scope == str(get_local_admin_scope_id() or "").strip()
    return {"username": user.get("username") or "admin", "user_scope_id": scope, "is_admin": is_admin}


def _scope_uuid(caller: Dict[str, Any]) -> Optional[UUID]:
    try:
        return UUID(str(caller["user_scope_id"])) if caller.get("user_scope_id") else None
    except (ValueError, TypeError):
        return None


def _channel_connected(channel: str, caller: Dict[str, Any]) -> bool:
    """Whether the caller's side of the channel is switched on: the local admin owns the
    global `<channel>_config.enabled`, everybody else has a slider under
    `connection_enabled_by_scope`, the rule of whatsapp_routes and telegram_routes."""
    if channel == "whatsapp":
        from vaf.core.messaging_connections import whatsapp_enabled_for_scope
        return whatsapp_enabled_for_scope(caller["user_scope_id"])
    if channel == "email":
        # Mail is connected when the caller has a mail account the engine can sync.
        try:
            from vaf.core.email_accounts import list_mail_accounts
            return bool(list_mail_accounts(caller["username"], user_scope_id=caller["user_scope_id"]))
        except Exception:
            return False
    cfg = Config.get(f"{channel}_config") or {}
    cfg = cfg if isinstance(cfg, dict) else {}
    if caller["is_admin"]:
        return bool(cfg.get("enabled"))
    by_scope = Config.get("connection_enabled_by_scope") or {}
    toggles = by_scope.get(caller["user_scope_id"], {}) if isinstance(by_scope, dict) else {}
    return bool(isinstance(toggles, dict) and toggles.get(channel, False))


# ── knowledge: the Front Office lane, listed and written ─────────────────────────────────

def _knowledge_dir(caller: Dict[str, Any]) -> Path:
    from vaf.core.front_office_profile import profile_path
    return profile_path(caller["username"]).parent / "front_office" / "knowledge"


def _safe_filename(name: str) -> str:
    base = Path(str(name or "")).name
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    return base or "document"


def _doc_tag_for(title: str) -> str:
    from vaf.tools.learn_document import _normalize_doc_tag
    return KNOWLEDGE_TAG_PREFIX + _normalize_doc_tag(title)[len("doc-"):]


async def _knowledge_rows(caller: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The documents of the caller's Front Office lane: the finished ones from their
    document_index roots, the ones in flight from their ledgers."""
    from sqlalchemy import and_, select
    from vaf.core.learn_ledger import LearnLedger
    from vaf.memory.database import get_db
    from vaf.memory.lanes import FRONT_OFFICE_SOURCE
    from vaf.memory.models import Memory

    scope = _scope_uuid(caller)
    rows: Dict[str, Dict[str, Any]] = {}
    conditions = [
        Memory.is_deleted == False,  # noqa: E712
        Memory.meta["type"].as_string() == "document_index",
        Memory.meta["source"].as_string() == FRONT_OFFICE_SOURCE,
    ]
    if scope is not None:
        conditions.append(Memory.user_scope_id == scope)
    async with get_db(user_scope_id=scope) as db:
        roots = (await db.execute(select(Memory).where(and_(*conditions)))).scalars().all()
        for root in roots:
            meta = dict(root.meta or {})
            tag = str(meta.get("doc_tag") or "")
            if not tag:
                continue
            rows[tag] = {
                "doc_tag": tag,
                "title": str(meta.get("title") or tag),
                "status": str(meta.get("learn_status") or "complete"),
                "sections": int(meta.get("page_count") or 0),
                "learned_pages": meta.get("learned_pages"),
                "total_pages": meta.get("total_pages"),
                "learned_at": meta.get("learned_at"),
                "batches_done": 0,
                "batches_total": 0,
                "error": None,
            }
    scope_str = str(caller.get("user_scope_id") or "")
    uid8 = (scope_str.replace("-", "")[:8]) or "admin"
    try:
        for path in LearnLedger._dir().glob(f"{KNOWLEDGE_TAG_PREFIX}*__{uid8}.json"):
            tag = path.name[: -len(f"__{uid8}.json")]
            ledger = LearnLedger.load(tag, scope_str)
            if ledger is None:
                continue
            row = rows.setdefault(tag, {
                "doc_tag": tag, "title": Path(ledger.source_path).stem, "status": ledger.status,
                "sections": ledger.section_count, "learned_pages": None,
                "total_pages": ledger.total_pages, "learned_at": None,
                "batches_done": 0, "batches_total": 0, "error": None,
            })
            row["status"] = ledger.status
            row["batches_done"] = len(ledger.done_batch_indices())
            row["batches_total"] = ledger.total_batches
            row["error"] = ledger.last_error
    except OSError:
        pass
    return sorted(rows.values(), key=lambda r: (r["status"] != "running", r["title"].lower()))


def _channel_contacts(caller: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Per Front Office channel: how many of the caller's contacts have a key there, and
    how many of those may reach the assistant (the window's "N contacts, M allowed")."""
    from vaf.core.contacts_store import contact_endpoints, list_contacts
    out = {ch: {"total": 0, "allowed": 0} for ch in FRONT_OFFICE_CHANNELS}
    try:
        for c in list_contacts(caller["username"], user_scope_id=caller["user_scope_id"]):
            keys = contact_endpoints(c)
            for ch in FRONT_OFFICE_CHANNELS:
                if keys.get(ch):
                    out[ch]["total"] += 1
                    if c.get("allow_as_assistant_user"):
                        out[ch]["allowed"] += 1
    except Exception:
        pass
    return out


async def _state(caller: Dict[str, Any]) -> Dict[str, Any]:
    from vaf.core.contacts_store import get_contacts_allowing_assistant
    from vaf.core.messaging_connections import reply_window_hours

    state = front_office_state(Config.get("channel_ingress_policy"))
    wc = Config.get("whatsapp_config") or {}
    wc = wc if isinstance(wc, dict) else {}
    try:
        reachable = len(get_contacts_allowing_assistant(caller["username"], user_scope_id=caller["user_scope_id"]))
    except Exception:
        reachable = 0
    memory_enabled = bool(Config.get("memory_enabled", True))
    knowledge: List[Dict[str, Any]] = []
    knowledge_error: Optional[str] = None
    if memory_enabled:
        try:
            knowledge = await _knowledge_rows(caller)
        except Exception as exc:
            logger.warning("Front Office knowledge could not be listed: %s", exc)
            knowledge_error = "unavailable"
    return {
        "enabled": state["enabled"],
        "channels": state["channels"],
        "contacts_only": state["contacts_only"],
        "email_reply_mode": state["email_reply_mode"],
        "channels_connected": {ch: _channel_connected(ch, caller) for ch in FRONT_OFFICE_CHANNELS},
        "channel_contacts": _channel_contacts(caller),
        # Off stops every sender before the agent, contacts included (whatsapp_bridge).
        "whatsapp_inbound_to_agent": bool(wc.get("inbound_to_agent", True)),
        # The other door that stays open with Front Office off: 0 means closed.
        "reply_window_hours": reply_window_hours(),
        "reachable_contacts": reachable,
        "admin": caller["is_admin"],
        "profile": load_front_office_profile(caller["username"]),
        "briefing_max_chars": BRIEFING_MAX_CHARS,
        "memory_enabled": memory_enabled,
        "knowledge": knowledge,
        "knowledge_error": knowledge_error,
    }


@router.get("")
async def get_front_office(request: Request) -> Dict[str, Any]:
    return await _state(_caller(request))


@router.put("")
async def put_front_office(
    body: FrontOfficeUpdate,
    request: Request,
    _admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    caller = _caller(request)
    channel = (body.channel or "").strip().lower() or None
    if channel is not None and channel not in FRONT_OFFICE_CHANNELS:
        raise HTTPException(status_code=400, detail=f"Not a Front Office channel: {body.channel}")
    config = Config.load()
    before = front_office_state(config.get("channel_ingress_policy"))
    policy = set_front_office(config.get("channel_ingress_policy"), body.enabled, channel)
    after = front_office_state(policy)
    changed = [ch for ch in FRONT_OFFICE_CHANNELS if before["channels"][ch] != after["channels"][ch]]
    if changed:
        config["channel_ingress_policy"] = policy
        Config.save(config)
        from vaf.core.contacts_store import grant_assistant_for_channel
        # One event per channel that really changed, like a pairing: the throttle keys
        # on the channel, so both channels of one switch stay two events. Switching a
        # channel on also grants every contact of that channel in the caller's book (a new
        # sender is enrolled by the bridge when they write); switching it off leaves the
        # flags alone, so the owner's per-person choices survive a round trip.
        for ch in changed:
            if after["channels"][ch]:
                granted = grant_assistant_for_channel(ch, caller["username"], caller["user_scope_id"])
                log_security_event("front_office_changed", channel=ch, username=str(caller["username"]),
                                   detail=f"on, {granted} contacts granted")
            else:
                log_security_event("front_office_changed", channel=ch, username=str(caller["username"]),
                                   detail="off")
    return await _state(caller)


@router.put("/mail")
async def put_front_office_mail(
    body: FrontOfficeMailUpdate,
    request: Request,
    _admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """The mail channel's reply mode (draft or send), instance policy like the switch."""
    from vaf.core.channel_ingress_policy import set_email_reply_mode
    caller = _caller(request)
    config = Config.load()
    try:
        policy = set_email_reply_mode(config.get("channel_ingress_policy"), body.reply_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    before = front_office_state(config.get("channel_ingress_policy"))["email_reply_mode"]
    if before != front_office_state(policy)["email_reply_mode"]:
        config["channel_ingress_policy"] = policy
        Config.save(config)
        log_security_event("front_office_changed", channel="email", username=str(caller["username"]),
                           detail=f"reply mode {front_office_state(policy)['email_reply_mode']}")
    return await _state(caller)


@router.put("/profile")
async def put_front_office_profile(body: FrontOfficeProfileUpdate, request: Request) -> Dict[str, Any]:
    """The caller's own briefing and memory switch; a LAN account briefs its own agent."""
    caller = _caller(request)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change")
    try:
        profile = save_front_office_profile(caller["username"], **changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile}


@router.post("/knowledge")
async def add_front_office_knowledge(body: KnowledgeUpload, request: Request) -> Dict[str, Any]:
    """Store one document under the caller's Front Office folder and learn it into the
    Front Office lane on a background thread. The click is the confirmation."""
    from vaf.tools.learn_document import _clean_title
    from vaf.tools.learn_job import LearnJobSpec, background_learn_running, start_background_learn
    from vaf.memory.lanes import FRONT_OFFICE_SOURCE

    caller = _caller(request)
    if not Config.get("memory_enabled", True):
        raise HTTPException(status_code=409, detail="Memory is switched off, so nothing can be learned")
    name = _safe_filename(body.filename)
    if not name.lower().endswith(KNOWLEDGE_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Only PDF, TXT and MD documents can be learned")
    try:
        content = base64.b64decode(body.content_base64 or "", validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="The upload is not valid base64") from exc
    if not content:
        raise HTTPException(status_code=400, detail="The document is empty")
    if len(content) > KNOWLEDGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The document is larger than 40 MB")
    title = _clean_title(name)
    doc_tag = _doc_tag_for(title)
    scope = _scope_uuid(caller)
    if background_learn_running(doc_tag, scope):
        raise HTTPException(status_code=409, detail="This document is being learned right now")
    folder = _knowledge_dir(caller)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.exists():
        target = folder / f"{int(time.time())}_{name}"
    target.write_bytes(content)
    spec = LearnJobSpec(path=str(target), document_title=title, doc_tag=doc_tag,
                        source=FRONT_OFFICE_SOURCE, force_relearn=True)
    if not start_background_learn(spec, user_scope_id=scope):
        raise HTTPException(status_code=409, detail="This document is being learned right now")
    return {"doc_tag": doc_tag, "title": title, "started": True}


@router.delete("/knowledge/{doc_tag}")
async def remove_front_office_knowledge(doc_tag: str, request: Request) -> Dict[str, Any]:
    """Forget one document: stop a running learn, delete its rows in the lane, drop the
    ledger and the stored file. Only tags of the Front Office lane are accepted."""
    from vaf.core.learn_ledger import LearnLedger
    from vaf.memory.database import get_db
    from vaf.memory.rag import RagPipeline
    from vaf.tools.learn_job import cancel_background_learn

    caller = _caller(request)
    tag = (doc_tag or "").strip()
    if not re.fullmatch(rf"{KNOWLEDGE_TAG_PREFIX}[a-z0-9-]+", tag):
        raise HTTPException(status_code=400, detail="Not a Front Office document")
    scope = _scope_uuid(caller)
    cancel_background_learn(tag, scope)
    removed = 0
    try:
        async with get_db(user_scope_id=scope) as db:
            removed = await RagPipeline(db).delete_by_tag(tag, soft=True, user_scope_id=scope)
    except Exception as exc:
        logger.warning("Front Office knowledge %s could not be deleted from memory: %s", tag, exc)
        raise HTTPException(status_code=503, detail="The memory store is not reachable") from exc
    scope_str = str(caller.get("user_scope_id") or "")
    ledger = LearnLedger.load(tag, scope_str)
    if ledger is not None:
        try:
            src = Path(ledger.source_path)
            if src.is_file() and src.resolve().parent == _knowledge_dir(caller).resolve():
                src.unlink()
        except OSError:
            pass
        ledger.delete()
    try:
        from vaf.memory.cache import get_cache
        await get_cache().invalidate_graph()
    except Exception:
        pass
    return {"doc_tag": tag, "removed": int(removed or 0)}
