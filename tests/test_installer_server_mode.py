# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Server-mode section of install.sh: static guards over the shell source.

Two live findings motivated this file. The installation-mode prompt was gated on
an interactive stdin, so the documented curl-pipe install could never produce a
server install (there was no flag to ask for one). And the Docker boot enable
only ran inside the engine bootstrap, which is reached only when the daemon was
DOWN at install time - a box with dockerd already running finished "successfully"
and booted without the engine the memory stack needs.

Guards use exact literals from install.sh, not layout-sensitive regexes, so
reformatting stays cheap while removing a step goes red.

A server install also leaves out what only a desktop session uses (the tray icon's GTK
libraries, the PortAudio headers, PyGObject, the desktop Python packages). The package
lists live in shell functions, which the tests below extract from install.sh and run
with bash, so the lists checked are the lists the installer runs."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SH = (ROOT / "install.sh").read_text(encoding="utf-8")
BOOTSTRAP = (ROOT / "packaging" / "install" / "bootstrap.sh").read_text(encoding="utf-8")


def test_noninteractive_mode_flags_exist():
    assert re.search(r"^\s*--server\)", SH, re.M), "install.sh must accept --server"
    assert re.search(r"^\s*--desktop\)", SH, re.M), "install.sh must accept --desktop"


def test_bootstrap_forwards_installer_flags():
    # `curl ... | bash -s -- --server` only works if bootstrap hands its args on
    assert 'install.sh "$@"' in BOOTSTRAP


def test_config_is_written_through_the_cli_not_inline_json():
    # The inline python heredoc bypassed Config's coercion invariants and
    # observers; provisioning must go through the CLI verb instead.
    assert "server provision" in SH
    assert "PYEOF" not in SH
    assert 'cfg["server_mode"]' not in SH


def test_unit_keeps_hardening_and_gains_the_env_file():
    assert "EnvironmentFile=-%h/.vaf/service.env" in SH
    assert "NoNewPrivileges=yes" in SH
    # The unit must declare journal ownership, or the tray tees a second copy
    # of every line into ~/.vaf/logs/vaf_run.log (vaf/core/stdio_tee.py).
    assert "Environment=VAF_LOG_TO_JOURNAL=1" in SH


def test_server_mode_enables_docker_outside_the_engine_bootstrap():
    # engine bootstrap (daemon was down): enable --now; server mode: plain enable,
    # unconditional, because a running daemon skips the bootstrap entirely
    assert "systemctl enable --now docker" in SH
    assert "sudo -n systemctl enable docker" in SH


def test_systemd_user_steps_are_guarded_under_set_e():
    # set -e would abort the whole installer on a failing user-manager call
    for cmd in ("systemctl --user daemon-reload", "systemctl --user enable vaf"):
        guarded = [
            line for line in SH.splitlines()
            if line.lstrip().startswith(cmd) and "||" in line
        ]
        assert guarded, f"'{cmd}' must carry a || fallback (set -e)"


def test_the_passphrase_never_travels_as_an_argument():
    # house rule (FIRST_RUN.md): secrets come from stdin or the environment,
    # never from argv where `ps` shows them
    assert "read -rs" in SH
    assert "--passphrase" not in SH


def test_a_new_docker_group_reaches_the_service_without_a_reboot():
    """Measured on a fresh server: the user manager, running since the login that ran the
    installer, kept its old groups, so the VAF service could not reach Docker until the next
    reboot. MUTATION: drop the restart of the user manager - red."""
    block = SH.split("# Start the service immediately", 1)[0].rsplit("loginctl enable-linger", 1)[1]
    assert 'sudo systemctl restart "user@${_uid}.service"' in block
    assert "grep -qw \"$_docker_gid\"" in block, "only when the manager lacks the group"
    # A service busy with other work is not cut off without the person's yes: the restart
    # waits for an answer at a terminal, or for the next reboot. MUTATION: restart anyway - red.
    assert "if [[ -n \"${_busy// /}\" ]]; then" in block and "[[ -t 0 ]]" in block
    # Busy is any service at work, not only a running one: one still starting is cut off the
    # same way. MUTATION: count running services only - red.
    assert "--type=service --state=running,activating,reloading" in block
    assert block.index('if [[ "$_restart" != "true" ]]; then') < block.index('sudo systemctl restart "user@')
    # An installer started from a desktop terminal runs inside that manager and would end
    # itself. MUTATION: drop the cgroup check - red.
    assert 'grep -q "/user@${_uid}\\.service/" "/proc/$$/cgroup"' in block
    assert block.index('"/proc/$$/cgroup"') < block.index('read -r -p "  Restart your user services')
    # Ctrl-D at that prompt answers no; under set -e a failed read ended the install before
    # the service was started. MUTATION: drop the fallback - red.
    assert '_answer || _answer=""' in block
    # ... and says why it did not restart, rather than naming an empty list of services.
    assert 'if [[ "$_inside" == "true" ]]; then' in block
    # What waited (timers, sockets, path watchers) and what was agreed to comes back, a unit
    # started by hand included. MUTATION: drop the restart of the recorded units - red.
    assert "--type=timer,socket,path --state=active" in block
    assert "--type=service --state=exited" in block, "a finished one-shot comes back too"
    assert 'for _unit in $_busy $_waiting; do' in block and 'systemctl --user start "$_unit"' in block


