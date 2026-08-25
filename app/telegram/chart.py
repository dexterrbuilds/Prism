from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw

from app.analysis.indicators import IndicatorSet
from app.models import CandleSeries, Direction, Signal, SupportResistanceZone, ZoneKind
from app.telegram.fonts import load_font

WIDTH = 1100
HEIGHT = 700
PLOT_LEFT = 88
PLOT_RIGHT = 900
PLOT_TOP = 105
PLOT_BOTTOM = 515
VOLUME_TOP = 550
VOLUME_BOTTOM = 635


def _price_text(value: float) -> str:
    if value >= 1_000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def render_signal_chart(
    signal: Signal,
    candles: CandleSeries,
    indicators: IndicatorSet,
    zones: tuple[SupportResistanceZone, ...],
    lookback: int = 80,
) -> bytes:
    """Render one bounded, readable chart for the selected signal."""
    start = max(0, len(candles) - lookback)
    count = len(candles) - start
    if count < 20:
        raise ValueError("at least 20 candles are required for chart rendering")

    visible_high = candles.high[start:]
    visible_low = candles.low[start:]
    plan_values = [signal.trade.entry_zone_low, signal.trade.entry_zone_high, signal.trade.stop_loss, signal.trade.tp1, signal.trade.tp2]
    if signal.trade.tp3 is not None:
        plan_values.append(signal.trade.tp3)
    plan_prices = np.asarray(plan_values, dtype=np.float64)
    price_min = float(min(np.min(visible_low), np.min(plan_prices)))
    price_max = float(max(np.max(visible_high), np.max(plan_prices)))
    padding = max((price_max - price_min) * 0.08, candles.latest_close * 0.002)
    price_min -= padding
    price_max += padding
    price_span = max(price_max - price_min, 1e-12)

    def x(index: int) -> float:
        return PLOT_LEFT + (index - start + 0.5) * (PLOT_RIGHT - PLOT_LEFT) / count

    def y(price: float) -> float:
        return PLOT_BOTTOM - (price - price_min) / price_span * (PLOT_BOTTOM - PLOT_TOP)

    image = Image.new("RGBA", (WIDTH, HEIGHT), "#0b1220")
    draw = ImageDraw.Draw(image)
    title_font = load_font(21, bold=True)
    subtitle_font = load_font(13)
    axis_font = load_font(11)
    label_font = load_font(12, bold=True)

    direction_color = "#22c55e" if signal.direction is Direction.LONG else "#ef4444"
    draw.text((PLOT_LEFT, 22), f"{signal.symbol}  ·  1H  ·  {signal.direction.value}", fill=direction_color, font=title_font)
    draw.text((PLOT_LEFT, 54), signal.strategy.replace("_", " ").title(), fill="#f8fafc", font=subtitle_font)
    context = f"{signal.regime.value.replace('_', ' ').title()}  ·  Confluence {signal.score}/100  ·  {signal.grade.value}"
    draw.text((PLOT_LEFT, 74), context, fill="#94a3b8", font=subtitle_font)
    draw.text((760, 28), "EMA20", fill="#38bdf8", font=subtitle_font)
    draw.text((825, 28), "EMA50", fill="#f59e0b", font=subtitle_font)
    draw.text((890, 28), "Scored S/R", fill="#94a3b8", font=subtitle_font)

    for step in range(6):
        grid_price = price_min + price_span * step / 5
        grid_y = y(grid_price)
        draw.line((PLOT_LEFT, grid_y, PLOT_RIGHT, grid_y), fill="#1e293b", width=1)
        label = _price_text(grid_price)
        box = draw.textbbox((0, 0), label, font=axis_font)
        draw.text((PLOT_LEFT - (box[2] - box[0]) - 10, grid_y - 7), label, fill="#64748b", font=axis_font)

    relevant_zones = sorted(
        (zone for zone in zones if zone.high >= price_min and zone.low <= price_max),
        key=lambda zone: zone.score,
        reverse=True,
    )[:4]
    zone_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    zone_draw = ImageDraw.Draw(zone_overlay)
    for zone in relevant_zones:
        top, bottom = y(min(zone.high, price_max)), y(max(zone.low, price_min))
        fill = (34, 197, 94, 24) if zone.kind is ZoneKind.SUPPORT else (239, 68, 68, 24) if zone.kind is ZoneKind.RESISTANCE else (148, 163, 184, 20)
        outline = "#166534" if zone.kind is ZoneKind.SUPPORT else "#991b1b" if zone.kind is ZoneKind.RESISTANCE else "#475569"
        zone_draw.rectangle((PLOT_LEFT, top, PLOT_RIGHT, bottom), fill=fill, outline=outline, width=1)
    image = Image.alpha_composite(image, zone_overlay)
    draw = ImageDraw.Draw(image)

    candle_step = (PLOT_RIGHT - PLOT_LEFT) / count
    body_half = max(2.0, candle_step * 0.29)
    max_volume = max(float(np.max(candles.volume[start:])), 1e-12)
    for index in range(start, len(candles)):
        center = x(index)
        bullish = candles.close[index] >= candles.open[index]
        color = "#22c55e" if bullish else "#ef4444"
        draw.line((center, y(float(candles.high[index])), center, y(float(candles.low[index]))), fill=color, width=2)
        top = y(float(max(candles.open[index], candles.close[index])))
        bottom = max(top + 1, y(float(min(candles.open[index], candles.close[index]))))
        draw.rectangle((center - body_half, top, center + body_half, bottom), fill=color)
        volume_height = float(candles.volume[index]) / max_volume * (VOLUME_BOTTOM - VOLUME_TOP)
        draw.rectangle(
            (center - body_half, VOLUME_BOTTOM - volume_height, center + body_half, VOLUME_BOTTOM),
            fill="#15803d" if bullish else "#b91c1c",
        )

    def draw_indicator(values: np.ndarray, color: str) -> None:
        points = [(x(index), y(float(values[index]))) for index in range(start, len(values)) if np.isfinite(values[index])]
        if len(points) >= 2:
            draw.line(points, fill=color, width=2)

    draw_indicator(indicators.ema20, "#38bdf8")
    draw_indicator(indicators.ema50, "#f59e0b")

    entry_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    entry_draw = ImageDraw.Draw(entry_overlay)
    entry_fill = (34, 197, 94, 38) if signal.direction is Direction.LONG else (239, 68, 68, 38)
    entry_draw.rectangle(
        (PLOT_LEFT, y(signal.trade.entry_zone_high), PLOT_RIGHT, y(signal.trade.entry_zone_low)),
        fill=entry_fill,
        outline=direction_color,
        width=2,
    )
    image = Image.alpha_composite(image, entry_overlay)
    draw = ImageDraw.Draw(image)

    price_levels = [
        (signal.trade.preferred_entry, "ENTRY", direction_color, 2),
        (signal.trade.stop_loss, "STOP", "#fb7185", 2),
        (signal.trade.tp1, "TP1", "#a3e635", 2),
        (signal.trade.tp2, "TP2 · 2R", "#22d3ee", 2),
    ]
    if signal.trade.tp3 is not None:
        price_levels.append((signal.trade.tp3, "TP3", "#c084fc", 2))

    # Price lines remain at their exact values; labels are spaced independently
    # in the gutter and connected back to the line to avoid cramped overlaps.
    for price, _, color, width in price_levels:
        draw.line((PLOT_LEFT, y(price), PLOT_RIGHT, y(price)), fill=color, width=width)
    sorted_levels = sorted(price_levels, key=lambda item: y(item[0]))
    label_centers: list[float] = []
    minimum_gap = 29.0
    for price, _, _, _ in sorted_levels:
        desired = y(price)
        label_centers.append(max(desired, label_centers[-1] + minimum_gap) if label_centers else desired)
    lower_bound = PLOT_TOP + 14
    upper_bound = PLOT_BOTTOM - 14
    if label_centers and label_centers[-1] > upper_bound:
        overflow = label_centers[-1] - upper_bound
        label_centers = [center - overflow for center in label_centers]
    if label_centers and label_centers[0] < lower_bound:
        underflow = lower_bound - label_centers[0]
        label_centers = [center + underflow for center in label_centers]

    for (price, label, color, _), label_y in zip(sorted_levels, label_centers, strict=True):
        line_y = y(price)
        text = f"{label}  {_price_text(price)}"
        text_box = draw.textbbox((0, 0), text, font=label_font)
        text_height = text_box[3] - text_box[1]
        draw.line((PLOT_RIGHT, line_y, PLOT_RIGHT + 10, label_y), fill=color, width=1)
        draw.rounded_rectangle(
            (PLOT_RIGHT + 10, label_y - 13, WIDTH - 12, label_y + 13),
            radius=5,
            fill="#111827",
            outline=color,
        )
        draw.text((PLOT_RIGHT + 17, label_y - text_height / 2 - 2), text, fill=color, font=label_font)

    draw.line((PLOT_LEFT, VOLUME_TOP - 10, PLOT_RIGHT, VOLUME_TOP - 10), fill="#334155", width=1)
    draw.text((PLOT_LEFT, VOLUME_TOP - 4), "VOLUME", fill="#64748b", font=axis_font)
    time_indexes = sorted({start, start + count // 4, start + count // 2, start + 3 * count // 4, len(candles) - 1})
    for index in time_indexes:
        label = datetime.fromtimestamp(int(candles.timestamp[index]) / 1000, UTC).strftime("%m-%d %H:%M")
        label_x = min(max(PLOT_LEFT, x(index) - 35), PLOT_RIGHT - 75)
        draw.text((label_x, 654), label, fill="#64748b", font=axis_font)

    output = BytesIO()
    image.convert("RGB").save(output, format="PNG")
    return output.getvalue()
