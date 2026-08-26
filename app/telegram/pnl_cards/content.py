from __future__ import annotations

from hashlib import sha256

from app.telegram.pnl_cards.models import MascotState, MascotThresholds, PnlCardData

QUOTES = (
    "Discipline today, freedom tomorrow.",
    "Good trades start with patience.",
    "Trust the setup, not the noise.",
    "Consistency beats excitement.",
    "Risk managed. Opportunity captured.",
    "Stay patient. Stay focused.",
    "Execution over emotion.",
    "Follow the plan.",
    "One trade at a time.",
    "Protect downside. Let upside work.",
    "Clean entries. Cleaner exits.",
    "The edge is in the discipline.",
    "Process first. Profit follows.",
    "Trade the setup, not the feeling.",
    "Patience paid.",
    "Another clean execution.",
)

CONTEXT_MESSAGES = (
    "Well done!",
    "Nice trade!",
    "Clean execution.",
    "Locked in.",
    "Another one.",
    "Trade secured.",
    "Profit captured.",
    "Onto the next.",
    "Good read.",
    "Let it compound.",
)

LOSS_MESSAGES = (
    "Risk respected.",
    "Plan protected.",
    "Reset. Refocus.",
    "Onto the next.",
)


def content_index(data: PnlCardData, namespace: str, size: int) -> int:
    seed = data.content_seed or f"{data.pair}|{data.direction.value}|{data.pnl_usd:.8f}|{data.pnl_percent:.8f}"
    digest = sha256(f"{namespace}|{seed}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % size


def select_quote(data: PnlCardData) -> str:
    return data.quote.strip() if data.quote and data.quote.strip() else QUOTES[content_index(data, "quote", len(QUOTES))]


def select_context_message(data: PnlCardData) -> str:
    if data.context_message and data.context_message.strip():
        return data.context_message.strip()
    messages = CONTEXT_MESSAGES if data.pnl_usd >= 0 else LOSS_MESSAGES
    return messages[content_index(data, "context", len(messages))]


def select_mascot(data: PnlCardData, thresholds: MascotThresholds | None = None) -> MascotState:
    thresholds = thresholds or MascotThresholds()
    if data.mascot_state is not None:
        return data.mascot_state
    if data.pnl_percent >= thresholds.huge_win_percent:
        return MascotState.HUGE_WIN
    if data.pnl_percent >= thresholds.big_win_percent:
        return MascotState.BIG_WIN
    if data.pnl_percent > 0:
        moderate = (MascotState.WIN, MascotState.CONFIDENT)
        return moderate[content_index(data, "mascot", len(moderate))]
    if data.pnl_percent <= thresholds.large_loss_percent:
        return MascotState.LOSS
    if data.pnl_percent < 0:
        return MascotState.SMALL_LOSS
    return MascotState.NEUTRAL
