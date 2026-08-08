#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Build the README hero banner in the style of the 'See it work' section on veyllo.app.

Colours are taken from the site's own CSS, not guessed:
  --accent-ink #1e40af -> --accent #1d4ed8, linear-gradient(180deg, ...)
  dot grid: radial-gradient(... 1px, transparent 0), background-size 26px 26px

Rendered at 2x and downsampled, so text and the screenshot stay crisp on HiDPI.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

S = 2                # supersampling factor
W = 1760 * S         # final 1760 wide (displayed at 880)
SHOT_W = 1300 * S    # product shot width
SHOT_TOP = 348 * S   # where the shot starts
SHOT_BOTTOM = 54 * S  # breathing room under it


def build_composite() -> Image.Image:
    """Terminal in front, desktop window behind and to the right.

    The existing hero.png overlaps them so far that the desktop's centred
    greeting is sliced in half, which reads as a rendering fault rather than a
    design. Rebuilt from the two source shots with the overlap chosen so the
    front window's edge falls LEFT of the greeting, leaving both legible.
    """
    term = Image.open(str(ASSETS / "terminal.png")).convert("RGBA")
    desk = Image.open(str(ASSETS / "desktop.png")).convert("RGBA")

    h = 900                                     # common height, pre-supersampling
    term = term.resize((round(term.width * h / term.height), h), Image.LANCZOS)
    desk = desk.resize((round(desk.width * h / desk.height), h), Image.LANCZOS)

    # The greeting sits mid-width in the desktop shot; keep its left edge clear.
    overlap = round(desk.width * 0.30)
    total_w = term.width + desk.width - overlap
    offset_y = 26                               # slight stagger, depth cue

    canvas = Image.new("RGBA", (total_w, h + offset_y), (0, 0, 0, 0))
    canvas.paste(desk, (term.width - overlap, 0), desk)
    canvas.paste(term, (0, offset_y), term)
    return canvas


_src = build_composite()
SHOT_H = round(_src.height * SHOT_W / _src.width)
H = SHOT_TOP + SHOT_H + SHOT_BOTTOM

TOP = (0x1E, 0x40, 0xAF)   # --accent-ink
BOT = (0x1D, 0x4E, 0xD8)   # --accent

R_BOLD = "/usr/share/fonts/truetype/Roboto-Bold.ttf"
R_MED = "/usr/share/fonts/truetype/Roboto-Medium.ttf"
R_REG = "/usr/share/fonts/truetype/Roboto-Regular.ttf"


def font(path, size, fallback=R_REG):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(fallback, size)


# ── background: vertical gradient ────────────────────────────────────────────
bg = Image.new("RGB", (1, H))
gd = ImageDraw.Draw(bg)
for y in range(H):
    t = y / (H - 1)
    gd.point((0, y), tuple(round(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)))
img = bg.resize((W, H), Image.BILINEAR)

# ── dot grid, as on the site (26px spacing, 1px dots) ────────────────────────
dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
dd = ImageDraw.Draw(dots)
step, radius = 26 * S, 1 * S
for y in range(step, H, step):
    for x in range(step, W, step):
        dd.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, 26))
img = Image.alpha_composite(img.convert("RGBA"), dots)

draw = ImageDraw.Draw(img)


def centre(text, f, y, fill, tracking=0):
    if tracking:
        widths = [draw.textlength(c, font=f) for c in text]
        total = sum(widths) + tracking * (len(text) - 1)
        x = (W - total) / 2
        for c, w in zip(text, widths):
            draw.text((x, y), c, font=f, fill=fill)
            x += w + tracking
        return
    w = draw.textlength(text, font=f)
    draw.text(((W - w) / 2, y), text, font=f, fill=fill)


# ── copy, mirroring the site's section ───────────────────────────────────────
centre("SEE IT WORK", font(R_BOLD, 20 * S), 62 * S, (255, 255, 255, 200), tracking=3.2 * S)
centre("The main agent", font(R_BOLD, 62 * S), 104 * S, (255, 255, 255, 255))
sub = font(R_REG, 25 * S)
centre("You ask in plain language. The main agent plans the work",
       sub, 196 * S, (255, 255, 255, 205))
centre("and delegates each part to a specialist sub-agent.",
       sub, 232 * S, (255, 255, 255, 205))

# ── the two surfaces, as the site's Desktop / CLI toggle implies ─────────────
pill_y, pill_h = 288 * S, 44 * S
labels = [("Desktop", True), ("CLI", False)]
pill_f = font(R_MED, 21 * S)
widths = [draw.textlength(t, font=pill_f) + 42 * S for t, _ in labels]
x = (W - (sum(widths) + 12 * S)) / 2
for (text, active), w in zip(labels, widths):
    box = (x, pill_y, x + w, pill_y + pill_h)
    if active:
        draw.rounded_rectangle(box, radius=pill_h / 2, fill=(17, 24, 39, 255))
        tc = (255, 255, 255, 255)
    else:
        draw.rounded_rectangle(box, radius=pill_h / 2, fill=(255, 255, 255, 235))
        tc = (17, 24, 39, 255)
    tw = draw.textlength(text, font=pill_f)
    draw.text((x + (w - tw) / 2, pill_y + 10 * S), text, font=pill_f, fill=tc)
    x += w + 12 * S

# ── the product shot: rounded corners, soft drop shadow ──────────────────────
shot = _src.resize((SHOT_W, SHOT_H), Image.LANCZOS)

# Round the corners so the window sits on the gradient like a card rather than
# a pasted rectangle.
corner = 14 * S
mask = Image.new("L", shot.size, 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, shot.width - 1, shot.height - 1),
                                       radius=corner, fill=255)
shot.putalpha(mask)

sx, sy = (W - shot.width) // 2, SHOT_TOP
shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle(
    (sx + 6 * S, sy + 16 * S, sx + shot.width - 6 * S, sy + shot.height + 12 * S),
    radius=corner, fill=(6, 14, 45, 160))
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(24 * S)))
img.paste(shot, (sx, sy), shot)

img.convert("RGB").resize((W // S, H // S), Image.LANCZOS).save(
    str(ASSETS / "banner.png"), optimize=True)
print(f"written: {ASSETS / 'banner.png'}")
