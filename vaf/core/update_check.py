# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Is a newer VAF published, and how is an update started from anywhere?

Applying an update is the CLI's job and stays there (`vaf/cli/cmd/update.py`):
it stops the service, swaps the checkout, reinstalls dependencies, migrates,
starts again and rolls back on failure. ASKING the question, and starting that
process from outside a terminal, are not CLI concerns - the web server needs
both, and reaching into `vaf.cli` for them would point the dependency the wrong
way round. So the check core lives here and the CLI imports it.

Two files carry the state, both under the user's home and both written by the
CLI updater long before this module existed:

- `~/.vaf/update_cache.json` - what the last check found and WHEN. The "last
  checked" line in the UI reads it without asking GitHub, which is what keeps
  the promise in README.md true: VAF makes its version check at startup and
  otherwise only when a person presses the button.
- `~/.vaf/last_update.json` - a breadcrumb written before an update mutates
  anything and cleared when it finishes. Present afterwards means an update
  did not complete, and `vaf update --recover` is the way out.
- `~/.vaf/update_result.json` - how the last update run ENDED. The breadcrumb
  only distinguishes "running" from "not running"; a failed update that rolled
  itself back clears the breadcrumb and restarts the OLD version, which from
  the outside is indistinguishable from "nothing ever happened". This file is
  the missing outcome: the updater clears it when a run starts and writes
  exactly one at every exit, so a client that finds the old version answering
  again can tell "the updater has not stopped the service yet" (no result)
  from "it failed and rolled back" (a fresh result says so).
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import requests

from vaf import __version__

GITHUB_REPO = "Veyllo-Labs/VAF"
REPO_URL = f"https://github.com/{GITHUB_REPO}.git"
# The LIST endpoint (newest first) - unlike /releases/latest it INCLUDES prereleases, so an alpha
# build (e.g. 0.1.0aN, published as a GitHub prerelease) is visible to the updater. Eligibility is
# then decided in code (eligible_prereleases) rather than by the endpoint.
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

CACHE_TTL_SECONDS = 86400


# ── version / release helpers ────────────────────────────────────────────────

def eligible_prereleases(include_prereleases: "bool | None" = None) -> bool:
    """Whether the update check should consider GitHub PRERELEASES.

    `include_prereleases` wins if given (CLI --pre/--stable). Else the `update_include_prereleases`
    config key wins if set (True/False). Else AUTO: track prereleases only when the INSTALLED build
    is itself a prerelease - so an alpha (0.1.0aN) follows alpha releases, while a stable build
    follows stable releases only.
    """
    if include_prereleases is not None:
        return bool(include_prereleases)
    try:
        from vaf.core.config import Config
        cfg = Config.get("update_include_prereleases", None)
        if cfg is not None:
            return bool(cfg)
    except Exception:
        pass
    try:
        from packaging.version import Version
        return Version(__version__).is_prerelease
    except Exception:
        return False


