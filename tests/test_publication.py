from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from telegram.error import BadRequest

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import Direction, EntryDecision, EntryQuality, PublicationState, SignalState
from app.scanner import Scanner
from app.signals.lifecycle import SignalStore, transition
from app.signals.repository import SQLiteOutcomeRepository
from app.telegram.bot import TelegramPublishResult, TelegramService
from app.telegram.formatter import format_signal
from tests.test_lifecycle import make_waiting_signal
from tests.test_scanner_smoke import FakeExchange


def _settings() -> Settings:
    return replace(
        Settings.from_env(),
        dry_run=False,
        telegram_chat_id="111",
        telegram_chat_ids=("111", "222"),
        telegram_channel_ids=("-100333",),
    )


def _publishable_signal(signal_id: str = "publish-me", *, direction: Direction = Direction.LONG):
    created = datetime.now(UTC) - timedelta(minutes=2)
    quality = EntryQuality(
        total=82,
        decision=EntryDecision.VALID,
        categories={},
        evidence=("closed retest confirmed",),
        retest_completed=True,
        lower_timeframe_confirmed=True,
    )
    return replace(
        make_waiting_signal(direction=direction),
        id=signal_id,
        state=SignalState.ENTRY_READY,
        created_at=created,
        state_changed_at=created,
        expires_at=created + timedelta(hours=6),
        publication_state=PublicationState.PUBLISH_PENDING,
        intended_destination_ids=("111", "222", "-100333"),
        entry_quality=quality,
    )


class SequencedTelegram:
    def __init__(self, results: list[TelegramPublishResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, bool, tuple[str, ...]]] = []

    async def publish(
        self,
        signal,
        lifecycle: bool = False,
        chart_png: bytes | None = None,
        *,
        destinations: tuple[str, ...] | None = None,
    ) -> TelegramPublishResult:
        del chart_png
        self.calls.append((signal.id, lifecycle, destinations or ()))
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_new_signal_publishes_once_and_published_at_is_persisted(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "published.db"))
    signal = _publishable_signal()
    assert await repository.record_signal(signal)
    telegram = SequencedTelegram(
        [
            TelegramPublishResult(
                ("111", "222", "-100333"),
                (),
                (("111", "10"), ("222", "11"), ("-100333", "12")),
            )
        ]
    )
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), repository)  # type: ignore[arg-type]
    scanner.store.restore(signal)

    published = await scanner._ensure_initial_publication(signal)
    repeated = await scanner._ensure_initial_publication(published)

    assert len(telegram.calls) == 1
    assert repeated.publication_state is PublicationState.PUBLISHED
    assert repeated.published_at is not None
    assert repeated.channel_message_id == "12"
    persisted = await repository.load_signal(signal.id)
    assert persisted is not None
    assert persisted.publication_state is PublicationState.PUBLISHED
    assert persisted.published_at == repeated.published_at
    await repository.close()


def test_genuine_published_duplicate_is_suppressed_but_new_origin_is_allowed() -> None:
    store = SignalStore()
    first = replace(
        _publishable_signal("A"),
        publication_state=PublicationState.PUBLISHED,
        published_at=datetime.now(UTC),
        setup_fingerprint="same-opportunity",
        setup_origin_at=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
    )
    duplicate = replace(first, id="B", publication_state=PublicationState.PUBLISH_PENDING, published_at=None)
    new_origin = replace(
        duplicate,
        id="C",
        setup_fingerprint="new-opportunity",
        setup_origin_at=datetime(2026, 9, 4, 8, 20, tzinfo=UTC),
    )
    store.restore(first)

    match = store.find_duplicate(duplicate)
    assert match is not None and match.signal.id == "A"
    assert store.find_duplicate(new_origin, window_minutes=360) is None


@pytest.mark.asyncio
async def test_initial_send_failure_retries_same_signal_id_and_never_records_false_success(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "retry.db"))
    signal = _publishable_signal("same-id")
    assert await repository.record_signal(signal)
    telegram = SequencedTelegram(
        [
            TelegramPublishResult((), ("111", "222", "-100333"), errors=("temporary outage",)),
            TelegramPublishResult(("111", "222", "-100333"), ()),
        ]
    )
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), repository)  # type: ignore[arg-type]
    scanner.store.restore(signal)

    failed = await scanner._ensure_initial_publication(signal)
    persisted_failure = await repository.load_signal(signal.id)
    recovered = await scanner._ensure_initial_publication(failed)

    assert failed.publication_state is PublicationState.PUBLISH_FAILED
    assert failed.published_at is None
    assert persisted_failure is not None and persisted_failure.published_at is None
    assert recovered.publication_state is PublicationState.PUBLISHED
    assert [call[0] for call in telegram.calls] == [signal.id, signal.id]
    await repository.close()


