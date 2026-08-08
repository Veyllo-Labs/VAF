#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Build the README hero banner, still and animated, in the style of the
'See it work' section on veyllo.app.

Colours come from the site's own CSS rather than from eyeballing the page:
  --accent-ink #1e40af -> --accent #1d4ed8 on a 180deg gradient
  dot grid: radial-gradient(... 1px, transparent 0), background-size 26px 26px

Outputs, both rendered at 2x and downsampled so they stay crisp on HiDPI:
  docs/assets/banner.png  - the still
  docs/assets/banner.gif  - the same frame, with a glint drifting across the
                            dotted background. Nothing else moves; its first
                            frame is the still, so it degrades gracefully
                            wherever animation is not played.

Run with no arguments to write both.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

S = 2                 # supersampling factor for the still
W = 1760 * S          # final 1760 wide (displayed at 880)
SHOT_W = 1300 * S     # width the window pair occupies
SHOT_TOP = 268 * S
SHOT_BOTTOM = 54 * S

# Window geometry in design units; scaled once to fit SHOT_W.
DESIGN_CARD_W = 1120
DESIGN_TERM_POS = (0, 148)      # behind, lower-left
# The front edge lands just right of the terminal's session panel. Further right
# and a sliver of the ASCII banner shows through cropped mid-glyph, which reads
# as corruption rather than as one window behind another.
DESIGN_DESK_POS = (352, 0)      # in front, upper-right

TOP = (0x1E, 0x40, 0xAF)
BOT = (0x1D, 0x4E, 0xD8)

R_BOLD = "/usr/share/fonts/truetype/Roboto-Bold.ttf"
R_REG = "/usr/share/fonts/truetype/Roboto-Regular.ttf"

TITLE = "VAF - Veyllo Agentic Framework"
SUBTEXT = ("The open-source AI agent Framework and Harness with persistent memory "
           "and tools for web, code and files. Fully self-sufficient in-house, or "
           "with models from the cloud.")


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(R_REG, size)


def card(img, width, radius):
    """Scale a screenshot and round its corners, so it sits on the gradient as a
    card rather than a pasted rectangle."""
    scaled = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    mask = Image.new("L", scaled.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, scaled.width - 1, scaled.height - 1),
                                           radius=radius, fill=255)
    scaled.putalpha(mask)
    return scaled


# ── geometry ─────────────────────────────────────────────────────────────────
_term_src = Image.open(str(ASSETS / "terminal.png")).convert("RGBA")
_desk_src = Image.open(str(ASSETS / "desktop.png")).convert("RGBA")

design_w = DESIGN_DESK_POS[0] + DESIGN_CARD_W
scale = SHOT_W / design_w
card_w = round(DESIGN_CARD_W * scale)
radius = round(13 * scale)

term = card(_term_src, card_w, radius)
desk = card(_desk_src, card_w, radius)

ox, oy = (W - SHOT_W) // 2, SHOT_TOP
term_at = (ox + round(DESIGN_TERM_POS[0] * scale), oy + round(DESIGN_TERM_POS[1] * scale))
desk_at = (ox + round(DESIGN_DESK_POS[0] * scale), oy + round(DESIGN_DESK_POS[1] * scale))

shot_bottom = max(term_at[1] + term.height, desk_at[1] + desk.height)
H = shot_bottom + SHOT_BOTTOM


DOT_STEP = 26 * S
DOT_R = 1 * S
DOT_ALPHA = 26          # the site's resting dot brightness
GLINT_ALPHA = 160       # peak of the travelling glint
GLINT_GROW = 0.45       # extra dot radius at the glint's core, in canvas px
GLINT_WIDTH = 0.20      # fraction of the canvas the glint spans


def dot_layer(centre_x=None):
    """The site's dot grid. With `centre_x`, a soft vertical band of brighter
    dots sits there - the glint the animation sweeps across the background.

    A band rather than a scatter on purpose: GIF stores each frame as one
    rectangle of changed pixels, so dots twinkling all over the canvas would
    make every frame a full frame. A band keeps the changed rectangle narrow,
    which is what buys the resolution back.
    """
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    half = GLINT_WIDTH * W / 2
    for y in range(DOT_STEP, H, DOT_STEP):
        for x in range(DOT_STEP, W, DOT_STEP):
            a, r = DOT_ALPHA, DOT_R
            if centre_x is not None:
                # Wrap the distance so the band re-enters on the left instead of
                # jumping, and fall off smoothly to avoid a hard edge.
                dist = abs(x - centre_x)
                dist = min(dist, W - dist)
                if dist < half:
                    fall = math.cos(dist / half * math.pi / 2) ** 2
                    a = round(DOT_ALPHA + (GLINT_ALPHA - DOT_ALPHA) * fall)
                    # Brightness alone tops out at white and then has nowhere to
                    # go; letting the dot swell a little as well is what reads as
                    # a spark rather than as a lamp being turned up.
                    r = DOT_R + GLINT_GROW * S * fall
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, a))
    return layer


