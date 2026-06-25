"""Image helpers for the vision pipeline.

Downscaling images before they are sent to an LLM provider. Full-resolution phone
photos (several MB of base64) routinely make OpenAI's chat/completions return a
500 Internal Server Error instead of a clean 400, and they waste a lot of tokens.
OpenAI internally caps high-detail images at ~2048px anyway, so shrinking the long
edge to a sane bound is lossless for vision while fixing the 500 and cutting cost.

Design: this MUST be safe — it runs on every user image in the hot path. The helper
never raises; on ANY problem (bad base64, unknown format, Pillow missing) it returns
the ORIGINAL data unchanged, and small images (within the bound) are returned
untouched so normal cases keep their exact bytes.
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Tuple

_log = logging.getLogger(__name__)


def downscale_image_b64(
    raw_b64: str,
    mime_type: str,
    max_edge: int = 2000,
    jpeg_quality: int = 85,
) -> Tuple[str, str]:
    """Return ``(base64, mime_type)``, downscaled only if the longest edge exceeds
    ``max_edge``. Returns the original unchanged on any failure or if already small.

    ``raw_b64`` may be raw base64 or a ``data:...;base64,`` URI (the prefix is
    stripped before decoding). The returned base64 is always prefix-free.
    """
    if not raw_b64:
        return raw_b64, mime_type
    try:
        from PIL import Image  # lazy: Pillow is a dependency but keep import local

        b64 = raw_b64
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[1] if "," in b64 else b64

        data = base64.b64decode(b64)
        im = Image.open(BytesIO(data))
        w, h = im.size
        if max(w, h) <= max_edge:
            # Already within bounds — never re-encode small images (keep exact bytes).
            return raw_b64, mime_type

        im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        # Keep PNG for images that carry transparency; otherwise JPEG (smaller).
        keep_png = (mime_type or "").lower().endswith("png") and im.mode in ("RGBA", "LA", "P")
        buf = BytesIO()
        if keep_png:
            im.save(buf, format="PNG", optimize=True)
            out_mime = "image/png"
        else:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")  # JPEG cannot encode RGBA/P
            im.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            out_mime = "image/jpeg"

        out_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        _log.debug(
            "[image] downscaled %dx%d (%d B) -> %dx%d (%d B)",
            w, h, len(data), *im.size, len(buf.getvalue()),
        )
        return out_b64, out_mime
    except Exception as e:  # never break the turn — fall back to the original image
        _log.debug("[image] downscale skipped (%s); sending original", e)
        return raw_b64, mime_type
