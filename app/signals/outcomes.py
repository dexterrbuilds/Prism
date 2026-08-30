from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from app.models import (
    AIReview,
    AIReviewVerdict,
    Direction,
    DirectionalBias,
    EntryDecision,
    EntryQuality,
    MarketRegime,
    Signal,
    SignalGrade,
    SignalMode,
    SignalState,
    TradePlan,
)
from app.signals.lifecycle import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    OPEN_STATES,
    STOP_STATES,
)


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
    average_r: float | None = None
    expectancy_r: float | None = None
    median_mae_atr: float | None = None
    p75_mae_atr: float | None = None
    p90_mae_atr: float | None = None
    median_mfe_atr: float | None = None
    average_mae_atr: float | None = None
    average_mfe_atr: float | None = None
    winners_adverse_over_half_atr_pct: float | None = None
    stopped_then_target_reached: int = 0
    by_mode: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    breakdowns: dict[str, dict[str, dict[str, float | int | None]]] = field(default_factory=dict)

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
        "trading_timeframe": signal.trading_timeframe,
        "analysis_timeframe": signal.analysis_timeframe,
        "expires_at": _iso(signal.expires_at),
        "validity_minutes": signal.validity_minutes,
        "valid_conditions": list(signal.valid_conditions),
        "max_missed_distance": signal.max_missed_distance,
        "entry_trigger_price": signal.entry_trigger_price,
        "missed_at": _iso(signal.missed_at),
        "invalidated_at": _iso(signal.invalidated_at),
        "expired_at": _iso(signal.expired_at),
        "tp2_hit_at": _iso(signal.tp2_hit_at),
        "stopped_at": _iso(signal.stopped_at),
        "lifecycle_reason": signal.lifecycle_reason,
        "mode": signal.mode.value,
        "entry_quality": asdict(signal.entry_quality) if signal.entry_quality is not None else None,
        "ai_review": (
            {
                "verdict": signal.ai_review.verdict.value,
                "reasoning": list(signal.ai_review.reasoning),
                "risks": list(signal.ai_review.risks),
                "provider": signal.ai_review.provider,
                "model": signal.ai_review.model,
                "reviewed_at": _iso(signal.ai_review.reviewed_at),
            }
            if signal.ai_review is not None
            else None
        ),
        "atr_at_entry": signal.atr_at_entry,
        "mae": signal.mae,
        "mfe": signal.mfe,
        "stopped_then_target_reached": signal.stopped_then_target_reached,
        "follow_up_until": _iso(signal.follow_up_until),
        "directional_bias": (
            {
                "direction": signal.directional_bias.direction.value if signal.directional_bias.direction else None,
                "strength": signal.directional_bias.strength,
                "timeframe": signal.directional_bias.timeframe,
                "evidence": list(signal.directional_bias.evidence),
            }
            if signal.directional_bias is not None
            else None
        ),
    }
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