def test_the_profile_step_uses_the_new_docker_group():
    """`vaf profile api` ran without the group this run added and reported a switch it could
    not make. MUTATION: run it without sg again - red."""
    step = SH.split('if [[ "$INSTALL_PROFILE" == "api" ]]; then', 1)[1].split("CORE_SERVICES=", 1)[0]
    assert 'sg docker -c' in step and '[[ -n "$DOCKER_SUDO" ]]' in step
    # Only for a member: sg asks a non-member for a group password. MUTATION: drop it - red.
    assert step.index("grep -qx docker") < step.index("sg docker -c")


def test_the_alias_goes_where_the_login_shell_reads():
    """The uv installer writes ~/.zshrc on a bash system too; choosing by that file put the
    alias where bash never reads it. MUTATION: choose by the file's existence again - red."""
    assert '[[ -f "$HOME/.zshrc" ]] && SHELL_CONFIG=' not in SH
    assert 'case "$(basename "${SHELL:-bash}")" in' in SH


def test_the_summary_prints_lan_and_vpn_addresses_only():
    """The closing summary printed `https://<default-route source>:8443`, which on a rented
    server is the PUBLIC address, exactly where VAF must not be reached. It asks VAF now,
    which lists the LAN and VPN addresses it admits and warns about a public one.
    MUTATION: put the `ip route get` line back - red."""
    assert "ip route get" not in SH
    block = SH[SH.index('if [[ "$SETUP_LAN" == "true" ]]; then\n    # The addresses come'):]
    block = block[:block.index("\nfi\n")]
    assert '"$PROJECT_ROOT/venv/bin/python3" -m vaf.main server status' in block
    assert '|| echo -e "    - vaf server status lists the addresses once the service runs"' in block
    assert "Never forward this port on your router or publish it to the internet." in block
    assert ('echo -e "    - Install the certificate authority on your devices once: vaf server ca '
            '(prints its fingerprint)."') in block
    assert SH.isascii()


# ── what a server install leaves out ──────────────────────────────────────────

# The system package lists of a desktop install, one string per install call. Pinned
# literally: a desktop install must install exactly what it did before servers were split
# off. MUTATION: drop portaudio19-dev from the desktop apt list - red.
_DESKTOP_CALLS = {
    "apt": ["portaudio19-dev python3-dev python3-venv build-essential git ffmpeg tesseract-ocr "
            "tesseract-ocr-deu python3-gi gir1.2-ayatanaappindicator3-0.1 libgirepository1.0-dev "
            "libcairo2-dev"],
    "dnf": ["portaudio-devel python3-devel gcc git ffmpeg tesseract tesseract-langpack-deu "
            "python3-gobject3 libappindicator-gtk3 gobject-introspection-devel cairo-devel"],
    "pacman": ["portaudio python git ffmpeg tesseract tesseract-data-deu base-devel python-gobject "
               "webkit2gtk libappindicator-gtk3 gobject-introspection cairo"],
    "zypper": ["portaudio-devel alsa-devel python3-devel gcc git ffmpeg tesseract-ocr "
               "tesseract-ocr-traineddata-german nodejs-default npm-default docker-compose "
               "typelib-1_0-WebKit2-4_1 libwebkit2gtk-4_1-0 typelib-1_0-AyatanaAppIndicator3-0_1 "
               "gobject-introspection-devel cairo-devel"],
}
_DESKTOP_CALLS["yum"] = _DESKTOP_CALLS["dnf"]

