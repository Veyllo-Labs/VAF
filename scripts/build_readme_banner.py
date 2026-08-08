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

import numpy as np

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

# The background is drawn at 2x and downsampled, which keeps the title and the
# gradient crisp on HiDPI. The screenshots deliberately skip that path and are
# composited at 1x - see card().
S = 2
W = 1760 * S          # final 1760 wide (displayed at 880)
SHOT_TOP = 268 * S    # where the window pair starts
SHOT_BOTTOM = 54 * S  # breathing room under it

TOP = (0x1E, 0x40, 0xAF)
BOT = (0x1D, 0x4E, 0xD8)

# Resolved by name, not by path. Pillow searches the platform's font
# directories, so this works on Linux, macOS and Windows alike; hardcoded
# absolute paths meant the script only ran on the machine it was written on,
# which for a script in a public repository is worse than having none - it
# looks maintainable and is not.
#
# Roboto is what the committed banner was rendered with, and the assets only
# reproduce byte for byte where it is installed. The rest of the list keeps the
# script usable elsewhere, at the cost of a substituted typeface; the run says
# which one it picked so a surprising result is explainable.
FONTS = {
    "bold": ["Roboto-Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
             "Arialbd.ttf", "arialbd.ttf", "seguisb.ttf", "HelveticaNeue.ttc"],
    "regular": ["Roboto-Regular.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                "Arial.ttf", "arial.ttf", "segoeui.ttf", "Helvetica.ttc"],
}
_reported = set()

TITLE = "VAF - Veyllo Agentic Framework"
SUBTEXT = ("The open-source AI agent Framework and Harness with persistent memory "
           "and tools for web, code and files. Fully self-sufficient in-house, or "
           "with models from the cloud.")


def font(weight, size):
    """First installed candidate for `weight`, searched by name."""
    for name in FONTS[weight]:
        try:
            f = ImageFont.truetype(name, size)
        except OSError:
            continue
        if weight not in _reported:
            _reported.add(weight)
            if name != FONTS[weight][0]:
                print(f"note: {FONTS[weight][0]} not installed, rendering {weight} "
                      f"with {name} - the committed assets will not match byte for byte")
        return f
    raise SystemExit(
        f"No usable {weight} font found. Install Roboto (the banner's typeface) or any "
        f"of: {', '.join(FONTS[weight][1:])}")


def card(img, radius):
    """Round a screenshot's corners so it sits on the gradient as a card rather
    than a pasted rectangle. Deliberately does NOT resize.

    Every resample of a screenshot costs something, and here it cost visibly:
    the app window's interior is one flat tone with a single-level step across
    it, invisible at 1:1, and rescaling smeared that step into a faint striped
    texture. The layout is sized around the screenshots now, not the other way
    round, so each is composited at its native pixel size and whatever is in the
    file is exactly what appears.
    """
    out = img.copy()
    mask = Image.new("L", out.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, out.width - 1, out.height - 1),
                                           radius=radius, fill=255)
    out.putalpha(mask)
    return out


# ── geometry ─────────────────────────────────────────────────────────────────
_term_src = Image.open(str(ASSETS / "terminal.png")).convert("RGBA")
_desk_src = Image.open(str(ASSETS / "desktop.png")).convert("RGBA")

radius = 13
term = card(_term_src, radius)
desk = card(_desk_src, radius)

# Offsets follow the screenshots' own pixel sizes rather than a design grid,
# because the layout is now sized around them instead of the other way round.
#
# The inset is measured off the TERMINAL, not the front window: it has to land
# just past the session panel's right edge. Any further and a strip of the ASCII
# banner shows through cropped mid-glyph, which reads as corruption rather than
# as one window standing behind another.
stagger_x = round(term.width * 0.295)      # front window's inset from the left
stagger_y = round(desk.height * 0.132)     # back window's drop
pair_w = stagger_x + desk.width