@pytest.mark.asyncio
async def test_restart_recovers_still_valid_unpublished_signal(tmp_path) -> None:
    path = str(tmp_path / "restart-publication.db")
    repository = SQLiteOutcomeRepository(path)
    signal = _publishable_signal("restart-pending")
    assert await repository.record_signal(signal)
    await repository.close()

    reopened = SQLiteOutcomeRepository(path)
    telegram = SequencedTelegram([TelegramPublishResult(("111", "222", "-100333"), ())])
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), reopened)  # type: ignore[arg-type]
    await scanner.restore_outcomes()

    recovered = await scanner.recover_unpublished_signals()

    assert recovered == 1
    assert telegram.calls[0][0] == signal.id
    persisted = await reopened.load_signal(signal.id)
    assert persisted is not None and persisted.publication_state is PublicationState.PUBLISHED
    await reopened.close()


@pytest.mark.asyncio
async def test_restart_marks_expired_unpublished_signal_without_sending_it(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "expired-unpublished.db"))
    signal = replace(
        _publishable_signal("expired-pending"),
        created_at=datetime.now(UTC) - timedelta(hours=8),
        state_changed_at=datetime.now(UTC) - timedelta(hours=8),
        expires_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert await repository.record_signal(signal)
    telegram = SequencedTelegram([])
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), repository)  # type: ignore[arg-type]
    scanner.store.restore(signal)

    recovered = await scanner.recover_unpublished_signals()

    assert recovered == 0
    assert telegram.calls == []
    persisted = await repository.load_signal(signal.id)
    assert persisted is not None
    assert persisted.state is SignalState.EXPIRED
    assert persisted.publication_state is PublicationState.UNPUBLISHED_TERMINAL
    await repository.close()


@pytest.mark.asyncio
async def test_failed_initial_signal_never_emits_contextless_invalidation(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "guard.db"))
    signal = _publishable_signal("unseen")
    assert await repository.record_signal(signal)
    telegram = SequencedTelegram(
        [TelegramPublishResult((), ("111", "222", "-100333"), errors=("Telegram unavailable",))]
    )
    health = RuntimeHealth("fake")
    scanner = Scanner(_settings(), FakeExchange(), telegram, health, repository)  # type: ignore[arg-type]
    scanner.store.restore(signal)
    failed = await scanner._ensure_initial_publication(signal)
    invalidated = transition(
        failed,
        SignalState.INVALIDATED,
        changed_at=datetime.now(UTC),
        reason="Structure invalidated before entry.",
    )
    scanner.store.restore(invalidated)

    await scanner._publish_lifecycle_events([invalidated], invalidated.symbol)

    assert len(telegram.calls) == 1
    assert health.lifecycle_notifications_suppressed == 1
    persisted = await repository.load_signal(signal.id)
    assert persisted is not None
    assert persisted.state is SignalState.INVALIDATED
    assert persisted.publication_state is PublicationState.UNPUBLISHED_TERMINAL
    await repository.close()


@pytest.mark.asyncio
async def test_internal_waiting_setup_sends_full_initial_alert_when_entry_becomes_ready(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "deferred.db"))
    pending = _publishable_signal("deferred")
    waiting = replace(
        pending,
        state=SignalState.WAITING_FOR_ENTRY,
        publication_state=PublicationState.INTERNAL_ONLY,
    )
    assert await repository.record_signal(waiting)
    telegram = SequencedTelegram(
        [TelegramPublishResult(("111", "222", "-100333"), ())]
    )
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), repository)  # type: ignore[arg-type]
    scanner.store.restore(waiting)
    assert pending.entry_quality is not None
    ready = scanner.store.mark_entry_ready(
        waiting.id,
        pending.entry_quality,
        observed_at=datetime.now(UTC),
        current_price=pending.current_price or pending.trade.preferred_entry,
    )
    assert ready is not None

    await scanner._publish_lifecycle_events([ready], ready.symbol)

    assert telegram.calls == [(waiting.id, False, ("111", "222", "-100333"))]
    persisted = await repository.load_signal(waiting.id)
    assert persisted is not None
    assert persisted.state is SignalState.ENTRY_READY
    assert persisted.publication_state is PublicationState.PUBLISHED
    await repository.close()


