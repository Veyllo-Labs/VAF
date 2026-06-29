# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF self-update.

`vaf update` targets the latest published GitHub Release (a git tag `v<version>`)
and updates the in-place git checkout: stop service -> fetch + checkout tag ->
reinstall deps -> invalidate web build -> run migrations -> restart -> verify,
with rollback to the previous commit on any failure. See docs/setup/RELEASING.md.

Safety rests on a verified fact: build artifacts (bin/, web/.next, node_modules,
venv) and all user state (~/.vaf) live OUTSIDE the git tree, so `git checkout`
only swaps tracked source and rollback is another checkout.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import typer

from vaf import __version__
from vaf.cli.cmd import service
from vaf.cli.cmd.git import is_git_repo, run_git
from vaf.cli.ui import UI

app = typer.Typer(help="Check for and apply VAF updates")

GITHUB_REPO = "Veyllo-Labs/VAF"
# The LIST endpoint (newest first) — unlike /releases/latest it INCLUDES prereleases, so an alpha
# build (e.g. 2.6.0aN, published as a GitHub prerelease) is visible to the updater. Eligibility is
# then decided in code (_eligible_prereleases) rather than by the endpoint.
_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"


class _UpdateError(Exception):
    """A recoverable failure during an update; triggers rollback."""


# ── version / release helpers ────────────────────────────────────────────────

