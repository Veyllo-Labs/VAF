"""
Admin-only API for reading debug log files from the VAF log directory.

GET /api/logs           → list available log files (admin only)
GET /api/logs/{fname}   → tail a specific log file  (admin only)
"""
import re
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from vaf.api.user_routes import require_admin
from vaf.core.log_helper import get_app_log_dir

router = APIRouter(prefix="/api/logs", tags=["logs"])

_DATE_RE = re.compile(r"^(.+?)_(\d{4}-\d{2}-\d{2})$")


def _describe_files() -> List[Dict[str, Any]]:
    log_dir = get_app_log_dir()
    files: List[Dict[str, Any]] = []
    try:
        for p in sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True):
            stat = p.stat()
            m = _DATE_RE.match(p.stem)
            domain = m.group(1) if m else p.stem
            date = m.group(2) if m else ""
            files.append({
                "filename": p.name,
                "domain": domain,
                "date": date,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
    except Exception:
        pass
    return files


@router.get("")
async def list_logs(_: Dict[str, Any] = Depends(require_admin)):
    """Return metadata for all log files in the log directory."""
    return {"files": _describe_files()}


@router.get("/{filename}")
async def read_log(
    filename: str,
    tail: int = Query(default=500, ge=1, le=10000),
    _: Dict[str, Any] = Depends(require_admin),
):
    """Return last `tail` lines of a specific log file."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.endswith(".log"):
        raise HTTPException(status_code=400, detail="Only .log files are accessible")

    log_dir = get_app_log_dir().resolve()
    path = (log_dir / filename).resolve()
    # Ensure the resolved path stays inside the log directory (guards symlinks)
    if path.parent != log_dir:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail_lines = [ln.rstrip("\n") for ln in all_lines[-tail:]]
        return {"filename": filename, "total_lines": len(all_lines), "lines": tail_lines}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read log: {exc}")
