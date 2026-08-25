from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

_LINUX_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_MAC_FONT_DIR = Path("/System/Library/Fonts/Supplemental")


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        ("DejaVuSans-Bold.ttf", _LINUX_FONT_DIR / "DejaVuSans-Bold.ttf", _MAC_FONT_DIR / "Arial Bold.ttf")
        if bold
        else ("DejaVuSans.ttf", _LINUX_FONT_DIR / "DejaVuSans.ttf", _MAC_FONT_DIR / "Arial.ttf")
    )
    for candidate in names:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)
