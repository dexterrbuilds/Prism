# Prism Signal Engine

Prism is a deterministic, asynchronous crypto-futures technical-analysis research and signal engine. It scans a deliberately small USD-M watchlist, analyzes only closed candles, and publishes only confirmed high-confluence plans with structural risk. It does not place orders or treat a score as a win probability. V2 can optionally call an external AI endpoint as a conservative entry-quality filter; deterministic analysis and hard risk rules remain authoritative.

## Architecture

The runtime owns one persistent CCXT client. Each scan fetches at most 250 closed candles for `4h`, `1h`, and `15m`, plus `5m` only when SCALP mode is enabled, with bounded concurrency and retry/backoff. NumPy arrays flow through pure analysis functions into modular setup detectors. Candidates pass separate setup and entry-quality gates, structural stop, 2R, opposing-structure, volume, volatility, and higher-timeframe filters. A bounded lifecycle store deduplicates alerts.

All valid candidates for a symbol are ranked before publication. Only one directional thesis can be sent per symbol per scan; overlapping detector labels are shown as supporting setups on that alert. Near-tied opposing directions are rejected as ambiguous. Deduplication is keyed by symbol and direction rather than strategy label.

Trade geometry is validated before publication. LONG plans must satisfy `stop < entry < TP1 <= TP2 < TP3` (when TP3 exists); SHORT plans use the exact inverse. TP1 may use opposing structure only when it lies between entry and the 2R target. Structure beyond 2R is treated as TP3, never mislabeled as TP1.

