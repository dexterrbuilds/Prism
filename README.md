# Prism Signal Engine

Prism is a deterministic, asynchronous crypto-futures technical-analysis research and signal engine. It scans a deliberately small USD-M watchlist, analyzes only closed candles, and publishes only confirmed high-confluence plans with structural risk. It does not place orders, use an LLM, or treat a confluence score as a win probability.

## Architecture

The runtime owns one persistent CCXT client. Each scan fetches at most 250 closed candles for `4h`, `1h`, and `15m`, with bounded concurrency and retry/backoff. NumPy arrays flow through pure analysis functions into modular setup detectors. Candidates pass category-capped scoring, entry/chase, structural stop, 2R, opposing-structure, volume, volatility, and higher-timeframe filters. A bounded lifecycle store deduplicates alerts.

All valid candidates for a symbol are ranked before publication. Only one directional thesis can be sent per symbol per scan; overlapping detector labels are shown as supporting setups on that alert. Near-tied opposing directions are rejected as ambiguous. Deduplication is keyed by symbol and direction rather than strategy label.

Trade geometry is validated before publication. LONG plans must satisfy `stop < entry < TP1 <= TP2 < TP3` (when TP3 exists); SHORT plans use the exact inverse. TP1 may use opposing structure only when it lies between entry and the 2R target. Structure beyond 2R is treated as TP3, never mislabeled as TP1.

```text
CCXT -> closed-candle validation -> indicators / regime / structure / zones
     -> momentum / volume / volatility / candles / patterns / divergence / liquidity
     -> setup registry -> category-capped score -> risk plan -> strict validator
     -> bounded lifecycle + dedupe -> Telegram (or DRY_RUN log)
```

`FastAPI`, the scanner, and `python-telegram-bot` run concurrently on one asyncio loop. SIGTERM/SIGINT stops Uvicorn and polling, then closes CCXT.

## Installation

Python 3.11 or newer is required (3.12 is used by the Docker image).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

TA-Lib 0.6.x publishes wheels for common Linux/macOS platforms that include the native library. If your platform builds from source, install the TA-Lib C library and compiler headers first (`brew install ta-lib` on macOS; on Linux, install/build `libta-lib` for the base distribution), then rerun pip. The provided Dockerfile requires a binary TA-Lib wheel, preventing a silently inconsistent source build.

## Configuration

All secrets come from the environment. Load `.env` with your process manager or shell; the app deliberately does not include an implicit dotenv loader.

| Variable | Default | Meaning |
|---|---:|---|
| `DRY_RUN` | `true` | Analyze real data and log would-send events without Telegram delivery |
| `TELEGRAM_BOT_TOKEN` | — | Required when dry-run is false |
| `TELEGRAM_CHAT_ID` | — | Backward-compatible primary recipient |
| `TELEGRAM_CHAT_IDS` | — | Comma-separated DM/group recipients; de-duplicated with the primary ID |
| `EXCHANGE` | `binance` | `binance` USD-M futures or `bybit` linear swaps |
| `WATCHLIST` | five requested pairs | Comma-separated CCXT symbols |
| `PORT` | `10000` | Health server port |
| `SCAN_INTERVAL_SECONDS` | `2700` | Delay after the complete watchlist (45 minutes) |
| `REQUEST_CONCURRENCY` | `3` | Maximum simultaneous exchange requests |
| `SEND_WATCH_ALERTS` | `false` | Enable 70–79 WATCH delivery |
| `MINIMUM_VALID_SCORE` | `80` | VALID publication threshold |

The remaining tunables are documented in `.env.example`.

## Run and verify

```bash
set -a; source .env; set +a
python -m app.main
curl http://localhost:10000/health
pytest
ruff check app tests
mypy app
```

For safe real-market verification, leave `DRY_RUN=true`. It still loads markets, fetches live futures OHLCV, performs the complete analysis, and logs signals that would be delivered.

For Telegram delivery every 45-minute scan cycle, configure the destination privately in the deployment environment:

```bash
DRY_RUN=false
SCAN_INTERVAL_SECONDS=2700
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_CHAT_ID=<destination chat ID>
TELEGRAM_CHAT_IDS=<additional ID>,<additional ID>
```

Only deduplicated WATCH alerts (when explicitly enabled), VALID/EXCEPTIONAL signals, and lifecycle events are delivered. A no-trade scan remains silent.

Initial signal alerts include a bounded 1100×700 PNG chart with the last 80 closed 1H candles, EMA20/EMA50, scored S/R zones, volume, UTC time markers, entry zone, stop, and ordered targets. Charts are rendered only for the selected alert and released after delivery.

Each configured Telegram recipient receives the same deduplicated alert independently; one failed DM does not prevent delivery to the others. A Telegram user must start the bot first before Telegram permits the bot to initiate that DM.

