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
