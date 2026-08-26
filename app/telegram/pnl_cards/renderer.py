from __future__ import annotations

import asyncio
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from app.models import Direction
from app.telegram.fonts import load_font
from app.telegram.pnl_cards.chart import normalized_chart
from app.telegram.pnl_cards.content import select_context_message, select_mascot, select_quote
from app.telegram.pnl_cards.formatters import format_leverage, format_price, format_signed_percent, format_signed_usd
from app.telegram.pnl_cards.models import MascotState, MascotThresholds, PnlCardData
from app.telegram.pnl_cards.theme import PnlCardTheme

WIDTH = 1200
HEIGHT = 1200
_ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets" / "prism" / "pnl"
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


def _fit_font(draw: ImageDraw.ImageDraw, text: str, maximum_width: int, start: int, minimum: int, *, bold: bool = True) -> Font:
    size = start
    while size > minimum:
        font = load_font(size, bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= maximum_width:
            return font
        size -= 2
    return load_font(minimum, bold)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Font, maximum_width: int, maximum_lines: int = 2) -> tuple[str, ...]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= maximum_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    if len(lines) <= maximum_lines:
        return tuple(lines)
    clipped = list(lines[:maximum_lines])
    while clipped[-1] and draw.textlength(f"{clipped[-1]}…", font=font) > maximum_width:
        clipped[-1] = clipped[-1][:-1]
    clipped[-1] = f"{clipped[-1].rstrip()}…"
    return tuple(clipped)


def _background(theme: PnlCardTheme) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), theme.background_left + (255,))
    draw = ImageDraw.Draw(image)
    for x in range(WIDTH):
        ratio = x / (WIDTH - 1)
        color = tuple(round(left * (1 - ratio) + right * ratio) for left, right in zip(theme.background_left, theme.background_right, strict=True))
        draw.line((x, 0, x, HEIGHT), fill=color + (255,))
    glows = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glows)
    glow_draw.ellipse((710, -180, 1370, 570), fill=(125, 40, 255, 120))
    glow_draw.ellipse((-260, 540, 470, 1330), fill=(0, 132, 255, 86))
    glow_draw.ellipse((340, 250, 970, 900), fill=(62, 30, 230, 38))
    return Image.alpha_composite(image, glows.filter(ImageFilter.GaussianBlur(105)))


def _draw_prism_logo(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float = 1.0) -> None:
    width = int(58 * scale)
    height = int(66 * scale)
    apex = (x + width // 2, y)
    left = (x, y + height)
    right = (x + width, y + height)
    center = (x + width // 2, y + int(height * 0.55))
    draw.polygon((apex, left, center), fill="#42e7ff")
    draw.polygon((apex, center, right), fill="#8f58ff")
    draw.polygon((left, center, right), fill="#2779ff")


def _draw_telegram(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    draw.ellipse((x, y, x + 38, y + 38), fill="#2788f8")
    draw.polygon(((x + 8, y + 19), (x + 30, y + 9), (x + 23, y + 30), (x + 18, y + 23), (x + 14, y + 28)), fill="#ffffff")


def _draw_glowing_text(image: Image.Image, position: tuple[int, int], text: str, font: Font, color: str, glow: tuple[int, int, int]) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.text(position, text, font=font, fill=glow + (145,))
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(16)))
    ImageDraw.Draw(image).text(position, text, font=font, fill=color)


@lru_cache(maxsize=2)
def _mascot_asset(state: MascotState, size: int) -> Image.Image:
    path = _ASSET_ROOT / f"mascot-{state.value}.png"
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (size, size), "#070827")
        canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        return canvas


def _composite_mascot(image: Image.Image, state: MascotState) -> None:
    size = 590
    mascot = _mascot_asset(state, size)
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((28, 12, size - 10, size - 8), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(34))
    image.paste(mascot, (590, 205), mask)


