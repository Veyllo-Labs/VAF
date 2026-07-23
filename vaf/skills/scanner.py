# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Native skill security scanner.

A lightweight, static (no-LLM, no-network) safety gate for uploaded / authored
Skills. Concept inspired by NVIDIA SkillSpector's risk taxonomy — implemented
fresh for VAF, no third-party code or dependency. It exists because the skills
feature lets users upload SKILL.md bundles whose instructions (prompt-injection)
and bundled scripts (dangerous code) are loaded by the agent on demand.

It scans:
  - the SKILL.md instruction body (prompt-injection, exfiltration, secrets, …)
  - bundled code/text files (dangerous exec, network exfil, hardcoded secrets, …)

It returns findings + a 0-100 risk score + a level. The caller gates on it:
  high  -> block (admin may override)
  medium-> warn (allowed)
  low   -> informational

Static only: regex/heuristics. Never executes anything. False positives are
possible by design — hence the admin override on the high block.

It also exposes a small content-hashing facility (SHA-2 and SHA-3) used for
skill integrity fingerprints - see the "content hashing" section below. Like
the scan itself it only reads bytes, never executes, and pulls in no
dependency (hashlib is stdlib).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List

# ── severity / scoring ────────────────────────────────────────────────────────

SEVERITY_WEIGHT = {"high": 40, "medium": 15, "low": 5}

# Only scan text files we can reasonably analyse; skip binaries / huge blobs.
_MAX_FILE_BYTES = 512 * 1024
_TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".zsh",
    ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".env", ".html", ".xml", "",
}

# Hidden / control characters often used to smuggle instructions past a human
# reviewer (zero-width joiners, BOM mid-text, bidi overrides).
_HIDDEN_CHARS = {
    0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,              # zero-width
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,              # bidi embedding/override
    0x2066, 0x2067, 0x2068, 0x2069,                      # bidi isolates
}

# ── rule catalogue ────────────────────────────────────────────────────────────
# applies: "body" (SKILL.md prose), "code" (bundled files), "any" (both).