# ── background: gradient, dot grid, copy ─────────────────────────────────────
def build_background(glint_x=None):
    strip = Image.new("RGB", (1, H))
    gd = ImageDraw.Draw(strip)
    for y in range(H):
        t = y / (H - 1)
        gd.point((0, y), tuple(round(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)))
    bg = strip.resize((W, H), Image.BILINEAR).convert("RGBA")

    bg = Image.alpha_composite(bg, dot_layer(glint_x))

    draw = ImageDraw.Draw(bg)

    def centre(text, f, y, fill):
        draw.text(((W - draw.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)

    # Fitted to the canvas rather than set at a fixed size: this title is twice
    # as long as the site section's headline, so a hard size would run into the
    # edges, and a later rename could overflow the image silently.
    title_f = font(R_BOLD, 62 * S)
    while draw.textlength(TITLE, font=title_f) > 1420 * S:
        title_f = font(R_BOLD, title_f.size - 2 * S)
    centre(TITLE, title_f, 74 * S, (255, 255, 255, 255))

    sub = font(R_REG, 25 * S)
    lines, line = [], ""
    for word in SUBTEXT.split():          # wrap measured against the real font,
        trial = f"{line} {word}".strip()  # so new copy cannot run off the canvas
        if draw.textlength(trial, font=sub) <= 1180 * S or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    lines.append(line)

    y = 168 * S
    for line in lines:
        centre(line, sub, y, (255, 255, 255, 205))
        y += 36 * S
    return bg


def place(canvas, blur, term_off=(0, 0), desk_off=(0, 0)):
    """Paste both windows, each with its own shadow. One shadow around the pair's
    bounding box would sit under the empty corners the stagger creates."""
    out = canvas
    for c, (bx, by), (dx, dy) in ((term, term_at, term_off), (desk, desk_at, desk_off)):
        x, y = bx + dx, by + dy
        shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (x + 5 * S, y + 12 * S, x + c.width - 5 * S, y + c.height + 8 * S),
            radius=radius, fill=(6, 14, 45, 165))
        out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(blur)))
        out.paste(c, (x, y), c)
    return out


def write_png(bg):
    img = place(bg.copy(), 18 * S)
    img.convert("RGB").resize((W // S, H // S), Image.LANCZOS).save(
        str(ASSETS / "banner.png"), optimize=True)
    print(f"written: {ASSETS / 'banner.png'}")


def write_gif(frames=52, ms=60, colors=256):
    """The still frame with a glint drifting across the dotted background.

    Nothing else moves. The earlier version drifted both windows, and at banner
    size a few pixels of travel per frame reads as a tremor rather than as
    floating; slowing it down does not help, because the jolt is the distance
    covered between frames, not the frame rate.

    Holding the windows still is also what makes this sharp. GIF stores each
    frame as one rectangle of changed pixels: with only a narrow band moving,
    the frames after the first are small, and the budget goes into keeping the
    full 2x resolution rather than into re-encoding two moving screenshots.

    Frame count is what makes it fluid, not frame duration. At 26 frames the
    band jumped 3.8% of the width per step and stuttered; at 52 it moves 1.9%
    and reads as a drift. That costs bytes, and there are bytes to spend: the
    proxy GitHub serves README images through refuses anything past 5 MB, and
    this sits near half a megabyte.
    """
    seq = []
    for i in range(frames):
        x = W * i / frames
        img = place(build_background(glint_x=x).copy(), 18 * S)
        seq.append(img.convert("RGB").resize((W // S, H // S), Image.LANCZOS))

    palette = seq[0].quantize(colors=colors, method=Image.MEDIANCUT)
    # No dithering, and that is the whole reason this fits at full resolution.
    # Floyd-Steinberg diffuses each pixel's error into its neighbours, so one
    # brighter dot rewrites a trail of pixels behind it and the frame deltas
    # explode - measured at 2.86 MB dithered against 0.29 MB flat, same frames,
    # same size. The palette spans a narrow blue and dark greys, so flat
    # quantisation costs almost nothing here.
    seq = [f.quantize(palette=palette, dither=Image.NONE) for f in seq]
    # disposal=1 keeps the previous frame and writes only what changed. Safe
    # because every pixel the glint leaves behind also changes, so it falls
    # inside the diff and gets repainted; verified frame by frame rather than
    # assumed, since a wrong disposal leaves smears no still frame would reveal.
    seq[0].save(str(ASSETS / "banner.gif"), save_all=True, append_images=seq[1:],
                duration=ms, loop=0, optimize=True, disposal=1)
    size = (ASSETS / "banner.gif").stat().st_size
    print(f"written: {ASSETS / 'banner.gif'}  ({W // S}px, {frames} frames, "
          f"{size / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    write_png(build_background())
    write_gif()
