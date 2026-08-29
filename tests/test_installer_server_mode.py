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
reformatting stays cheap while removing a step goes red."""
import re
from pathlib import Path

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