def resolve_latest_release(include_prereleases: "bool | None" = None):
    """Fetch the newest ELIGIBLE published VAF release from GitHub (offline-safe).

    Uses the releases LIST endpoint (newest first) instead of /releases/latest, because the latter
    excludes prereleases - during the alpha that hides every release. Stable releases are always
    eligible; prereleases only when `eligible_prereleases()` allows.

    Returns `(release, why)`: the highest-version eligible release as {tag, version (tag without
    leading 'v'), html_url, body, prerelease} with why=None, or `(None, reason)` naming WHY there
    is no answer. The reason exists because three different situations used to collapse into one
    sentence ("offline, or none published yet") whose two halves suggest opposite reactions - and
    the live case that exposed it was neither: GitHub's ANONYMOUS API limit is 60 requests/hour
    per IP, shared by every process on the network, so a burst from any tool on the same
    connection makes the updater claim there is no release while one is sitting published.
    Reasons: "rate_limited:<epoch>", "http:<code>", "offline", "malformed", "none".
    """
    incl = eligible_prereleases(include_prereleases)
    try:
        from packaging.version import parse as _parse
        # per_page=100 (the max) instead of GitHub's default 30, so eligibility is computed over the
        # full release set, not just the 30 most-recently-created tags.
        try:
            resp = requests.get(RELEASES_URL, timeout=5, params={"per_page": 100},
                                headers={"Accept": "application/vnd.github+json"})
        except requests.RequestException:
            return None, "offline"
        if resp.status_code != 200:
            # 403 is how GitHub says "rate limited" (429 is the documented spare);
            # the remaining-header check keeps a real permission 403 honest.
            if (resp.status_code in (403, 429)
                    and resp.headers.get("X-RateLimit-Remaining") == "0"):
                return None, f"rate_limited:{resp.headers.get('X-RateLimit-Reset', '')}"
            return None, f"http:{resp.status_code}"
        data = resp.json()
        if not isinstance(data, list):
            return None, "malformed"
        best = None
        best_v = None
        for r in data:
            if not isinstance(r, dict) or r.get("draft"):
                continue
            if r.get("prerelease") and not incl:
                continue
            tag = r.get("tag_name", "") or ""
            ver = tag[1:] if tag.startswith("v") else tag
            if not ver:
                continue
            try:
                pv = _parse(ver)
            except Exception:
                continue
            if best_v is None or pv > best_v:
                best_v, best = pv, {
                    "tag": tag,
                    "version": ver,
                    "html_url": r.get("html_url", ""),
                    "body": r.get("body", ""),
                    "prerelease": bool(r.get("prerelease", False)),
                }
        return best, (None if best is not None else "none")
    except Exception:
        pass
    return None, "malformed"


def resolve_failure_message(why: "str | None") -> str:
    """One honest sentence per reason - each names the reaction it calls for."""
    why = why or ""
    if why.startswith("rate_limited"):
        suffix = ""
        try:
            reset = int(why.split(":", 1)[1])
            suffix = f" - try again after {datetime.fromtimestamp(reset).strftime('%H:%M')}"
        except Exception:
            pass
        return ("GitHub's API rate limit for this network is used up"
                f"{suffix}. This says nothing about whether a release exists.")
    if why == "offline":
        return "Could not reach GitHub (offline, or a proxy in the way?)."
    if why == "none":
        return "No published release found yet."
    return f"GitHub answered unexpectedly ({why or 'unknown error'})."


def compare_versions(current: str, latest: str) -> int:
    """Return -1 if current < latest, 0 if equal, 1 if current > latest."""
    try:
        from packaging.version import parse
        c, lt = parse(current), parse(latest)
    except Exception:
        c, lt = current, latest
    return (c > lt) - (c < lt)


# ── the on-disk answer: what was found, and when ─────────────────────────────

def update_cache_path() -> Path:
    return Path.home() / ".vaf" / "update_cache.json"


def read_update_cache() -> Optional[Dict[str, Any]]:
    """The last check's result, or None. Never asks the network.

    Fresh or stale is the caller's business: the dialog shows WHEN the check
    happened, so an old answer is information rather than a reason to fetch.
    """
    try:
        data = json.loads(update_cache_path().read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("checked_at"):
        return None
    return {
        "checked_at": str(data.get("checked_at") or ""),
        "latest_version": data.get("latest_version"),
        "relevant": bool(data.get("relevant")),
    }


def _write_update_cache(version: Optional[str], relevant: bool) -> str:
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        cache = update_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "checked_at": checked_at,
            "latest_version": version,
            "relevant": relevant,
        }))
    except Exception:
        pass
    return checked_at


def cached_or_fetch_latest():
    """Return {version, relevant} from a <24h cache, else fetch once and cache it.

    None when the latest version is unknown (offline). `relevant` is True when the
    latest published release is newer than the installed version.
    """
    cached = read_update_cache()
    if cached and cached.get("latest_version"):
        try:
            checked = datetime.fromisoformat(cached["checked_at"])
            if (datetime.now(timezone.utc) - checked).total_seconds() < CACHE_TTL_SECONDS:
                return {"version": cached.get("latest_version"),
                        "relevant": bool(cached.get("relevant"))}
        except Exception:
            pass
    rel, _why = resolve_latest_release()   # background check: silent either way
    if not rel or not rel.get("version"):
        return None
    version = rel["version"]
    relevant = compare_versions(__version__, version) < 0
    _write_update_cache(version, relevant)
    return {"version": version, "relevant": relevant}


