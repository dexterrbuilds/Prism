from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.models import Direction, MarketRegime, Signal, SignalGrade, SignalState, TradePlan
from app.signals.lifecycle import ALLOWED_TRANSITIONS


@dataclass(frozen=True, slots=True)
class PerformanceStats:
    tracking_since: datetime
    period_days: int | None
    signals: int
    activated: int
    wins: int
    losses: int
    open_signals: int
    tp1_runners: int
    tp2_hits: int
    invalidated: int
    average_hold_hours: float | None

    @property
    def resolved(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.resolved * 100 if self.resolved else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def serialize_signal(signal: Signal) -> str:
    data: dict[str, Any] = {
        "id": signal.id,
        "symbol": signal.symbol,
        "strategy": signal.strategy,
        "direction": signal.direction.value,
        "regime": signal.regime.value,
        "score": signal.score,
        "grade": signal.grade.value,
        "state": signal.state.value,
        "trade": asdict(signal.trade),
        "evidence": list(signal.evidence),
        "created_at": _iso(signal.created_at),
        "supporting_strategies": list(signal.supporting_strategies),
        "current_price": signal.current_price,
        "state_changed_at": _iso(signal.state_changed_at),
        "activated_at": _iso(signal.activated_at),
        "tp1_hit_at": _iso(signal.tp1_hit_at),
    }
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


def deserialize_signal(raw: str) -> Signal:
    data = json.loads(raw)
    return Signal(
        id=str(data["id"]),
        symbol=str(data["symbol"]),
        strategy=str(data["strategy"]),
        direction=Direction(data["direction"]),
        regime=MarketRegime(data["regime"]),
        score=int(data["score"]),
        grade=SignalGrade(data["grade"]),
        state=SignalState(data["state"]),
        trade=TradePlan(**data["trade"]),
        evidence=tuple(str(item) for item in data["evidence"]),
        created_at=_datetime(data["created_at"]) or datetime.now(UTC),
        supporting_strategies=tuple(str(item) for item in data.get("supporting_strategies", ())),
        current_price=float(data["current_price"]) if data.get("current_price") is not None else None,
        state_changed_at=_datetime(data.get("state_changed_at")),
        activated_at=_datetime(data.get("activated_at")),
        tp1_hit_at=_datetime(data.get("tp1_hit_at")),
    )


class OutcomeLedger:
    """Small durable signal ledger; TP1 is the configured binary win threshold."""

    def __init__(self, path: str, history_limit: int = 5000) -> None:
        database_path = Path(path).expanduser()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(database_path)
        self._history_limit = history_limit
        # The async adapter serializes access but executes these blocking calls in
        # worker threads, so the connection cannot be bound to its creator thread.
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA journal_size_limit=1048576")
        self._connection.execute("PRAGMA wal_autocheckpoint=100")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                signal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                score INTEGER NOT NULL,
                current_price REAL,
                activated_at TEXT,
                tp1_hit_at TEXT,
                tp2_hit_at TEXT,
                stopped_at TEXT,
                invalidated_at TEXT,
                win INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_created ON signal_outcomes(created_at);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_state ON signal_outcomes(state);
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('tracking_started_at', ?)",
            (_iso(datetime.now(UTC)),),
        )
        self._connection.commit()

    def contains_signal(self, signal_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM signal_outcomes WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        return row is not None

    def record_signal(self, signal: Signal) -> bool:
        now = signal.state_changed_at or signal.created_at
        activated_at = _iso(signal.activated_at or now) if signal.state is SignalState.ACTIVE else _iso(signal.activated_at)
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO signal_outcomes(
                signal_id, created_at, updated_at, state, symbol, strategy,
                direction, score, current_price, activated_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.id,
                _iso(signal.created_at),
                _iso(now),
                signal.state.value,
                signal.symbol,
                signal.strategy,
                signal.direction.value,
                signal.score,
                signal.current_price,
                activated_at,
                serialize_signal(signal),
            ),
        )
        self._prune()
        self._connection.commit()
        return cursor.rowcount == 1

    def record_event(self, signal: Signal) -> bool:
        existing = self._connection.execute(
            "SELECT state FROM signal_outcomes WHERE signal_id = ?",
            (signal.id,),
        ).fetchone()
        inserted = False
        if existing is None:
            inserted = self.record_signal(signal)
        else:
            persisted_state = SignalState(str(existing["state"]))
            if signal.state is persisted_state or signal.state not in ALLOWED_TRANSITIONS[persisted_state]:
                return False
        event_at = signal.state_changed_at or datetime.now(UTC)
        fields: dict[SignalState, tuple[str, ...]] = {
            SignalState.ACTIVE: ("activated_at",),
            SignalState.TP1_HIT: ("tp1_hit_at",),
            SignalState.TP2_HIT: ("tp1_hit_at", "tp2_hit_at"),
            SignalState.STOPPED: ("stopped_at",),
            SignalState.INVALIDATED: ("invalidated_at",),
        }
        timestamp_fields = fields.get(signal.state, ())
        assignments = ["updated_at = ?", "state = ?", "current_price = ?", "payload = ?"]
        values: list[Any] = [_iso(event_at), signal.state.value, signal.current_price, serialize_signal(signal)]
        for field in timestamp_fields:
            assignments.append(f"{field} = COALESCE({field}, ?)")
            values.append(_iso(event_at))
        if signal.state in {SignalState.TP1_HIT, SignalState.TP2_HIT}:
            assignments.append("win = 1")
        values.append(signal.id)
        self._connection.execute(
            f"UPDATE signal_outcomes SET {', '.join(assignments)} WHERE signal_id = ?",  # noqa: S608 - fixed column names
            values,
        )
        self._connection.commit()
        return inserted or existing is not None

    def load_open_signals(self) -> tuple[Signal, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM signal_outcomes WHERE state IN (?, ?, ?)",
            (SignalState.CONFIRMED.value, SignalState.ACTIVE.value, SignalState.TP1_HIT.value),
        ).fetchall()
        signals: list[Signal] = []
        for row in rows:
            try:
                signals.append(deserialize_signal(str(row["payload"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(signals)

    def stats(self, period_days: int | None = None, now: datetime | None = None) -> PerformanceStats:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        tracking_raw = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'tracking_started_at'"
        ).fetchone()
        tracking_since = _datetime(str(tracking_raw["value"])) if tracking_raw else current
        assert tracking_since is not None
        cutoff = max(tracking_since, current - timedelta(days=period_days)) if period_days is not None else tracking_since
        rows = self._connection.execute(
            """
            SELECT state, win, activated_at, tp1_hit_at, tp2_hit_at,
                   stopped_at, invalidated_at
            FROM signal_outcomes WHERE created_at >= ?
            """,
            (_iso(cutoff),),
        ).fetchall()
        wins = sum(int(row["win"]) for row in rows)
        losses = sum(row["state"] == SignalState.STOPPED.value and not int(row["win"]) for row in rows)
        durations: list[float] = []
        for row in rows:
            activated = _datetime(row["activated_at"])
            resolved = _datetime(row["tp1_hit_at"]) if int(row["win"]) else _datetime(row["stopped_at"])
            if activated and resolved and resolved >= activated:
                durations.append((resolved - activated).total_seconds() / 3600)
        return PerformanceStats(
            tracking_since=cutoff,
            period_days=period_days,
            signals=len(rows),
            activated=sum(row["activated_at"] is not None for row in rows),
            wins=wins,
            losses=losses,
            open_signals=sum(row["state"] in {SignalState.CONFIRMED.value, SignalState.ACTIVE.value} for row in rows),
            tp1_runners=sum(row["state"] == SignalState.TP1_HIT.value for row in rows),
            tp2_hits=sum(row["tp2_hit_at"] is not None for row in rows),
            invalidated=sum(row["invalidated_at"] is not None for row in rows),
            average_hold_hours=sum(durations) / len(durations) if durations else None,
        )

    def _prune(self) -> None:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM signal_outcomes").fetchone()
        excess = max(0, int(row["count"]) - self._history_limit)
        if not excess:
            return
        terminal = (
            SignalState.TP2_HIT.value,
            SignalState.STOPPED.value,
            SignalState.INVALIDATED.value,
            SignalState.EXPIRED.value,
        )
        self._connection.execute(
            """
            DELETE FROM signal_outcomes WHERE signal_id IN (
                SELECT signal_id FROM signal_outcomes
                WHERE state IN (?, ?, ?, ?) ORDER BY created_at ASC LIMIT ?
            )
            """,
            (*terminal, excess),
        )

    def close(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.close()
