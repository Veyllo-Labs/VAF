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
SHOT_TOP = 268 * S   # where the shots start
SHOT_BOTTOM = 54 * S  # breathing room under it


def _card(img: Image.Image, width: int, radius: int = 13) -> Image.Image:
    """Scale a screenshot to `width` and round its corners, so it sits on the
    gradient as a card rather than a pasted rectangle."""
    scaled = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    mask = Image.new("L", scaled.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, scaled.width - 1, scaled.height - 1),
                                           radius=radius, fill=255)
    scaled.putalpha(mask)
    return scaled


def build_composite() -> Image.Image:
    """Desktop window in front and higher, terminal behind and lower.

    Staggered on a diagonal rather than side by side: the terminal keeps its
    left column visible - the session list and the ASCII banner, the parts that
    identify it at a glance - while the desktop window stays whole. Each card
    carries its own shadow; one shadow around the bounding box would sit under
    the empty corners the stagger creates.
    """
    term = Image.open(str(ASSETS / "terminal.png")).convert("RGBA")
    desk = Image.open(str(ASSETS / "desktop.png")).convert("RGBA")

    card_w = 1120                       # pre-supersampling
    term = _card(term, card_w)
    desk = _card(desk, card_w)

    term_pos = (0, 148)                 # behind, lower-left
    # The front edge lands just right of the terminal's session panel. Further
    # right and a sliver of the ASCII banner shows through, cropped mid-glyph,
    # which reads as corruption rather than as a window behind another window.
    desk_pos = (352, 0)                 # in front, upper-right

    total = (desk_pos[0] + desk.width,
             max(term_pos[1] + term.height, desk_pos[1] + desk.height))
    canvas = Image.new("RGBA", total, (0, 0, 0, 0))

    for card, (x, y) in ((term, term_pos), (desk, desk_pos)):
        shadow = Image.new("RGBA", total, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (x + 5, y + 12, x + card.width - 5, y + card.height + 8),
            radius=13, fill=(6, 14, 45, 165))
        canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(18)))
        canvas.paste(card, (x, y), card)
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


def wrap(text, f, max_width):
    """Greedy wrap. Measured against the real font rather than a character
    count, so a copy change cannot silently run past the canvas edge."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=f) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


# ── copy ─────────────────────────────────────────────────────────────────────
# The banner carries the project title itself, so the README has no separate
# heading above it to repeat. Sized to the canvas rather than fixed: this title
# is twice as long as the site section's, and a hard size would either overflow
# or leave the short one looking lost.
TITLE = "VAF - Veyllo Agentic Framework"
title_f = font(R_BOLD, 62 * S)
while draw.textlength(TITLE, font=title_f) > 1420 * S:
    title_f = font(R_BOLD, title_f.size - 2 * S)
centre(TITLE, title_f, 74 * S, (255, 255, 255, 255))

sub = font(R_REG, 25 * S)
SUBTEXT = ("The open-source AI agent Framework and Harness with persistent memory "
           "and tools for web, code and files. Fully self-sufficient in-house, or "
           "with models from the cloud.")
y = 168 * S
for line in wrap(SUBTEXT, sub, 1180 * S):
    centre(line, sub, y, (255, 255, 255, 205))
    y += 36 * S

# ── the product shots ────────────────────────────────────────────────────────
shot = _src.resize((SHOT_W, SHOT_H), Image.LANCZOS)
img.paste(shot, ((W - shot.width) // 2, SHOT_TOP), shot)

img.convert("RGB").resize((W // S, H // S), Image.LANCZOS).save(
    str(ASSETS / "banner.png"), optimize=True)
print(f"written: {ASSETS / 'banner.png'}")
