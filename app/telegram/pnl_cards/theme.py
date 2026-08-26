from __future__ import annotations

from dataclasses import dataclass

from app.models import Direction


@dataclass(frozen=True, slots=True)
class PnlCardTheme:
    background_left: tuple[int, int, int] = (3, 12, 48)
    background_right: tuple[int, int, int] = (30, 8, 90)
    white: str = "#f8fbff"
    muted: str = "#97a9d6"
    subtle: str = "#6876a8"
    cyan: str = "#19e3c0"
    long: str = "#19e3c0"
    short: str = "#ff6f7d"
    loss: str = "#ff6f7d"
    violet: str = "#8c55ff"
    blue: str = "#2d8cff"
    panel: tuple[int, int, int, int] = (11, 14, 62, 210)
    panel_border: tuple[int, int, int, int] = (91, 91, 246, 185)

    def direction_color(self, direction: Direction) -> str:
        return self.long if direction is Direction.LONG else self.short

