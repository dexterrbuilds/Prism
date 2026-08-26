from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.models import Direction
from app.telegram.pnl_cards import MascotState, PnlCardData, generate_pnl_card


def main() -> None:
    output = Path("output/pnl-card-demo")
    output.mkdir(parents=True, exist_ok=True)
    base = PnlCardData("SOL/USDT", Direction.LONG, 5_678.90, 32.14, 142.65, 151.21, 151.21, 5, trade_duration="Held 6.4h")
    cards = {
        "huge-long-win.png": replace(base, pnl_usd=18_420.75, pnl_percent=63.80),
        "normal-long-win.png": replace(base, pnl_usd=842.60, pnl_percent=8.42),
        "short-win.png": replace(base, pair="ETH/USDT", direction=Direction.SHORT, pnl_usd=2_306.40, pnl_percent=23.06),
        "small-loss.png": replace(base, pair="BTC/USDT", pnl_usd=-312.45, pnl_percent=-3.12),
        "large-loss.png": replace(base, pair="XRP/USDT", direction=Direction.SHORT, pnl_usd=-1_875.00, pnl_percent=-18.75),
        "very-large-pnl.png": replace(base, pair="1000PEPE/USDT", pnl_usd=1_234_567.89, pnl_percent=51.25, mascot_state=MascotState.STREAK),
    }
    for filename, data in cards.items():
        (output / filename).write_bytes(generate_pnl_card(data))
        print(output / filename)


if __name__ == "__main__":
    main()
