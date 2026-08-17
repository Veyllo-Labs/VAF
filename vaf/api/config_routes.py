# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
REST API for app config (for onboarding and other clients that cannot use WebSocket).

Endpoints:
- GET   /api/config - Get full config (auth required when local_network_enabled)
- PATCH /api/config - Merge and save config (auth required when local_network_enabled)
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from vaf.api.user_routes import require_admin
from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])


def get_current_user_or_local_admin(request: Request) -> Dict[str, Any]:
    """Return current user from request.state (set by auth middleware) or treat as local admin.
    Includes user_scope_id for UUID-based data isolation."""
    user = getattr(request.state, "user", None)
    if user and isinstance(user, dict):
        scope = user.get("user_scope_id")
        return {
            "username": user.get("username", "admin"),
            "role": (user.get("role") or "user").lower(),
            "user_scope_id": str(scope) if scope else get_local_admin_scope_id(),
        }
    return {
        "username": get_local_admin_username(),
        "role": "admin",
        "user_scope_id": get_local_admin_scope_id(),
    }


def get_current_scope_id(request: Request) -> str:
    """Return current user's user_scope_id (for data scoping). Use get_current_user_or_local_admin when you need username/role too."""
    return get_current_user_or_local_admin(request).get("user_scope_id", get_local_admin_scope_id())


def get_current_username(request: Request) -> str:
    return get_current_user_or_local_admin(request).get("username", "admin")


@router.get("/config")
async def get_config(request: Request) -> Dict[str, Any]:
    """Return app config. Non-admins receive a scoped view (only their own connections)."""
    user = get_current_user_or_local_admin(request)
    full = Config.load()
    return Config.config_for_user(
        full,
        user.get("user_scope_id"),
        user.get("role", "user"),
    )


