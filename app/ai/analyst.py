from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import Settings
from app.models import AIReview, AIReviewVerdict, Signal

logger = logging.getLogger(__name__)


class AIAnalyst(Protocol):
    async def review(self, summary: Mapping[str, Any]) -> AIReview: ...


class OpenAICompatibleAnalyst:
    """Minimal async adapter for a strict JSON, chat-completions-compatible endpoint."""

    def __init__(self, endpoint: str, api_key: str, model: str, timeout: float) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def _request(self, summary: Mapping[str, Any]) -> AIReview:
        system = (
            "You are a conservative trade-entry quality reviewer. Use only supplied facts. "
            "Return JSON with verdict APPROVE, WAIT, or REJECT and arrays reasoning and risks. "
            "Never create a counter-trade and never override a deterministic hard rule."
        )
        body = json.dumps(
            {
                "model": self._model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(summary, separators=(",", ":"))},
                ],
            }
        ).encode()
        request = Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - configured HTTPS AI endpoint
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        verdict = AIReviewVerdict(str(parsed["verdict"]).upper())
        reasoning = tuple(str(item)[:180] for item in parsed.get("reasoning", ())[:5])
        risks = tuple(str(item)[:180] for item in parsed.get("risks", ())[:5])
        return AIReview(verdict, reasoning, risks, "openai_compatible", self._model, datetime.now(UTC))

    async def review(self, summary: Mapping[str, Any]) -> AIReview:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._request, summary), timeout=self._timeout + 1)
        except (TimeoutError, HTTPError, URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("ai_review_fallback error=%s", type(exc).__name__)
            return AIReview(
                AIReviewVerdict.UNAVAILABLE,
                ("AI review unavailable; deterministic rules remain authoritative.",),
                (),
                "openai_compatible",
                self._model,
                datetime.now(UTC),
            )


class AIReviewService:
    def __init__(self, analyst: AIAnalyst | None, cache_size: int = 128) -> None:
        self._analyst = analyst
        self._cache: OrderedDict[str, AIReview] = OrderedDict()
        self._cache_size = cache_size

    @property
    def enabled(self) -> bool:
        return self._analyst is not None

    @staticmethod
    def summary(signal: Signal) -> dict[str, Any]:
        quality = signal.entry_quality
        return {
            "symbol": signal.symbol,
            "mode": signal.mode.value,
            "direction": signal.direction.value,
            "directional_bias": signal.directional_bias.direction.value if signal.directional_bias and signal.directional_bias.direction else None,
            "bias_strength": signal.directional_bias.strength if signal.directional_bias else None,
            "regime": signal.regime.value,
            "setup": signal.strategy,
            "setup_score": signal.score,
            "entry_quality_score": quality.total if quality else None,
            "current_price": signal.current_price,
            "entry_zone": [signal.trade.entry_zone_low, signal.trade.entry_zone_high],
            "preferred_entry": signal.trade.preferred_entry,
            "stop_loss": signal.trade.stop_loss,
            "target_2r": signal.trade.tp2,
            "atr": signal.atr_at_entry,
            "entry_evidence": list(quality.evidence[:8]) if quality else [],
            "setup_evidence": list(signal.evidence[:10]),
            "warnings": list(quality.warnings[:8]) if quality else [],
            "hard_reasons": list(quality.hard_reasons) if quality else [],
            "valid_conditions": list(signal.valid_conditions[:5]),
            "stop_distance_atr": signal.trade.stop_distance_atr,
            "reward_risk": signal.trade.reward_risk,
        }

    @staticmethod
    def _fingerprint(signal: Signal) -> str:
        quality = signal.entry_quality
        bucket = quality.total // 5 if quality else -1
        price = signal.current_price or signal.trade.preferred_entry
        normalized = round(price / max(signal.trade.risk_per_unit, 1e-12), 1)
        raw = f"{signal.id}|{signal.state.value}|{bucket}|{normalized}"
        return sha256(raw.encode()).hexdigest()[:24]

    async def review(self, signal: Signal) -> AIReview:
        quality = signal.entry_quality
        if self._analyst is None:
            return AIReview(AIReviewVerdict.UNAVAILABLE, ("AI analysis is disabled.",))
        if signal.score < 70 or quality is None or quality.hard_reasons:
            return AIReview(
                AIReviewVerdict.REJECT,
                ("Deterministic hard rules rejected the candidate before AI review.",),
                tuple(quality.hard_reasons if quality else ("NO_ENTRY_QUALITY",)),
            )
        key = self._fingerprint(signal)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        try:
            review = await self._analyst.review(self.summary(signal))
        except Exception as exc:
            logger.warning("ai_review_fallback error=%s", type(exc).__name__)
            review = AIReview(
                AIReviewVerdict.UNAVAILABLE,
                ("AI review failed; deterministic rules remain authoritative.",),
                (),
                reviewed_at=datetime.now(UTC),
            )
        # AI cannot rescue a sub-threshold entry. An APPROVE is demoted to WAIT.
        if quality.total < 75 and review.verdict is AIReviewVerdict.APPROVE:
            review = AIReview(
                AIReviewVerdict.WAIT,
                review.reasoning + ("Deterministic entry quality has not reached the activation threshold.",),
                review.risks,
                review.provider,
                review.model,
                review.reviewed_at,
            )
        self._cache[key] = review
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return review


def build_ai_review_service(settings: Settings) -> AIReviewService:
    analyst: AIAnalyst | None = None
    if settings.ai_analysis_enabled and settings.ai_api_key and settings.ai_model and settings.ai_endpoint:
        analyst = OpenAICompatibleAnalyst(
            settings.ai_endpoint,
            settings.ai_api_key,
            settings.ai_model,
            settings.ai_timeout_seconds,
        )
    elif settings.ai_analysis_enabled:
        logger.warning("ai_review_disabled reason=missing_endpoint")
    return AIReviewService(analyst, settings.ai_cache_size)
