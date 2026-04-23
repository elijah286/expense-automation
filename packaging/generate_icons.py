#!/usr/bin/env python3
"""Generate ExpenseAutomator.icns (macOS) and ExpenseAutomator.ico (Windows) for desktop builds."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1024


def _draw_master() -> Image.Image:
    """Rounded tile + dollar mark — readable at 16px."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 64
    box = (margin, margin, SIZE - margin, SIZE - margin)
    r = 180
    draw.rounded_rectangle(box, radius=r, fill=(15, 118, 110, 255), outline=(6, 95, 90, 255), width=8)

    # Subtle inner highlight (top edge)
    hi = (margin + 24, margin + 24, SIZE - margin - 24, margin + 120)
    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(hi, radius=r - 40, fill=(255, 255, 255, 28))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # Dollar — prefer a bold system font
    font = None
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ):
        try:
            font = ImageFont.truetype(path, 520)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    ch = "$"
    if hasattr(draw, "textbbox"):
        x0, y0, x1, y1 = draw.textbbox((0, 0), ch, font=font)
        tw, th = x1 - x0, y1 - y0
    else:
        tw, th = draw.textsize(ch, font=font)
    tx = (SIZE - tw) // 2
    ty = (SIZE - th) // 2 - 36
    draw.text((tx, ty), ch, font=font, fill=(255, 255, 255, 255))
    return img


def _write_ico(png: Image.Image, dest: Path) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    images = [png.resize(s, Image.Resampling.LANCZOS) for s in sizes]
    dest.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        dest,
        format="ICO",
        sizes=[(im.width, im.height) for im in images],
        append_images=images[1:],
    )


def _write_icns(png: Image.Image, dest: Path) -> None:
    if sys.platform != "darwin":
        print("Skipping .icns (macOS only); build .icns on a Mac or use existing file.", file=sys.stderr)
        return

    work = dest.parent / ".iconset_build"
    if work.exists():
        shutil.rmtree(work)
    iconset = work / "ExpenseAutomator.iconset"
    iconset.mkdir(parents=True)

    mapping = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]
    for name, dim in mapping:
        im = png.resize((dim, dim), Image.Resampling.LANCZOS)
        im.save(iconset / name)

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(dest)],
        check=True,
    )
    shutil.rmtree(work)
    print(f"Wrote {dest}")


def main() -> None:
    root = Path(__file__).resolve().parent
    out_dir = root / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    master = _draw_master()
    png_path = out_dir / "ExpenseAutomator_source.png"
    master.save(png_path)
    print(f"Wrote {png_path}")

    ico_path = out_dir / "ExpenseAutomator.ico"
    _write_ico(master, ico_path)
    print(f"Wrote {ico_path}")

    icns_path = out_dir / "ExpenseAutomator.icns"
    _write_icns(master, icns_path)


if __name__ == "__main__":
    main()
