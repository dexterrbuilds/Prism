from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

_LINUX_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_MAC_FONT_DIR = Path("/System/Library/Fonts/Supplemental")
_INTER_DIRS = (
    Path("/usr/share/fonts/opentype/inter"),
    Path("/usr/share/fonts/truetype/inter"),
    Path("/usr/share/fonts/truetype/inter-vf"),
)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    inter_name = "Inter-Bold.otf" if bold else "Inter-Regular.otf"
    truetype_name = "Inter-Bold.ttf" if bold else "Inter-Regular.ttf"
    fallback_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    mac_name = "Arial Bold.ttf" if bold else "Arial.ttf"
    names = (
        *(_directory / inter_name for _directory in _INTER_DIRS),
        *(_directory / truetype_name for _directory in _INTER_DIRS),
        "Inter.var.ttf",
        _INTER_DIRS[-1] / "Inter.var.ttf",
        fallback_name,
        _LINUX_FONT_DIR / fallback_name,
        _MAC_FONT_DIR / mac_name,
    )
    for candidate in names:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)
