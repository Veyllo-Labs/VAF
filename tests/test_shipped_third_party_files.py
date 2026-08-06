# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Foreign files we SHIP carry their license. Dependencies do not need to, and must not.

The notice requirements of MIT, BSD, ISC and Apache-2.0 attach to distributing a copy:
"in all copies or substantial portions of the Software", "Redistributions of source code
must retain the above copyright notice", "reproduce and distribute copies ... You must
give any other recipients of the Work ... a copy of this License". They do not attach to
depending on something a package manager fetches onto the user's own machine.

That distinction is the whole design of this guard, and it cuts both ways:

- A foreign file checked INTO this repository travels with every clone, source archive
  and package, so it owes its notice. This test refuses a new one that arrives without.
- A dependency in `requirements.txt` or `package.json` owes nothing here. Vendoring
  several hundred license texts to be safe would create a hand-kept inventory that rots
  within weeks and claims a diligence it does not have.

The measurement that produced this file, 2026-08-03: three foreign files were shipped and
only one was in order. `vaf/vendor/langid/langid.py` carried its BSD-2 text in full.
`web/public/pdf.worker.min.mjs` carried Mozilla's copyright and a LINK to Apache-2.0,
where section 4(a) asks for a copy - the full text existed nowhere in the tree. And
`vaf/tools/_stealth_payload.js` was 43 KB of bundled MIT code with no notice at all, kept
alive by nothing: the browser agent had stopped injecting it and injects a different file.
It was deleted rather than licensed, which is the better repair when it is available.
"""
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LICENSES = _REPO / "licenses"

# Our own files say so in the SPDX header; everything else is a candidate.
_OURS = re.compile(r"SPDX-FileCopyrightText:.*Veyllo GmbH")
_FOREIGN_MARKER = re.compile(r"copyright|licen[sc]e", re.I)

# Extensions that can carry code or a font we would be shipping. Data and prose are not
# in scope: a .md or .json file is not a work whose license travels with it this way.
_CODE_SUFFIXES = (".js", ".mjs", ".py", ".ts", ".tsx", ".css", ".ttf", ".otf", ".woff", ".woff2")

_SKIP_PREFIXES = ("venv/", "node_modules/", "web/node_modules/", "tests/")

# Every foreign file we ship, and HOW its obligation is met. This map may only SHRINK
# (by deleting the file) or grow together with a real new vendored file - never by
# quietly adding a name to make the test pass.
_SHIPPED = {
    "vaf/vendor/langid/langid.py": {
        "how": "inline",
        "reason": "BSD-2-Clause, Copyright 2011 Marco Lui. Condition 1 asks that a source "
                  "redistribution retain the notice, the conditions and the disclaimer, and "
                  "the file header carries all three verbatim. A second copy under licenses/ "
                  "would add a place to drift, not a right.",
    },
    "web/public/pdf.worker.min.mjs": {
        "how": "licenses/pdfjs-dist-Apache-2.0.txt",
        "reason": "Apache-2.0, Copyright 2024 Mozilla Foundation. Checked in because react-pdf "
                  "requires the worker to be served as a static asset. The header carries the "
                  "notice and a link, but section 4(a) asks for a COPY of the license, so the "
                  "full text ships alongside.",
    },
}


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=_REPO, capture_output=True, text=True).stdout
    return [p for p in out.split("\n") if p]


def _foreign_files():
    for rel in _tracked():
        if rel.startswith(_SKIP_PREFIXES) or not rel.endswith(_CODE_SUFFIXES):
            continue
        path = _REPO / rel
        if not path.is_file():
            continue
        if rel.endswith((".ttf", ".otf", ".woff", ".woff2")):
            yield rel                      # a font is always someone else's work
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if _OURS.search(head):
            continue
        if _FOREIGN_MARKER.search(head):
            yield rel


def test_every_shipped_foreign_file_is_accounted_for():
    """A new vendored file has to bring its license with it, in the same change."""
    found = set(_foreign_files())
    unaccounted = sorted(found - set(_SHIPPED))
    assert not unaccounted, (
        "Foreign file(s) checked in without a license record:\n  "
        + "\n  ".join(unaccounted)
        + "\n\nEither keep the full license text in the file's own header, or add the text "
          "under licenses/ and register the file in _SHIPPED with the reason. If nothing "
          "reads the file, deleting it is the better repair - that is what happened to "
          "vaf/tools/_stealth_payload.js."
    )


def test_the_record_does_not_describe_files_that_are_gone():
    """The map shrinks when a file goes, so it cannot claim coverage for nothing."""
    missing = [name for name in _SHIPPED if not (_REPO / name).exists()]
    assert not missing, f"_SHIPPED still lists deleted files: {missing}"


@pytest.mark.parametrize("name", sorted(_SHIPPED))
def test_each_record_carries_a_reason(name):
    assert len(_SHIPPED[name]["reason"]) > 80, f"{name} needs a reason a stranger can act on"


def test_the_inline_licenses_are_actually_complete():
    """"Inline" has to mean the whole text, not just a copyright line. BSD-2 needs both
    numbered conditions AND the disclaimer - a notice without them is not the license."""
    for name, rec in _SHIPPED.items():
        if rec["how"] != "inline":
            continue
        head = (_REPO / name).read_text(encoding="utf-8", errors="replace")[:6000]
        assert "Copyright" in head, f"{name} lost its copyright line"
        assert "Redistribution and use in source and binary forms" in head, f"{name} lost the grant"
        assert "AS IS" in head and "WARRANT" in head.upper(), f"{name} lost the disclaimer"


def test_the_referenced_license_files_exist_and_are_whole():
    """A truncated license text satisfies nothing, so the length and the closing section
    are pinned rather than the mere presence of a file."""
    for name, rec in _SHIPPED.items():
        target = rec["how"]
        if target == "inline":
            continue
        path = _REPO / target
        assert path.exists(), f"{name} points at {target}, which does not exist"
        text = path.read_text(encoding="utf-8")
        assert "Apache License" in text and "Version 2.0" in text
        assert "4. Redistribution" in text, f"{target} is missing the redistribution section"
        assert "END OF TERMS AND CONDITIONS" in text, f"{target} looks truncated"


def test_the_licenses_folder_says_what_does_not_belong_in_it():
    """Without this note the folder invites the mistake it exists to avoid: someone fills
    it with every dependency and produces an inventory that rots."""
    readme = (_LICENSES / "README.md").read_text(encoding="utf-8")
    assert "not a dependency inventory" in readme
    assert "docs/legal/THIRD_PARTY.md" in readme


def test_the_deleted_payload_stays_deleted():
    """It was bundled third-party code with no notice at all, and nothing read it. If it
    ever comes back it has to come back with its license, which the scan above enforces -
    this pin just names the incident so the next reader finds the reason."""
    assert not (_REPO / "vaf" / "tools" / "_stealth_payload.js").exists()
    supplement = _REPO / "vaf" / "tools" / "_stealth_supplement.js"
    assert supplement.exists(), "the file that actually IS injected must still be there"
