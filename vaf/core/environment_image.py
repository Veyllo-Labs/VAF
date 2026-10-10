# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The image behind the sandbox environments: which one, when it is built, and
what runs while it is not there.

The Dockerfile ships INSIDE the package (vaf/assets/sandbox/Dockerfile) and is piped
to `docker build -` on stdin: a wheel install has no docker/ directory, and an
embedder building on the facade must get the same image. The tag is a hash of that
file (`vaf-sandbox-env:<12 hex>`), so a changed Dockerfile is a new image and the old
one cannot be mistaken for it.

The build is slow the first time (measured: 1.64 GB on disk, 1.44 GB of it beyond the
Python base - Node, a compiler, git, tinyproxy and chromium-headless-shell), so
nothing waits for it unless it must:
- the stack start kicks it off in the background (`start_background_build`);
- a caller that needs the image now asks `ensure_image()`, which builds under a
  lock shared across processes - the web server, the CLI and a coder child may all
  ask at once, and only one build runs;
- the scratch environment that python_sandbox uses runs on FALLBACK_IMAGE until
  the built image exists, so code execution never goes dark behind a build or an
  offline machine. The fallback has no Node and no browser; what needs them says so.

Like the browser image, the base and the Debian packages age while the Dockerfile
text stands still: past `sandbox_env_image_max_age_days` (default 14, 0 switches it
off) the next build pulls the base and skips the cache.

Tests never build: `VAF_SANDBOX_ENV_NO_BUILD` (set session-wide by the suite) turns
every build into a refusal.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Optional

from vaf.core import containers
from vaf.core.log_helper import append_domain_log

IMAGE_REPO = "vaf-sandbox-env"
FALLBACK_IMAGE = "python:3.12-slim-bookworm"
DEFAULT_MAX_AGE_DAYS = 14
BUILD_TIMEOUT_S = 1800

_DOCKERFILE = Path(__file__).resolve().parent.parent / "assets" / "sandbox" / "Dockerfile"
_background_lock = threading.Lock()
_background_running = False


def dockerfile_text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def image_tag() -> str:
    """`vaf-sandbox-env:<first 12 hex of the Dockerfile's sha256>`."""
    digest = hashlib.sha256(dockerfile_text().encode("utf-8")).hexdigest()[:12]
    return f"{IMAGE_REPO}:{digest}"


def image_present(tag: Optional[str] = None) -> bool:
    try:
        r = containers.docker(["image", "inspect", tag or image_tag(), "--format", "{{.Id}}"],
                              timeout=20)
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False


def max_age_days() -> int:
    """The freshness budget in days; 0 disables the age rebuild. The environment
    variable first, then the admin-only config key, then the default."""
    try:
        raw = os.environ.get("VAF_SANDBOX_ENV_IMAGE_MAX_AGE_DAYS")
        if raw is None or not str(raw).strip():
            from vaf.core.config import Config
            raw = Config.get("sandbox_env_image_max_age_days")
        return DEFAULT_MAX_AGE_DAYS if raw is None else max(0, int(raw))
    except Exception:
        return DEFAULT_MAX_AGE_DAYS


def _is_stale(tag: str) -> bool:
    budget = max_age_days()
    if budget <= 0:
        return False
    age = containers.image_age_days(tag)
    return age is not None and age > budget


def builds_disabled() -> bool:
    return str(os.environ.get("VAF_SANDBOX_ENV_NO_BUILD", "")).strip().lower() in ("1", "true", "yes")


def _build_lock():
    """The cross-process lock secure_store uses (filelock), or None without it - a
    second build in another process then only costs time, docker serialises the tag."""
    from vaf.core.secure_store import _get_filelock_cls
    cls = _get_filelock_cls()
    if cls is None:
        return None
    try:
        from vaf.core.platform import Platform
        path = Path(Platform.vaf_dir()) / "sandbox_env_image.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(str(path), timeout=BUILD_TIMEOUT_S)
    except Exception:
        return None


def _build(tag: str, *, refresh: bool) -> bool:
    args = ["build", "-t", tag]
    if refresh:
        args += ["--pull", "--no-cache"]
    args.append("-")
    try:
        r = containers.docker(args, timeout=BUILD_TIMEOUT_S, input=dockerfile_text())
    except Exception as e:
        append_domain_log("webui", f"[sandbox_env] image build could not run: {e}")
        return False
    if r.returncode != 0:
        tail = ((r.stderr or "") or (r.stdout or "")).strip().splitlines()
        detail = tail[-1].strip()[:200] if tail else "no output"
        append_domain_log("webui", f"[sandbox_env] image build failed: {detail}")
        return False
    append_domain_log("webui", f"[sandbox_env] image {tag} built")
    _remove_superseded(tag)
    return True


IMAGE_LABEL = "org.veyllo.vaf.image=sandbox-env"     # the Dockerfile's LABEL


def _remove_superseded(current: str) -> None:
    """Drop the images earlier Dockerfiles produced, and the untagged ones a refresh
    leaves behind: the age rebuild takes the same tag, so the old image (1.6 GB) stays
    as a dangling one. The prune is scoped to this image's label and only takes dangling
    images; one still in use by a container refuses to go either way, which is right:
    it goes once that environment is deleted."""
    try:
        r = containers.docker(["image", "ls", IMAGE_REPO, "--format", "{{.Repository}}:{{.Tag}}"],
                              timeout=20)
        for ref in (r.stdout or "").split():
            if ref != current and ref.startswith(IMAGE_REPO + ":"):
                containers.docker(["image", "rm", ref], timeout=60)
        containers.docker(["image", "prune", "-f", "--filter", f"label={IMAGE_LABEL}"], timeout=120)
    except Exception:
        pass


def ensure_image() -> Optional[str]:
    """The built image's tag, building it first when it is missing or past its age
    budget. Blocks for the build (minutes the first time). None when it cannot be
    had: no docker, builds disabled, or the build failed and no older copy exists."""
    tag = image_tag()
    present = image_present(tag)
    if present and not _is_stale(tag):
        return tag
    if builds_disabled():
        return tag if present else None
    lock = _build_lock()
    try:
        if lock is not None:
            lock.acquire()
        # Another process may have finished the build while this one waited.
        present = image_present(tag)
        if present and not _is_stale(tag):
            return tag
        if _build(tag, refresh=present):
            return tag
        return tag if present else None
    except Exception as e:
        append_domain_log("webui", f"[sandbox_env] image unavailable: {e}")
        return tag if present else None
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def start_background_build() -> None:
    """Build (or refresh) the image off the caller's path, once per process at a time.
    The stack start calls it; it never raises and never blocks."""
    global _background_running
    if builds_disabled():
        return
    with _background_lock:
        if _background_running:
            return
        _background_running = True

    def _run():
        global _background_running
        try:
            ensure_image()
        finally:
            with _background_lock:
                _background_running = False

    threading.Thread(target=_run, daemon=True, name="sandbox-env-image-build").start()


def usable_image() -> str:
    """The built image when it exists, else FALLBACK_IMAGE. Never builds: for the
    scratch environment, which must answer now."""
    tag = image_tag()
    return tag if image_present(tag) else FALLBACK_IMAGE
