"""
Shared log directory and domain log writers for VAF.
Consolidates logs into one file per domain (rag, memory, webui, prompt, headless, backend) with timestamps.
Respects Config.debug_logs_enabled: when False, no domain logs and no queue.log are written.
"""
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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


def log_telegram_reply(message: str) -> None:
    """Always append to logs/telegram_reply.log (for diagnosing Telegram delivery). No-op on error."""
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "telegram_reply.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_discord_reply(message: str) -> None:
    """Always append to logs/discord_reply.log (for diagnosing Discord delivery). No-op on error."""
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "discord_reply.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_qr(message: str) -> None:
    """Always append to logs/whatsapp_qr.log (for diagnosing QR/link failures). No-op on error."""
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "whatsapp_qr.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_inbound(message: str) -> None:
    """Always append to logs/whatsapp_inbound.log (for diagnosing inbound/self-chat). No-op on error."""
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "whatsapp_inbound.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def log_whatsapp_reply(message: str) -> None:
    """Always append to logs/whatsapp_reply.log (for diagnosing WhatsApp delivery). No-op on error."""
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "whatsapp_reply.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def append_domain_log(domain: str, message: str) -> None:
    """
    Append one timestamped line to {domain}.log.
    domain: one of rag, memory, webui, prompt, headless, backend.
    No-op if debug_logs_enabled is False. Silently ignores errors.
    """
    if not is_debug_logging_enabled() or domain not in ALLOWED_DOMAINS:
        return
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(log_dir / f"{domain}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def append_domain_log_always(domain: str, message: str) -> None:
    """
    Append one timestamped line to {domain}.log even when debug_logs_enabled is False.
    Use only for important diagnostics (e.g. [CALENDAR] status, [EMAIL_OAUTH]) so users
    can see what the backend did without enabling Debug Logs.
    """
    if domain not in ALLOWED_DOMAINS:
        return
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(log_dir / f"{domain}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def append_domain_log_block(domain: str, first_line: str, rest_lines: Optional[List[str]] = None) -> None:
    """
    Append a timestamped first line and optional continuation lines (indented, no extra timestamp).
    Use for multi-line blocks (e.g. full system prompt dump) into {domain}.log.
    No-op if debug_logs_enabled is False.
    """
    if not is_debug_logging_enabled() or domain not in ALLOWED_DOMAINS:
        return
    try:
        log_dir = get_app_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        with open(log_dir / f"{domain}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {first_line}\n")
            for line in rest_lines or []:
                f.write(f"    {line}\n")
    except Exception:
        pass