_RULES: List[Dict[str, Any]] = [
    # Prompt injection / instruction subversion (body)
    {"id": "pi_ignore", "cat": "prompt_injection", "sev": "high", "applies": "body",
     "re": r"(?i)ignore\s+(all\s+|any\s+)?(previous|prior|the\s+above|earlier)\s+instructions",
     "msg": "Instruction to ignore previous/system instructions (prompt injection)."},
    {"id": "pi_disregard", "cat": "prompt_injection", "sev": "high", "applies": "body",
     "re": r"(?i)disregard\s+(the\s+)?(previous|above|prior|system|earlier)",
     "msg": "Instruction to disregard prior/system context (prompt injection)."},
    {"id": "pi_role_override", "cat": "prompt_injection", "sev": "medium", "applies": "body",
     "re": r"(?i)\b(you are now|act as if|developer mode|jailbreak|DAN mode)\b",
     "msg": "Role-override / jailbreak phrasing."},
    {"id": "sys_prompt_leak", "cat": "system_prompt_leak", "sev": "high", "applies": "body",
     "re": r"(?i)(reveal|print|show|repeat|output|dump)\b.{0,40}\b(system\s*prompt|your\s+instructions|initial\s+prompt)",
     "msg": "Attempt to extract the system prompt / hidden instructions."},
    {"id": "hide_from_user", "cat": "covert_action", "sev": "medium", "applies": "body",
     "re": r"(?i)\b(do not|don'?t|never|without)\b.{0,30}\b(tell|inform|notify|show|ask)\b.{0,20}\b(the\s+)?user",
     "msg": "Instruction to act without informing the user (covert action)."},
    # Data exfiltration (any)
    {"id": "exfil_words", "cat": "data_exfiltration", "sev": "high", "applies": "any",
     "re": r"(?i)\b(exfiltrat\w*|leak\s+(the\s+)?(data|secret|credential|token)|steal\s+(the\s+)?(data|secret|credential|token))",
     "msg": "Explicit data-exfiltration intent."},
    {"id": "send_external", "cat": "data_exfiltration", "sev": "medium", "applies": "any",
     "re": r"(?i)\b(send|upload|post|transmit|forward)\b.{0,40}(https?://|webhook|external\s+server|attacker)",
     "msg": "Sends/uploads data to an external destination."},
    # Fetch-and-execute / destructive shell (any)
    {"id": "fetch_exec", "cat": "remote_code_exec", "sev": "high", "applies": "any",
     "re": r"(?i)\b(curl|wget)\b[^\n|]{0,200}\|\s*(sudo\s+)?(sh|bash|zsh|python\d?)\b",
     "msg": "Pipe-to-shell remote code execution (curl|sh)."},
    {"id": "rm_rf", "cat": "destructive", "sev": "high", "applies": "any",
     "re": r"(?i)\brm\s+-rf?\b\s+(/|~|\$HOME|\*)",
     "msg": "Destructive recursive delete of a root/home path."},
    # Secret / credential access (any)
    {"id": "secret_paths", "cat": "credential_access", "sev": "medium", "applies": "any",
     "re": r"(?i)(~/?\.ssh|\bid_rsa\b|\.aws/credentials|/etc/(passwd|shadow)|\.netrc\b)",
     "msg": "Accesses sensitive credential / system files."},
    {"id": "env_secrets", "cat": "credential_access", "sev": "low", "applies": "code",
     "re": r"\b(os\.environ|process\.env|getenv)\b",
     "msg": "Reads environment variables (possible secret access)."},
    # Hardcoded secrets (any)
    {"id": "aws_key", "cat": "hardcoded_secret", "sev": "high", "applies": "any",
     "re": r"\bAKIA[0-9A-Z]{16}\b", "msg": "Hardcoded AWS access key id."},
    {"id": "openai_key", "cat": "hardcoded_secret", "sev": "high", "applies": "any",
     "re": r"\bsk-[A-Za-z0-9]{20,}\b", "msg": "Hardcoded API key (sk-…)."},
    {"id": "github_token", "cat": "hardcoded_secret", "sev": "high", "applies": "any",
     "re": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "msg": "Hardcoded GitHub token."},
    {"id": "private_key", "cat": "hardcoded_secret", "sev": "high", "applies": "any",
     "re": r"-----BEGIN (RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----",
     "msg": "Embedded private key."},
    # Dangerous code constructs (code)
    {"id": "py_eval_exec", "cat": "dangerous_code", "sev": "high", "applies": "code",
     "re": r"(?<![A-Za-z0-9_])(eval|exec)\s*\(", "msg": "Dynamic code execution (eval/exec)."},
    {"id": "py_os_system", "cat": "dangerous_code", "sev": "high", "applies": "code",
     "re": r"\bos\.system\s*\(", "msg": "Shell command execution via os.system."},
    {"id": "py_subprocess_shell", "cat": "dangerous_code", "sev": "high", "applies": "code",
     "re": r"subprocess\.(run|call|Popen|check_output|check_call)\b[^\n)]{0,160}shell\s*=\s*True",
     "msg": "subprocess with shell=True (command injection risk)."},
    {"id": "py_pickle", "cat": "dangerous_code", "sev": "medium", "applies": "code",
     "re": r"\bpickle\.loads?\s*\(", "msg": "Untrusted deserialization via pickle."},
    {"id": "py_dunder_import", "cat": "dangerous_code", "sev": "medium", "applies": "code",
     "re": r"__import__\s*\(", "msg": "Dynamic __import__ (obfuscation/evasion)."},
    {"id": "b64_decode", "cat": "obfuscation", "sev": "medium", "applies": "code",
     "re": r"\b(b64decode|base64\.b64decode|atob)\s*\(", "msg": "Base64 decoding (possible payload obfuscation)."},
    {"id": "net_post", "cat": "network", "sev": "medium", "applies": "code",
     "re": r"\b(requests\.(post|put|patch)|urllib\.request|http\.client|socket\.socket)\b",
     "msg": "Outbound network call (possible exfiltration channel)."},
    {"id": "js_eval", "cat": "dangerous_code", "sev": "high", "applies": "code",
     "re": r"(?<![A-Za-z0-9_])(eval\s*\(|new\s+Function\s*\()", "msg": "Dynamic code execution in JS (eval/new Function)."},
    {"id": "js_child_process", "cat": "dangerous_code", "sev": "high", "applies": "code",
     "re": r"\b(child_process|execSync\s*\(|spawnSync\s*\()", "msg": "Process spawning in JS (child_process)."},
]

_COMPILED = [{**r, "rx": re.compile(r["re"])} for r in _RULES]


# ── finding helpers ───────────────────────────────────────────────────────────

