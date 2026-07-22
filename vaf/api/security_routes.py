# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Admin-only security overview API for the Logs "Overview" dashboard.

GET /api/security/overview -> aggregated protection-module status. Starts with
the code-sandbox block (live `docker inspect` of the vaf-sandbox container:
running state + the actually-enforced hardening, not the compose claims);
further modules (isolation, phishing, findings, ...) join this response as
their dashboard rows are wired.

Design rules carried over from the dashboard plan:
- Admin-gated like /api/logs (an unauthenticated localhost caller is floored to
  admin by the auth middleware in genuine single-user mode, so the desktop
  window keeps working).
- The status derivation is a pure function over the probe results so it is
  unit-testable without Docker (pattern: vaf/core/display_platform.py).
- Absent data is reported as absent ("state": "nodata"), never as a green
  default - the dashboard's honesty floor depends on it.
"""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from vaf.api.user_routes import require_admin
from vaf.core.security_events import read_security_events

router = APIRouter(prefix="/api/security", tags=["security"])

# Mirrors vaf/tools/python_sandbox.py SANDBOX_CONTAINER (import avoided: that
# module pulls the whole tool stack; the name is a stable public contract of
# docker-compose.memory.yml).
SANDBOX_CONTAINER = "vaf-sandbox"


def _docker_available() -> bool:
    """True when the docker daemon answers. Mirrors python_sandbox._ensure_docker_available."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def _inspect_sandbox() -> Optional[Dict[str, Any]]:
    """Raw `docker inspect` JSON for the sandbox container, or None when unavailable."""
    try:
        result = subprocess.run(
            ["docker", "inspect", SANDBOX_CONTAINER],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        parsed = json.loads(result.stdout)
        return parsed[0] if isinstance(parsed, list) and parsed else None
    except Exception:
        return None


def derive_sandbox_status(docker_available: bool,
                          inspect: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure derivation of the sandbox module status from probe results.

    States (dashboard traffic light):
      ok      -> docker up; execution is container-enforced. Two flavors:
                 the persistent container is running (hardening live-verified),
                 or it is not running and executions fall back to ephemeral
                 `--network none` containers (still enforced, just slower).
      warn    -> docker daemon down/missing: sandboxed execution is BLOCKED.
                 Fail-closed (nothing escapes to the host), but the feature is
                 unavailable - attention, not critical.
      (nodata is produced by the caller when the probe itself was impossible.)
    """
    if not docker_available:
        return {"state": "warn", "reason": "docker_unavailable", "container_running": False}

    running = False
    hardening: Dict[str, Any] = {}
    if inspect:
        try:
            running = bool((inspect.get("State") or {}).get("Running"))
            host_cfg = inspect.get("HostConfig") or {}
            cap_drop = [str(c).upper() for c in (host_cfg.get("CapDrop") or [])]
            security_opt = [str(s) for s in (host_cfg.get("SecurityOpt") or [])]
            networks = list(((inspect.get("NetworkSettings") or {}).get("Networks") or {}).keys())
            hardening = {
                "cap_drop_all": "ALL" in cap_drop,
                "no_new_privileges": any("no-new-privileges" in s for s in security_opt),
                "memory_bytes": host_cfg.get("Memory") or 0,
                "nano_cpus": host_cfg.get("NanoCpus") or 0,
                "networks": networks,
                "isolated_network": networks == ["vaf-sandbox-network"],
            }
        except Exception:
            running = False
            hardening = {}

    return {
        "state": "ok",
        "reason": "container_running" if running else "ephemeral_on_demand",
        "container_running": running,
        **({"hardening": hardening} if hardening else {}),
    }


def collect_sandbox_status(
    docker_probe=_docker_available,
    inspect_probe=_inspect_sandbox,
) -> Dict[str, Any]:
    """Run the probes (injectable for tests) and derive the sandbox block."""
    docker_ok = docker_probe()
    inspect: Optional[Dict[str, Any]] = None
    if docker_ok:
        inspect = inspect_probe()
    return derive_sandbox_status(docker_ok, inspect)


# ── Firewall / LAN perimeter block ───────────────────────────────────────────

# Event kinds that count as "blocked access attempts" vs "failed logins" for the
# dashboard's firewall module (source: vaf/core/security_events.py contract).
_BLOCKED_KINDS = ("ip_blocked", "unauthenticated_blocked", "token_rejected", "ws_rejected")
_LOGIN_KINDS = ("login_failed", "twofa_failed")


def summarize_security_events(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """Pure: count blocked-access vs failed-login events for the firewall module."""
    blocked = sum(1 for e in events if e.get("kind") in _BLOCKED_KINDS)
    logins = sum(1 for e in events if e.get("kind") in _LOGIN_KINDS)
    return {"blocked": blocked, "failed_logins": logins}


def derive_firewall_status(lan_enabled: bool, firewall_flag: bool,
                           counts: Dict[str, int]) -> Dict[str, Any]:
    """Pure derivation of the firewall/LAN-perimeter module status.

    Semantics: blocks HAPPENING is the firewall doing its job (stays ok);
    lan disabled means the remote surface is closed entirely (ok, different
    label). No warn state yet - a burst-of-failed-logins heuristic can add one
    later without changing this contract.
    """
    return {
        "state": "ok",
        "reason": "lan_enabled" if lan_enabled else "lan_disabled",
        "lan_enabled": bool(lan_enabled),
        "os_rules_enabled": bool(firewall_flag),
        "blocked_today": int(counts.get("blocked", 0)),
        "failed_logins_today": int(counts.get("failed_logins", 0)),
    }


def collect_firewall_status() -> Dict[str, Any]:
    """Read config flags + today's security events and derive the firewall block."""
    try:
        from vaf.core.config import Config
        lan_enabled = bool(Config.get("local_network_enabled", False))
        firewall_flag = bool(Config.get("local_network_firewall_enabled", True))
    except Exception:
        lan_enabled, firewall_flag = False, True
    today = datetime.now().strftime("%Y-%m-%d")
    counts = summarize_security_events(read_security_events(today, limit=1000))
    return derive_firewall_status(lan_enabled, firewall_flag, counts)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/alert-count")
def security_alert_count(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Cheap poll for the sidebar Logs notification dot: how many security
    events were recorded today and the timestamp of the newest one. Every entry
    in the security log is a rejected/blocked/failed attempt, so all count.
    The frontend compares latest_ts against a per-user 'last seen' marker to
    show an UNREAD dot that clears when the Logs window is opened."""
    today = datetime.now().strftime("%Y-%m-%d")
    events = read_security_events(today, limit=1000)
    latest = events[-1].get("ts") if events else None
    return {"count": len(events), "latest_ts": latest}


@router.get("/events")
def security_events(
    date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(default=100, ge=1, le=1000),
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Structured blocked/rejected access attempts for a day (admin only).

    Backs the firewall detail popup; the same data is mirrored human-readably
    into security_<date>.log (the "security" domain in the Logs file rail).
    """
    day = date or datetime.now().strftime("%Y-%m-%d")
    if not _DATE_RE.match(day):
        raise HTTPException(status_code=400, detail="Invalid date")
    return {"date": day, "events": read_security_events(day, limit=limit)}


# ── Docker network isolation (the "inner firewall") ──────────────────────────

# All VAF containers (docker-compose.memory.yml container_name entries).
_VAF_CONTAINERS = (
    "vaf-memory-db", "vaf-redis", "vaf-sandbox", "vaf-tts",
    "vaf-gotenberg", "vaf-stt", "vaf-browser",
)
_INTERNAL_NETWORK = "vaf-network"
_LOOPBACK_IPS = ("127.0.0.1", "::1")


def _inspect_containers() -> List[Dict[str, Any]]:
    """Raw docker inspect for all VAF containers. Missing containers are simply
    absent from the result (docker inspect still prints the found ones)."""
    try:
        result = subprocess.run(
            ["docker", "inspect", *_VAF_CONTAINERS],
            capture_output=True, text=True, timeout=10,
        )
        parsed = json.loads(result.stdout or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def derive_docker_isolation(inspects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure derivation of the Docker network-segmentation status.

    This is VAF's inner firewall and exists independent of LAN mode: the
    sandbox lives on its own bridge (must NOT touch the internal vaf-network),
    and every published port must bind to loopback only. Any 0.0.0.0/:: binding
    is LAN exposure -> state warn.
    """
    containers: List[Dict[str, Any]] = []
    any_exposed = False
    sandbox_off_internal: Optional[bool] = None
    for ins in inspects:
        try:
            name = str(ins.get("Name") or "").lstrip("/")
            running = bool((ins.get("State") or {}).get("Running"))
            networks = list(((ins.get("NetworkSettings") or {}).get("Networks") or {}).keys())
            bindings = (ins.get("HostConfig") or {}).get("PortBindings") or {}
            ports: List[Dict[str, str]] = []
            exposed = False
            for cport, binds in bindings.items():
                for b in (binds or []):
                    host_ip = str(b.get("HostIp") or "")
                    ports.append({"port": str(cport), "host_ip": host_ip, "host_port": str(b.get("HostPort") or "")})
                    if host_ip not in _LOOPBACK_IPS:
                        # "" and 0.0.0.0/:: bind on all interfaces -> reachable from the LAN
                        exposed = True
            if name == "vaf-sandbox":
                sandbox_off_internal = _INTERNAL_NETWORK not in networks and not ports
            any_exposed = any_exposed or exposed
            containers.append({
                "name": name, "running": running, "networks": networks,
                "ports": ports, "lan_exposed": exposed,
            })
        except Exception:
            continue
    return {
        "state": "warn" if any_exposed else "ok",
        "any_lan_exposed": any_exposed,
        "sandbox_off_internal": sandbox_off_internal,
        "containers": containers,
    }


def collect_docker_isolation() -> Optional[Dict[str, Any]]:
    """Probe + derive; None when docker is unavailable (no phantom green)."""
    if not _docker_available():
        return None
    return derive_docker_isolation(_inspect_containers())


# ── Channel perimeter (messenger ingress) ────────────────────────────────────

_CHANNELS = ("telegram", "whatsapp", "discord")


def derive_channels_status(channels: List[Dict[str, Any]],
                           rejected_by_channel: Dict[str, int]) -> Dict[str, Any]:
    """Pure derivation of the channel-perimeter module.

    Input per channel: {name, enabled, paired, last_ts, mode, contact_fallback}.
    warn when any ENABLED channel runs in permissive mode (everyone may reach
    the agent) - that answers the owner's core question "is someone unauthorized
    able to talk to the bot?". Rejections happening is the perimeter WORKING
    (ok, with counts).
    """
    out_channels: List[Dict[str, Any]] = []
    any_permissive = False
    total_rejected = 0
    for ch in channels:
        name = str(ch.get("name") or "")
        enabled = bool(ch.get("enabled"))
        mode = str(ch.get("mode") or "paired_only")
        rejected = int(rejected_by_channel.get(name, 0))
        total_rejected += rejected
        if enabled and mode == "permissive":
            any_permissive = True
        out_channels.append({
            "name": name,
            "enabled": enabled,
            "mode": mode,
            "contact_fallback": bool(ch.get("contact_fallback")),
            "paired": int(ch.get("paired") or 0),
            "last_ts": ch.get("last_ts"),
            "rejected_today": rejected,
        })
    return {
        "state": "warn" if any_permissive else "ok",
        "any_permissive": any_permissive,
        "rejected_today": total_rejected,
        "channels": out_channels,
    }


def collect_channels_status() -> Dict[str, Any]:
    """Read messenger configs + today's channel_rejected events and derive."""
    channels: List[Dict[str, Any]] = []
    try:
        from vaf.core.config import Config
        from vaf.core.channel_ingress_policy import resolve_channel_policy
        raw_policy = Config.get("channel_ingress_policy")

        def last_ts(cfg: Dict[str, Any]) -> Optional[float]:
            try:
                acts = cfg.get("chat_activity") or []
                return float(acts[-1].get("ts")) if acts else None
            except Exception:
                return None

        tc = Config.get("telegram_config") or {}
        tc = tc if isinstance(tc, dict) else {}
        wc = Config.get("whatsapp_config") or {}
        wc = wc if isinstance(wc, dict) else {}
        dc = Config.get("discord_config") or {}
        dc = dc if isinstance(dc, dict) else {}

        for name, cfg, paired in (
            ("telegram", tc, len(tc.get("whitelist") or []) + len(tc.get("relay_whitelist") or [])),
            ("whatsapp", wc, sum(1 for e in (wc.get("whitelist") or []) if isinstance(e, dict) and str(e.get("phone_number") or "").strip())),
            ("discord", dc, 1 if (dc.get("verified") and str(dc.get("admin_user_id") or "").strip()) else 0),
        ):
            pol = resolve_channel_policy(name, raw_policy)
            channels.append({
                "name": name,
                "enabled": bool(cfg.get("enabled")),
                "paired": paired,
                "last_ts": last_ts(cfg),
                "mode": pol.get("mode", "paired_only"),
                "contact_fallback": bool(pol.get("allow_contact_fallback")),
            })
    except Exception:
        pass
    today = datetime.now().strftime("%Y-%m-%d")
    rejected: Dict[str, int] = {}
    for ev in read_security_events(today, limit=1000):
        if ev.get("kind") == "channel_rejected":
            ch = str(ev.get("channel") or "")
            rejected[ch] = rejected.get(ch, 0) + 1
    return derive_channels_status(channels, rejected)


# ── Skills scanned / threats blocked ─────────────────────────────────────────

_SKILL_LEVELS = ("clean", "low", "medium", "high")


def derive_skills_status(skills: Dict[str, Any], events: List[Dict[str, Any]],
                         last_rescan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure derivation of the skills module.

    critical when a HIGH-level skill is INSTALLED (only possible via an admin
    override or post-install tampering caught by the re-scan), warn on medium.
    Blocked/override/alert counters come from today's security events.
    """
    counts = {lvl: 0 for lvl in _SKILL_LEVELS}
    items: List[Dict[str, Any]] = []
    worst = "clean"
    rank = {lvl: i for i, lvl in enumerate(_SKILL_LEVELS)}
    quarantined_total = 0
    acknowledged_total = 0
    # "effective" worst drives the banner colour and EXCLUDES acknowledged
    # mediums (admin reviewed + kept them). `worst` still reflects the true
    # riskiest level so the panel/donut are unchanged.
    effective_worst = "clean"
    for sid, entry in (skills or {}).items():
        scan = (entry or {}).get("scan") if isinstance(entry, dict) else None
        level = str((scan or {}).get("level", "clean") or "clean")
        if level not in counts:
            level = "clean"
        counts[level] += 1
        if rank[level] > rank[worst]:
            worst = level
        is_quarantined = bool(isinstance(entry, dict) and entry.get("quarantined"))
        is_acknowledged = bool(isinstance(entry, dict) and entry.get("acknowledged"))
        if is_quarantined:
            quarantined_total += 1
        if is_acknowledged:
            acknowledged_total += 1
        # An acknowledged MEDIUM no longer counts toward the banner state;
        # highs always count (acknowledge is a medium-only concept).
        eff = "clean" if (level == "medium" and is_acknowledged) else level
        if rank.get(eff, 0) > rank.get(effective_worst, 0):
            effective_worst = eff
        items.append({
            "id": str(sid), "level": level,
            "score": int((scan or {}).get("score", 0) or 0),
            "quarantined": is_quarantined,
            "acknowledged": is_acknowledged,
        })
    items.sort(key=lambda s: (-int(s["quarantined"]), -rank.get(s["level"], 0) if not s["acknowledged"] else 0, -s["score"], s["id"]))
    blocked = sum(1 for e in events if e.get("kind") == "skill_blocked")
    overrides = sum(1 for e in events if e.get("kind") == "skill_override")
    alerts = sum(1 for e in events if e.get("kind") in ("skill_scan_alert", "skill_quarantined"))
    return {
        "state": "critical" if effective_worst == "high" else "warn" if effective_worst == "medium" else "ok",
        "total": len(items),
        "counts": counts,
        "worst": worst,
        "quarantined_total": quarantined_total,
        "acknowledged_total": acknowledged_total,
        "skills": items,
        "blocked_today": blocked,
        "overrides_today": overrides,
        "alerts_today": alerts,
        "last_rescan": last_rescan,
    }


def collect_skills_status() -> Dict[str, Any]:
    """Read the skills manifest, today's skill events and the last re-scan summary."""
    skills: Dict[str, Any] = {}
    try:
        from vaf.core.skills_registry import load_manifest
        skills = load_manifest().get("skills") or {}
    except Exception:
        pass
    last: Optional[Dict[str, Any]] = None
    try:
        from vaf.skills.rescan import get_last_rescan
        last = get_last_rescan()
    except Exception:
        pass
    today = datetime.now().strftime("%Y-%m-%d")
    return derive_skills_status(skills, read_security_events(today, limit=1000), last)


# ── Guardrails / tool policy ─────────────────────────────────────────────────


def derive_guardrails_status(flags: Dict[str, bool],
                             tool_levels: Dict[str, int],
                             trust: Dict[str, Any]) -> Dict[str, Any]:
    """Pure assembly of the guardrails module.

    Deliberate call: channel_tools_unrestricted=True is surfaced as a warning
    ROW in the popup but does NOT amber the module/hero - it is the shipped
    DEFAULT, and a banner that is amber on every default install teaches alarm
    fatigue. The row coloring keeps it visible to the admin.
    """
    return {
        "state": "ok",
        "plan_gate": bool(flags.get("plan_gate")),
        "confirmation_gate": True,  # structural: dangerous tools always require confirmation
        "proactive_reply_gate": bool(flags.get("proactive_reply_gate")),
        "ask_first_drain_gate": bool(flags.get("ask_first_drain_gate")),
        "channel_tools_unrestricted": bool(flags.get("channel_tools_unrestricted")),
        "tools": {
            "total": int(tool_levels.get("total", 0)),
            "read": int(tool_levels.get("read", 0)),
            "write": int(tool_levels.get("write", 0)),
            "dangerous": int(tool_levels.get("dangerous", 0)),
            "system": int(tool_levels.get("system", 0)),
            "admin_only": int(tool_levels.get("admin_only", 0)),
            "channel_restricted": int(tool_levels.get("channel_restricted", 0)),
        },
        "trust": {
            "trusted_dirs": list(trust.get("trusted_dirs") or []),
            "allow_always_tools": list(trust.get("allow_always_tools") or []),
        },
    }


def collect_guardrails_status() -> Dict[str, Any]:
    """Read gate flags (config), the LIVE agent tool registry (permission-level
    inventory) and the persisted trust store (standing permissions)."""
    flags: Dict[str, bool] = {}
    try:
        from vaf.core.config import Config
        flags = {
            "plan_gate": bool(Config.get("plan_gate_enabled", True)),
            "proactive_reply_gate": bool(Config.get("proactive_reply_mutation_gate_enabled", True)),
            "ask_first_drain_gate": bool(Config.get("ask_first_drain_gate_enabled", True)),
            "channel_tools_unrestricted": bool(Config.get("channel_tools_unrestricted", True)),
        }
    except Exception:
        pass
    levels: Dict[str, int] = {"total": 0, "read": 0, "write": 0, "dangerous": 0, "system": 0, "admin_only": 0, "channel_restricted": 0}
    try:
        from vaf.core.web_interface import get_web_interface
        agent = getattr(get_web_interface(), "agent_instance", None)
        tools = getattr(agent, "tools", None) or {}
        for tool in tools.values():
            levels["total"] += 1
            lvl = str(getattr(tool, "permission_level", "read") or "read").strip().lower()
            if lvl not in ("read", "write", "dangerous", "system"):
                lvl = "read"
            levels[lvl] += 1
            if getattr(tool, "admin_only", False):
                levels["admin_only"] += 1
            if getattr(tool, "channel_restrictions", None):
                levels["channel_restricted"] += 1
    except Exception:
        pass
    trust: Dict[str, Any] = {}
    try:
        from vaf.core.trust import load_trust_state
        state = load_trust_state()
        trust = {
            "trusted_dirs": sorted(state.trusted_dirs),
            "allow_always_tools": sorted(t for t, p in state.tool_policies.items() if p == "allow"),
        }
    except Exception:
        pass
    return derive_guardrails_status(flags, levels, trust)


# ── Isolation admin metrics (workspace folders) ──────────────────────────────

_DIR_SIZE_FILE_CAP = 20000  # bounded walk: stop counting after this many files


def dir_size_bounded(root: Path, file_cap: int = _DIR_SIZE_FILE_CAP) -> Dict[str, Any]:
    """Pure-ish bounded recursive dir size: {size_bytes, files, truncated}.

    Never raises; unreadable entries are skipped. The cap keeps a pathological
    workspace from stalling the admin endpoint - when hit, `truncated` marks the
    size as a lower bound.
    """
    size = 0
    files = 0
    truncated = False
    try:
        stack = [Path(root)]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                size += entry.stat(follow_symlinks=False).st_size
                                files += 1
                                if files >= file_cap:
                                    return {"size_bytes": size, "files": files, "truncated": True}
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        truncated = True
    return {"size_bytes": size, "files": files, "truncated": truncated}


UNASSIGNED = "__unassigned__"  # bucket for folders no user can be attributed to


def _folder_owner(root_kind: str, dirname: str, uid8_names: Dict[str, str]) -> Optional[str]:
    """Map a per-user folder name to its owning user for aggregation.

    users/<username> dirs carry the username directly (scope_<uid8> variants
    resolve via the scope->username map). In the shared VAF_Projects root only
    <uid8> dirs are per-user by design; legacy project folders land in the
    UNASSIGNED bucket instead of masquerading as users. Hidden dirs (.git,
    .vaf, ...) are skipped entirely (None).
    """
    if dirname.startswith("."):
        return None
    name = dirname
    if name.startswith("scope_"):
        name = name[len("scope_"):]
    if re.fullmatch(r"[0-9a-f]{8}", name.lower()):
        return uid8_names.get(name.lower(), dirname)
    if root_kind == "projects":
        return UNASSIGNED
    return dirname


def collect_workspace_metrics(uid8_names: Optional[Dict[str, str]] = None,
                              roots: Optional[List[Any]] = None) -> Dict[str, Any]:
    """PER-USER folder totals (owner decision, round 2): who owns how many
    isolated folders and how big they are in sum - plus the grand total.

    Two roots: ~/.vaf/users/<username> (identity/soul/logs per user) and
    Documents/VAF_Projects/<uid8> (the per-user filesystem jail for outputs).
    `roots` is injectable for tests.
    """
    uid8_names = uid8_names or {}
    out: Dict[str, Any] = {"user_folders": [], "workspace_count": 0, "total_size_bytes": 0, "truncated": False}
    if roots is None:
        roots = []
        try:
            from vaf.core.config import Config
            roots.append(("users", Path(Config.APP_DIR) / "users"))
        except Exception:
            pass
        try:
            from vaf.core.platform import Platform
            roots.append(("projects", Platform.documents_dir() / "VAF_Projects"))
        except Exception:
            pass
    per_user: Dict[str, Dict[str, Any]] = {}
    for root_kind, root in roots:
        try:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                owner = _folder_owner(root_kind, child.name, uid8_names)
                if owner is None:
                    continue
                info = dir_size_bounded(child)
                agg = per_user.setdefault(owner, {"name": owner, "folders": 0, "size_bytes": 0, "truncated": False})
                agg["folders"] += 1
                agg["size_bytes"] += info["size_bytes"]
                agg["truncated"] = agg["truncated"] or bool(info.get("truncated"))
                out["workspace_count"] += 1
                out["total_size_bytes"] += info["size_bytes"]
                out["truncated"] = out["truncated"] or bool(info.get("truncated"))
        except Exception:
            continue
    out["user_folders"] = sorted(per_user.values(), key=lambda u: -u["size_bytes"])
    return out


async def _scope_username_map() -> Dict[str, str]:
    """Map full user_scope_id -> username from the auth DB. Defensive: {} on failure."""
    try:
        from sqlalchemy import select
        from vaf.auth.database import get_auth_db
        from vaf.auth.models import LocalUser
        async with get_auth_db() as db:
            rows = (await db.execute(select(LocalUser.username, LocalUser.user_scope_id))).all()
        return {str(r.user_scope_id): str(r.username) for r in rows if r.user_scope_id}
    except Exception:
        return {}


async def _verify_admin_totp(user: Dict[str, Any], code: str) -> None:
    """Verify the CURRENT admin's TOTP code or raise (403 wrong code, 400 no 2FA).

    The false-positive restore is the one action that deliberately re-exposes a
    HIGH-scanned skill to the agent - the owner mandated a second factor for it,
    so a stolen admin session alone cannot lift a quarantine."""
    code = str(code or "").strip().replace(" ", "")
    if not code:
        raise HTTPException(status_code=403, detail="2FA code required")
    user_id = str(user.get("user_id") or "").strip()
    if not user_id:
        # Tokenless localhost floor has no user record to verify against.
        raise HTTPException(status_code=400, detail="2FA verification requires a logged-in admin account")
    try:
        import uuid as _uuid
        from sqlalchemy import select
        from vaf.auth.database import get_auth_db
        from vaf.auth.models import LocalUser
        from vaf.auth.crypto import decrypt_totp_secret, verify_totp
        async with get_auth_db() as db:
            result = await db.execute(select(LocalUser).where(LocalUser.id == _uuid.UUID(user_id)))
            db_user = result.scalar_one_or_none()
        if db_user is None or not db_user.totp_secret or not db_user.totp_nonce:
            raise HTTPException(status_code=400, detail="2FA is not configured for this account")
        secret = decrypt_totp_secret(db_user.totp_secret, db_user.totp_nonce)
        if not verify_totp(secret, code):
            raise HTTPException(status_code=403, detail="Invalid 2FA code")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="2FA verification failed")


@router.post("/skills/{skill_id}/delete")
def quarantined_skill_delete(skill_id: str,
                             user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Delete a QUARANTINED skill entirely (admin). Deleting removes the threat,
    so no second factor is required - unlike restoring it."""
    from vaf.core.skills_registry import delete_skill, get_skill_manifest_entry, validate_skill_id
    sid = validate_skill_id(skill_id)
    entry = get_skill_manifest_entry(sid)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown skill")
    if not entry.get("quarantined"):
        raise HTTPException(status_code=400, detail="Skill is not quarantined")
    delete_skill(sid)
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("skill_removed", username=str(user.get("username") or ""), detail=f"quarantine-delete:{sid}")
    except Exception:
        pass
    return {"ok": True, "deleted": sid}


@router.get("/skills/{skill_id}/scan")
def skill_scan_detail(skill_id: str,
                      _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Live re-scan of an installed skill so the dashboard can show WHY it was
    flagged (per-rule findings). Manifest stores only the aggregate, so the
    reasons are recomputed on demand from the folder on disk (static, cheap)."""
    from vaf.core.skills_registry import get_skill_manifest_entry, skill_folder, validate_skill_id
    from vaf.skills.scanner import scan_skill_folder
    sid = validate_skill_id(skill_id)
    entry = get_skill_manifest_entry(sid)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown skill")
    try:
        scan = scan_skill_folder(skill_folder(sid))
    except Exception:
        raise HTTPException(status_code=500, detail="scan failed")
    return {
        "id": sid,
        "level": scan.get("level"),
        "score": scan.get("score"),
        "quarantined": bool(entry.get("quarantined")),
        "acknowledged": bool(entry.get("acknowledged")),
        "findings": [
            {k: f.get(k) for k in ("category", "severity", "message", "file", "line", "snippet")}
            for f in (scan.get("findings") or [])
        ][:40],
    }


@router.post("/skills/{skill_id}/acknowledge")
async def skill_acknowledge(skill_id: str,
                            payload: Dict[str, Any] = Body(...),
                            user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Acknowledge a MEDIUM-risk skill (admin reviewed + keeps it). Requires the
    admin's 2FA code (owner mandate: silencing a security warning is a security
    decision). The skill stays visible + still shown as medium, but the banner
    stops going amber for it. Refused for high-risk skills."""
    from vaf.core.skills_registry import get_skill_manifest_entry, set_skill_acknowledged, validate_skill_id
    sid = validate_skill_id(skill_id)
    entry = get_skill_manifest_entry(sid)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown skill")
    level = str(((entry.get("scan") or {}).get("level")) or "clean")
    if level == "high":
        raise HTTPException(status_code=400, detail="High-risk skills cannot be acknowledged - delete or restore instead")
    await _verify_admin_totp(user, str(payload.get("code") or ""))
    set_skill_acknowledged(sid, by=str(user.get("username") or ""))
    return {"ok": True, "acknowledged": sid}


@router.post("/skills/{skill_id}/isolate")
def skill_isolate(skill_id: str,
                  user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Manually quarantine an installed skill (e.g. an override-installed high).
    Hiding it reduces risk, so no second factor is required."""
    from vaf.core.skills_registry import get_skill_manifest_entry, set_skill_quarantined, validate_skill_id
    sid = validate_skill_id(skill_id)
    entry = get_skill_manifest_entry(sid)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown skill")
    set_skill_quarantined(sid, "manual")
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("skill_quarantined", username=str(user.get("username") or ""), detail=f"manual:{sid}")
    except Exception:
        pass
    return {"ok": True, "quarantined": sid}


@router.post("/skills/{skill_id}/restore")
async def quarantined_skill_restore(skill_id: str,
                                    payload: Dict[str, Any] = Body(...),
                                    user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """False-positive resolution: lift the quarantine. Requires the admin's 2FA
    code (owner mandate) - this re-exposes the skill to the agent."""
    from vaf.core.skills_registry import clear_skill_quarantine, get_skill_manifest_entry, validate_skill_id
    sid = validate_skill_id(skill_id)
    entry = get_skill_manifest_entry(sid)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown skill")
    if not entry.get("quarantined"):
        raise HTTPException(status_code=400, detail="Skill is not quarantined")
    await _verify_admin_totp(user, str(payload.get("code") or ""))
    clear_skill_quarantine(sid)
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("skill_override", username=str(user.get("username") or ""), detail=f"quarantine-restore:{sid}")
    except Exception:
        pass
    return {"ok": True, "restored": sid}


@router.get("/overview")
async def security_overview(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Aggregated protection-module status for the Overview dashboard (admin only).

    Blocking probes (docker subprocesses, file walks) run in the threadpool;
    the DB metrics are async on the owner engine. Every block degrades
    independently - one failing collector never takes the others down.
    """
    sandbox = await run_in_threadpool(collect_sandbox_status)
    firewall = await run_in_threadpool(collect_firewall_status)
    # Docker network segmentation is the inner firewall and is shown regardless
    # of LAN mode (owner request); it can independently flip the module amber.
    try:
        firewall["docker"] = await run_in_threadpool(collect_docker_isolation)
    except Exception:
        firewall["docker"] = None
    isolation: Optional[Dict[str, Any]] = None
    try:
        from vaf.memory.database import get_admin_isolation_metrics
        isolation = await get_admin_isolation_metrics()
        # Attach usernames (auth DB) to the memory scopes, then shorten the ids
        # for display - the full UUIDs never leave the backend.
        names = await _scope_username_map()
        for s in isolation.get("scopes", []):
            full = str(s.get("scope") or "")
            username = names.get(full)
            if username:
                s["username"] = username
            s["scope"] = full[:8] or "unscoped"
        uid8_names = {full[:8].lower(): name for full, name in names.items() if full}
        isolation.update(await run_in_threadpool(collect_workspace_metrics, uid8_names))
    except Exception:
        isolation = None
    channels = await run_in_threadpool(collect_channels_status)
    guardrails = await run_in_threadpool(collect_guardrails_status)
    skills = await run_in_threadpool(collect_skills_status)
    # Newest security-event timestamp today: lets the window's log badge be
    # unread-based (clears when the admin opens the security log), shared with
    # the sidebar dot via the same seen-marker.
    _today = datetime.now().strftime("%Y-%m-%d")
    _evts = read_security_events(_today, limit=1000)
    security_latest_ts = _evts[-1].get("ts") if _evts else None
    return {
        "sandbox": sandbox,
        "firewall": firewall,
        "isolation": isolation,
        "channels": channels,
        "guardrails": guardrails,
        "skills": skills,
        "security_latest_ts": security_latest_ts,
    }
