"""Regression: vision images must be downscaled before send (fixes OpenAI HTTP 500
on full-resolution photos) — and the helper must NEVER raise on the hot path.

Pins vaf.core.image_utils.downscale_image_b64: shrink only oversized images, leave
small ones byte-identical, and fall back to the original on any bad input.
Pillow generates the test images in-memory — no network, no files.
"""
import base64
from io import BytesIO

from PIL import Image

from vaf.core.image_utils import downscale_image_b64


def _b64_img(w: int, h: int, mode: str = "RGB", fmt: str = "JPEG") -> str:
    color = (10, 20, 30, 128) if mode == "RGBA" else (123, 222, 64)
    im = Image.new(mode, (w, h), color)
    buf = BytesIO()
    im.save(buf, format=fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _size(b64: str):
    return Image.open(BytesIO(base64.b64decode(b64))).size


def test_large_image_is_downscaled_and_smaller():
    src = _b64_img(4000, 3000, "RGB", "JPEG")
    out, mime = downscale_image_b64(src, "image/jpeg", max_edge=2000)
    w, h = _size(out)
    assert max(w, h) <= 2000
    assert len(out) < len(src)          # fewer bytes -> the 500-causing payload shrinks
    assert mime == "image/jpeg"


def test_small_image_is_returned_untouched():
    src = _b64_img(800, 600, "RGB", "JPEG")
    out, mime = downscale_image_b64(src, "image/jpeg", max_edge=2000)
    assert out == src                   # exact same bytes — no re-encode for small images
    assert mime == "image/jpeg"


def test_corrupt_base64_returns_original():
    bad = "this is not!! valid base64 @@@@"
    out, mime = downscale_image_b64(bad, "image/jpeg")
    assert out == bad and mime == "image/jpeg"


def test_non_image_base64_returns_original():
    src = base64.b64encode(b"plain text, definitely not an image").decode("ascii")
    out, mime = downscale_image_b64(src, "image/jpeg")
    assert out == src


def test_large_rgba_png_downscales_without_error():
    src = _b64_img(3000, 2500, "RGBA", "PNG")
    out, mime = downscale_image_b64(src, "image/png", max_edge=2000)
    w, h = _size(out)
    assert max(w, h) <= 2000
    assert mime in ("image/png", "image/jpeg")   # alpha kept as PNG by our rule


def test_data_uri_prefix_is_tolerated():
    src = _b64_img(3000, 1000, "RGB", "JPEG")
    out, _ = downscale_image_b64("data:image/jpeg;base64," + src, "image/jpeg", max_edge=2000)
    w, h = _size(out)                   # output must be prefix-free + decodable
    assert max(w, h) <= 2000


def test_empty_input_is_safe():
    assert downscale_image_b64("", "image/jpeg") == ("", "image/jpeg")
