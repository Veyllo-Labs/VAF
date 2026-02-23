"""
Shared log directory and domain log writers for VAF.
Consolidates logs into one file per domain (rag, memory, webui, prompt, headless, backend) with timestamps.
Respects Config.debug_logs_enabled: when False, no domain logs and no queue.log are written.
"""
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

# Domains get a single {domain}.log file each; use prefixes like [COMPACTION], [EMIT] inside messages.
ALLOWED_DOMAINS = ("rag", "memory", "webui", "prompt", "headless", "backend")


def is_debug_logging_enabled() -> bool:
    """True if domain logs and queue.log should be written (Advanced → Debug Logs)."""
    try:
        from vaf.core.config import Config
        return bool(Config.get("debug_logs_enabled", False))
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
    """Always append to logs/telegram_reply_YYYY-MM-DD.log (for diagnosing Telegram delivery). No-op on error."""
    try:
        path = get_dated_log_path("telegram_reply", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_discord_reply(message: str) -> None:
    """Always append to logs/discord_reply_YYYY-MM-DD.log (for diagnosing Discord delivery). No-op on error."""
    try:
        path = get_dated_log_path("discord_reply", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_qr(message: str) -> None:
    """Always append to logs/whatsapp_qr_YYYY-MM-DD.log (for diagnosing QR/link failures). No-op on error."""
    try:
        path = get_dated_log_path("whatsapp_qr", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_inbound(message: str) -> None:
    """Always append to logs/whatsapp_inbound_YYYY-MM-DD.log (for diagnosing inbound/self-chat). No-op on error."""
    try:
        path = get_dated_log_path("whatsapp_inbound", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_reply(message: str) -> None:
    """Always append to logs/whatsapp_reply_YYYY-MM-DD.log (for diagnosing WhatsApp delivery). No-op on error."""
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
    Append one timestamped line to {domain}_YYYY-MM-DD.log even when debug_logs_enabled is False.
    Use only for important diagnostics (e.g. [CALENDAR] status, [EMAIL_OAUTH]) so users
    can see what the backend did without enabling Debug Logs.
    """
    if domain not in ALLOWED_DOMAINS:
        return
    try:
        path = get_dated_log_path(domain, "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
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


def log_thinking_run(
    run_id: str,
    scope_key: str,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    messages: List[Dict[str, Any]],
) -> None:
    """
    Always append a thinking-mode run to logs/vaf_denk.log for debugging.
    One log file for all users; each run gets a separator block with run metadata
    and a human-readable summary of what the agent did.
    """
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

        path = get_dated_log_path("vaf_denk", "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