def _snippet(text: str, start: int, end: int) -> str:
    s = text[max(0, start - 20): min(len(text), end + 20)].replace("\n", " ").strip()
    return (s[:120] + "…") if len(s) > 120 else s


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _scan_text(text: str, scope: str, file_label: str) -> List[Dict[str, Any]]:
    """Run rules whose applies matches `scope` ('body' or 'code') or 'any'."""
    findings: List[Dict[str, Any]] = []
    seen = set()
    for r in _COMPILED:
        if r["applies"] != scope and r["applies"] != "any":
            continue
        m = r["rx"].search(text)
        if not m:
            continue
        key = (r["id"], file_label)
        if key in seen:
            continue
        seen.add(key)
        findings.append({
            "id": r["id"],
            "category": r["cat"],
            "severity": r["sev"],
            "message": r["msg"],
            "file": file_label,
            "line": _line_of(text, m.start()),
            "snippet": _snippet(text, m.start(), m.end()),
        })
    # Hidden / control characters (heuristic, not a regex rule).
    for i, ch in enumerate(text):
        if ord(ch) in _HIDDEN_CHARS:
            findings.append({
                "id": "hidden_chars", "category": "obfuscation", "severity": "high",
                "message": "Hidden / bidi control characters (smuggled instructions).",
                "file": file_label, "line": _line_of(text, i), "snippet": f"U+{ord(ch):04X}",
            })
            break
    return findings


def _score_and_level(findings: List[Dict[str, Any]]) -> tuple[int, str]:
    score = min(100, sum(SEVERITY_WEIGHT.get(f["severity"], 0) for f in findings))
    if any(f["severity"] == "high" for f in findings):
        level = "high"
    elif any(f["severity"] == "medium" for f in findings):
        level = "medium"
    elif findings:
        level = "low"
    else:
        level = "clean"
    return score, level


def _result(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Stable order: high → medium → low, then by file.
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f.get("file", ""), f.get("line", 0)))
    score, level = _score_and_level(findings)
    return {"score": score, "level": level, "findings": findings, "blocked": level == "high"}


class SkillScanBlocked(Exception):
    """Raised when a skill is blocked by the security scanner (high risk).

    Carries the full scan result so callers can surface findings and offer an
    admin override.
    """
    def __init__(self, scan: Dict[str, Any]):
        self.scan = scan
        super().__init__(format_findings(scan))


# ── public API ────────────────────────────────────────────────────────────────

def scan_skill_md_text(content: str) -> Dict[str, Any]:
    """Scan a SKILL.md text (body-level rules). For editor-authored skills."""
    body = content
    # Drop the YAML frontmatter so name/description metadata isn't scanned as prose.
    m = re.match(r"^---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n?(.*)$", content, re.DOTALL)
    if m:
        body = m.group(1)
    return _result(_scan_text(body, "body", "SKILL.md"))


def scan_skill_folder(folder: Path | str) -> Dict[str, Any]:
    """Scan a skill folder: the SKILL.md body plus every bundled text/code file."""
    folder = Path(folder)
    findings: List[Dict[str, Any]] = []

    skill_md = folder / "SKILL.md"
    if skill_md.exists():
        try:
            findings += scan_skill_md_text(skill_md.read_text(encoding="utf-8-sig", errors="replace"))["findings"]
        except Exception:
            pass

    base = folder.resolve()
    for p in sorted(folder.rglob("*")):
        if p.is_dir() or p.name == "SKILL.md" or p.name.startswith("."):
            continue
        try:
            if not p.resolve().is_relative_to(base):
                continue  # symlink escape — never read
            if p.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
            rel = p.relative_to(folder).as_posix()
            findings += _scan_text(p.read_text(encoding="utf-8", errors="replace"), "code", rel)
        except (OSError, ValueError):
            continue
    return _result(findings)


# ── content hashing (SHA-2 / SHA-3) ───────────────────────────────────────────
# A small, dependency-free hashing facility for integrity checks: computing a
# stable fingerprint of a skill's bytes so tampering after install is provable,
# and reusable for any other "did these bytes change / do they match" need.
# Deliberately allow-listed to strong algorithms only (no md5/sha1) - a caller
# must not be able to request a weak digest for an integrity purpose.

# Canonical hashlib names -> the supported set. SHA-2 = sha256/sha512,
# SHA-3 = sha3_256/sha3_512. Both families ship in the stdlib (hashlib).
SUPPORTED_HASH_ALGOS = frozenset({"sha256", "sha512", "sha3_256", "sha3_512"})
DEFAULT_HASH_ALGO = "sha256"

# Friendly spellings a caller might pass -> canonical name.
_HASH_ALGO_ALIASES = {
    "sha256": "sha256", "sha-256": "sha256", "sha2": "sha256", "sha-2": "sha256",
    "sha512": "sha512", "sha-512": "sha512",
    "sha3": "sha3_256", "sha-3": "sha3_256", "sha3-256": "sha3_256", "sha3_256": "sha3_256",
    "sha3-512": "sha3_512", "sha3_512": "sha3_512",
}

