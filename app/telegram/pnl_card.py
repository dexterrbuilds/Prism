from __future__ import annotations

from datetime import UTC
from io import BytesIO

from PIL import Image, ImageDraw

from app.models import Direction, Signal, SignalState
from app.telegram.fonts import load_font

WIDTH = 1200
HEIGHT = 675


def _price(value: float) -> str:
    if value >= 1_000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:,.6f}"


def _duration(signal: Signal) -> str:
    if signal.activated_at is None or signal.state_changed_at is None:
        return "—"
    hours = max(0.0, (signal.state_changed_at - signal.activated_at).total_seconds() / 3600)
    return f"{max(1, round(hours * 60))}m" if hours < 1 else f"{hours:.1f}h"


def render_pnl_card(signal: Signal) -> bytes:
    if signal.state not in {SignalState.TP1_HIT, SignalState.TP2_HIT}:
        raise ValueError("PnL cards are only rendered for target-hit events")
    target_name = "TP2" if signal.state is SignalState.TP2_HIT else "TP1"
    target = signal.trade.tp2 if signal.state is SignalState.TP2_HIT else signal.trade.tp1
    current = signal.current_price if signal.current_price is not None else target
    achieved_r = abs(target - signal.trade.preferred_entry) / signal.trade.risk_per_unit
    move_pct = (target - signal.trade.preferred_entry) / signal.trade.preferred_entry * 100
    if signal.direction is Direction.SHORT:
        move_pct *= -1

    image = Image.new("RGB", (WIDTH, HEIGHT), "#07111f")
    draw = ImageDraw.Draw(image)
    for row in range(HEIGHT):
        ratio = row / HEIGHT
        draw.line((0, row, WIDTH, row), fill=(7, int(17 + ratio * 8), int(31 + ratio * 15)))
    draw.ellipse((850, -250, 1350, 250), fill="#0d3440")
    draw.ellipse((-250, 500, 350, 1100), fill="#0b2930")

    accent = "#22d3a6" if signal.state is SignalState.TP1_HIT else "#38bdf8"
    muted = "#8ba0b8"
    white = "#f8fafc"
    panel = "#0d1b2d"
    draw.rounded_rectangle((42, 34, WIDTH - 42, HEIGHT - 34), radius=28, fill="#091727", outline="#18324a", width=2)
    draw.rounded_rectangle((42, 34, 54, HEIGHT - 34), radius=6, fill=accent)

    draw.text((86, 68), "PRISM", fill=white, font=load_font(22, True))
    draw.text((190, 71), "SIGNAL PERFORMANCE", fill=muted, font=load_font(16, True))
    badge = f"{target_name}  HIT"
    badge_box = draw.textbbox((0, 0), badge, font=load_font(17, True))
    badge_width = badge_box[2] - badge_box[0] + 38
    draw.rounded_rectangle((WIDTH - 86 - badge_width, 61, WIDTH - 86, 103), radius=20, fill=accent)
    draw.text((WIDTH - 67 - badge_width, 71), badge, fill="#031611", font=load_font(17, True))

    draw.text((86, 138), f"{signal.symbol}  ·  {signal.direction.value}", fill=white, font=load_font(31, True))
    draw.text((86, 183), signal.strategy.replace("_", " ").title(), fill=muted, font=load_font(18))
    draw.text((86, 230), f"+{achieved_r:.2f}R", fill=accent, font=load_font(76, True))
    draw.text((355, 254), f"{move_pct:+.2f}%", fill=white, font=load_font(34, True))
    draw.text((358, 300), "PRICE MOVE FROM ENTRY", fill=muted, font=load_font(14, True))

    metrics = (
        ("ENTRY", _price(signal.trade.preferred_entry)),
        (target_name, _price(target)),
        ("CURRENT", _price(current)),
    )
    panel_left = 86
    panel_top = 366
    panel_width = 242
    for index, (label, value) in enumerate(metrics):
        left = panel_left + index * (panel_width + 18)
        draw.rounded_rectangle((left, panel_top, left + panel_width, panel_top + 105), radius=15, fill=panel, outline="#1b3850")
        draw.text((left + 20, panel_top + 17), label, fill=muted, font=load_font(14, True))
        draw.text((left + 20, panel_top + 50), value, fill=white, font=load_font(22, True))

    sim_left = 874
    draw.rounded_rectangle((sim_left, 138, WIDTH - 86, 471), radius=20, fill=panel, outline="#1b3850")
    draw.text((sim_left + 24, 163), "$5,000 MARGIN EXAMPLE", fill=muted, font=load_font(14, True))
    draw.text((sim_left + 24, 197), "Hypothetical P&L", fill=white, font=load_font(22, True))
    for index, leverage in enumerate((2, 5)):
        notional = 5_000 * leverage
        quantity = notional / signal.trade.preferred_entry
        pnl = abs(target - signal.trade.preferred_entry) * quantity
        top = 250 + index * 91
        draw.text((sim_left + 24, top), f"{leverage}×", fill=accent, font=load_font(25, True))
        draw.text((sim_left + 82, top + 2), f"${notional:,.0f} notional", fill=muted, font=load_font(15))
        draw.text((sim_left + 24, top + 36), f"+${pnl:,.0f}", fill=white, font=load_font(27, True))

    event_time = (signal.state_changed_at or signal.created_at).astimezone(UTC).strftime("%Y-%m-%d  %H:%M UTC")
    footer_y = 531
    draw.line((86, footer_y, WIDTH - 86, footer_y), fill="#1b3850", width=1)
    draw.text((86, footer_y + 25), f"CONFLUENCE  {signal.score}/100", fill=muted, font=load_font(15, True))
    draw.text((370, footer_y + 25), f"HOLD  {_duration(signal)}", fill=muted, font=load_font(15, True))
    draw.text((580, footer_y + 25), event_time, fill=muted, font=load_font(15))
    draw.text((904, footer_y + 19), "WIN RECORDED", fill=accent, font=load_font(18, True))
    draw.text((86, 605), "Technical research result · Fees, funding and slippage excluded", fill="#61778f", font=load_font(13))

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
