# Vento Labs paper desk (cloud, always-on)

**PAPER TRADING ONLY.** No exchange accounts, no API keys, no orders, no wallets, no real money. This is a forward test of the strategies in this repo against live public market data. It is not financial advice.

## What it does
- A scheduled GitHub Actions workflow (`.github/workflows/paper-desk.yml`) runs `desk.py` about every 10 minutes. GitHub can delay scheduled runs; that's fine.
- Each run downloads closed candles from public market-data endpoints (Binance's public mirror `data-api.binance.vision`; falls back to Kraken's public OHLC API). No keys are used.
- It **replays every closed 1-minute bar since the previous run** (up to 50 h of catch-up, per symbol). Hard stops and take-profits are checked against each bar's low/high, and strategies fire on their 1m/5m/15m/4h bar closes. Delays between runs therefore leave no blind gaps.
- State is committed back to `data/`:
  - `data/STATUS.md`: human-readable equity table
  - `data/status.json`: books, open positions, last run, errors
  - `data/trades.csv`: every simulated fill (fees and slippage included)
  - `data/runs.csv`: one row per run (source, minutes replayed, gap before the run, duration)
  - `data/state.json`: the engine's persistent state

## Strategies (`strategies.py`)
Each book starts with $100 of virtual money. Long-only, at most 25% of equity per position, 0.1% fee, 0.05% slippage, and a hard stop on every position.

| name | timeframe | rule |
|---|---|---|
| eth_ema50_100_4h | 4h | ETHUSDT: long while EMA50 > EMA100 (passed a 2-year in-sample/out-of-sample backtest) |
| sol_donchian110_10_4h | 4h | SOLUSDT: enter when the close tops the prior 110-bar high; exit when it drops under the prior 10-bar low (passed the same backtest) |
| breakout | 5m | close above the 20-bar high, 1.5 ATR stop, 3 ATR target |
| ema_cross_atr | 15m | EMA9/EMA21 cross, 2 ATR stop |
| momentum | 15m | 12-bar rate of change > 1.5% |
| rsi_meanrev | 5m | RSI14 < 25 buy, > 55 sell |
| volume_spike | 1m | volume > 4x its 30-bar average on a green bar |

Universe: BTC, ETH, SOL, TRX plus the top 10 Binance USDT pairs by 24h volume (stablecoins excluded), refreshed daily. Symbols with open positions are always tracked.

## Run locally
`python3 desk.py` (Python 3.9+, stdlib only).