def check_now(include_prereleases: "bool | None" = None) -> Dict[str, Any]:
    """Ask GitHub right now, ignoring the cache, and record the answer.

    This is the button and the command, never a background timer: the cache
    above is what everything else reads, so nothing in VAF reaches the network
    because a window happened to open.
    """
    rel, why = resolve_latest_release(include_prereleases)
    latest = (rel or {}).get("version")
    relevant = bool(latest) and compare_versions(__version__, latest) < 0
    checked_at = _write_update_cache(latest, relevant) if latest else \
        (read_update_cache() or {}).get("checked_at", "")
    return {
        "current": __version__,
        "latest": latest,
        "relevant": relevant,
        "checked_at": checked_at,
        "release_url": (rel or {}).get("html_url") or "",
        "prerelease": bool((rel or {}).get("prerelease")),
        "why": why,
        "message": resolve_failure_message(why) if latest is None else "",
    }


# ── the breadcrumb: did the last update finish? ──────────────────────────────

def last_update_breadcrumb_path() -> Path:
    return Path.home() / ".vaf" / "last_update.json"


def write_breadcrumb(data: dict) -> None:
    p = last_update_breadcrumb_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def clear_breadcrumb() -> None:
    last_update_breadcrumb_path().unlink(missing_ok=True)


def read_last_update() -> Optional[Dict[str, Any]]:
    """The unfinished-update breadcrumb, or None when the last one completed."""
    try:
        data = json.loads(last_update_breadcrumb_path().read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── the outcome: how did the last update run end? ────────────────────────────

# The four ways an update run can end. "failed" is an abort BEFORE anything was
# mutated (nothing to roll back); "rolled_back" is a failure after mutation with
# a successful restore; "recover_needed" means the restore itself failed and the
# breadcrumb was kept for `vaf update --recover`.
UPDATE_OUTCOMES = ("succeeded", "rolled_back", "recover_needed", "failed")


def update_result_path() -> Path:
    return Path.home() / ".vaf" / "update_result.json"


def write_update_result(outcome: str, from_version: str, target_version: str,
                        error: Optional[str] = None) -> None:
    """Record how an update run ended. Exactly one write per run, at its exit.

    Plain status file like the cache and the breadcrumb above: tolerant reader,
    no schema tag. The NEW version reads a result the OLD version wrote (and
    vice versa after a rollback), so unknown fields must never be an error.
    """
    if outcome not in UPDATE_OUTCOMES:
        raise ValueError(f"unknown update outcome: {outcome!r}")
    p = update_result_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "outcome": outcome,
            "from_version": from_version,
            "target_version": target_version,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            # Cap the stored text: the updater's real log is update_web.log,
            # this is the one-line reason a UI can show.
            "error": (str(error)[:500] if error else None),
        }, indent=2))
    except Exception:
        pass    # the outcome file is best-effort; the update itself must not fail on it


def clear_update_result() -> None:
    update_result_path().unlink(missing_ok=True)


