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


def test_the_health_check_covers_the_stream_not_only_cdp():
    """Both halves have to answer before the container counts as healthy: CDP on
    9222, which the agent drives, and the KasmVNC stream on 6901, which is what a
    person actually sees.

    They fail apart. An image built before the stream existed serves CDP perfectly
    while nothing listens on 6901, and a CDP-only check called exactly that
    container healthy - so the pool handed it out and the ticket route answered
    502 on the first human click. Both places are pinned, because either one left
    alone keeps reporting the same lie.
    """
    docker = DOCKERFILE.read_text(encoding="utf-8")
    probe = docker.split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]
    assert "9222" in probe and "6901" in probe, f"Dockerfile health-check misses a half: {probe!r}"

    # ignore comment lines: the two ports are named in comments on purpose
    compose = "\n".join(ln for ln in COMPOSE.read_text(encoding="utf-8").splitlines()
                        if not ln.lstrip().startswith("#"))
    browser_probes = [ln for ln in compose.splitlines() if "CMD-SHELL" in ln and "9222" in ln]
    assert browser_probes, "no browser health-check found in compose"
    assert all("6901" in ln for ln in browser_probes), (
        f"compose health-check misses the stream half: {browser_probes}"
    )


# ── container hardening: Chromium's own sandbox ───────────────────────────

SECCOMP_PROFILE = ENTRYPOINT.parent / "chromium-seccomp.json"


def test_no_sandbox_exists_only_as_the_probed_fallback():
    """Chromium runs WITH its own sandbox: Docker's default seccomp profile was
    the only blocker (clone(CLONE_NEWUSER) EPERM, measured 2026-08-26), and the
    shipped profile lifts exactly that. --no-sandbox may only survive inside the
    probe's fallback assignment for runtimes that do not apply the profile; a
    bare --no-sandbox launch line is the regression this test pins out."""
    src = _script()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    # the user-namespace probe decides, in if-form (a bare probe would abort under set -e)
    assert "if unshare -U true" in code
    # every non-comment line that says --no-sandbox is the fallback assignment
    for ln in code.splitlines():
        if "--no-sandbox" in ln:
            assert "SANDBOX_ARGS=" in ln, f"--no-sandbox outside the fallback assignment: {ln!r}"
    # the launch consumes the variable and never hardcodes the flag
    body = src.split("start_chromium() {", 1)[1].split("\n}", 1)[0]
    body_code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "$SANDBOX_ARGS" in body_code
    assert "--no-sandbox" not in body_code
    # unshare comes from util-linux; pin the dependency instead of inheriting it
    docker = DOCKERFILE.read_text(encoding="utf-8")
    assert "util-linux" in docker


def test_both_container_lanes_carry_the_hardening():
    """The compose service and the pool's docker run are two copies of the same
    start arguments (Rule 2); each must carry cap_drop ALL + SYS_CHROOT +
    no-new-privileges + the seccomp profile, or one lane silently runs the
    browser without Chromium's sandbox."""
    compose = "\n".join(ln for ln in COMPOSE.read_text(encoding="utf-8").splitlines()
                        if not ln.lstrip().startswith("#"))
    browser_block = compose.split("vaf-browser:", 1)[1].split("\nvolumes:", 1)[0]
    assert "cap_drop:" in browser_block and "- ALL" in browser_block
    assert "- SYS_CHROOT" in browser_block
    assert "no-new-privileges:true" in browser_block
    assert "seccomp=./docker/browser/chromium-seccomp.json" in browser_block

    pool_src = (ENTRYPOINT.parent.parent.parent / "vaf" / "core" / "browser_pool.py").read_text(
        encoding="utf-8")
    assert '"--cap-drop", "ALL"' in pool_src
    assert '"--cap-add", "SYS_CHROOT"' in pool_src
    assert '"no-new-privileges:true"' in pool_src
    assert "chromium-seccomp.json" in pool_src


def test_the_seccomp_profile_is_default_deny_plus_userns():
    """The profile is Docker's default (deny by default) plus one allow rule for
    the user-namespace syscalls Chromium's sandbox needs. A profile that lost
    the deny default, or the rule, would either weaken the container or bring
    back --no-sandbox via the entrypoint fallback."""
    import json
    profile = json.loads(SECCOMP_PROFILE.read_text(encoding="utf-8"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    userns_rules = [
        r for r in profile["syscalls"]
        if r.get("action") == "SCMP_ACT_ALLOW"
        and {"clone", "clone3", "unshare", "setns"} <= set(r.get("names", []))
        and "includes" not in r and "excludes" not in r and "args" not in r
    ]
    assert userns_rules, "unconditional userns allow rule missing from the profile"