ox = (W // S - pair_w) // 2
oy = SHOT_TOP // S
term_at = (ox, oy + stagger_y)
desk_at = (ox + stagger_x, oy)

shot_bottom = max(term_at[1] + term.height, desk_at[1] + desk.height)
H = (shot_bottom + SHOT_BOTTOM // S) * S


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
    title_f = font("bold", 62 * S)
    while draw.textlength(TITLE, font=title_f) > 1420 * S:
        title_f = font("bold", title_f.size - 2 * S)
    centre(TITLE, title_f, 74 * S, (255, 255, 255, 255))

    sub = font("regular", 25 * S)
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


def render(glint_x=None):
    """The finished frame: background downsampled from 2x, windows composited at
    final size so they are resampled exactly once."""
    out = build_background(glint_x).convert("RGB").resize(
        (W // S, H // S), Image.LANCZOS).convert("RGBA")
    for c, (x, y) in ((term, term_at), (desk, desk_at)):
        shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (x + 3, y + 6, x + c.width - 3, y + c.height + 4),
            radius=radius, fill=(6, 14, 45, 165))
        out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(9)))
        out.paste(c, (x, y), c)
    return out.convert("RGB")


def write_png():
    render().save(str(ASSETS / "banner.png"), optimize=True)
    print(f"written: {ASSETS / 'banner.png'}")


def build_palette(sample, colors=256, exact=176):
    """A palette that carries the picture's dominant colours verbatim.

    Both stock methods split a large flat area across several entries, and the
    eye reads that as horizontal bands in exactly the places that should be one
    tone - the interiors of the two app windows. Median cut divides the palette
    by how much area each colour covers, so the blue gradient claims it and the
    interiors are left snapping between levels; max coverage spreads entries
    evenly instead, which fixes the interiors and wrecks the gradient.

    Neither is needed. A flat area is flat because one colour repeats thousands
    of times, so the fix is to reserve entries for the most FREQUENT colours,
    exactly as they are: they then map to themselves and cannot band. The
    remaining entries come from median cut, which is well suited to what is
    left - edges, text antialiasing, window chrome.
    """
    counts = sample.getcolors(maxcolors=1 << 24) or []
    counts.sort(key=lambda c: -c[0])
    entries = [c[1][:3] for c in counts[:exact]]

    rest = colors - len(entries)
    if rest > 0:
        mc = sample.quantize(colors=rest, method=Image.MEDIANCUT).getpalette()[:rest * 3]
        entries += [tuple(mc[i * 3:i * 3 + 3]) for i in range(rest)]

    return np.array(entries[:colors], dtype=np.uint8)


def map_to_palette(im, pal_rgb):
    """Map every pixel to its nearest palette entry, computed exactly.

    Pillow's own palette mapping is approximate, and here it is wrong in a way
    that is plainly visible: with (32,32,32) sitting in the palette, a source
    pixel of (31,31,31) was mapped to (28,28,28) - three levels away, past the
    obvious neighbour. That is what striped the app windows' flat interiors,
    and no choice of palette could have fixed it.

    Distances are computed over the frame's DISTINCT colours rather than its
    pixels, so this stays cheap: a banner frame has a few tens of thousands of
    distinct colours against two million pixels.
    """
    a = np.asarray(im, dtype=np.int32)
    h, w, _ = a.shape
    uniq, inverse = np.unique(a.reshape(-1, 3), axis=0, return_inverse=True)

    nearest = np.empty(len(uniq), dtype=np.uint8)
    pal = pal_rgb.astype(np.int32)
    for start in range(0, len(uniq), 4096):        # chunked: the full distance
        block = uniq[start:start + 4096]           # matrix would be hundreds of MB
        d = ((block[:, None, :] - pal[None, :, :]) ** 2).sum(axis=2)
        nearest[start:start + 4096] = d.argmin(axis=1)

    out = Image.fromarray(nearest[inverse].reshape(h, w), mode="P")
    flat = pal_rgb.flatten().tolist()
    out.putpalette(flat + [0] * (768 - len(flat)))
    return out


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
        seq.append(render(glint_x=x))

    pal_rgb = build_palette(seq[0], colors)
    seq = [map_to_palette(f, pal_rgb) for f in seq]
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
    write_png()
    write_gif()
