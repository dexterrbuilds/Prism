from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from app.config import Settings
from app.exchange.client import ExchangeClient
from app.logging_config import configure_logging
from app.signals.reconciliation import HistoricalSignalReconciler
from app.signals.repository import create_outcome_repository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prism-reconcile-signal",
        description="Replay bounded historical candles for exactly one persisted signal ID.",
    )
    parser.add_argument("signal_id", help="Exact immutable signal ID to inspect")
    parser.add_argument(
        "--lookback-hours",
        type=float,
        required=True,
        help="Historical window beginning at signal creation/activation",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist proven transitions; without this flag the command is a read-only preview",
    )
    return parser


async def run(signal_id: str, lookback_hours: float, *, apply: bool) -> int:
    settings = Settings.from_env()
    settings.validate_maintenance()
    configure_logging(settings.log_level)
    repository = await create_outcome_repository(settings)
    exchange = ExchangeClient(settings)
    try:
        await exchange.load_markets()
        report = await HistoricalSignalReconciler(repository, exchange).reconcile(
            signal_id,
            lookback_hours=lookback_hours,
            apply=apply,
        )
        payload = asdict(report)
        payload["initial_state"] = report.initial_state.value
        payload["final_state"] = report.final_state.value
        payload["started_at"] = report.started_at.isoformat()
        payload["ended_at"] = report.ended_at.isoformat()
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        await exchange.close()
        await repository.close()


def main() -> None:
    args = _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.signal_id, args.lookback_hours, apply=args.apply)))
    except (LookupError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