```text
CCXT -> closed-candle validation -> indicators / regime / structure / zones
     -> momentum / volume / volatility / candles / patterns / divergence / liquidity
     -> setup registry -> category-capped setup score -> structure-derived entry plan
     -> entry-quality score -> strict risk validator -> optional AI quality review
     -> bounded lifecycle + dedupe -> Supabase Postgres / SQLite outcomes
     -> Telegram (or DRY_RUN log)
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
| `TELEGRAM_CHANNEL_IDS` | — | Comma-separated channel destinations: private `-100...` IDs or public `@username` |
| `EXCHANGE` | `binance` | `binance` USD-M futures or `bybit` linear swaps |
| `WATCHLIST` | five requested pairs | Comma-separated CCXT symbols |
| `PORT` | `10000` | Health server port |
| `SCAN_INTERVAL_SECONDS` | `2700` | Delay after the complete watchlist (45 minutes) |
| `LIFECYCLE_MONITOR_SECONDS` | `60` | Batch-ticker cadence for open setups between full scans |
| `REQUEST_CONCURRENCY` | `3` | Maximum simultaneous exchange requests |
| `SEND_WATCH_ALERTS` | `false` | Enable 70–79 WATCH delivery |
| `MINIMUM_VALID_SCORE` | `80` | VALID publication threshold |
| `MINIMUM_ENTRY_SCORE` | `75` | Independent intraday entry-quality activation threshold |
| `SCALP_ENABLED` | `false` | Enable the separate 1H/15M/5M scalp strategy engine |
| `SCALP_MINIMUM_SETUP_SCORE` / `SCALP_MINIMUM_ENTRY_SCORE` | `80` / `75` | Independent scalp gates |
| `DRY_RUN_TRACK_OUTCOMES` | `true` | Persist hypothetical lifecycle and excursion analytics during dry-run |
| `AI_ANALYSIS_ENABLED` | `false` | Enable the optional external AI entry-quality filter |
| `AI_PROVIDER` | `openai_compatible` | V2 provider adapter |
| `AI_API_KEY` / `AI_MODEL` / `AI_ENDPOINT` | — | Required only when AI analysis is enabled |
| `OUTCOME_BACKEND` | `auto` | `auto` selects Postgres when `DATABASE_URL` exists; or force `postgres` / `sqlite` |
| `DATABASE_URL` | — | Supabase/Postgres connection string; use the Supabase Session Pooler URI on Railway |
| `DATABASE_SCHEMA` | `prism` | Isolated schema created for Prism's tables |
| `DATABASE_POOL_MIN` / `MAX` | `1` / `3` | Small async connection pool suitable for a 512 MB instance |
| `DATABASE_SSL_REQUIRE` | `true` | Require TLS for the database connection |
| `SIGNAL_DB_PATH` | `/tmp/prism_signals.db` | Local SQLite fallback path |
| `SIGNAL_HISTORY_LIMIT` | `5000` | Bounded maximum outcome rows |

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

For safe real-market verification, leave `DRY_RUN=true`. It still loads markets, fetches live futures OHLCV, performs the complete analysis, persists hypothetical lifecycle/MAE/MFE when `DRY_RUN_TRACK_OUTCOMES=true`, and logs signals that would be delivered.

For Telegram delivery every 45-minute scan cycle, configure the destination privately in the deployment environment:

```bash
DRY_RUN=false
SCAN_INTERVAL_SECONDS=2700
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_CHAT_ID=<destination chat ID>
TELEGRAM_CHAT_IDS=<additional ID>,<additional ID>
TELEGRAM_CHANNEL_IDS=-1001234567890,@public_channel_username
OUTCOME_BACKEND=postgres
DATABASE_URL=<Supabase Session Pooler connection string>
```

Only deduplicated WATCH alerts (when explicitly enabled), VALID/EXCEPTIONAL signals, and lifecycle events are delivered. A no-trade scan remains silent.

Confirmed signals are persisted as live setups with an exact UTC expiry. Validity is derived per setup from its strategy-aware projected horizon and analysis timeframe, rounded to complete analysis candles; there is no global fixed validity duration. V2 separates directional bias, setup score, and entry quality. A strong thesis remains `WAITING_FOR_ENTRY` until a closed execution-timeframe retest and structure response pass the independent entry gate; a ticker touch alone cannot confirm a V2 entry. Terminal pre-entry states cannot activate after a restart. Only activated setups enter win-rate accounting. TP1 remains a win if the runner later reaches its stop.

Initial signal alerts include the latest closed 15M price, the exact entry trigger, and a bounded 1100×700 PNG chart with the last 80 closed 1H candles, EMA20/EMA50, scored S/R zones, volume, UTC time markers, entry zone, stop, and ordered targets. Charts are rendered only for the selected alert and released after delivery.

TP1, TP2, and pre-TP1 stop alerts include a square performance card with simulated `$5,000` margin PnL at 5×, entry/exit, direction, pair, and hold time. Target detection uses closed 15M candle highs/lows rather than only the sampled close. If entry and stop occur in the same candle and their sequence cannot be known, Prism records the conservative filled-then-stopped outcome.

### Dynamic PnL social cards

Lifecycle results now use a share-ready 1200×1200 PRISM card generator. It separates trade direction from result state, so profitable SHORT, losing SHORT, profitable LONG, and losing LONG cards retain the correct direction color while PnL independently controls positive/negative styling. TP1, TP2, and pre-TP1 stop events render cards; stopped runners are excluded because V1 does not model partial-position allocation after TP1.

The reusable typed API accepts direct trade results and supports entry/exit/mark price, leverage, realized/unrealized PnL, duration, username, quote/context overrides, mascot overrides, configurable mascot thresholds, and actual chart series. When chart data is absent, a deterministic trade-seeded curve is rendered. Runtime generation is local Pillow work—no LLM or network call is used.

```python
from app.models import Direction
from app.telegram.pnl_cards import PnlCardData, generate_pnl_card_async

