#!/usr/bin/env python3
"""Vento Labs paper desk, cloud edition. PAPER ONLY: no API keys, no orders, no wallets.
One invocation = one catch-up step. It replays every CLOSED 1m bar since the last run through the
same strategies as the box engine (trading/engine), checks hard stops / take-profits on each 1m bar's
low/high, fires 5m/15m/4h strategies on their bar closes, then saves state to data/.
Run it on a schedule (GitHub Actions cron); gaps between runs are filled from public klines, so jitter is harmless.
Data: Binance public market-data mirror (data-api.binance.vision, no key); falls back to Kraken public OHLC."""
import json, os, sys, time, csv, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from strategies import ALL, Signal
D = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(D, "data")
ART = timezone(timedelta(hours=-3))
CORE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "TRXUSDT"]
FEE = 0.001; SLIP = 0.0005; START = 100.0; MAXPOS = 0.25
BIN = "https://data-api.binance.vision"; KRA = "https://api.kraken.com/0/public"
STABLE = {"USDC","FDUSD","TUSD","BUSD","DAI","USDP","EUR","USD1","XUSD","PAXG","WBTC","USDE","RLUSD","BFUSD"}
KR_FALLBACK = CORE + ["XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","LTCUSDT","DOTUSDT","BCHUSDT","SUIUSDT","PEPEUSDT"]
MAX_CATCHUP_MIN = 3000   # if a run is >50h late, only the last 50h are replayed (gap is logged)
HIST = {"1m": 120, "5m": 300, "15m": 300, "4h": 400}
SEC = {"1m": 60, "5m": 300, "15m": 900, "4h": 14400}
UA = {"User-Agent": "vento-paper-desk/1.0 (+https://github.com/ventolabs-hq/paper-desk)"}

def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 418, 451) or i == tries - 1: raise   # geo-block / ban: don't hammer, fall back
        except Exception:
            if i == tries - 1: raise
        time.sleep(2 * (i + 1))
def art(ts): return datetime.fromtimestamp(ts, ART).strftime("%Y-%m-%d %H:%M:%S ART")
def jload(p, d):
    try: return json.load(open(p))
    except Exception: return d
def jsave(p, o):
    json.dump(o, open(p + ".tmp", "w"), indent=1); os.replace(p + ".tmp", p)

