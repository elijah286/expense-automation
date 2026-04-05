#!/usr/bin/env python3
"""
Generate Finder window background for create-dmg (Firefox-style night scene + arrow).

Dimensions must match --window-size in packaging/build_macos.sh.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Must match --window-size in packaging/build_macos.sh
W, H = 900, 520

# Icon centers must match create-dmg --icon and --app-drop-link (y ≈ vertical center of icons)
ICON_APP_X = 200
ICON_APP_Y = 238
# Applications drop link in build_macos.sh: --app-drop-link 700 238


def _try_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _sky_gradient() -> Image.Image:
    """Deep indigo / purple night sky with a soft horizon glow (Firefox DMG vibe)."""
    img = Image.new("RGB", (W, H))
    px = img.load()
    horizon = int(H * 0.52)
    for y in range(H):
        t = y / max(H - 1, 1)
        # Top: very dark blue-violet
        r0, g0, b0 = 14, 6, 38
        # Mid: rich purple
        r1, g1, b1 = 32, 14, 72
        # Near horizon: slightly warmer purple (moonlit haze)
        r2, g2, b2 = 48, 22, 88
        if y < horizon:
            u = y / max(horizon, 1)
            r = int(r0 + (r1 - r0) * (u**0.85))
            g = int(g0 + (g1 - g0) * (u**0.85))
            b = int(b0 + (b1 - b0) * (u**0.85))
        else:
            u = (y - horizon) / max(H - horizon, 1)
            r = int(r1 + (r2 - r1) * (u**0.7))
            g = int(g1 + (g2 - g1) * (u**0.7))
            b = int(b1 + (b2 - b1) * (u**0.7))
        for x in range(W):
            px[x, y] = (min(r, 255), min(g, 255), min(b, 255))
    return img


def _hill_polygon(y0: float, amp: float, freq: float, phase: float) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    step = 6
    for x in range(0, W + step, step):
        wave = amp * math.sin(x * freq + phase) + 0.35 * amp * math.sin(x * freq * 2.3 + phase * 1.7)
        y = y0 + wave
        pts.append((float(x), y))
    pts.append((float(W), float(H)))
    pts.append((0.0, float(H)))
    return pts


def _add_hills(base: Image.Image) -> None:
    draw = ImageDraw.Draw(base)
    # Back ridge — cooler, darker
    draw.polygon(
        _hill_polygon(300, 22, 0.009, 0.4),
        fill=(38, 18, 62),
    )
    draw.polygon(
        _hill_polygon(318, 18, 0.011, 1.1),
        fill=(52, 24, 82),
    )
    # Mid rolling hills
    draw.polygon(
        _hill_polygon(340, 28, 0.008, 2.0),
        fill=(58, 28, 92),
    )
    # Front — warmest purple (ground)
    draw.polygon(
        _hill_polygon(372, 32, 0.007, 0.2),
        fill=(72, 36, 108),
    )


def _add_flat_clouds(overlay: Image.Image) -> None:
    """Flat, dark purple ‘paper’ clouds (Firefox installer style)."""
    draw = ImageDraw.Draw(overlay)
    blobs = [
        (95, 62, 100, 28),
        (240, 48, 120, 34),
        (430, 72, 95, 26),
        (590, 52, 140, 38),
        (740, 68, 115, 30),
    ]
    for cx, cy, rw, rh in blobs:
        # Layered ellipses for a chunky flat look
        rgba = (28, 12, 52, 210)
        draw.ellipse([cx - rw, cy - rh, cx + rw, cy + rh], fill=rgba)
        draw.ellipse([cx - rw * 0.55, cy - rh * 0.9, cx + rw * 0.45, cy + rh * 0.35], fill=(40, 20, 68, 160))
    # Soft upper atmosphere haze
    for i in range(3):
        a = 25 - i * 6
        draw.ellipse([-80 + i * 40, -30, W + 80, 120 + i * 15], fill=(60, 40, 100, max(0, a)))


def _subtle_stars(overlay: Image.Image) -> None:
    rnd = random.Random(42)
    draw = ImageDraw.Draw(overlay)
    for _ in range(55):
        x = rnd.randint(20, W - 20)
        y = rnd.randint(10, 210)
        s = rnd.choice([1, 1, 2])
        a = rnd.randint(35, 90)
        draw.ellipse([x, y, x + s, y + s], fill=(255, 255, 255, a))


def _glow_behind_app() -> Image.Image:
    """Warm orange–pink radial glow behind the app icon (Firefox promo art)."""
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(g)
    cx, cy = ICON_APP_X, ICON_APP_Y
    # Stronger core + wider falloff
    for i in range(120, 0, -1):
        t = i / 120.0
        a = int(8 * (t**1.8))
        r = 28 + (120 - i) * 2.1
        # Peach → soft amber
        col = (255, int(140 + 70 * t), int(90 + 80 * t), a)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    return g.filter(ImageFilter.GaussianBlur(radius=14))


def _arrow(draw: ImageDraw.ImageDraw, x0: int, y: int, x1: int) -> None:
    """Simple white arrow with shadow (matches classic macOS DMG art)."""
    for dx, dy in ((2, 3), (0, 0)):
        col = (25, 12, 45) if dx else (255, 255, 255)
        w = 4 if dx else 3
        draw.line([(x0 + dx, y + dy), (x1 - 24 + dx, y + dy)], fill=col, width=w + (1 if dx else 2))
        head = [
            (x1 + dx, y + dy),
            (x1 - 20 + dx, y - 12 + dy),
            (x1 - 20 + dx, y + 12 + dy),
        ]
        draw.polygon(head, fill=col)


def main() -> None:
    out = Path(__file__).resolve().parent / "dmg_background.png"

    base = _sky_gradient()
    _add_hills(base)

    clouds = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _add_flat_clouds(clouds)
    stars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _subtle_stars(stars)

    comp = base.convert("RGBA")
    comp = Image.alpha_composite(comp, clouds)
    comp = Image.alpha_composite(comp, stars)

    glow = _glow_behind_app()
    comp = Image.alpha_composite(comp, glow)

    base_rgb = comp.convert("RGB")
    draw = ImageDraw.Draw(base_rgb)

    # Arrow between icon slots (left app → right Applications)
    _arrow(draw, 288, ICON_APP_Y, 612)

    font = _try_font(17)
    font_sm = _try_font(13)
    msg = "Drag Expense Automator to Applications to install"
    sub = "Then eject this disk image."

    if hasattr(draw, "textbbox"):
        b1 = draw.textbbox((0, 0), msg, font=font)
        tw = b1[2] - b1[0]
        b2 = draw.textbbox((0, 0), sub, font=font_sm)
        tw2 = b2[2] - b2[0]
    else:
        tw, _ = draw.textsize(msg, font=font)
        tw2, _ = draw.textsize(sub, font=font_sm)

    y_text = H - 68
    tx = (W - tw) // 2
    for ox, oy in ((1, 1), (2, 2)):
        draw.text((tx + ox, y_text + oy), msg, font=font, fill=(12, 6, 28))
    draw.text((tx, y_text), msg, font=font, fill=(248, 244, 255))

    tx2 = (W - tw2) // 2
    draw.text((tx2 + 1, y_text + 24 + 1), sub, font=font_sm, fill=(12, 6, 28))
    draw.text((tx2, y_text + 24), sub, font=font_sm, fill=(198, 188, 228))

    base_rgb.save(out, "PNG", optimize=True)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
