from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.ai.analyst import AIReviewService
from app.models import AIReview, AIReviewVerdict, Direction, EntryDecision, EntryQuality
from tests.test_scanner_smoke import _ranked_signal


class FakeAnalyst:
    def __init__(self, verdict: AIReviewVerdict) -> None:
        self.calls = 0
        self.verdict = verdict

    async def review(self, summary: object) -> AIReview:
        del summary
        self.calls += 1
        return AIReview(self.verdict, ("fixture review",), (), "fake", "fake", datetime.now(UTC))


class FailingAnalyst:
    async def review(self, summary: object) -> AIReview:
        del summary
        raise TimeoutError("fixture timeout")


def _signal(score: int = 88, entry_score: int = 82, hard: tuple[str, ...] = ()):
    signal = _ranked_signal("BREAKOUT_RETEST", Direction.LONG, score)
    quality = EntryQuality(entry_score, EntryDecision.VALID, {}, (), hard_reasons=hard)
    return replace(signal, entry_quality=quality)


@pytest.mark.asyncio
async def test_ai_review_cache_and_verdicts() -> None:
    analyst = FakeAnalyst(AIReviewVerdict.APPROVE)
    service = AIReviewService(analyst, cache_size=8)
    first = await service.review(_signal())
    second = await service.review(_signal())
    assert first.verdict is AIReviewVerdict.APPROVE
    assert second == first
    assert analyst.calls == 1


@pytest.mark.asyncio
async def test_ai_cannot_rescue_bad_entry_or_hard_reject() -> None:
    analyst = FakeAnalyst(AIReviewVerdict.APPROVE)
    service = AIReviewService(analyst)
    waited = await service.review(_signal(entry_score=60))
    assert waited.verdict is AIReviewVerdict.WAIT
    rejected = await service.review(_signal(hard=("ENTRY_TOO_LATE",)))
    assert rejected.verdict is AIReviewVerdict.REJECT
    assert analyst.calls == 1


@pytest.mark.asyncio
async def test_ai_disabled_falls_back_without_blocking() -> None:
    review = await AIReviewService(None).review(_signal())
    assert review.verdict is AIReviewVerdict.UNAVAILABLE


@pytest.mark.asyncio
async def test_ai_provider_failure_falls_back_to_deterministic_engine() -> None:
    review = await AIReviewService(FailingAnalyst()).review(_signal())
    assert review.verdict is AIReviewVerdict.UNAVAILABLE
