# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Guards for the vaf-browser container entrypoint (docker/browser/entrypoint.sh).

Two regressions must never silently return:

1. Debian's chromium 150.0.7871.46 SIGTRAPs ~1s into startup when --no-first-run is set and the
   profile resolves to an EEA region (the container reports TZ=Europe/Berlin) - a real M149->M150
   search-engine-choice regression, Debian bug #1141618. The fix, verified empirically by bisecting
   the launch flags on the box, is to launch WITHOUT --no-first-run; the first-run search-engine
   choice is instead kept quiet with --disable-search-engine-choice-screen +
   --search-engine-choice-country=US. Reintroducing --no-first-run bricks the browser tool on every
   box that has built its image against Chromium 150+.

2. The entrypoint used to launch Chromium once and then `exec socat`, so any single Chromium death
   left socat forwarding forever to a dead port and the browser service was permanently down until a
   manual recreate. It must supervise Chromium (relaunch loop), reap orphaned child processes so a
   crash-loop cannot pile up zombies, and run socat only while the CDP endpoint is live.

Pure text assertions on the shell script - no Docker, no containers.
"""
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "browser" / "entrypoint.sh"


def _script() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_no_first_run_absent_and_choice_flags_present():
    src = _script()
    # ignore comment lines: the fix is explained in comments that name the flag on purpose
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    # --no-first-run is the confirmed M150 startup-SIGTRAP trigger; it must NOT be passed
    assert "--no-first-run" not in code
    # the first-run search-engine choice is kept quiet without crashing instead
    assert "--disable-search-engine-choice-screen" in code
    assert "--search-engine-choice-country=US" in code


def test_chromium_is_supervised_not_launch_once():
    src = _script()
    # the old launch-once tail permanently bricked the service after any single Chromium crash
    assert "exec socat" not in src
    # a supervise/relaunch loop with socat backgrounded (a child of the supervisor, not exec'd)
    assert "while :" in src or "while true" in src
    assert "socat TCP-LISTEN:9222" in src
    assert "start_chromium" in src
    assert "wait_for_cdp" in src
    # orphaned child processes from a prior crash are reaped so a crash-loop cannot pile up zombies
    assert "pkill" in src


def test_clean_shutdown_trap_present():
    # the supervisor is PID 1 now (socat is no longer exec'd), so it must trap SIGTERM or every
    # `docker stop` hangs for the full grace period before SIGKILL
    src = _script()
    assert "trap cleanup TERM INT" in src


# ── container hardening: content blocking and DNS filtering ───────────────

DOCKERFILE = ENTRYPOINT.parent / "Dockerfile"
COMPOSE = ENTRYPOINT.parent.parent.parent / "docker-compose.memory.yml"


def test_ublock_origin_lite_pinned_in_image_and_installable_at_launch():
    """The blocker ships as a version-pinned, checksum-verified release artifact,
    packed to a CRX at build time and registered for Chromium's external-
    extensions provider - the ONLY install lane this Chromium still honours.
    The launch flags decide whether that provider runs at all, both measured in
    the container: --load-extension is silently ignored by Chromium 150 (a dead
    flag that looks load-bearing must not return), and --disable-default-apps /
    --disable-extensions each kill the install lane outright."""
    docker = DOCKERFILE.read_text(encoding="utf-8")
    assert "UBOL_VERSION=" in docker
    assert "UBOL_SHA256=" in docker
    assert "sha256sum -c" in docker
    assert "uBOL-home/releases/download" in docker      # official releases, not the Web Store
    assert "--pack-extension=" in docker
    assert "/usr/share/chromium/extensions" in docker
    assert "external_crx" in docker
    code = "\n".join(ln for ln in _script().splitlines() if not ln.lstrip().startswith("#"))
    assert "--load-extension" not in code
    assert "--disable-default-apps" not in code
    assert "--disable-extensions" not in code


def test_profile_scrub_marker_is_honoured_between_launches():
    """The full handover scrub: VAF drops a marker and kills Chromium; the
    supervisor's next launch must wipe the profile and the downloads BEFORE
    starting Chromium - a container restart cannot do this (the container
    filesystem survives restarts), so the wipe lives here or nowhere."""
    src = _script()
    assert ".scrub-profile" in src
    assert "rm -rf /home/browser/.config/chromium /home/browser/Downloads /home/browser/Workspace" in src
    # The file picker is anchored to the transfer folders: XDG dirs plus a GTK
    # bookmark plus the seeded start directory - without them "upload from my
    # workspace" begins in the container's empty home.
    assert 'XDG_DOCUMENTS_DIR="/home/browser/Workspace"' in src
    assert "file:///home/browser/Workspace" in src
    assert '"selectfile"' in src
    # The wipe must run inside start_chromium (both the first launch and every
    # supervisor relaunch pass through it), before Chromium comes up.
    body = src.split("start_chromium() {", 1)[1].split("\n}", 1)[0]
    assert ".scrub-profile" in body


def test_dns_filtering_is_the_security_variant_never_the_family_one():
    """Deliberate: Cloudflare's 1.1.1.2/security lane blocks malware and phishing
    domains; the 1.1.1.3/family lane additionally censors adult content, which
    this browser must not do. Both halves are pinned - the DoH policy in the
    image and the fallback resolvers in compose - so neither can drift to the
    censoring variant on its own."""
    docker = DOCKERFILE.read_text(encoding="utf-8")
    assert '"DnsOverHttpsMode": "automatic"' in docker
    assert "security.cloudflare-dns.com" in docker
    assert "family.cloudflare-dns.com" not in docker
    # ignore comment lines: the deliberate exclusion is explained in comments
    # that name the family variant on purpose
    compose = "\n".join(ln for ln in COMPOSE.read_text(encoding="utf-8").splitlines()
                        if not ln.lstrip().startswith("#"))
    assert "1.1.1.2" in compose and "1.0.0.2" in compose
    assert "1.1.1.3" not in compose and "1.0.0.3" not in compose
