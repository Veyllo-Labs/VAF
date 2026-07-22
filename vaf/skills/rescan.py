# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Periodic re-scan of all installed skills (post-install tamper detection).

The install/update/import/editor paths already scan every skill BEFORE it lands
on disk. What they cannot catch is a skill whose files are modified AFTER
installation (edited on disk, synced from elsewhere, a compromised bundle
swap). This module closes that gap: every ``skills_rescan_interval_hours``
(default 5, ``0`` disables) the security scanner re-checks every installed
skill folder, updates the manifest scan block via the registry, and raises a
``skill_scan_alert`` security event whenever a skill's risk level WORSENED -
which flips the Skills module (and the protection banner) on the Overview
dashboard.

The worker is a daemon thread and idempotent to arm (the FastAPI startup hook
runs twice in TLS mode - one app on 8001 and 8005).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_LEVEL_RANK = {"clean": 0, "low": 1, "medium": 2, "high": 3}

_started_lock = threading.Lock()
_started = False


def level_worsened(old: str, new: str) -> bool:
    """True when the new scan level is strictly riskier than the old one."""
    return _LEVEL_RANK.get(str(new or "clean"), 0) > _LEVEL_RANK.get(str(old or "clean"), 0)


def _last_rescan_path() -> Path:
    from vaf.core.skills_registry import get_skills_dir
    return get_skills_dir() / "last_rescan.json"


def get_last_rescan() -> Optional[Dict[str, Any]]:
    """The persisted summary of the last full re-scan, or None. Never raises."""
    try:
        path = _last_rescan_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def rescan_all_skills() -> Dict[str, Any]:
    """Re-scan every installed skill folder once. Returns a summary dict.

    Per skill: scan the folder (body + bundled files), compare against the
    manifest's stored level, persist the new scan block on change, and emit a
    security event when the level worsened. Defensive throughout - one broken
    skill folder never stops the sweep.
    """
    from vaf.core.skills_registry import load_manifest, skill_folder, update_skill_scan
    from vaf.skills.scanner import scan_skill_folder

    summary: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scanned": 0, "changed": 0, "alerts": 0, "quarantined": 0, "worst": "clean",
    }
    try:
        skills = (load_manifest().get("skills") or {})
    except Exception:
        skills = {}
    for skill_id, entry in skills.items():
        try:
            folder = skill_folder(str(skill_id))
            if not (folder / "SKILL.md").exists():
                continue
            scan = scan_skill_folder(folder)
            summary["scanned"] += 1
            new_level = str(scan.get("level", "clean"))
            if _LEVEL_RANK.get(new_level, 0) > _LEVEL_RANK.get(summary["worst"], 0):
                summary["worst"] = new_level
            old_level = str(((entry or {}).get("scan") or {}).get("level", "clean"))
            if new_level != old_level or int(scan.get("score", 0) or 0) != int(((entry or {}).get("scan") or {}).get("score", 0) or 0):
                update_skill_scan(str(skill_id), scan)
                summary["changed"] += 1
            if level_worsened(old_level, new_level):
                summary["alerts"] += 1
                cats = ",".join(sorted({str(f.get("category", "")) for f in (scan.get("findings") or []) if f.get("category")}))
                # Worsened to HIGH -> immediate quarantine (owner decision): the
                # skill vanishes from every agent path until the admin resolves
                # it (delete, or false-positive restore with 2FA). An
                # override-installed high skill is untouched: its STORED level
                # is already high, so nothing "worsens".
                if new_level == "high":
                    try:
                        from vaf.core.skills_registry import set_skill_quarantined
                        if set_skill_quarantined(str(skill_id), "rescan"):
                            summary["quarantined"] += 1
                    except Exception:
                        pass
                try:
                    from vaf.core.security_events import log_security_event
                    kind = "skill_quarantined" if new_level == "high" else "skill_scan_alert"
                    log_security_event(
                        kind,
                        detail=f"{skill_id}: {old_level}->{new_level} score={scan.get('score')} cats={cats}"[:200],
                    )
                except Exception:
                    pass
        except Exception:
            continue
    try:
        _last_rescan_path().write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return summary


def start_periodic_rescan() -> bool:
    """Arm the daemon worker once (idempotent). Returns True when (newly) armed."""
    global _started
    with _started_lock:
        if _started:
            return False
        try:
            from vaf.core.config import Config
            interval_h = float(Config.get("skills_rescan_interval_hours", 5) or 0)
        except Exception:
            interval_h = 5.0
        if interval_h <= 0:
            return False

        def _loop() -> None:
            # First sweep shortly after startup (freshness for the dashboard),
            # then on the configured cadence.
            time.sleep(120)
            while True:
                try:
                    rescan_all_skills()
                except Exception:
                    pass
                time.sleep(max(300.0, interval_h * 3600.0))

        threading.Thread(target=_loop, name="skills-rescan", daemon=True).start()
        _started = True
        return True
