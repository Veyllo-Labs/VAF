# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
from pathlib import Path

from vaf.tools.filesystem import _resolve_folder_alias

HOME = str(Path.home())


def test_alias_with_separator_resolves():
    # Legitimate alias use: 'Documents/<file>' must resolve into the home folder.
    out = _resolve_folder_alias("Documents/report.txt")
    assert os.path.isabs(out) and out.startswith(HOME) and out.endswith("report.txt")


def test_alias_exact_resolves():
    # The bare alias resolves to the folder itself.
    out = _resolve_folder_alias("Documents")
    assert os.path.isabs(out) and out.startswith(HOME)


def test_alias_prefix_without_separator_is_not_rerouted():
    # 'Documentsfile.txt' is a relative FILENAME, not the Documents folder.
    # It must NOT be rerouted into ~/Documents/file.txt.
    out = _resolve_folder_alias("Documentsfile.txt")
    assert out == "Documentsfile.txt"


# ── temp exemption from the blocked-dir screen ────────────────────────────

def test_macos_temp_spelling_passes_the_blocked_prefix_screen(monkeypatch):
    """macOS hands out temp paths in the /var/folders SYMLINK spelling, which
    the absolute /var entry in BLOCKED_DIRS refused - every staging lane
    (upload funnel, sidebar documents) got "Access denied: /var" back, on
    macOS only, so no local gate could see it. The temp roots are exempt from
    the blocked-dir screen; /var OUTSIDE them stays blocked. Pinned through
    the seam so the class is testable on Linux."""
    import vaf.tools.filesystem as fs
    monkeypatch.setattr(fs, "_TEMP_ROOTS", ("/var/folders",))
    ok, msg = fs.is_safe_path("/var/folders/ab/T/upload.txt")
    assert ok, msg
    ok, _ = fs.is_safe_path("/var/log/system.log")
    assert not ok, "/var outside the temp root must stay blocked"
    # Only the LOCATION screens stand down in temp: the name-based tokens
    # describe what a file IS, and a staged .env stays refused.
    ok, _ = fs.is_safe_path("/var/folders/ab/T/.env")
    assert not ok, "name-based blocks must keep applying inside temp"


def test_this_platforms_real_temp_dir_is_readable():
    """The invariant behind the seam test, on whatever platform runs this."""
    import tempfile
    import vaf.tools.filesystem as fs
    ok, msg = fs.is_safe_path(os.path.join(tempfile.gettempdir(), "vaf-probe.txt"))
    assert ok, msg
