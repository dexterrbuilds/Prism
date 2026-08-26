from __future__ import annotations


def format_signed_usd(value: float) -> str:
    sign = "+" if value >= 0 else "−"
    return f"{sign}${abs(value):,.2f}"


def format_signed_percent(value: float) -> str:
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):,.2f}%"


def format_price(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000:
        return f"${value:,.2f}"
    if absolute >= 1:
        return f"${value:,.4f}"
    return f"${value:,.6f}"


def format_leverage(value: float) -> str:
    return f"{value:g}×"

