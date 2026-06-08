"""
Shared log directory and domain log writers for VAF.
Consolidates logs into one file per domain (rag, memory, webui, prompt, headless, backend) with timestamps.
Respects Config.debug_logs_enabled: when False, no domain logs and no queue.log are written.
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

# Domains get a single {domain}.log file each; use prefixes like [COMPACTION], [EMIT] inside messages.
ALLOWED_DOMAINS = ("rag", "memory", "webui", "prompt", "headless", "backend", "attach")


def is_debug_logging_enabled() -> bool:
    """True if domain logs and queue.log should be written. ON by default (Advanced -> Debug Logs to opt out)."""
    try:
        from vaf.core.config import Config
        return bool(Config.get("debug_logs_enabled", True))
    except Exception:
        return True


def get_app_log_dir() -> Path:
    """Resolve app log directory (same order as headless _get_debug_log_dir)."""
    candidates: list[Path] = []
    env_dir = os.environ.get("VAF_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    repo_logs = Path(__file__).resolve().parents[2] / "logs"
    candidates.append(repo_logs)
    candidates.append(Platform.data_dir() / "logs")
    candidates.append(Platform.vaf_dir() / "logs")
    candidates.append(Path(__file__).resolve().parents[1] / "logs")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    return Path.cwd()


def get_dated_log_path(basename: str, ext: str = "log") -> Path:
    """Return path for a log file with today's date in the name: {basename}_YYYY-MM-DD.{ext}.
    Same day always uses the same file (append). GC deletes files whose date is older than gc_max_age_hours."""
    log_dir = get_app_log_dir()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return log_dir / f"{basename}_{date_str}.{ext}"


def log_tool_use(
    tool_name: str,
    session_id: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    arguments_preview: Optional[str] = None,
) -> None:
    """When debug_logs_enabled, append one line to tool_use_YYYY-MM-DD.log for user-scope isolation debugging.
    Logs which session_id and user_scope_id (UUID) were active for each tool call. No-op when debug off or on error."""
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("tool_use", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        sid = session_id or ""
        scope = user_scope_id or ""
        args = (arguments_preview or "")[:200].replace("\n", " ")
        line = f"{ts} tool={tool_name} session_id={sid} user_scope_id={scope} args_preview={args}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def log_telegram_reply(message: str) -> None:
    """Append to logs/telegram_reply_YYYY-MM-DD.log when debug_logs_enabled. No-op on error."""
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("telegram_reply", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_discord_reply(message: str) -> None:
    """Append to logs/discord_reply_YYYY-MM-DD.log when debug_logs_enabled. No-op on error."""
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("discord_reply", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_qr(message: str) -> None:
    """Append to logs/whatsapp_qr_YYYY-MM-DD.log when debug_logs_enabled. No-op on error."""
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("whatsapp_qr", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_inbound(message: str) -> None:
    """Append to logs/whatsapp_inbound_YYYY-MM-DD.log when debug_logs_enabled. No-op on error."""
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("whatsapp_inbound", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_reply(message: str) -> None:
    """Append to logs/whatsapp_reply_YYYY-MM-DD.log when debug_logs_enabled. No-op on error."""
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("whatsapp_reply", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def append_domain_log(domain: str, message: str) -> None:
    """
    Append one timestamped line to {domain}_YYYY-MM-DD.log.
    domain: one of rag, memory, webui, prompt, headless, backend.
    No-op if debug_logs_enabled is False. Silently ignores errors.
    """
    if not is_debug_logging_enabled() or domain not in ALLOWED_DOMAINS:
        return
    try:
        path = get_dated_log_path(domain, "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def append_domain_log_always(domain: str, message: str) -> None:
    """
    Append one timestamped line to {domain}_YYYY-MM-DD.log.
    Respects debug_logs_enabled — no-op when debug logging is off.
    """
    if not is_debug_logging_enabled() or domain not in ALLOWED_DOMAINS:
        return
    try:
        path = get_dated_log_path(domain, "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def log_attachment(event: str, **kwargs) -> None:
    """
    Attachment diagnostic log → attach_YYYY-MM-DD.log when debug_logs_enabled.
    Helps diagnose why specific PDFs fail to be seen by the agent.
    GC deletes old log files automatically (gc_max_age_hours config).

    Usage:
        log_attachment("FILE_RECEIVED", session="xxx", name="foo.pdf", size_bytes=1234567)
        log_attachment("EXTRACT_DONE", name="foo.pdf", content_len=450, preview="### PDF: foo...")
        log_attachment("SAVE_OK", session="xxx", docs=1)
        log_attachment("AGENT_SEES", session="xxx", docs=1, names=["foo.pdf"])
    """
    if not is_debug_logging_enabled():
        return
    try:
        path = get_dated_log_path("attach", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        parts = [f"{k}={v!r}" for k, v in kwargs.items()]
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{event}] {' '.join(parts)}\n")
    except Exception:
        pass


def append_domain_log_block(domain: str, first_line: str, rest_lines: Optional[List[str]] = None) -> None:
    """
    Append a timestamped first line and optional continuation lines (indented, no extra timestamp).
    Use for multi-line blocks (e.g. full system prompt dump) into {domain}_YYYY-MM-DD.log.
    No-op if debug_logs_enabled is False.
    """
    if not is_debug_logging_enabled() or domain not in ALLOWED_DOMAINS:
        return
    try:
        path = get_dated_log_path(domain, "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {first_line}\n")
            for line in rest_lines or []:
                f.write(f"    {line}\n")
    except Exception:
        pass


def _timeline_prev_hash(path: Path) -> str:
    """Return the hash of the last event in the timeline JSONL file, or 'GENESIS' if empty/missing."""
    try:
        if not path.exists():
            return "GENESIS"
        with open(path, "rb") as f:
            # Seek to last non-empty line efficiently
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return "GENESIS"
            # Walk backwards to find the last newline
            pos = size - 1
            while pos > 0:
                f.seek(pos)
                ch = f.read(1)
                if ch == b"\n" and pos < size - 1:
                    break
                pos -= 1
            f.seek(pos + 1 if pos > 0 else 0)
            last_line = f.read().decode("utf-8", errors="replace").strip()
        if not last_line:
            return "GENESIS"
        obj = json.loads(last_line)
        return obj.get("hash", "GENESIS")
    except Exception:
        return "GENESIS"


def _timeline_hash(event_dict: Dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON representation of the event (excluding the 'hash' field)."""
    payload = {k: v for k, v in event_dict.items() if k != "hash"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def log_timeline_event(event_type: str, **kwargs) -> None:
    """
    Append one JSONL event to timeline_YYYY-MM-DD.jsonl when debug_logs_enabled.
    Each event carries a SHA-256 hash chain: prev_hash → hash — enables tamper detection.

    event_type: 'tool_start' | 'tool_end' | 'subagent_start' | 'subagent_end' | 'thinking_run'
                | 'ww_train_start' | 'ww_train_end' (Whare Wananga tool-training runs, paired by run_id)
    kwargs: tool, call_id, session, scope, args, status, duration_s, result, task_id, agent_type, run_id, ...
            Pass _ts='2026-...' to override the event timestamp (e.g. for historical thinking runs).
    """
    if not is_debug_logging_enabled():
        return
    try:
        ts_override: Optional[str] = kwargs.pop("_ts", None)
        path = get_dated_log_path("timeline", "jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        prev_hash = _timeline_prev_hash(path)
        event: Dict[str, Any] = {
            "ts": ts_override or datetime.now().isoformat(),
            "type": event_type,
            "prev_hash": prev_hash,
        }
        event.update(kwargs)
        event["hash"] = _timeline_hash(event)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def log_thinking_run(
    run_id: str,
    scope_key: str,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    messages: List[Dict[str, Any]],
) -> None:
    """
    Append a thinking-mode run to logs/vaf_think.log when debug_logs_enabled.
    One log file for all users; each run gets a separator block with run metadata
    and a human-readable summary of what the agent did.
    """
    if not is_debug_logging_enabled():
        return
    try:
        ts = datetime.now().isoformat()

        lines: list[str] = []
        lines.append(f"{'=' * 80}")
        lines.append(f"[THINKING RUN] {ts}")
        lines.append(f"  run_id:    {run_id}")
        lines.append(f"  user:      {scope_key}")
        lines.append(f"  started:   {started_at}")
        lines.append(f"  ended:     {ended_at}")
        lines.append(f"  duration:  {duration_seconds:.1f}s")
        lines.append(f"  turns:     {len([m for m in messages if isinstance(m, dict) and m.get('role') == 'assistant'])}")
        lines.append("")

        # Log each message in a readable format
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "?")
            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []

            if role == "system":
                # Skip system prompt (too long), just note it
                lines.append(f"  [{role}] (system prompt, {len(content)} chars)")
            elif role == "user":
                # Truncate long user prompts
                preview = content[:300] + "..." if len(content) > 300 else content
                lines.append(f"  [{role}] {preview}")
            elif role == "assistant":
                if tool_calls:
                    tools_str = ", ".join(str(t) for t in tool_calls)
                    lines.append(f"  [{role}] Tools: {tools_str}")
                if content and content != "(no content)":
                    preview = content[:500] + "..." if len(content) > 500 else content
                    lines.append(f"  [{role}] {preview}")
                elif not tool_calls:
                    lines.append(f"  [{role}] (no content)")
            elif role == "tool":
                # Tool results – just note them briefly
                preview = content[:200] + "..." if len(content) > 200 else content
                lines.append(f"  [{role}] {preview}")

        lines.append("")

        path = get_dated_log_path("vaf_think", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        log_timeline_event(
            "thinking_run",
            _ts=started_at,
            run_id=run_id,
            scope=scope_key,
            ended_at=ended_at,
            duration_s=round(duration_seconds, 2),
        )
    except Exception:
        pass
