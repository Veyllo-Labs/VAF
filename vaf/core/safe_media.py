# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which content types VAF passes on to the browser as an image.

Three routes hand the browser image bytes whose type someone else chose: the mail image proxy
(the sender's server), a mail's inline parts (the sender) and WhatsApp profile pictures (the
CDN). They checked "starts with image/ and is not exactly image/svg+xml" and then served the
foreign value as it came. Two Content-Type headers arrive joined by a comma
(``image/png, text/html``), which passes that check, and browsers take the LAST type in such
a list - HTML on the app's origin. SVG is an image type that runs script.

So the decision is an allowlist of raster types, and what goes out is the canonical type from
this table, never the value that came in.
"""
from __future__ import annotations

from typing import Optional

_RASTER_IMAGE_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
    "image/avif": "image/avif",
    "image/bmp": "image/bmp",
    "image/x-ms-bmp": "image/bmp",
    "image/x-icon": "image/x-icon",
    "image/vnd.microsoft.icon": "image/x-icon",
}


def raster_image_type(content_type: Optional[str]) -> Optional[str]:
    """The canonical type to serve these bytes as, or None when they must not be served as an
    image: SVG, or anything that is not a raster format. The match is exact, so a joined value
    (``image/png, text/html``) is no key; and whatever matched, what goes out is this table's
    value, never the incoming string - so a parameter part (``; charset=...``) is dropped and no
    second type can ride along."""
    raw = (content_type or "").strip().lower()
    return _RASTER_IMAGE_TYPES.get(raw.split(";", 1)[0].strip())