@pytest.mark.asyncio
async def test_channel_success_publishes_while_failed_dm_fanout_retries_independently(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "fanout.db"))
    signal = _publishable_signal("fanout")
    assert await repository.record_signal(signal)
    telegram = SequencedTelegram(
        [
            TelegramPublishResult(("-100333",), ("111", "222"), (("-100333", "99"),), ("DM outage",)),
            TelegramPublishResult(("111", "222"), ()),
        ]
    )
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), repository)  # type: ignore[arg-type]
    scanner.store.restore(signal)

    channel_published = await scanner._ensure_initial_publication(signal)
    completed = await scanner._ensure_initial_publication(channel_published)

    assert channel_published.publication_state is PublicationState.PUBLISHED
    assert channel_published.channel_message_id == "99"
    assert telegram.calls[1][2] == ("111", "222")
    assert completed.delivered_destination_ids == ("111", "222", "-100333")
    assert completed.dm_success_count == 2
    assert completed.dm_failure_count == 0
    await repository.close()


@pytest.mark.asyncio
async def test_lifecycle_fanout_only_targets_recipients_that_received_initial_signal(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "recipient-guard.db"))
    signal = _publishable_signal("recipient-guard")
    assert await repository.record_signal(signal)
    telegram = SequencedTelegram(
        [
            TelegramPublishResult(("-100333",), ("111", "222"), (("-100333", "99"),), ("DM outage",)),
            TelegramPublishResult(("-100333",), ()),
        ]
    )
    scanner = Scanner(_settings(), FakeExchange(), telegram, RuntimeHealth("fake"), repository)  # type: ignore[arg-type]
    scanner.store.restore(signal)
    published = await scanner._ensure_initial_publication(signal)
    invalidated = transition(
        published,
        SignalState.INVALIDATED,
        changed_at=datetime.now(UTC),
        reason="Structure invalidated.",
    )
    scanner.store.restore(invalidated)

    await scanner._publish_lifecycle_events([invalidated], invalidated.symbol)

    assert telegram.calls[1] == (signal.id, True, ("-100333",))
    await repository.close()


@pytest.mark.asyncio
async def test_telegram_destination_uses_bounded_retry() -> None:
    settings = replace(_settings(), telegram_chat_ids=("111",), telegram_channel_ids=())

    class FlakyBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **kwargs: Any) -> Any:
            del kwargs
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("temporary network failure")
            return type("Message", (), {"message_id": 42})()

    application = type("Application", (), {"bot": FlakyBot()})()
    service = TelegramService(settings, RuntimeHealth("fake"))
    service._application = application  # type: ignore[assignment]

    result = await service.publish(_publishable_signal(), destinations=("111",))

    assert result.delivered_destination_ids == ("111",)
    assert application.bot.calls == 3


@pytest.mark.asyncio
async def test_telegram_markdown_parse_failure_falls_back_to_plain_text() -> None:
    settings = replace(_settings(), telegram_chat_ids=("111",), telegram_channel_ids=())

    class ParseFallbackBot:
        def __init__(self) -> None:
            self.parse_modes: list[object] = []

        async def send_message(self, **kwargs: Any) -> Any:
            self.parse_modes.append(kwargs.get("parse_mode"))
            if len(self.parse_modes) == 1:
                raise BadRequest("Can't parse entities")
            return type("Message", (), {"message_id": 43})()

    application = type("Application", (), {"bot": ParseFallbackBot()})()
    service = TelegramService(settings, RuntimeHealth("fake"))
    service._application = application  # type: ignore[assignment]

    result = await service.publish(_publishable_signal(), destinations=("111",))

    assert result.delivered_destination_ids == ("111",)
    assert application.bot.parse_modes[1] is None


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_production_initial_formatter_payload_is_telegram_compatible(direction: Direction) -> None:
    text = format_signal(_publishable_signal(direction=direction))

    assert 0 < len(text) <= 4096
    assert direction.value in text
    keyboard = TelegramService._manual_scan_keyboard()
    assert keyboard.inline_keyboard[0][0].callback_data == "manual_scan"
    assert len(keyboard.inline_keyboard[0][0].callback_data or "") <= 64