@router.get("/config/context-effort")
async def get_context_effort(
    request: Request,
    provider: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """The context-budget ladder for the CONFIGURED model, for the settings control.

    Read-only and computed, which is why it is its own endpoint rather than
    extra fields on GET /config: the ladder depends on the model's real window,
    not on a stored value. Writing goes through PATCH /config, where
    `context_compress_tokens` is admin-only like every other spend knob.

    `provider`/`model` override the saved config so a settings screen can ask
    what the ladder WOULD be for a provider the user has selected but not saved
    yet - the alternative was a second copy of the ladder in the browser.
    """
    get_current_user_or_local_admin(request)
    from vaf.core.context import resolve_context_effort

    cfg = Config.load()
    if provider:
        cfg = {**cfg, "provider": provider}
        if model:
            cfg[f"api_model_{provider}"] = model
    return resolve_context_effort(cfg)


@router.get("/usage")
async def get_usage(
    request: Request,
    days: int = 30,
    _admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Token and estimated-cost totals per user, newest-heaviest first.

    Admin-only, and that is not a formality: it lists every tenant's line by
    name, which is exactly what one tenant must not see about another. A
    non-admin asking about themselves has /usage/me below.
    """
    from vaf.core.cost import usage_totals

    return usage_totals(days=days)


@router.get("/usage/me")
async def get_usage_me(request: Request, days: int = 30) -> Dict[str, Any]:
    """The caller's OWN line from the same ledger - no other user's numbers."""
    from vaf.core.cost import usage_totals

    user = get_current_user_or_local_admin(request)
    scope = str(user.get("user_scope_id") or "").strip()
    full = usage_totals(days=days)
    from vaf.core.cost import _scope_key

    mine = _scope_key(scope or None)
    rows = [r for r in full.get("users", []) if r.get("scope") == mine]
    totals = rows[0] if rows else {"input_tokens": 0, "output_tokens": 0, "tokens": 0,
                                   "usd": 0.0, "calls": 0}
    return {"days": full.get("days"), "users": rows, "totals": totals}


@router.get("/usage/prices")
async def get_usage_prices(request: Request) -> Dict[str, Any]:
    """Public list prices per provider, for the "what would this cost elsewhere" panel."""
    from vaf.core.cost import price_catalog

    get_current_user_or_local_admin(request)
    return {"providers": price_catalog()}


@router.get("/usage/export")
async def export_usage(
    request: Request,
    days: int = 30,
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """The usage report as XML, for a transparency record outside the product.

    Carries HOW each number was arrived at, not only the number: the tokens are
    the provider's own report for calls it billed, the money is an estimate from
    a price table, and calls to a model missing from that table are priced at
    the expensive end. A record that omits its own method invites being read as
    an invoice.
    """
    from xml.etree.ElementTree import Element, SubElement, tostring

    from fastapi.responses import Response

    from vaf.core.cost import usage_totals

    data = usage_totals(days=days)
    root = Element("vaf-usage", {"days": str(data.get("days", days))})
    method = SubElement(root, "method")
    SubElement(method, "tokens").text = (
        "Reported by the provider for each billed call (usage.input_tokens / "
        "usage.output_tokens). Not counted by a tokenizer in VAF, so providers "
        "that tokenize differently still sum to their own invoices.")
    SubElement(method, "cost").text = (
        "Estimated from public list prices per million tokens. A model absent "
        "from that table is priced at the most expensive entry, so the figure "
        "is an upper bound, never an invoice.")
    SubElement(method, "scope").text = (
        "One ledger per account under the data directory; local models are free "
        "and contribute tokens but no cost.")

    totals = data.get("totals") or {}
    SubElement(root, "totals", {
        "input-tokens": str(totals.get("input_tokens", 0)),
        "output-tokens": str(totals.get("output_tokens", 0)),
        "tokens": str(totals.get("tokens", 0)),
        "api-calls": str(totals.get("calls", 0)),
        "estimated-usd": f"{float(totals.get('usd', 0.0)):.4f}",
        "cost-is-upper-bound": "true" if totals.get("estimated_usd_incomplete") else "false",
    })

    users = SubElement(root, "users")
    for row in data.get("users") or []:
        SubElement(users, "user", {
            "name": str(row.get("username", "")),
            "input-tokens": str(row.get("input_tokens", 0)),
            "output-tokens": str(row.get("output_tokens", 0)),
            "tokens": str(row.get("tokens", 0)),
            "api-calls": str(row.get("calls", 0)),
            "token-share-percent": str(row.get("token_share", 0.0)),
            "estimated-usd": f"{float(row.get('usd', 0.0)):.4f}",
            "tokens-recorded": "true" if row.get("tokens_recorded") else "false",
        })

    daily = SubElement(root, "daily")
    for day in data.get("daily") or []:
        SubElement(daily, "day", {
            "date": str(day.get("day", "")),
            "tokens": str(day.get("tokens", 0)),
            "api-calls": str(day.get("calls", 0)),
            "estimated-usd": f"{float(day.get('usd', 0.0)):.4f}",
        })

    xml = tostring(root, encoding="utf-8", xml_declaration=True)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="vaf-usage-{days}d.xml"'},
    )


@router.get("/provider-models")
async def get_provider_models() -> Dict[str, Any]:
    """Static per-provider model metadata (default + fallback list) — the single source
    (Config.PROVIDER_MODELS) the web UI reads to populate provider/model dropdowns. Static,
    non-sensitive: no auth required. The live /v1/models list still takes precedence in the UI."""
    return Config.PROVIDER_MODELS


@router.patch("/config")
async def patch_config(
    body: Dict[str, Any],
    request: Request,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
) -> Dict[str, Any]:
    """Merge provided keys into config and save. Non-admins: global keys ignored; connection toggles (Telegram/WhatsApp/Discord) stored per-user only."""
    current = Config.load()

    # In server_mode: LAN settings are locked — they cannot be disabled via the API.
    if current.get("server_mode", False):
        _SERVER_LOCKED = {"local_network_enabled", "local_network_tls_enabled", "server_mode"}
        body = {k: v for k, v in body.items() if k not in _SERVER_LOCKED}

    if _user.get("role") != "admin":
        body_filtered, scope_toggles = Config.extract_connection_toggles_for_scope(body, _user.get("user_scope_id"))
        body = Config.filter_for_non_admin(body_filtered)
        if scope_toggles:
            by_scope = current.get("connection_enabled_by_scope") or {}
            if not isinstance(by_scope, dict):
                by_scope = {}
            for scope_id, toggles in scope_toggles.items():
                by_scope[scope_id] = {**(by_scope.get(scope_id) or {}), **toggles}
            current["connection_enabled_by_scope"] = by_scope
    # API keys leave the payload here and go into the encrypted store. Without this the
    # read side would migrate a key on first read while this path kept writing raw into a
    # file nobody asks any more - the user changes their key, the UI says saved, and the
    # agent keeps using the old one. `absorb_config_keys` also fires the Veyllo-STT seed,
    # which used to hang off the key appearing in the config dict.
    from vaf.core.api_keys import absorb_config_keys
    merged = Config.merge_preserving_nonempty_sensitive(current, absorb_config_keys(body))
    Config.save(merged)
    # Through the same funnel as GET, never raw: `merged` carries everything the file
    # holds - estate API keys, the KEK, other users' connection configs - and this
    # response goes to whoever sent the PATCH, admin or not. Returning it unfiltered was
    # a second copy of the leak the GET route had, one save away from every reader.
    return Config.config_for_user(merged, _user.get("user_scope_id"), _user.get("role", "user"))


@router.get("/config/api-keys")
async def list_api_keys(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Which providers have a key. Booleans only - a value is never returned.

    Settings has no other way to know since keys left `config.json`: `GET /api/config`
    answers `api_key_<provider>` with the empty default, so a working key reads there as
    "not configured". Handing the secret back to the browser to fix that would be the wrong
    repair; a per-provider boolean is the whole requirement, for showing the state and for
    offering the delete this sits next to.

    An unreadable store is a 503, not an empty list: "nothing configured" and "I cannot tell
    you" must not render as the same screen.
    """
    from vaf.core.api_keys import configured_providers, lossy_hint, stored_key_hints
    from vaf.core.secure_store import SecureStoreUnreadable

    # UI-managed config secrets (the OAuth client secrets) ride the same response: state
    # and a lossy hint, never a value. Restricted to `UI_MANAGED_SECRET_KEYS` on purpose -
    # `is_secret_config_key` also matches the KEK, the JWT signing secret and DB URLs, and
    # a hint of THOSE would be a leak with no user need behind it.
    cfg = Config.load()
    secrets = {
        key: lossy_hint(str(cfg.get(key)).strip())
        for key in sorted(Config.UI_MANAGED_SECRET_KEYS)
        if isinstance(cfg.get(key), str) and str(cfg.get(key)).strip()
    }
    try:
        # `hints` are lossy display strings (start + bullets + tail), for the question a
        # boolean cannot answer: WHICH key is stored here. They are placeholders in the
        # UI, never form values - a value would be echoed by the next save and stored as
        # the key, the loop that already poisoned one entry.
        return {"providers": configured_providers(), "hints": stored_key_hints(), "secrets": secrets}
    except SecureStoreUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The stored API keys could not be read ({exc}).",
        ) from exc


@router.delete("/config/secrets/{key_name}")
async def delete_config_secret(
    key_name: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Remove one UI-managed config secret (an OAuth client secret). Explicit, like every
    secret removal here: a blank field means "not re-sent" on the save path and never
    deletes - the guard that keeps an unrelated save from wiping a secret is the same one
    that makes deletion impossible without its own call.

    The allowlist is the boundary, and it is deliberately much smaller than "everything
    classified secret": provider API keys have their own revocation endpoint with
    multi-source ordering this key shape does not need, and infrastructure secrets (the
    KEK, the JWT signing secret, DB URLs) must not be deletable from a settings page at
    all - that button would be an outage.
    """
    key = (key_name or "").strip()
    if key not in Config.UI_MANAGED_SECRET_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not a UI-managed secret. Provider API keys use "
                   "DELETE /api/config/api-keys/{provider}.",
        )
    cfg = Config.load()
    if cfg.get(key):
        cfg[key] = ""
        Config.save(cfg)
    if Config.load().get(key):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"'{key}' still holds a value after the delete; treat it as still set.",
        )
    return {"status": "deleted", "key": key}


@router.post("/config/api-keys/{provider}/check")
async def check_api_key(
    provider: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Ask the provider whether the STORED key is usable. The key never leaves the server.

    Saving a wrong key used to be indistinguishable from saving a right one until the next
    chat turn failed, which is a bad place to learn it: the answer arrives as a chat error,
    detached from the screen that caused it.

    THE DISTINCTION THAT DECIDES THE ANSWER, and collapsing it would be the defect: a key
    the provider REJECTS (401/403) is a fact about the key, while a timeout, a DNS failure
    or a 5xx is a fact about the network or about the provider's day. Both are "the check
    did not succeed" and only the first says anything about what the user typed. They are
    reported as different outcomes, so nothing downstream can treat an outage as proof that
    a key is wrong.

    Deliberately NOT destructive. A failed check leaves the key exactly where it is: the
    user is told, and decides. Removing or rolling back on a failure would hand a provider's
    bad afternoon the power to undo a correct key, and a check is not a revocation.
    """
    import httpx

    from vaf.core.api_keys import ApiKeyUnavailable, resolve_api_key
    from vaf.core.provider_registry import models_discovery

    name = (provider or "").strip().lower()
    try:
        key = resolve_api_key(name)
    except ApiKeyUnavailable as exc:
        return {"result": "unreadable", "detail": str(exc)}
    if not key:
        return {"result": "missing"}

    disc = models_discovery(name)
    if disc is None:
        # No live listing for this provider - saying "ok" would be a claim nothing measured.
        return {"result": "unsupported"}

    url, auth = disc
    headers: Dict[str, str] = {}
    params: Dict[str, str] = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif auth == "x-api-key":
        headers["X-Api-Key"] = key
        headers["anthropic-version"] = "2023-06-01"
    else:
        params["key"] = key

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, headers=headers, params=params)
    except Exception as exc:                                # noqa: BLE001 - see docstring
        return {"result": "unreachable", "detail": type(exc).__name__}

    if resp.status_code in (401, 403):
        return {"result": "rejected", "status": resp.status_code}
    if resp.status_code >= 400:
        # Google is the one provider that refuses a bad key WITHOUT a 401: measured
        # 2026-08-01, an invalid key gets HTTP 400 with `"status": "INVALID_ARGUMENT"` and
        # the machine-readable `"reason": "API_KEY_INVALID"` in the error body. Found live -
        # the owner's mistyped Google key rendered as "could not reach the provider", which
        # is this endpoint committing the exact confusion it exists to prevent, from the
        # other side. So a 400-class answer is a verdict on the key precisely when the body
        # says so; a bare 400 can just as well be a malformed request and stays an outage.
        try:
            body = resp.text[:4000]
        except Exception:                                   # noqa: BLE001 - body is optional evidence
            body = ""
        if "API_KEY_INVALID" in body:
            return {"result": "rejected", "status": resp.status_code}
        return {"result": "unreachable", "status": resp.status_code}
    return {"result": "ok", "status": resp.status_code}


@router.delete("/config/api-keys/{provider}")
async def delete_api_key_route(
    provider: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Revoke one provider key. Its own call, deliberately, not an empty field on save.

    People delete a key because it leaked, so this is a revocation and it reports like one:
    either the key is gone from every source that can answer for it, or this fails with the
    instruction to rotate it upstream. A blank field on the ordinary save path still means
    "not re-sent" and still changes nothing - that guard is what keeps a partially filled
    form from wiping a key, and it stays.
    """
    from vaf.core.api_keys import ApiKeyRevocationFailed, delete_api_key

    try:
        delete_api_key(provider)
    except ApiKeyRevocationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return {"status": "revoked", "provider": (provider or "").strip().lower()}