# ---------------- data sources ----------------
class Binance:
    name = "binance"
    def universe(self):
        t = get(BIN + "/api/v3/ticker/24hr")
        u = [x for x in t if x["symbol"].endswith("USDT") and x["symbol"][:-4] not in STABLE and float(x["quoteVolume"]) > 0]
        u.sort(key=lambda x: -float(x["quoteVolume"]))
        return CORE + [x["symbol"] for x in u if x["symbol"] not in CORE][:10]
    def klines(self, sym, tf, start, now):
        """closed bars with open time >= start (seconds), oldest->newest"""
        out = []; s = int(start) * 1000
        for _ in range(6):
            d = get(f"{BIN}/api/v3/klines?symbol={sym}&interval={tf}&startTime={s}&limit=1000")
            bars = [dict(t=x[0] // 1000, o=float(x[1]), h=float(x[2]), l=float(x[3]), c=float(x[4]), v=float(x[5])) for x in d]
            out += [b for b in bars if b["t"] + SEC[tf] <= now]
            if len(d) < 1000: break
            s = d[-1][0] + SEC[tf] * 1000
        return out
class Kraken:
    name = "kraken"; IV = {"1m": 1, "5m": 5, "15m": 15, "4h": 240}
    def universe(self): return KR_FALLBACK
    def pair(self, sym):
        b = sym[:-4]; return ("XBT" if b == "BTC" else "XDG" if b == "DOGE" else b) + "USD"
    def klines(self, sym, tf, start, now):
        d = get(f"{KRA}/OHLC?pair={self.pair(sym)}&interval={self.IV[tf]}&since={int(start) - 1}")
        if d.get("error"): raise RuntimeError(d["error"])
        rows = next(v for k, v in d["result"].items() if k != "last")
        bars = [dict(t=int(x[0]), o=float(x[1]), h=float(x[2]), l=float(x[3]), c=float(x[4]), v=float(x[6])) for x in rows]
        return [b for b in bars if b["t"] >= start and b["t"] + SEC[tf] <= now]

# ---------------- paper books ----------------
# Books cut by the Lead (decisions in money-experiment/decisions/2026-11-03-kill-hf-books.md, 2026-11-05-kill-hf-books-2.md): no new entries, open positions closed at the next bar.
KILLED = {"breakout": "2026-11-03", "volume_spike": "2026-11-03", "rsi_meanrev": "2026-11-05", "ema_cross_atr": "2026-11-05", "momentum": "2026-11-05"}
class Desk:
    def __init__(s):
        os.makedirs(DATA, exist_ok=True)
        s.st = jload(os.path.join(DATA, "state.json"), {})
        s.st.setdefault("books", {}); s.st.setdefault("strat_state", {}); s.st.setdefault("last_bar", None); s.st.setdefault("last_bar_sym", {})
        s.strats = [c() for c in ALL]
        for x in s.strats:
            s.st["books"].setdefault(x.name, dict(cash=START, pos={}, realized=0.0, n=0, wins=0))
            x.load(s.st["strat_state"].get(x.name, {}))
        s.px = s.st.get("px", {}); s.new_trades = []
    def equity(s, b): return b["cash"] + sum(p["qty"] * s.px.get(k, p["entry"]) for k, p in b["pos"].items())
    def log(s, name, sym, action, price, qty, fee, pnl, cash, reason, ts):
        s.new_trades.append(dict(ts_art=art(ts), bar_ts=int(ts), strategy=name, symbol=sym, action=action, price=f"{price:.8g}",
                                 qty=f"{qty:.8g}", fee=f"{fee:.6f}", pnl=f"{pnl:.6f}", cash_after=f"{cash:.4f}", reason=reason))
    def open(s, name, sym, price, sig, ts):
        b = s.st["books"][name]
        if name in KILLED or sym in b["pos"]: return False
        alloc = min(s.equity(b) * MAXPOS, b["cash"] / (1 + FEE))
        if alloc < 1: return False
        fill = price * (1 + SLIP); qty = alloc / fill; fee = fill * qty * FEE
        stop = sig.stop if sig.stop and sig.stop < fill else fill * 0.97
        b["cash"] -= fill * qty + fee
        b["pos"][sym] = dict(qty=qty, entry=fill, stop=stop, tp=sig.take_profit, ts=ts, cost=fill * qty + fee)
        s.log(name, sym, "BUY", fill, qty, fee, 0.0, b["cash"], f"{sig.reason}; stop={stop:.6g}", ts); return True
    def close(s, name, sym, price, reason, ts):
        b = s.st["books"][name]; p = b["pos"].pop(sym)
        fill = price * (1 - SLIP); proceeds = fill * p["qty"] * (1 - FEE); pnl = proceeds - p["cost"]
        b["cash"] += proceeds; b["realized"] += pnl; b["n"] += 1; b["wins"] += int(pnl > 0)
        s.log(name, sym, "SELL", fill, p["qty"], fill * p["qty"] * FEE, pnl, b["cash"], reason, ts)
    def run(s):
        t0 = time.time(); now = int(t0); now_min = now // 60 * 60
        default = s.st["last_bar"] or (now_min - 16 * 60)        # first run: start ~15 min back
        floor = now_min - (MAX_CATCHUP_MIN + 1) * 60
        lbs = s.st["last_bar_sym"]
        def start_of(sym): return max(lbs.get(sym, default), floor) + 60
        gap_min = (now_min - (default + 60)) // 60
        skipped = max(0, gap_min - MAX_CATCHUP_MIN)
        src = None; errs = []
        for cand in (Binance(), Kraken()):
            try:
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if s.st.get("universe_day") != day or s.st.get("source") != cand.name:
                    s.st["universe"] = cand.universe(); s.st["universe_day"] = day
                syms = list(dict.fromkeys(s.st["universe"] + [k for b in s.st["books"].values() for k in b["pos"]]))
                first_new = min(start_of(x) for x in syms)
                data = {}
                def fetch(sym):
                    try:
                        return sym, {tf: cand.klines(sym, tf, start_of(sym) - HIST[tf] * SEC[tf], now)
                                     for tf in ("1m", "5m", "15m") + (("4h",) if any(sym in x.symbols4h for x in s.strats) else ())}
                    except Exception as e: errs.append(f"{cand.name} {sym}: {e}"); return sym, None
                with ThreadPoolExecutor(6) as ex:
                    for sym, v in ex.map(fetch, syms):
                        if v: data[sym] = v
                if len(data) < len(syms) / 2: raise RuntimeError(f"only {len(data)}/{len(syms)} symbols fetched")
                src = cand; break
            except Exception as e: errs.append(f"{cand.name}: {e}")
        if not src:
            s.finish(t0, None, 0, gap_min, skipped, errs); sys.exit(1)
        s.st["source"] = src.name
        # timeline of new 1m bars
        idx = {sym: {tf: {b["t"]: i for i, b in enumerate(v[tf])} for tf in v} for sym, v in data.items()}
        minutes = sorted({b["t"] for v in data.values() for b in v["1m"] if b["t"] >= first_new})
        bars = 0
        for t in minutes:
            close_ts = t + 60
            for sym, v in data.items():
                i = idx[sym]["1m"].get(t)
                if i is None or t < start_of(sym): continue
                bar = v["1m"][i]; bars += 1; s.px[sym] = bar["c"]
                for kn in KILLED:
                    kb = s.st["books"].get(kn)
                    if kb and sym in kb["pos"]: s.close(kn, sym, bar["c"], f"KILLED {KILLED[kn]}", close_ts)
                for name, b in s.st["books"].items():           # hard stops / TPs on the bar's range
                    p = b["pos"].get(sym)
                    if not p: continue
                    if bar["l"] <= p["stop"]: s.close(name, sym, min(bar["o"], p["stop"]), f"HARD STOP {p['stop']:.6g}", close_ts)
                    elif p.get("tp") and bar["h"] >= p["tp"]: s.close(name, sym, max(bar["o"], p["tp"]), f"take profit {p['tp']:.6g}", close_ts)
                for tf in ("1m", "5m", "15m", "4h"):
                    if close_ts % SEC[tf] or tf not in v: continue
                    j = idx[sym][tf].get(close_ts - SEC[tf])
                    if j is None: continue
                    hist = v[tf][max(0, j - HIST[tf] + 1): j + 1]; cbar = hist[-1]
                    for x in s.strats:
                        if tf not in x.timeframes: continue
                        try: sig = x.on_candle(sym, tf, hist)
                        except Exception as e: errs.append(f"{x.name} {sym} {tf}: {e}"); continue
                        if not sig: continue
                        if sig.side == "buy": s.open(x.name, sym, cbar["c"], sig, close_ts)
                        elif sig.side == "sell" and sym in s.st["books"][x.name]["pos"]: s.close(x.name, sym, cbar["c"], sig.reason, close_ts)
                lbs[sym] = t
            s.st["last_bar"] = t
        s.finish(t0, src.name, bars, gap_min, skipped, errs, len(minutes))
    def finish(s, t0, src, bars, gap_min, skipped, errs, minutes=0):
        s.st["px"] = s.px
        s.st["strat_state"] = {x.name: x.dump() for x in s.strats}
        jsave(os.path.join(DATA, "state.json"), s.st)
        if s.new_trades:
            p = os.path.join(DATA, "trades.csv"); new = not os.path.exists(p)
            with open(p, "a", newline="") as f:
                w = csv.DictWriter(f, list(s.new_trades[0])); 
                if new: w.writeheader()
                w.writerows(s.new_trades)
        lb = s.st.get("last_bar")
        run = dict(run_art=art(t0), source=src or "FAILED", minutes_replayed=minutes, bars=bars, gap_min_before_run=gap_min,
                   skipped_min=skipped, new_trades=len(s.new_trades), last_bar_art=art(lb) if lb else "", secs=round(time.time() - t0, 1),
                   errors=len(errs), gha_run=os.environ.get("GITHUB_RUN_ID", "local"))
        p = os.path.join(DATA, "runs.csv"); new = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            w = csv.DictWriter(f, list(run))
            if new: w.writeheader()
            w.writerow(run)
        books = {n: dict(equity=round(s.equity(b), 4), cash=round(b["cash"], 4), realized=round(b["realized"], 4), closed_trades=b["n"], wins=b["wins"],
                         open_positions={k: dict(qty=v["qty"], entry=v["entry"], stop=v["stop"], tp=v.get("tp"), last=s.px.get(k)) for k, v in b["pos"].items()})
                 for n, b in s.st["books"].items()}
        status = dict(updated_art=art(time.time()), last_run=run, symbols=s.st.get("universe"), strategies=books, errors=errs[:20])
        jsave(os.path.join(DATA, "status.json"), status)
        lines = ["# Paper desk status (PAPER ONLY)", "", f"Updated: {status['updated_art']} | source: {run['source']} | last closed 1m bar: {run['last_bar_art']} | gap before run: {gap_min} min", "",
                 "| strategy | equity $ | realized $ | closed | wins | open |", "|---|---|---|---|---|---|"]
        for n, b in books.items():
            lines.append(f"| {n}{' (killed ' + KILLED[n] + ')' if n in KILLED else ''} | {b['equity']:.2f} | {b['realized']:+.2f} | {b['closed_trades']} | {b['wins']} | {', '.join(b['open_positions']) or '-'} |")
        lines += ["", "Each book starts with $100 virtual. Fees 0.1%, slippage 0.05%, long-only, max 25% per position. Not financial advice."]
        open(os.path.join(DATA, "STATUS.md"), "w").write("\n".join(lines) + "\n")
        print(json.dumps(run))
if __name__ == "__main__": Desk().run()