# Only a desktop session uses these: the terminal microphone's headers, the tray icon's
# GTK/AppIndicator/WebKitGTK libraries and the headers PyGObject is built against.
_DESKTOP_ONLY_PACKAGES = (
    "portaudio19-dev", "portaudio-devel", "portaudio", "alsa-devel", "python3-gi",
    "libgirepository1.0-dev", "libcairo2-dev", "python3-gobject3", "libappindicator-gtk3",
    "cairo-devel", "cairo", "python-gobject",
)
_DESKTOP_ONLY_PREFIXES = ("gir1.2-", "gobject-introspection", "webkit2gtk", "typelib-1_0-",
                          "libwebkit2gtk-")

# What a server keeps, per manager: git, ffmpeg, tesseract with the German data, the
# compiler and the Python headers.
_SERVER_KEEPS = {
    "apt": ("git", "ffmpeg", "tesseract-ocr", "tesseract-ocr-deu", "build-essential", "python3-dev"),
    "dnf": ("git", "ffmpeg", "tesseract", "tesseract-langpack-deu", "gcc", "python3-devel"),
    "pacman": ("git", "ffmpeg", "tesseract", "tesseract-data-deu", "base-devel", "python"),
    "zypper": ("git", "ffmpeg", "tesseract-ocr", "tesseract-ocr-traineddata-german", "gcc",
               "python3-devel"),
}
_SERVER_KEEPS["yum"] = _SERVER_KEEPS["dnf"]

_needs_bash = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                                 reason="runs install.sh functions with bash")


def _function(name: str) -> str:
    """The source of one top-level shell function of install.sh: its header line up to the
    first line that is a lone closing brace."""
    lines = SH.splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start:end + 1])


def _bash(script: str) -> str:
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _install_calls(manager: str, mode: str) -> list:
    out = _bash(_function("linux_install_calls") + f"\nlinux_install_calls {manager} {mode}\n")
    return [line for line in out.splitlines() if line]


@_needs_bash
@pytest.mark.parametrize("manager", sorted(_DESKTOP_CALLS))
def test_a_desktop_install_keeps_its_package_lists(manager):
    assert _install_calls(manager, "desktop") == _DESKTOP_CALLS[manager]


@_needs_bash
@pytest.mark.parametrize("manager", sorted(_SERVER_KEEPS))
def test_a_server_install_leaves_out_the_desktop_packages(manager):
    """MUTATION: return the desktop list for server - red."""
    calls = _install_calls(manager, "server")
    packages = [p for call in calls for p in call.split() if not p.startswith("-")]
    leaked = [p for p in packages
              if p in _DESKTOP_ONLY_PACKAGES or p.startswith(_DESKTOP_ONLY_PREFIXES)]
    assert not leaked, f"{manager} server install names desktop-only packages: {leaked}"
    missing = [p for p in _SERVER_KEEPS[manager] if p not in packages]
    assert not missing, f"{manager} server install lost: {missing}"


@_needs_bash
def test_apt_installs_ffmpeg_on_a_server_without_its_recommendations():
    """Ubuntu's ffmpeg recommends what pulls pocketsphinx's model, GTK 3, icon themes and
    video drivers onto a server. MUTATION: drop --no-install-recommends - red."""
    calls = _install_calls("apt", "server")
    ffmpeg = [c for c in calls if "ffmpeg" in c.split()]
    assert ffmpeg == ["--no-install-recommends ffmpeg"], calls
    # ... and only ffmpeg: git's recommendations (ca-certificates, ssh-client) are wanted.
    assert sum("--no-install-recommends" in c for c in calls) == 1


@_needs_bash
def test_an_unknown_manager_gets_no_install_call():
    assert _install_calls("unknown", "server") == []
    assert _install_calls("unknown", "desktop") == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the runner is Linux-only (mapfile)")
def test_the_system_deps_step_runs_every_listed_call():
    """The runner executes each printed line as one install call. INSTALL_CMD stands in
    for the package manager."""
    script = "\n".join([
        'print_info() { :; }', 'print_warning() { echo "WARN $1"; }',
        _function("linux_install_calls"), _function("install_linux_packages"),
        'PKG_MANAGER=apt INSTALL_MODE=server INSTALL_CMD="echo RUN"',
        "install_linux_packages",
    ])
    assert _bash(script).splitlines() == [
        "RUN python3-dev python3-venv build-essential git tesseract-ocr tesseract-ocr-deu",
        "RUN --no-install-recommends ffmpeg",
    ]
    step = SH[SH.index('print_step "Installing System Dependencies..."'):SH.index("# 3. GPU DETECTION")]
    for manager in ("apt)", "dnf|yum)", "pacman|zypper)"):
        branch = step[step.index(f"        {manager}\n"):]
        branch = branch[:branch.index(";;")]
        assert "install_linux_packages" in branch, manager
    assert "DEPS=" not in step.split("elif [[ \"$OS_TYPE\" == \"linux\" ]]; then", 1)[1]


