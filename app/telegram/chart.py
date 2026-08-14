from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.analysis.indicators import IndicatorSet
from app.models import CandleSeries, Direction, Signal, SupportResistanceZone, ZoneKind

WIDTH = 1000
HEIGHT = 650
PLOT_LEFT = 70
PLOT_RIGHT = 970
PLOT_TOP = 75
PLOT_BOTTOM = 500
VOLUME_TOP = 525
VOLUME_BOTTOM = 610


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
    """Render a bounded, dependency-light chart for a published signal."""
    start = max(0, len(candles) - lookback)
    count = len(candles) - start
    if count < 20:
        raise ValueError("at least 20 candles are required for chart rendering")

    visible_high = candles.high[start:]
    visible_low = candles.low[start:]
    plan_prices = np.asarray(
        [
            signal.trade.entry_zone_low,
            signal.trade.entry_zone_high,
            signal.trade.stop_loss,
            signal.trade.tp1,
            signal.trade.tp2,
        ],
        dtype=np.float64,
    )
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

    image = Image.new("RGB", (WIDTH, HEIGHT), "#0b1220")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.text((PLOT_LEFT, 20), f"{signal.symbol}  1H  {signal.direction.value}  {signal.strategy.replace('_', ' ')}", fill="#f8fafc", font=font)
    draw.text((PLOT_LEFT, 40), f"Regime: {signal.regime.value.replace('_', ' ')}    Confluence: {signal.score}/100", fill="#94a3b8", font=font)

    for step in range(6):
        grid_price = price_min + price_span * step / 5
        grid_y = y(grid_price)
        draw.line((PLOT_LEFT, grid_y, PLOT_RIGHT, grid_y), fill="#1e293b", width=1)
        draw.text((4, grid_y - 6), _price_text(grid_price), fill="#64748b", font=font)

    relevant_zones = sorted(
        (zone for zone in zones if zone.high >= price_min and zone.low <= price_max),
        key=lambda zone: zone.score,
        reverse=True,
    )[:5]
    for zone in relevant_zones:
        top, bottom = y(min(zone.high, price_max)), y(max(zone.low, price_min))
        color = "#173c35" if zone.kind is ZoneKind.SUPPORT else "#44252d" if zone.kind is ZoneKind.RESISTANCE else "#303047"
        draw.rectangle((PLOT_LEFT, top, PLOT_RIGHT, bottom), fill=color, outline="#475569")

    candle_step = (PLOT_RIGHT - PLOT_LEFT) / count
    body_half = max(2.0, candle_step * 0.3)
    max_volume = max(float(np.max(candles.volume[start:])), 1e-12)
    for index in range(start, len(candles)):
        center = x(index)
        bullish = candles.close[index] >= candles.open[index]
        color = "#22c55e" if bullish else "#ef4444"
        draw.line((center, y(float(candles.high[index])), center, y(float(candles.low[index]))), fill=color, width=1)
        top = y(float(max(candles.open[index], candles.close[index])))
        bottom = y(float(min(candles.open[index], candles.close[index])))
        if bottom - top < 1:
            bottom = top + 1
        draw.rectangle((center - body_half, top, center + body_half, bottom), fill=color)
        volume_height = float(candles.volume[index]) / max_volume * (VOLUME_BOTTOM - VOLUME_TOP)
        draw.rectangle((center - body_half, VOLUME_BOTTOM - volume_height, center + body_half, VOLUME_BOTTOM), fill="#166534" if bullish else "#7f1d1d")

    def draw_indicator(values: np.ndarray, color: str, width: int = 2) -> None:
        points = [(x(index), y(float(values[index]))) for index in range(start, len(values)) if np.isfinite(values[index])]
        if len(points) >= 2:
            draw.line(points, fill=color, width=width)

    draw_indicator(indicators.ema20, "#38bdf8")
    draw_indicator(indicators.ema50, "#f59e0b")

    entry_color = "#16a34a" if signal.direction is Direction.LONG else "#dc2626"
    entry_top = y(signal.trade.entry_zone_high)
    entry_bottom = y(signal.trade.entry_zone_low)
    draw.rectangle((PLOT_LEFT, entry_top, PLOT_RIGHT, entry_bottom), outline=entry_color, width=2)

    def price_line(price: float, label: str, color: str, width: int = 2) -> None:
        line_y = y(price)
        draw.line((PLOT_LEFT, line_y, PLOT_RIGHT, line_y), fill=color, width=width)
        text = f" {label} {_price_text(price)} "
        text_box = draw.textbbox((0, 0), text, font=font)
        text_width = text_box[2] - text_box[0]
        draw.rectangle((PLOT_RIGHT - text_width, line_y - 7, PLOT_RIGHT, line_y + 7), fill="#0b1220")
        draw.text((PLOT_RIGHT - text_width, line_y - 6), text, fill=color, font=font)

    price_line(signal.trade.preferred_entry, "ENTRY", entry_color, 2)
    price_line(signal.trade.stop_loss, "STOP", "#fb7185", 2)
    price_line(signal.trade.tp1, "TP1", "#a3e635")
    price_line(signal.trade.tp2, "TP2 2R", "#22d3ee")

    draw.text((PLOT_LEFT, 617), "EMA20", fill="#38bdf8", font=font)
    draw.text((PLOT_LEFT + 60, 617), "EMA50", fill="#f59e0b", font=font)
    draw.text((PLOT_LEFT + 125, 617), "Shaded areas: scored S/R zones", fill="#94a3b8", font=font)

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