def read_update_result() -> Optional[Dict[str, Any]]:
    """The last run's outcome, or None (no run recorded yet, or unreadable)."""
    try:
        data = json.loads(update_result_path().read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("outcome") not in UPDATE_OUTCOMES:
        return None
    return {
        "outcome": str(data.get("outcome")),
        "from_version": str(data.get("from_version") or ""),
        "target_version": str(data.get("target_version") or ""),
        "finished_at": str(data.get("finished_at") or ""),
        "error": (str(data["error"]) if data.get("error") else None),
    }


# ── starting an update from outside a terminal ───────────────────────────────

def update_command_hint() -> str:
    """The platform-correct command a person can type to update by hand.

    `vaf` is a console-script/alias/shim that is not always on PATH (esp. on Windows,
    where the installer ships `vaf.bat` and adds it to PATH, but a NEW terminal is needed
    for that to take effect). Point users at a command that always works regardless:
    the shipped run script in the install directory, which forwards its args to the CLI.
    """
    if os.name == "nt":
        return "run_vaf.bat update"
    return "vaf update"


def update_log_path() -> Path:
    return Path.home() / ".vaf" / "logs" / "update_web.log"


def spawn_update_process(extra_args: Sequence[str] = ("--yes",),
                         log_path: Optional[Path] = None) -> Dict[str, Any]:
    """Start `vaf update` as a process that OUTLIVES this one.

    An update stops the VAF service, and the web server asking for the update
    IS that service - a child in this process group would be killed halfway
    through its own update. So it is detached: a new session on POSIX, a
    detached process group on Windows, output to a file because there is no
    console to inherit, and a working directory outside the checkout the update
    is about to swap.

    In server mode on Linux there is a second way to die: the systemd user unit
    kills its whole control group on stop, and a plain child sits in it. Where
    `systemd-run` exists the updater is placed in its own transient unit, which
    is outside that group. Where it does not, the caller is told to run the
    update from a terminal instead of being handed a process that will be shot.
    """
    log = Path(log_path) if log_path else update_log_path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    argv = [sys.executable, "-m", "vaf.main", "update", *list(extra_args)]
    via = "popen"
    if _server_mode() and platform.system() == "Linux":
        systemd_run = _which("systemd-run")
        if not systemd_run:
            raise RuntimeError(
                "In server mode an update must be started from a terminal "
                f"(`{update_command_hint()}`): without systemd-run the updater would be "
                "stopped together with the service it restarts."
            )
        argv = [systemd_run, "--user", "--collect",
                f"--unit=vaf-update-{int(datetime.now(timezone.utc).timestamp())}",
                *argv]
        via = "systemd-run"

    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "cwd": str(Path.home()),
        "close_fds": True,
    }
    if platform.system() == "Windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    handle = None
    try:
        handle = open(log, "ab")
        kwargs["stdout"] = handle
        kwargs["stderr"] = subprocess.STDOUT
    except Exception:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(argv, **kwargs)
    finally:
        if handle is not None:
            try:
                handle.close()   # the child keeps its own descriptor
            except Exception:
                pass
    return {"pid": getattr(proc, "pid", None), "log": str(log), "via": via,
            "command": " ".join(argv)}


def _server_mode() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("server_mode", False))
    except Exception:
        return False


def _which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


def is_source_tree(root: Path) -> bool:
    """True when `root` is a VAF source tree the self-updater may manage.

    A git checkout and a downloaded ZIP both carry vaf/version.py AND
    requirements.txt at the root; a pip/wheel install (site-packages) and a
    foreign repository do not.
    """
    return (root / "vaf" / "version.py").exists() and (root / "requirements.txt").exists()


def describe_update_ability(root: Optional[Path] = None) -> Tuple[bool, str]:
    """Can an update be applied WITHOUT a terminal, and if not, why not?

    Three installs cannot: a package install (no source tree to swap), a
    non-git source tree, and a server-mode machine without systemd-run (the
    updater would be killed together with the service). Each gets a sentence
    naming what to do instead, because the alternative is a button that fails
    after the user has already been promised a restart.

    The non-git case is the subtle one. `vaf update` CAN adopt a downloaded ZIP
    by turning it into a git checkout, but it asks first, in a prompt that
    explains what `git reset --hard` will do to the source tree. An unattended
    run answers that prompt with yes, so offering the button here would perform
    a conversion the user was never shown. The terminal keeps the prompt, so
    the terminal is where that decision belongs.
    """
    tree = root if root is not None else Path(__file__).resolve().parents[2]
    if not is_source_tree(tree):
        return False, ("This VAF was installed as a package, so it cannot update itself "
                       "in place. Update it with `pip install -U --pre vaf`.")
    if not _is_git_checkout(tree):
        return False, ("This VAF was installed from a source archive rather than a git "
                       f"checkout. Run `{update_command_hint()}` in a terminal once: it "
                       "explains how it converts the folder into a checkout and asks "
                       "before touching your files. Updates work from here afterwards.")
    if _server_mode() and platform.system() == "Linux" and not _which("systemd-run"):
        return False, ("In server mode without systemd-run an update has to be started "
                       f"from a terminal: `{update_command_hint()}`.")
    return True, ""


def _is_git_checkout(root: Path) -> bool:
    """True when `root` is a git working tree the updater can move by tag."""
    try:
        return (root / ".git").exists()
    except Exception:
        return False
