#!/usr/bin/env python3
"""Generate Finder window background for create-dmg (arrow + install hint)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Must match --window-size in packaging/build_macos.sh
W, H = 660, 420
TEAL = (15, 118, 110)
TEXT_GRAY = (80, 80, 85)


def _try_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_arrow(draw: ImageDraw.ImageDraw, x0: int, y: int, x1: int, color: tuple[int, int, int]) -> None:
    """Thick arrow pointing right."""
    w = 7
    draw.line([(x0, y), (x1 - 22, y)], fill=color, width=w)
    # Head
    head = [(x1, y), (x1 - 24, y - 16), (x1 - 24, y + 16)]
    draw.polygon(head, fill=color)


def main() -> None:
    out = Path(__file__).resolve().parent / "dmg_background.png"
    img = Image.new("RGB", (W, H), (245, 245, 247))
    draw = ImageDraw.Draw(img)

    # Arrow between icon zones (align with create-dmg --icon / --app-drop-link)
    _draw_arrow(draw, 215, 200, 455, TEAL)

    font = _try_font(17)
    msg = "Drag Expense Automator to Applications to install"
    if hasattr(draw, "textbbox"):
        tw = draw.textbbox((0, 0), msg, font=font)[2]
    else:
        tw, _ = draw.textsize(msg, font=font)
    tx = (W - tw) // 2
    draw.text((tx, 318), msg, fill=TEXT_GRAY, font=font)

    img.save(out, "PNG", optimize=True)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
