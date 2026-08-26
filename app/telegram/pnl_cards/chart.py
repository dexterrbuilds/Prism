from __future__ import annotations

import math
import random

from app.telegram.pnl_cards.content import content_index
from app.telegram.pnl_cards.models import PnlCardData


def normalized_chart(data: PnlCardData, points: int = 36) -> tuple[float, ...]:
    """Return bounded chart coordinates; synthesize a stable curve when absent."""
    if data.chart_data is not None:
        values = tuple(float(value) for value in data.chart_data)
    else:
        rng = random.Random(content_index(data, "chart", 2**32))
        direction = 1.0 if data.pnl_usd >= 0 else -1.0
        values = tuple(
            direction * index / (points - 1)
            + math.sin(index * 0.78 + rng.random()) * 0.11
            + rng.uniform(-0.055, 0.055)
            for index in range(points)
        )
    low = min(values)
    spread = max(values) - low
    if spread <= 1e-12:
        return tuple(0.5 for _ in values)
    return tuple((value - low) / spread for value in values)

