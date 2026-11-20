# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Opening a Word or PowerPoint file named from outside, with no path in what goes wrong.

python-docx's ``Document()`` and python-pptx's ``Presentation()`` put the path they were handed
into their errors: ``PackageNotFoundError`` ("Package not found at '<path>'") for a file that
is not a package, and a plain ``ValueError`` ("file '<path>' is not a Word file, content type
is ...") for a valid package of the wrong type: a workbook or a deck renamed to .docx, a
template (.dotx) or a macro file saved as .docx, the reverse for .pptx. Neither is an OSError,
so `describe_error` passes their text, and with a ``~`` spelling resolved it is the absolute
path under the home folder. Measured: every wrong-type case of the editor's two conversion
routes and of the upload lane (the temporary folder, which on Windows sits under
``C:\\Users\\<name>``) answered with it.

So the package is opened from its bytes. The library then names an ``io.BytesIO`` object in
its errors, never a path; an open file handle would not do, its repr carries the name. And
an error of the open itself becomes `OfficeFileUnreadable` with words of its own ("not a
valid Word file"), which every caller can show as it is.

The cost is the file in memory: opened from a path the libraries read the parts they need
(a 20 MB media part: 2.3 MB peak), from bytes they hold the whole package (22.3 MB). So no
more than `OFFICE_OPEN_MAX_BYTES` is read, the size of the largest file VAF takes in as an
upload; a larger file is refused as too large to open here. The librarian refuses Office
files over 20 MB before it gets here (``librarian_max_doc_size_mb``); the editor's conversion
routes had no bound of their own.

NAMED BOUNDARY, the facade: not exported. The two libraries are optional dependencies and
the facade has no Office surface at all; what an embedder needs to know (open from bytes,
answer the library's error with words of your own) is said where `vaf.describe_error` is
documented in docs/EMBEDDING.md.
"""
import io
import zipfile
from pathlib import Path

__all__ = ["OFFICE_OPEN_MAX_BYTES", "OfficeFileUnreadable", "open_powerpoint", "open_word"]

OFFICE_OPEN_MAX_BYTES = 100 * 1024 * 1024


class OfficeFileUnreadable(ValueError):
    """The file is not a package the library opens, or too large to open here. Its text
    names no path: "not a valid Word file", "not a valid PowerPoint file", "too large to
    open here (over 100 MB)"."""


def _package_bytes(path, max_bytes: int) -> io.BytesIO:
    # Read once, up to one byte past the bound: a size asked before the read could change
    # before it. An OSError (missing, no permission) is the caller's to answer, through
    # describe_error, which keeps only its reason.
    with open(Path(path), "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OfficeFileUnreadable(
            f"too large to open here (over {max_bytes // (1024 * 1024)} MB)")
    return io.BytesIO(data)


def _open(path, max_bytes: int, label: str, opener, not_found):
    package = _package_bytes(path, max_bytes)
    try:
        return opener(package)
    except (ValueError, KeyError, zipfile.BadZipFile, not_found) as e:
        raise OfficeFileUnreadable(f"not a valid {label} file") from e


def open_word(path, *, max_bytes: int = OFFICE_OPEN_MAX_BYTES):
    """The python-docx ``Document`` of the file at ``path``, opened from its bytes."""
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError
    return _open(path, max_bytes, "Word", Document, PackageNotFoundError)


def open_powerpoint(path, *, max_bytes: int = OFFICE_OPEN_MAX_BYTES):
    """The python-pptx ``Presentation`` of the file at ``path``, opened from its bytes."""
    from pptx import Presentation
    from pptx.exc import PackageNotFoundError
    return _open(path, max_bytes, "PowerPoint", Presentation, PackageNotFoundError)