def _draw_chart(draw: ImageDraw.ImageDraw, data: PnlCardData, color: str) -> None:
    left, top, right, bottom = 54, 665, 655, 825
    for index in range(5):
        y = top + index * (bottom - top) / 4
        draw.line((left, y, right, y), fill=(83, 91, 173, 42), width=1)
    values = normalized_chart(data)
    points = [
        (
            left + index * (right - left) / (len(values) - 1),
            bottom - 15 - value * (bottom - top - 38),
        )
        for index, value in enumerate(values)
    ]
    polygon = [(left, bottom), *points, (right, bottom)]
    rgb = ImageColor.getrgb(color)
    draw.polygon(polygon, fill=rgb + (30,))
    draw.line(points, fill=rgb + (115,), width=3, joint="curve")
    x, y = points[-1]
    draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill="#ffffff", outline=color, width=3)


def _draw_direction_arrow(draw: ImageDraw.ImageDraw, x: int, y: int, direction: Direction, color: str) -> None:
    if direction is Direction.LONG:
        draw.line((x, y + 21, x + 25, y - 4), fill=color, width=6)
        draw.line((x + 12, y - 4, x + 25, y - 4, x + 25, y + 9), fill=color, width=6, joint="curve")
    else:
        draw.line((x, y - 4, x + 25, y + 21), fill=color, width=6)
        draw.line((x + 12, y + 21, x + 25, y + 21, x + 25, y + 8), fill=color, width=6, joint="curve")