The alert includes hypothetical `$5,000` margin examples at 2× and 5× leverage, showing notional size, approximate base-asset quantity, and linear P/L at stop, TP1, and TP2. These examples exclude fees, funding, slippage, maintenance margin, and liquidation mechanics and are not position-sizing advice.

Signals also include an estimated holding-time range derived from the 1H timeframe, ATR distance to TP2, and setup family. It is explicitly labeled as a technical estimate—not a historical average or probability. A genuine average should be added only after enough lifecycle/backtest outcomes have been collected.

### Telegram commands

- `/start` confirms that Prism is online and displays the exchange, 45-minute cadence, watchlist, and WATCH-alert policy. It does not force an immediate scan or manufacture a signal.
- `/status` displays scanner state, exchange, delivery mode, cadence, last completed scan, completed-symbol count, and cumulative scan errors.
- **Run Manual Scan** appears below both command responses. It wakes the normal scanner loop immediately without creating an overlapping scan. If a scan is already running, the request is rejected with a status message.

Commands only respond in configured `TELEGRAM_CHAT_ID` / `TELEGRAM_CHAT_IDS` destinations. The command menu is registered automatically at startup.

## Docker

```bash
docker build -t prism-signal-engine .
docker run --rm -p 10000:10000 --env-file .env prism-signal-engine
```

## Railway and Render

Railway: create a service from the repository, select the Dockerfile builder, add environment variables, and expose the generated domain. `railway.json` configures `/health`. Use at least one always-on replica if continuous scanning is required; free services that sleep cannot scan while suspended.

Render: create a Blueprint from `render.yaml`, add Telegram secrets in the dashboard, and switch `DRY_RUN=false` only after inspecting dry-run output. Free web services may suspend due to inactivity, so continuous operation may require a paid always-on instance or an external health ping permitted by the platform's terms.

## Scoring

The 100-point confluence score caps independent evidence families: trend/regime 20, structure 20, location/S&R 15, momentum 10, volume 10, setup/pattern 10, candlestick 5, volatility 5, and higher timeframe 5. RSI/Stochastic/CCI share one momentum cap; EMA families share trend/regime; OBV/MFI/A-D share volume. Scores mean confluence, not probability: below 70 IGNORE, 70–79 WATCH, 80–89 VALID, and 90–100 EXCEPTIONAL. An unclear regime caps the score and requires at least 84.

## Implemented setup classifications

`TREND_PULLBACK`, `EMA_PULLBACK`, `BREAKOUT`, `BREAKDOWN`, `BREAKOUT_RETEST`, `BREAKDOWN_RETEST`, `SUPPORT_BOUNCE`, `RESISTANCE_REJECTION`, `RANGE_LOW_REVERSAL`, `RANGE_HIGH_REVERSAL`, `RANGE_BREAKOUT`, `TRENDLINE_BREAK`, `TRENDLINE_RETEST`, `LIQUIDITY_SWEEP_REVERSAL`, `FAILED_BREAKOUT`, `FAILED_BREAKDOWN`, `BOS_CONTINUATION`, `CHOCH_REVERSAL`, `DIVERGENCE_REVERSAL`, `VOLATILITY_BREAKOUT`, `BOLLINGER_MEAN_REVERSION`, `BULL_FLAG_BREAKOUT`, `BEAR_FLAG_BREAKDOWN`, `TRIANGLE_BREAKOUT`, `WEDGE_BREAKOUT`, `DOUBLE_BOTTOM_REVERSAL`, `DOUBLE_TOP_REVERSAL`, `HEAD_AND_SHOULDERS`, `INVERSE_HEAD_AND_SHOULDERS`, and `MOMENTUM_CONTINUATION`.

## Known V1 limitations

- Daily/weekly levels are derived from bounded 4H history, so the previous week may be unavailable after large exchange gaps.
- Trendline detection uses confirmed pivot boundaries rather than discretionary hand-drawn lines.
- Pattern recognition is intentionally conservative and lightweight; weak geometry returns no pattern.
- Signal state is memory-only and resets on restart. It is bounded to remain suitable for a 512 MB service.
- No open interest, funding, liquidation, positioning, execution, or backtest database is included.
- Free-tier egress, exchange availability, and sleeping policies vary by region/provider.

The expected steady-state application memory is roughly 90–180 MB depending on CCXT/PTB versions and allocator behavior. Candle matrices themselves are tiny (about 180 KB per full five-symbol scan before indicator arrays); caches and signal history are bounded.

The recommended next step is a walk-forward backtest adapter that feeds historical `MarketSnapshot(as_of_ms=...)` slices through the exact same `analyze_snapshot` and setup/scoring/risk functions, records outcomes by strategy/regime/symbol, then calibrates thresholds without changing features on the test window.