def deserialize_signal(raw: str) -> Signal:
    data = json.loads(raw)
    quality_data = data.get("entry_quality")
    entry_quality = None
    if isinstance(quality_data, dict):
        entry_quality = EntryQuality(
            total=int(quality_data["total"]),
            decision=EntryDecision(quality_data["decision"]),
            categories={str(key): int(value) for key, value in quality_data.get("categories", {}).items()},
            evidence=tuple(str(item) for item in quality_data.get("evidence", ())),
            warnings=tuple(str(item) for item in quality_data.get("warnings", ())),
            hard_reasons=tuple(str(item) for item in quality_data.get("hard_reasons", ())),
            retest_completed=bool(quality_data.get("retest_completed", False)),
            lower_timeframe_confirmed=bool(quality_data.get("lower_timeframe_confirmed", False)),
            distance_from_entry_atr=float(quality_data.get("distance_from_entry_atr", 0.0)),
        )
    review_data = data.get("ai_review")
    ai_review = None
    if isinstance(review_data, dict):
        ai_review = AIReview(
            verdict=AIReviewVerdict(review_data["verdict"]),
            reasoning=tuple(str(item) for item in review_data.get("reasoning", ())),
            risks=tuple(str(item) for item in review_data.get("risks", ())),
            provider=str(review_data["provider"]) if review_data.get("provider") else None,
            model=str(review_data["model"]) if review_data.get("model") else None,
            reviewed_at=_datetime(review_data.get("reviewed_at")),
        )
    bias_data = data.get("directional_bias")
    directional_bias = None
    if isinstance(bias_data, dict):
        directional_bias = DirectionalBias(
            Direction(bias_data["direction"]) if bias_data.get("direction") else None,
            float(bias_data.get("strength", 0.0)),
            str(bias_data.get("timeframe", "4h/1h")),
            tuple(str(item) for item in bias_data.get("evidence", ())),
        )
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
        trading_timeframe=str(data.get("trading_timeframe", "15m")),
        analysis_timeframe=str(data.get("analysis_timeframe", "1h")),
        expires_at=_datetime(data.get("expires_at")),
        validity_minutes=int(data["validity_minutes"]) if data.get("validity_minutes") is not None else None,
        valid_conditions=tuple(str(item) for item in data.get("valid_conditions", ())),
        max_missed_distance=(
            float(data["max_missed_distance"]) if data.get("max_missed_distance") is not None else None
        ),
        entry_trigger_price=(
            float(data["entry_trigger_price"]) if data.get("entry_trigger_price") is not None else None
        ),
        missed_at=_datetime(data.get("missed_at")),
        invalidated_at=_datetime(data.get("invalidated_at")),
        expired_at=_datetime(data.get("expired_at")),
        tp2_hit_at=_datetime(data.get("tp2_hit_at")),
        stopped_at=_datetime(data.get("stopped_at")),
        lifecycle_reason=str(data["lifecycle_reason"]) if data.get("lifecycle_reason") else None,
        mode=SignalMode(data.get("mode", SignalMode.INTRADAY.value)),
        entry_quality=entry_quality,
        ai_review=ai_review,
        atr_at_entry=float(data["atr_at_entry"]) if data.get("atr_at_entry") is not None else None,
        mae=float(data.get("mae", 0.0)),
        mfe=float(data.get("mfe", 0.0)),
        stopped_then_target_reached=bool(data.get("stopped_then_target_reached", False)),
        follow_up_until=_datetime(data.get("follow_up_until")),
        directional_bias=directional_bias,
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
                expires_at TEXT,
                entry_trigger_price REAL,
                missed_at TEXT,
                expired_at TEXT,
                lifecycle_reason TEXT,
                mode TEXT NOT NULL DEFAULT 'INTRADAY',
                entry_quality_score INTEGER,
                atr_at_entry REAL,
                mae REAL NOT NULL DEFAULT 0,
                mfe REAL NOT NULL DEFAULT 0,
                stopped_then_target_reached INTEGER NOT NULL DEFAULT 0,
                follow_up_until TEXT,
                win INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_created ON signal_outcomes(created_at);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_state ON signal_outcomes(state);
            """
        )
        existing_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(signal_outcomes)").fetchall()
        }
        migrations = {
            "expires_at": "TEXT",
            "entry_trigger_price": "REAL",
            "missed_at": "TEXT",
            "expired_at": "TEXT",
            "lifecycle_reason": "TEXT",
            "mode": "TEXT NOT NULL DEFAULT 'INTRADAY'",
            "entry_quality_score": "INTEGER",
            "atr_at_entry": "REAL",
            "mae": "REAL NOT NULL DEFAULT 0",
            "mfe": "REAL NOT NULL DEFAULT 0",
            "stopped_then_target_reached": "INTEGER NOT NULL DEFAULT 0",
            "follow_up_until": "TEXT",
        }
        for column, column_type in migrations.items():
            if column not in existing_columns:
                self._connection.execute(
                    f"ALTER TABLE signal_outcomes ADD COLUMN {column} {column_type}"  # noqa: S608 - fixed migration map
                )
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_mode ON signal_outcomes(mode)")
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_strategy ON signal_outcomes(strategy)")
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
        activated_at = _iso(signal.activated_at or now) if signal.state in ACTIVE_STATES else _iso(signal.activated_at)
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO signal_outcomes(
                signal_id, created_at, updated_at, state, symbol, strategy,
                direction, score, current_price, activated_at, expires_at,
                entry_trigger_price, mode, entry_quality_score, atr_at_entry,
                mae, mfe, stopped_then_target_reached, follow_up_until, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _iso(signal.expires_at),
                signal.entry_trigger_price,
                signal.mode.value,
                signal.entry_quality.total if signal.entry_quality else None,
                signal.atr_at_entry,
                signal.mae,
                signal.mfe,
                int(signal.stopped_then_target_reached),
                _iso(signal.follow_up_until),
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
            SignalState.ENTRY_TRIGGERED: ("activated_at",),
            SignalState.TP1_HIT: ("tp1_hit_at",),
            SignalState.TP2_HIT: ("tp1_hit_at", "tp2_hit_at"),
            SignalState.STOPPED: ("stopped_at",),
            SignalState.SL_HIT: ("stopped_at",),
            SignalState.MISSED: ("missed_at",),
            SignalState.INVALIDATED: ("invalidated_at",),
            SignalState.EXPIRED: ("expired_at",),
        }
        timestamp_fields = fields.get(signal.state, ())
        assignments = [
            "updated_at = ?",
            "state = ?",
            "current_price = ?",
            "entry_trigger_price = COALESCE(entry_trigger_price, ?)",
            "lifecycle_reason = ?",
            "payload = ?",
            "mode = ?",
            "entry_quality_score = ?",
            "atr_at_entry = ?",
            "mae = ?",
            "mfe = ?",
            "stopped_then_target_reached = ?",
            "follow_up_until = ?",
        ]
        values: list[Any] = [
            _iso(event_at),
            signal.state.value,
            signal.current_price,
            signal.entry_trigger_price,
            signal.lifecycle_reason,
            serialize_signal(signal),
            signal.mode.value,
            signal.entry_quality.total if signal.entry_quality else None,
            signal.atr_at_entry,
            signal.mae,
            signal.mfe,
            int(signal.stopped_then_target_reached),
            _iso(signal.follow_up_until),
        ]
        for timestamp_field in timestamp_fields:
            assignments.append(f"{timestamp_field} = COALESCE({timestamp_field}, ?)")
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

    def record_observation(self, signal: Signal) -> bool:
        cursor = self._connection.execute(
            """
            UPDATE signal_outcomes
            SET updated_at = ?, current_price = ?, mae = ?, mfe = ?,
                stopped_then_target_reached = ?, follow_up_until = ?, payload = ?
            WHERE signal_id = ?
            """,
            (
                _iso(datetime.now(UTC)),
                signal.current_price,
                signal.mae,
                signal.mfe,
                int(signal.stopped_then_target_reached),
                _iso(signal.follow_up_until),
                serialize_signal(signal),
                signal.id,
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def load_open_signals(self) -> tuple[Signal, ...]:
        open_values = tuple(state.value for state in OPEN_STATES | STOP_STATES)
        placeholders = ", ".join("?" for _ in open_values)
        rows = self._connection.execute(
            f"SELECT payload FROM signal_outcomes WHERE state IN ({placeholders})",  # noqa: S608 - generated placeholders
            open_values,
        ).fetchall()
        signals: list[Signal] = []
        for row in rows:
            try:
                signal = deserialize_signal(str(row["payload"]))
                if signal.state in STOP_STATES and (
                    signal.stopped_then_target_reached
                    or signal.follow_up_until is None
                    or datetime.now(UTC) >= signal.follow_up_until
                ):
                    continue
                signals.append(signal)
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
                   stopped_at, invalidated_at, payload, mode, mae, mfe,
                   atr_at_entry, stopped_then_target_reached
            FROM signal_outcomes WHERE created_at >= ?
            """,
            (_iso(cutoff),),
        ).fetchall()
        wins = sum(int(row["win"]) for row in rows)
        losses = sum(row["state"] in {state.value for state in STOP_STATES} and not int(row["win"]) for row in rows)
        durations: list[float] = []
        resolved_r: list[float] = []
        mae_atr: list[float] = []
        mfe_atr: list[float] = []
        winner_mae_atr: list[float] = []
        mode_rows: dict[str, list[sqlite3.Row]] = {}
        dimension_rows: dict[str, dict[str, list[sqlite3.Row]]] = {
            "symbol": {},
            "strategy": {},
            "direction": {},
            "regime": {},
            "ai_verdict": {},
            "setup_score_band": {},
            "entry_score_band": {},
        }
        for row in rows:
            activated = _datetime(row["activated_at"])
            resolved = _datetime(row["tp1_hit_at"]) if int(row["win"]) else _datetime(row["stopped_at"])
            if activated and resolved and resolved >= activated:
                durations.append((resolved - activated).total_seconds() / 3600)
            try:
                signal = deserialize_signal(str(row["payload"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            mode_rows.setdefault(signal.mode.value, []).append(row)
            dimensions = {
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "direction": signal.direction.value,
                "regime": signal.regime.value,
                "ai_verdict": signal.ai_review.verdict.value if signal.ai_review else "NOT_REVIEWED",
                "setup_score_band": f"{signal.score // 5 * 5}-{signal.score // 5 * 5 + 4}",
                "entry_score_band": (
                    f"{signal.entry_quality.total // 5 * 5}-{signal.entry_quality.total // 5 * 5 + 4}"
                    if signal.entry_quality
                    else "UNKNOWN"
                ),
            }
            for dimension, value in dimensions.items():
                dimension_rows[dimension].setdefault(value, []).append(row)
            if int(row["win"]):
                target = signal.trade.tp2 if signal.tp2_hit_at else signal.trade.tp1
                resolved_r.append(abs(target - (signal.entry_trigger_price or signal.trade.preferred_entry)) / signal.trade.risk_per_unit)
            elif row["state"] in {state.value for state in STOP_STATES} and signal.activated_at is not None:
                resolved_r.append(-1.0)
            atr_entry = float(row["atr_at_entry"]) if row["atr_at_entry"] else None
            if atr_entry and atr_entry > 0 and signal.activated_at is not None:
                normalized_mae = float(row["mae"]) / atr_entry
                mae_atr.append(normalized_mae)
                mfe_atr.append(float(row["mfe"]) / atr_entry)
                if int(row["win"]):
                    winner_mae_atr.append(normalized_mae)
        by_mode: dict[str, dict[str, float | int | None]] = {}
        for mode, grouped in mode_rows.items():
            mode_wins = sum(int(row["win"]) for row in grouped)
            mode_losses = sum(row["state"] in {state.value for state in STOP_STATES} and not int(row["win"]) for row in grouped)
            mode_resolved = mode_wins + mode_losses
            by_mode[mode] = {
                "signals": len(grouped),
                "wins": mode_wins,
                "losses": mode_losses,
                "win_rate": mode_wins / mode_resolved * 100 if mode_resolved else None,
            }
        breakdowns: dict[str, dict[str, dict[str, float | int | None]]] = {}
        for dimension, groups in dimension_rows.items():
            breakdowns[dimension] = {}
            for value, grouped in groups.items():
                group_wins = sum(int(row["win"]) for row in grouped)
                group_losses = sum(
                    row["state"] in {state.value for state in STOP_STATES} and not int(row["win"])
                    for row in grouped
                )
                group_resolved = group_wins + group_losses
                breakdowns[dimension][value] = {
                    "signals": len(grouped),
                    "wins": group_wins,
                    "losses": group_losses,
                    "win_rate": group_wins / group_resolved * 100 if group_resolved else None,
                }

        def percentile(values: list[float], quantile: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = (len(ordered) - 1) * quantile
            lower = int(index)
            upper = min(len(ordered) - 1, lower + 1)
            weight = index - lower
            return ordered[lower] * (1 - weight) + ordered[upper] * weight
        return PerformanceStats(
            tracking_since=cutoff,
            period_days=period_days,
            signals=len(rows),
            activated=sum(row["activated_at"] is not None for row in rows),
            wins=wins,
            losses=losses,
            open_signals=sum(
                row["state"] in {state.value for state in OPEN_STATES - {SignalState.TP1_HIT}}
                for row in rows
            ),
            tp1_runners=sum(row["state"] == SignalState.TP1_HIT.value for row in rows),
            tp2_hits=sum(row["tp2_hit_at"] is not None for row in rows),
            invalidated=sum(
                row["state"]
                in {
                    SignalState.INVALIDATED.value,
                    SignalState.MISSED.value,
                    SignalState.EXPIRED.value,
                }
                for row in rows
            ),
            average_hold_hours=sum(durations) / len(durations) if durations else None,
            average_r=sum(resolved_r) / len(resolved_r) if resolved_r else None,
            expectancy_r=sum(resolved_r) / len(resolved_r) if resolved_r else None,
            median_mae_atr=median(mae_atr) if mae_atr else None,
            p75_mae_atr=percentile(mae_atr, 0.75),
            p90_mae_atr=percentile(mae_atr, 0.90),
            median_mfe_atr=median(mfe_atr) if mfe_atr else None,
            average_mae_atr=sum(mae_atr) / len(mae_atr) if mae_atr else None,
            average_mfe_atr=sum(mfe_atr) / len(mfe_atr) if mfe_atr else None,
            winners_adverse_over_half_atr_pct=(
                sum(value > 0.5 for value in winner_mae_atr) / len(winner_mae_atr) * 100
                if winner_mae_atr
                else None
            ),
            stopped_then_target_reached=sum(int(row["stopped_then_target_reached"]) for row in rows),
            by_mode=by_mode,
            breakdowns=breakdowns,
        )

    def _prune(self) -> None:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM signal_outcomes").fetchone()
        excess = max(0, int(row["count"]) - self._history_limit)
        if not excess:
            return
        terminal = (
            SignalState.TP2_HIT.value,
            SignalState.STOPPED.value,
            SignalState.SL_HIT.value,
            SignalState.MISSED.value,
            SignalState.INVALIDATED.value,
            SignalState.EXPIRED.value,
        )
        self._connection.execute(
            """
            DELETE FROM signal_outcomes WHERE signal_id IN (
                SELECT signal_id FROM signal_outcomes
                WHERE state IN (?, ?, ?, ?, ?, ?) ORDER BY created_at ASC LIMIT ?
            )
            """,
            (*terminal, excess),
        )

    def close(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.close()