def _draw_optional_metadata(draw: ImageDraw.ImageDraw, data: PnlCardData, theme: PnlCardTheme) -> None:
    metrics: list[tuple[str, str]] = []
    if data.entry_price is not None:
        metrics.append(("ENTRY", format_price(data.entry_price)))
    if data.exit_price is not None:
        metrics.append(("EXIT", format_price(data.exit_price)))
    elif data.mark_price is not None:
        metrics.append(("MARK", format_price(data.mark_price)))
    if data.leverage is not None:
        metrics.append(("LEVERAGE", format_leverage(data.leverage)))
    if not metrics:
        return
    width = min(170, 540 // len(metrics))
    for index, (label, value) in enumerate(metrics[:3]):
        left = 78 + index * (width + 12)
        draw.rounded_rectangle((left, 752, left + width, 820), radius=15, fill=(11, 20, 70, 150), outline=(70, 84, 170, 120), width=1)
        draw.text((left + 14, 763), label, fill=theme.subtle, font=load_font(13, True))
        value_font = _fit_font(draw, value, width - 28, 18, 13)
        draw.text((left + 14, 786), value, fill=theme.white, font=value_font)


def generate_pnl_card(
    data: PnlCardData,
    *,
    thresholds: MascotThresholds | None = None,
    theme: PnlCardTheme | None = None,
) -> bytes:
    """Render a share-ready deterministic 1200×1200 PRISM PnL card."""
    thresholds = thresholds or MascotThresholds()
    theme = theme or PnlCardTheme()
    image = _background(theme)
    draw = ImageDraw.Draw(image, "RGBA")

    draw.rounded_rectangle((28, 28, WIDTH - 28, HEIGHT - 28), radius=42, fill=(2, 8, 35, 38), outline=(89, 78, 255, 225), width=3)
    draw.rounded_rectangle((32, 32, WIDTH - 32, HEIGHT - 32), radius=39, outline=(28, 199, 255, 125), width=1)

    _draw_prism_logo(draw, 82, 70, 0.9)
    draw.text((154, 74), "PRISM", fill=theme.white, font=load_font(50, True))
    _draw_telegram(draw, 86, 142)
    username_font = _fit_font(draw, data.username, 365, 28, 18, bold=False)
    draw.text((139, 145), data.username, fill="#dce7ff", font=username_font)

    context = select_context_message(data)
    context_font = _fit_font(draw, context, 330, 32, 20)
    context_box = draw.textbbox((0, 0), context, font=context_font)
    draw.text((1100 - (context_box[2] - context_box[0]), 82), context, fill=theme.white, font=context_font)
    draw.line((930, 127, 1098, 127), fill=(145, 95, 255, 170), width=2)

    draw.text((84, 270), "TOTAL PNL", fill=theme.muted, font=load_font(28, True), stroke_width=0)
    pnl_text = format_signed_usd(data.pnl_usd)
    pnl_font = _fit_font(draw, pnl_text, 585, 88, 48)
    pnl_color = theme.white if data.pnl_usd >= 0 else theme.loss
    glow_color = (19, 209, 188) if data.pnl_usd >= 0 else (255, 74, 104)
    _draw_glowing_text(image, (78, 318), pnl_text, pnl_font, pnl_color, glow_color)
    draw = ImageDraw.Draw(image, "RGBA")

    percent_text = format_signed_percent(data.pnl_percent)
    percent_color = theme.cyan if data.pnl_usd >= 0 else theme.loss
    percent_font = _fit_font(draw, percent_text, 245, 38, 24)
    percent_box = draw.textbbox((0, 0), percent_text, font=percent_font)
    badge_width = percent_box[2] - percent_box[0] + 50
    badge_fill = (4, 111, 111, 150) if data.pnl_usd >= 0 else (113, 30, 58, 165)
    badge_outline = (23, 229, 194, 185) if data.pnl_usd >= 0 else (255, 96, 119, 185)
    draw.rounded_rectangle((80, 446, 80 + badge_width, 510), radius=29, fill=badge_fill, outline=badge_outline, width=2)
    draw.text((105, 456), percent_text, fill=percent_color, font=percent_font)

    chart_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    _draw_chart(ImageDraw.Draw(chart_layer, "RGBA"), data, theme.violet if data.pnl_usd >= 0 else theme.loss)
    image.alpha_composite(chart_layer)
    draw = ImageDraw.Draw(image, "RGBA")

    direction_color = theme.direction_color(data.direction)
    draw.text((82, 603), "DIRECTION", fill=theme.muted, font=load_font(18, True))
    _draw_direction_arrow(draw, 86, 656, data.direction, direction_color)
    draw.text((126, 640), data.direction.value, fill=direction_color, font=load_font(35, True))
    draw.line((320, 605, 320, 700), fill=(91, 103, 201, 150), width=2)
    draw.text((362, 603), "PAIR", fill=theme.muted, font=load_font(18, True))
    pair_font = _fit_font(draw, data.pair, 265, 31, 19)
    draw.text((362, 644), data.pair, fill=theme.white, font=pair_font)
    _draw_optional_metadata(draw, data, theme)

    _composite_mascot(image, select_mascot(data, thresholds))
    draw = ImageDraw.Draw(image, "RGBA")

    draw.rounded_rectangle((70, 850, 1130, 1065), radius=34, fill=theme.panel, outline=theme.panel_border, width=2)
    draw.text((107, 885), "“", fill="#7069ff", font=load_font(72, True))
    quote_font = load_font(31)
    quote_lines = _wrap_text(draw, select_quote(data), quote_font, 700, 2)
    for index, line in enumerate(quote_lines):
        draw.text((184, 905 + index * 48), line, fill=theme.white, font=quote_font)
    bars = (22, 48, 35, 78, 66, 112)
    for index, height in enumerate(bars):
        left = 870 + index * 36
        draw.rounded_rectangle((left, 1010 - height, left + 24, 1010), radius=5, fill=(104, 47 + index * 5, 245, 190))

    _draw_prism_logo(draw, 274, 1100, 0.48)
    footer_font = load_font(22, True)
    draw.text((316, 1103), "PRISM", fill=theme.muted, font=footer_font)
    draw.text((404, 1103), "•  Trade Smarter, Grow Stronger.", fill=theme.subtle, font=load_font(22))
    if data.calculation_label:
        label = data.calculation_label[:32]
        label_font = _fit_font(draw, label, 255, 14, 11)
        label_width = draw.textbbox((0, 0), label, font=label_font)[2]
        draw.text((1106 - label_width, 1094), label, fill=theme.subtle, font=label_font)
    if data.trade_duration:
        duration = data.trade_duration[:28]
        duration_font = _fit_font(draw, duration, 245, 15, 12)
        duration_width = draw.textbbox((0, 0), duration, font=duration_font)[2]
        draw.text((1106 - duration_width, 1120), duration, fill=theme.subtle, font=duration_font)

    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True, compress_level=7)
    return output.getvalue()


async def generate_pnl_card_async(
    data: PnlCardData,
    *,
    thresholds: MascotThresholds | None = None,
    theme: PnlCardTheme | None = None,
) -> bytes:
    return await asyncio.to_thread(generate_pnl_card, data, thresholds=thresholds, theme=theme)