card_png = await generate_pnl_card_async(
    PnlCardData(
        pair="SOL/USDT",
        direction=Direction.LONG,
        pnl_usd=5_678.90,
        pnl_percent=32.14,
        entry_price=142.65,
        exit_price=151.21,
        leverage=5,
    )
)
```

Eight bundled PRISM mascot states are selected dynamically: `huge-win`, `big-win`, `win`, `confident`, `loss`, `small-loss`, `neutral`, and `streak`. Generate the six-state development preview with:

```bash
python -m scripts.generate_pnl_card_demo
```

Preview PNGs are written to `output/pnl-card-demo/`. Lifecycle cards derived from research signals explicitly display `$5K MARGIN SIMULATION`; direct `PnlCardData` cards do not add that label.

Each configured Telegram recipient receives the same deduplicated alert independently; one failed DM does not prevent delivery to the others. A Telegram user must start the bot first before Telegram permits the bot to initiate that DM.

Channel destinations receive the same chart and signal lifecycle alerts. Add the bot to the channel as an administrator with **Post Messages** permission. Channel destinations are delivery-only: they do not gain access to `/status` or the manual-scan control.

The alert includes hypothetical `$5,000` margin examples at 2× and 5× leverage, showing notional size, approximate base-asset quantity, and linear P/L at stop, TP1, and TP2. These examples exclude fees, funding, slippage, maintenance margin, and liquidation mechanics and are not position-sizing advice.

Signals also include an estimated holding-time range derived from the 1H timeframe, ATR distance to TP2, and setup family. It is explicitly labeled as a technical estimate—not a historical average or probability. A genuine average should be added only after enough lifecycle/backtest outcomes have been collected.

### Telegram commands

- `/start` confirms that Prism is online and displays the exchange, 45-minute cadence, watchlist, and WATCH-alert policy. It does not force an immediate scan or manufacture a signal.
- `/status` displays scanner state, exchange, delivery mode, cadence, last completed scan, completed-symbol count, and cumulative scan errors.
- `/stats`, `/stats 7d`, `/stats 30d`, and `/stats all` report persisted results. TP1 is the binary win threshold; a stop before TP1 is a loss, while pre-entry invalidations are excluded.
- **Run Manual Scan** appears below both command responses. It wakes the normal scanner loop immediately without creating an overlapping scan. If a scan is already running, the request is rejected with a status message.

Commands only respond in configured `TELEGRAM_CHAT_ID` / `TELEGRAM_CHAT_IDS` destinations. The command menu is registered automatically at startup.

## Docker

```bash
docker build -t prism-signal-engine .
docker run --rm -p 10000:10000 --env-file .env prism-signal-engine
```

## Supabase database

Create a Supabase project, open **Connect**, choose **Session pooler**, and copy its Postgres URI. The session pooler uses port `5432` and works with Railway's IPv4 network. Put the complete URI in Railway as the secret `DATABASE_URL`; percent-encode special characters in the database password if constructing the URI manually. Do not use the Supabase project URL, anon key, service-role key, or transaction REST API—Prism connects directly to Postgres.

Prism creates an isolated `prism` schema plus `metadata` and `signal_outcomes` tables on first startup. Lifecycle updates use transactions and row locks, signal IDs are primary keys, and duplicate or stale transitions are rejected. TP1 remains the permanent WIN marker even if a runner later stops; TP2 remains a separate statistic. The pool is intentionally bounded at 1–3 connections and asyncpg's prepared-statement cache is disabled for Supavisor compatibility.

Use these production values:

```bash
OUTCOME_BACKEND=postgres
DATABASE_URL=postgresql://postgres.<project-ref>:<encoded-password>@<pooler-host>:5432/postgres
DATABASE_SCHEMA=prism
DATABASE_POOL_MIN=1
DATABASE_POOL_MAX=3
DATABASE_SSL_REQUIRE=true
SIGNAL_HISTORY_LIMIT=5000
```

Run only one Prism replica. Supabase can handle multiple database clients, but this V1 scanner and Telegram long-polling process deliberately has no distributed leader election; multiple replicas could scan and race to deliver the same alert. The database prevents duplicate outcomes, but it cannot retract an already-sent Telegram message.

## Railway and Render

Railway: create a service from the repository, select the Dockerfile builder, add environment variables, and expose the generated domain. `railway.json` configures `/health`. Use at least one always-on replica if continuous scanning is required; free services that sleep cannot scan while suspended.

With Supabase, do **not** attach a Railway volume and do not set `RAILWAY_RUN_UID`. Set the Postgres variables above and keep exactly one Railway replica. History then survives rebuilds, redeploys, and region changes independently of Railway's filesystem. Tracking begins when this version first starts against that Supabase database; older Telegram alerts cannot be reconstructed automatically.

SQLite remains available for local/offline deployments. If you intentionally use SQLite on Railway, attach a volume at `/home/prism/data`, set `SIGNAL_DB_PATH=/home/prism/data/prism_signals.db`, and set `RAILWAY_RUN_UID=0` because Railway volumes mount as root.

Render: create a Blueprint from `render.yaml`, add Telegram secrets in the dashboard, and switch `DRY_RUN=false` only after inspecting dry-run output. Free web services may suspend due to inactivity, so continuous operation may require a paid always-on instance or an external health ping permitted by the platform's terms.

## Scoring

The 100-point confluence score caps independent evidence families: trend/regime 20, structure 20, location/S&R 15, momentum 10, volume 10, setup/pattern 10, candlestick 5, volatility 5, and higher timeframe 5. RSI/Stochastic/CCI share one momentum cap; EMA families share trend/regime; OBV/MFI/A-D share volume. Scores mean confluence, not probability: below 70 IGNORE, 70–79 WATCH, 80–89 VALID, and 90–100 EXCEPTIONAL. An unclear regime caps the score and requires at least 84.

Entry quality is a separate 100-point gate: location 20, retest 20, execution-timeframe structure 20, structural stop 15, room to target 10, momentum 5, volume 5, and ATR/chase quality 5. Scores below 65 reject the entry, 65–74 wait, 75–84 are valid, and 85+ are high quality. Prism never averages setup and entry scores: the defaults require setup ≥80 and entry ≥75, plus all hard risk rules.

The optional AI adapter receives only a compact JSON summary after deterministic setup, entry, and hard-risk validation. It can return `APPROVE`, `WAIT`, or `REJECT`; it cannot invent setups, reverse direction, rescue sub-threshold entry quality, or override a hard rejection. Reviews are cached in a bounded in-memory LRU. Timeout, HTTP failure, malformed JSON, or provider unavailability returns `UNAVAILABLE` and the deterministic engine continues unchanged.

## Implemented setup classifications

`TREND_PULLBACK`, `EMA_PULLBACK`, `BREAKOUT`, `BREAKDOWN`, `BREAKOUT_RETEST`, `BREAKDOWN_RETEST`, `SUPPORT_BOUNCE`, `RESISTANCE_REJECTION`, `RANGE_LOW_REVERSAL`, `RANGE_HIGH_REVERSAL`, `RANGE_BREAKOUT`, `TRENDLINE_BREAK`, `TRENDLINE_RETEST`, `LIQUIDITY_SWEEP_REVERSAL`, `FAILED_BREAKOUT`, `FAILED_BREAKDOWN`, `BOS_CONTINUATION`, `CHOCH_REVERSAL`, `DIVERGENCE_REVERSAL`, `VOLATILITY_BREAKOUT`, `BOLLINGER_MEAN_REVERSION`, `BULL_FLAG_BREAKOUT`, `BEAR_FLAG_BREAKDOWN`, `TRIANGLE_BREAKOUT`, `WEDGE_BREAKOUT`, `DOUBLE_BOTTOM_REVERSAL`, `DOUBLE_TOP_REVERSAL`, `HEAD_AND_SHOULDERS`, `INVERSE_HEAD_AND_SHOULDERS`, and `MOMENTUM_CONTINUATION`.

SCALP mode adds `SCALP_LIQUIDITY_SWEEP_RECLAIM`, `SCALP_BREAKOUT_RETEST`, `SCALP_FAILED_BREAKOUT`, `SCALP_FAILED_BREAKDOWN`, `SCALP_SUPPORT_REJECTION`, `SCALP_RESISTANCE_REJECTION`, `SCALP_RANGE_LOW_LONG`, `SCALP_RANGE_HIGH_SHORT`, `SCALP_EMA_PULLBACK`, `SCALP_VWAP_RECLAIM`, `SCALP_VWAP_REJECTION`, `SCALP_BOS_CONTINUATION`, `SCALP_CHOCH_REVERSAL`, `SCALP_MOMENTUM_CONTINUATION`, and `SCALP_VOLATILITY_EXPANSION`.

## Known V1 limitations

- Daily/weekly levels are derived from bounded 4H history, so the previous week may be unavailable after large exchange gaps.
- Trendline detection uses confirmed pivot boundaries rather than discretionary hand-drawn lines.
- Pattern recognition is intentionally conservative and lightweight; weak geometry returns no pattern.
- Open signal state and outcomes are restored from a bounded Supabase Postgres ledger, or from SQLite when that fallback is selected.
- Live WR is an operational signal statistic, not a historical backtest or calibrated probability. Small samples are explicitly flagged.
- No open interest, funding, liquidation, positioning, execution, or historical backtest database is included.
- Free-tier egress, exchange availability, and sleeping policies vary by region/provider.

The expected steady-state application memory is roughly 90–180 MB depending on CCXT/PTB versions and allocator behavior. Candle matrices themselves are tiny (about 180 KB per full five-symbol scan before indicator arrays); caches and signal history are bounded.

The recommended next step is a walk-forward backtest adapter that feeds historical `MarketSnapshot(as_of_ms=...)` slices through the exact same `analyze_snapshot` and setup/scoring/risk functions, records outcomes by strategy/regime/symbol, then calibrates thresholds without changing features on the test window.