_HASH_CHUNK = 64 * 1024


def resolve_hash_algo(algo: str = DEFAULT_HASH_ALGO) -> str:
    """Normalize an algorithm spelling to its canonical name, or raise
    ValueError for anything not in the allow-list (weak/unknown digests)."""
    key = str(algo or "").strip().lower()
    canon = _HASH_ALGO_ALIASES.get(key)
    if canon not in SUPPORTED_HASH_ALGOS:
        raise ValueError(
            f"Unsupported hash algorithm {algo!r}; use one of "
            f"{', '.join(sorted(SUPPORTED_HASH_ALGOS))} (SHA-2 / SHA-3)."
        )
    return canon


def hash_bytes(data: bytes, algo: str = DEFAULT_HASH_ALGO) -> str:
    """Hex digest of raw bytes with the chosen SHA-2/SHA-3 algorithm."""
    return hashlib.new(resolve_hash_algo(algo), data).hexdigest()


def hash_text(text: str, algo: str = DEFAULT_HASH_ALGO) -> str:
    """Hex digest of a string (UTF-8 encoded) with the chosen algorithm."""
    return hash_bytes(text.encode("utf-8"), algo)


def hash_skill_folder(folder: Path | str, algo: str = DEFAULT_HASH_ALGO) -> str:
    """Deterministic content fingerprint of a skill folder for integrity checks.

    Independent of filesystem walk order and machine: every regular file
    (including binaries - integrity must cover everything the skill ships, not
    only the text files the scanner reads) is folded in as a canonical,
    length-prefixed record `relpath \\0 size \\0 file-digest \\0`, processed in
    sorted path order. Same content always yields the same hash; any changed,
    added, removed, renamed or moved byte changes it. Symlinks and paths that
    resolve outside the folder are skipped (never followed). Files are streamed,
    so memory stays bounded regardless of size. A file that fails to read is
    skipped whole (never a partial record), keeping the digest well-defined.
    """
    canon = resolve_hash_algo(algo)
    folder = Path(folder)
    base = folder.resolve()

    records: List[tuple[str, Path]] = []
    for p in folder.rglob("*"):
        try:
            if p.is_dir() or p.is_symlink():
                continue
            if not p.resolve().is_relative_to(base):
                continue  # symlink/traversal escape — never read
            records.append((p.relative_to(folder).as_posix(), p))
        except (OSError, ValueError):
            continue

    h = hashlib.new(canon)
    for rel, p in sorted(records, key=lambda t: t[0]):
        fh = hashlib.new(canon)
        size = 0
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                    fh.update(chunk)
                    size += len(chunk)
        except OSError:
            continue  # unreadable -> skip the whole record, digest stays well-defined
        h.update(rel.encode("utf-8")); h.update(b"\0")
        h.update(str(size).encode("ascii")); h.update(b"\0")
        h.update(fh.digest()); h.update(b"\0")
    return h.hexdigest()


def format_findings(scan: Dict[str, Any], limit: int = 12) -> str:
    """Human-readable one-block summary for an error message / log."""
    findings = scan.get("findings", [])
    if not findings:
        return f"Security scan: clean (score {scan.get('score', 0)})."
    head = f"Security scan: {scan.get('level', '?').upper()} risk (score {scan.get('score', 0)}/100), {len(findings)} finding(s):"
    lines = [head]
    for f in findings[:limit]:
        loc = f.get("file", "?")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"  [{f['severity'].upper()}] {f['category']} — {f['message']} ({loc})")
    if len(findings) > limit:
        lines.append(f"  … and {len(findings) - limit} more.")
    return "\n".join(lines)


def emit_skill_security_event(kind: str, source: str, skill_id: str,
                              scan: Dict[str, Any]) -> None:
    """Mirror a scan decision into the security event log (dashboard +
    security_<date>.log). kinds: skill_blocked (HIGH stopped an install/update),
    skill_override (admin explicitly accepted a HIGH result). Lazy import,
    never raises - auditing must not break the skill pipeline."""
    try:
        from vaf.core.security_events import log_security_event
        cats = ",".join(sorted({str(f.get("category", "")) for f in (scan.get("findings") or []) if f.get("category")}))
        log_security_event(
            kind,
            detail=f"{source}:{skill_id} score={scan.get('score')} cats={cats}"[:200],
        )
    except Exception:
        pass