def _eligible_prereleases(include_prereleases: "bool | None" = None) -> bool:
    """Whether the update check should consider GitHub PRERELEASES.

    `include_prereleases` wins if given (CLI --pre/--stable). Else the `update_include_prereleases`
    config key wins if set (True/False). Else AUTO: track prereleases only when the INSTALLED build
    is itself a prerelease — so an alpha (2.6.0aN) follows alpha releases, while a stable build
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


def _resolve_latest_release(include_prereleases: "bool | None" = None):
    """Fetch the newest ELIGIBLE published VAF release from GitHub (offline-safe -> None).

    Uses the releases LIST endpoint (newest first) instead of /releases/latest, because the latter
    excludes prereleases — during the alpha that hides every release. Stable releases are always
    eligible; prereleases only when `_eligible_prereleases()` allows. Returns the highest-version
    eligible release as {tag, version (tag without leading 'v'), html_url, body, prerelease}.
    """
    incl = _eligible_prereleases(include_prereleases)
    try:
        from packaging.version import parse as _parse
        resp = requests.get(_RELEASES_URL, timeout=5, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list):
            return None
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
        return best
    except Exception:
        pass
    return None


def _compare_versions(current: str, latest: str) -> int:
    """Return -1 if current < latest, 0 if equal, 1 if current > latest."""
    try:
        from packaging.version import parse
        c, lt = parse(current), parse(latest)
    except Exception:
        c, lt = current, latest
    return (c > lt) - (c < lt)


# ── opt-in startup "update available" hint ────────────────────────────────────

def _update_cache_path() -> Path:
    return Path.home() / ".vaf" / "update_cache.json"


def _cached_or_fetch_latest():
    """Return {version, relevant} from a <24h cache, else fetch once and cache it.

    None when the latest version is unknown (offline). `relevant` is True when the
    latest published release is newer than the installed version.
    """
    cache = _update_cache_path()
    now = datetime.now(timezone.utc)
    try:
        cached = json.loads(cache.read_text())
        checked = datetime.fromisoformat(cached["checked_at"])
        if (now - checked).total_seconds() < 86400:
            return {"version": cached.get("latest_version"), "relevant": bool(cached.get("relevant"))}
    except Exception:
        pass
    rel = _resolve_latest_release()
    if not rel or not rel.get("version"):
        return None
    version = rel["version"]
    relevant = _compare_versions(__version__, version) < 0
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"checked_at": now.isoformat(), "latest_version": version, "relevant": relevant}))
    except Exception:
        pass
    return {"version": version, "relevant": relevant}


def maybe_notify_update() -> None:
    """Print a one-line 'update available' hint at startup, if enabled.

    Opt-out via the `update_check_on_start` config flag; throttled to one network
    check per day via an on-disk cache. Fully defensive — any error is ignored,
    and it never applies an update.
    """
    try:
        from vaf.core.config import Config
        if not Config.get("update_check_on_start", True):
            return
        info = _cached_or_fetch_latest()
        if info and info.get("relevant") and info.get("version"):
            UI.event("Update", f"Update available: {__version__} -> {info['version']}. Run `vaf update`.")
    except Exception:
        pass


# ── checkout / paths ─────────────────────────────────────────────────────────

def _repo_root() -> Path:
    guess = Path(__file__).resolve().parents[3]
    code, out, _ = run_git(["rev-parse", "--show-toplevel"], cwd=str(guess))
    if code == 0 and out.strip():
        return Path(out.strip())
    return guess


def _git(root: Path, *args):
    code, out, err = run_git(list(args), cwd=str(root))
    return code, (out or "").strip(), (err or "").strip()


def _breadcrumb_path() -> Path:
    return Path.home() / ".vaf" / "last_update.json"


def _write_breadcrumb(data: dict) -> None:
    p = _breadcrumb_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _clear_breadcrumb() -> None:
    _breadcrumb_path().unlink(missing_ok=True)


# ── update steps (each effect goes through a mockable seam) ───────────────────

def _stop_service() -> None:
    try:
        service.cmd_stop()
    except typer.Exit as e:  # server mode delegates to systemctl and raises Exit(rc)
        if e.exit_code not in (0, None):
            raise _UpdateError("failed to stop the VAF service")


def _start_service() -> None:
    try:
        service.cmd_start()
    except typer.Exit as e:
        if e.exit_code not in (0, None):
            raise _UpdateError("failed to start the VAF service")


def _install_python_deps(root: Path) -> None:
    UI.info("Installing Python dependencies...")
    # Don't let `pip install -e .` re-trigger setup.py's platform post-install (setup_mac.sh /
    # setup_win.ps1) during an update — that would redo brew/venv/alias/.app work.
    env = {**os.environ, "VAF_SKIP_POSTINSTALL": "1"}
    for args in (["-e", "."], ["-r", "requirements.txt"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", *args], cwd=str(root), env=env)
        if r.returncode != 0:
            raise _UpdateError(f"pip install {' '.join(args)} failed")


def _install_web_deps(root: Path, prev_sha: str = "", force: bool = False) -> None:
    """Update frontend npm deps after a checkout. Without this, the lazy `npm run build` on the
    next start would build against stale node_modules and the Web UI could break. Runs
    `npm install` only when web/package.json|package-lock.json changed (or node_modules is
    missing, or force on rollback). Non-fatal: a failure warns but does not roll back."""
    web = root / "web"
    if not (web / "package.json").exists():
        return
    npm = shutil.which("npm")
    if not npm:
        if force or prev_sha:
            UI.warning("npm not found - skipping Web UI dependency update (install Node to update the dashboard).")
        return
    need = force or not (web / "node_modules").is_dir()
    if not need and prev_sha:
        _, changed, _ = _git(root, "diff", "--name-only", prev_sha, "HEAD",
                             "--", "web/package.json", "web/package-lock.json")
        need = bool(changed.strip())
    if not need:
        return
    UI.info("Updating frontend dependencies (npm install)...")
    r = subprocess.run([npm, "install"], cwd=str(web))
    if r.returncode != 0:
        UI.warning("npm install failed; the Web UI may not rebuild. Run `cd web && npm install` manually.")


def _invalidate_web_build(root: Path) -> None:
    # The frontend rebuilds lazily when web/.next/BUILD_ID is missing
    # (frontend_manager.py), so invalidating is enough — no build here.
    try:
        bid = root / "web" / ".next" / "BUILD_ID"
        if bid.exists():
            bid.unlink()
    except Exception:
        pass  # non-fatal


def _run_migrations() -> None:
    # Config migrations run inside Config.load(); state migrations run on session load.
    try:
        from vaf.core.config import Config
        Config.load()
    except Exception:
        pass

    # DB schema reconcile (add missing columns / ordered migrations / dimension check). The memory
    # DB is a SEPARATE service (Docker) that may be down at update time (e.g. tray-only start); if so
    # we say it is deferred to the next start (get_engine runs the same reconcile) instead of failing
    # silently. The reconcile is idempotent, so running it here AND at startup is harmless.
    try:
        import asyncio
        from sqlalchemy import text as _text
        from vaf.memory.database import get_owner_engine, _run_schema_migrations

        async def _reconcile():
            engine = await get_owner_engine()
            async with engine.begin() as conn:   # connectivity probe — fails fast if the DB is down
                await conn.execute(_text("SELECT 1"))
            await _run_schema_migrations(engine)

        asyncio.run(_reconcile())
        UI.info("Database schema reconciled.")
    except Exception as e:
        UI.warning(
            f"Memory DB not reachable now — the schema reconcile will run on the next start. ({type(e).__name__})"
        )


def _verify(target_version: str) -> None:
    r = subprocess.run([sys.executable, "-m", "vaf.main", "--version"], capture_output=True, text=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if target_version not in out:
        raise _UpdateError(f"post-update version check failed (expected {target_version}, got: {out[:80]})")


def _rollback(root: Path, anchor: str) -> None:
    UI.info("Rolling back to the previous version...")
    _git(root, "checkout", anchor)
    try:
        _install_python_deps(root)
    except Exception:
        UI.error("Rollback dependency reinstall failed; you may need `pip install -e .` manually.")
    _install_web_deps(root, force=True)
    _invalidate_web_build(root)
    try:
        _start_service()
    except Exception:
        pass


# ── apply ─────────────────────────────────────────────────────────────────────

def _apply(dry_run: bool, assume_yes: bool, target_tag: str | None,
           include_prereleases: "bool | None" = None) -> None:
    root = _repo_root()
    if not is_git_repo(str(root)):
        UI.error(f"VAF at {root} is not a git checkout; `vaf update` needs one. Re-install from git.")
        raise typer.Exit(1)

    # Resolve the target tag/version.
    if target_tag:
        target = target_tag if target_tag.startswith("v") else f"v{target_tag}"
        target_version = target[1:]
        notes_url = ""
    else:
        rel = _resolve_latest_release(include_prereleases)
        if not rel or not rel.get("tag"):
            UI.error("Could not determine the latest release (offline, or none published yet).")
            raise typer.Exit(1)
        target, target_version, notes_url = rel["tag"], rel["version"], rel.get("html_url", "")
        if _compare_versions(__version__, target_version) >= 0:
            UI.event("Update", f"Already up to date ({__version__}).")
            raise typer.Exit(0)

    # Clean-tree pre-check (untracked files are fine — build artifacts are gitignored).
    code, dirty, _ = _git(root, "status", "--porcelain", "--untracked-files=no")
    if code != 0:
        UI.error("git status failed; cannot update safely.")
        raise typer.Exit(1)
    tree_dirty = bool(dirty)

    code, cur_sha, _ = _git(root, "rev-parse", "HEAD")
    _, cur_branch, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")

    UI.event("Update", f"Update: {__version__} -> {target_version}  (tag {target})")
    if notes_url:
        UI.print(f"Release notes: {notes_url}")

    if dry_run:
        UI.event("Update", "Dry run — nothing will be changed. Planned steps:")
        for s in [
            "stop the VAF service",
            "git fetch origin --tags",
            f"git checkout {target}",
            "pip install -e .  +  pip install -r requirements.txt",
            "npm install in web/ (only if frontend deps changed)",
            "invalidate web/.next (lazy rebuild on next start)",
            "run config/state migrations",
            "restart the VAF service",
            f"verify `vaf --version` == {target_version}",
        ]:
            UI.print(f"  - {s}")
        UI.print(f"Rollback anchor: {cur_sha[:12]} ({cur_branch}).")
        if tree_dirty:
            UI.warning("Your checkout has local changes to tracked files; a real update would abort until they are committed/stashed.")
        raise typer.Exit(0)

    if tree_dirty:
        UI.error("Your VAF checkout has local changes to tracked files:")
        for line in dirty.splitlines()[:20]:
            UI.print(f"  {line}")
        UI.print("Commit or `git stash` them, then re-run `vaf update`.")
        raise typer.Exit(1)

    if not assume_yes:
        ans = UI.prompt(f"Update VAF {__version__} -> {target_version}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            UI.event("Update", "Cancelled.")
            raise typer.Exit(0)

    _write_breadcrumb({
        "recorded_head": cur_sha,
        "branch": cur_branch,
        "from_version": __version__,
        "target_tag": target,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    try:
        UI.info("Stopping VAF service...")
        _stop_service()

        UI.info(f"Fetching and checking out {target}...")
        code, _, err = _git(root, "fetch", "origin", "--tags")
        if code != 0:
            raise _UpdateError(f"git fetch failed: {err}")
        code, _, err = _git(root, "checkout", target)
        if code != 0:
            raise _UpdateError(f"git checkout {target} failed: {err}")

        _install_python_deps(root)
        _install_web_deps(root, cur_sha)
        _invalidate_web_build(root)
        _run_migrations()

        UI.info("Restarting VAF service...")
        _start_service()
        _verify(target_version)

        _clear_breadcrumb()
        UI.success(f"VAF updated to {target_version}.")
        if notes_url:
            UI.print(f"Release notes: {notes_url}")
    except Exception as e:
        UI.error(f"Update failed: {e}")
        _rollback(root, cur_sha or cur_branch or "HEAD")
        _clear_breadcrumb()
        raise typer.Exit(1)


def _recover() -> None:
    p = _breadcrumb_path()
    if not p.exists():
        UI.event("Update", "No interrupted update to recover.")
        raise typer.Exit(0)
    try:
        data = json.loads(p.read_text())
    except Exception:
        UI.error("Update breadcrumb is unreadable; remove ~/.vaf/last_update.json manually.")
        raise typer.Exit(1)
    root = _repo_root()
    anchor = data.get("recorded_head") or data.get("branch") or "main"
    UI.info(f"Recovering an interrupted update — restoring {str(anchor)[:12]}...")
    _rollback(root, anchor)
    _clear_breadcrumb()
    UI.success("Recovered to the previous version.")


# ── CLI ───────────────────────────────────────────────────────────────────────

@app.callback(invoke_without_command=True)
def _update_main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen; change nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt for confirmation"),
    tag: str = typer.Option(None, "--tag", help="Update to a specific tag instead of the latest release"),
    pre: bool = typer.Option(None, "--pre/--stable", help="Force including / excluding prereleases (default: auto by the installed version)"),
    recover: bool = typer.Option(False, "--recover", help="Recover from an interrupted update"),
):
    """Update VAF to the latest published release (bare `vaf update`)."""
    if ctx.invoked_subcommand is not None:
        return
    if recover:
        _recover()
        return
    _apply(dry_run=dry_run, assume_yes=yes, target_tag=tag, include_prereleases=pre)


@app.command("check")
def check(
    pre: bool = typer.Option(None, "--pre/--stable", help="Force including / excluding prereleases (default: auto by the installed version)"),
):
    """Check whether a newer VAF release is available (read-only)."""
    UI.event("Update", f"Installed version: {__version__}")
    if _breadcrumb_path().exists():
        UI.warning("A previous update did not finish. Run `vaf update --recover`.")
    rel = _resolve_latest_release(pre)
    if rel is None:
        UI.event("Update", "Could not reach GitHub to check for updates (offline?).", style="warning")
        raise typer.Exit(0)
    latest = rel["version"]
    if not latest:
        UI.event("Update", "No published release found yet.", style="warning")
        raise typer.Exit(0)
    cmp = _compare_versions(__version__, latest)
    if cmp < 0:
        UI.event("Update", f"Update available: {__version__} -> {latest}")
        if rel.get("html_url"):
            UI.print(f"Release notes: {rel['html_url']}")
        UI.print("Run `vaf update` to install it.")
    elif cmp == 0:
        UI.event("Update", f"VAF is up to date ({__version__}).")
    else:
        UI.event("Update", f"Installed version {__version__} is newer than the latest release ({latest}).")