def _guard_of(prefix: str) -> str:
    """The line that opens the block a command starts in: the first line above it that is
    not a comment or a print_info. Also asserts the command's continuation lines are
    followed by the block's `fi`, so the guard covers the whole chain."""
    lines = SH.splitlines()
    matches = [i for i, line in enumerate(lines) if line.strip().startswith(prefix)]
    assert len(matches) == 1, f"{prefix!r} found {len(matches)} times"
    i = matches[0]
    end = i
    while lines[end].rstrip().endswith("\\"):
        end += 1
    assert lines[end + 1].strip() == "fi", f"{prefix!r} is not the last command of its block"
    j = i - 1
    while lines[j].strip().startswith(("#", "print_info")):
        j -= 1
    return lines[j].strip()


@pytest.mark.parametrize("prefix", [
    "$INSTALL_CMD gir1.2-webkit2-4.1",
    "$INSTALL_CMD webkit2gtk4.1",
    'pip install "PyGObject<3.52"',
    'verify_module "speech_recognition"',
])
def test_desktop_only_steps_run_outside_server_mode_only(prefix):
    """The WebKitGTK chains, the PyGObject build and the speech_recognition check run on a
    desktop only. MUTATION: remove one of the guards - red."""
    guard = _guard_of(prefix)
    assert guard.startswith("if ") and '[[ "$INSTALL_MODE" != "server" ]]' in guard, guard


def test_the_unknown_manager_hint_names_no_portaudio_on_a_server():
    step = SH[SH.index('print_step "Installing System Dependencies..."'):SH.index("# 3. GPU DETECTION")]
    hint = step[step.rindex("        *)\n"):]
    server = hint[hint.index('if [[ "$INSTALL_MODE" == "server" ]]; then'):hint.index("else")]
    assert 'print_warning "Please manually install: git, ffmpeg, python dev headers"' in server
    assert "portaudio" not in server


@_needs_bash
def test_a_server_installs_the_headless_python_set():
    """install.sh and `vaf update` name the same file for a server. MUTATION: let
    python_requirements_file print requirements.txt for server - red."""
    from vaf.core.update_check import HEADLESS_REQUIREMENTS

    fn = _function("python_requirements_file")
    assert _bash(fn + "\npython_requirements_file server\n").strip() == HEADLESS_REQUIREMENTS
    assert (ROOT / HEADLESS_REQUIREMENTS).is_file()
    for mode in ("desktop", ""):
        assert _bash(fn + f'\npython_requirements_file "{mode}"\n').strip() == "requirements.txt"
    assert 'REQUIREMENTS_FILE="$(python_requirements_file "$INSTALL_MODE")"' in SH
    assert 'python3 -m pip install -r "$REQUIREMENTS_FILE" --quiet' in SH
    assert "pip install -r requirements.txt" not in SH


def _installer_flag_labels() -> set:
    """The case labels of install.sh's argument loop, e.g. --profile from `--profile|--profile=*)`."""
    block = SH.split("# ARGUMENT PARSING", 1)[1].split("# BANNER", 1)[0]
    labels = set()
    for line in block.splitlines():
        m = re.match(r"\s+(-[^)\s]+)\)\s*$", line)
        if m:
            for label in m.group(1).split("|"):
                label = re.sub(r"=\*$", "", label)
                if label != "*":
                    labels.add(label)
    return labels


def test_every_documented_install_sh_flag_is_one_the_installer_accepts():
    """A flag the docs name and the parser does not know fails on a stranger's server, not
    here. MUTATION: rename the --profile label in install.sh, or write --profiles in the README
    block, or delete the README's server command (floor) - red."""
    labels = _installer_flag_labels()
    docs = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
    found = set()
    for path in docs:
        text = path.read_bytes().decode("utf-8")
        for m in re.finditer(r"install\.sh((?:\s+-{1,2}[A-Za-z][\w-]*(?:[ =](?!-)[\w.-]+)?)+)", text):
            for flag in re.findall(r"(-{1,2}[A-Za-z][\w-]*)", m.group(1)):
                found.add(flag)
                assert flag in labels, f"{path.relative_to(ROOT)} names install.sh {flag}, the installer knows {sorted(labels)}"
    assert {"--profile", "--server"} <= found, f"the scan found too little: {sorted(found)}"
    assert "./install.sh --server --profile api" in (ROOT / "README.md").read_bytes().decode("utf-8")
