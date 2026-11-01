"""Strategies ported 1:1 from the box engine (trading/engine/strategies/core.py and vento_oos_4h.py). PAPER ONLY.
on_candle(symbol, tf, candles) gets CLOSED candles (dicts t,o,h,l,c,v oldest->newest) and returns None or a Signal."""
from dataclasses import dataclass
@dataclass
class Signal:
    side: str; stop: float = None; reason: str = ""; take_profit: float = None
def ema_series(vals, n):
    out = [None] * len(vals)
    if len(vals) < n: return out
    k = 2 / (n + 1); e = sum(vals[:n]) / n; out[n - 1] = e
    for i in range(n, len(vals)): e = vals[i] * k + e * (1 - k); out[i] = e
    return out
def atr(c, n=14):
    if len(c) < n + 1: return None
    trs = [max(c[i]['h'] - c[i]['l'], abs(c[i]['h'] - c[i - 1]['c']), abs(c[i]['l'] - c[i - 1]['c'])) for i in range(1, len(c))]
    return sum(trs[-n:]) / n
def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    g = l = 0.0
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]; g += max(d, 0); l += max(-d, 0)
    if l == 0: return 100.0
    return 100 - 100 / (1 + (g / n) / (l / n))
class Strategy:
    name = "base"; timeframes = ("5m",); symbols4h = ()
    def on_candle(self, s, tf, c): return None
    def load(self, d): pass
    def dump(self): return {}
class EmaCrossATR(Strategy):
    name = "ema_cross_atr"; timeframes = ("15m",)
    def on_candle(self, s, tf, c):
        if len(c) < 60: return None
        cl = [x['c'] for x in c]; f = ema_series(cl, 9); sl = ema_series(cl, 21); a = atr(c)
        if f[-2] <= sl[-2] and f[-1] > sl[-1]: return Signal('buy', stop=cl[-1] - 2 * a, reason=f"EMA9 x> EMA21, ATR={a:.6g}")
        if f[-2] >= sl[-2] and f[-1] < sl[-1]: return Signal('sell', reason="EMA9 x< EMA21")
class Breakout(Strategy):
    name = "breakout"; timeframes = ("5m",)
    def on_candle(self, s, tf, c):
        if len(c) < 25: return None
        prev = c[-21:-1]; hi = max(x['h'] for x in prev); lo = min(x['l'] for x in prev); a = atr(c)
        if c[-1]['c'] > hi: return Signal('buy', stop=c[-1]['c'] - 1.5 * a, take_profit=c[-1]['c'] + 3 * a, reason=f"close>20-bar high {hi:.6g}")
        if c[-1]['c'] < lo: return Signal('sell', reason="close<20-bar low")
class RsiMeanRev(Strategy):
    name = "rsi_meanrev"; timeframes = ("5m",)
    def on_candle(self, s, tf, c):
        if len(c) < 30: return None
        cl = [x['c'] for x in c]; r = rsi(cl); a = atr(c)
        if r < 25: return Signal('buy', stop=cl[-1] - 2 * a, reason=f"RSI {r:.1f}<25")
        if r > 55: return Signal('sell', reason=f"RSI {r:.1f}>55 (revert)")
class Momentum(Strategy):
    name = "momentum"; timeframes = ("15m",)
    def on_candle(self, s, tf, c):
        if len(c) < 30: return None
        roc = c[-1]['c'] / c[-13]['c'] - 1; a = atr(c)
        if roc > 0.015 and c[-1]['c'] > c[-2]['c']: return Signal('buy', stop=c[-1]['c'] - 2 * a, reason=f"ROC12 {roc:+.2%}")
        if roc < 0: return Signal('sell', reason=f"ROC12 {roc:+.2%}")
class VolumeSpike(Strategy):
    name = "volume_spike"; timeframes = ("1m",)
    def on_candle(self, s, tf, c):
        if len(c) < 40: return None
        avg = sum(x['v'] for x in c[-31:-1]) / 30; b = c[-1]
        if avg > 0 and b['v'] > 4 * avg and b['c'] > b['o'] * 1.002:
            a = atr(c); return Signal('buy', stop=b['c'] - 2 * a, take_profit=b['c'] + 3 * a, reason=f"vol {b['v'] / avg:.1f}x avg, green bar")
def _ema(v, n):
    k = 2 / (n + 1); e = v[0]; out = []
    for x in v: e = x * k + e * (1 - k); out.append(e)
    return out
class _Base4h(Strategy):
    """Backtest-passed 4h strategies (2y Binance 4h, IS/OOS 70/30). Buy on 0->1 target, sell on 1->0.
    Hard stop = close - min(15%, 3*ATR14(4h))."""
    timeframes = ("4h",); SYMBOL = ""
    def __init__(self): self.prev = None
    @property
    def symbols4h(self): return (self.SYMBOL,)
    def load(self, d): self.prev = d.get("prev")
    def dump(self): return {"prev": self.prev}
    def target(self, b): raise NotImplementedError
    def on_candle(self, sym, tf, b):
        if sym != self.SYMBOL or len(b) < 150: return None
        t = self.target(b); p = self.prev; self.prev = t; px = b[-1]['c']
        if t == 1 and p != 1: return Signal('buy', stop=px - min(0.15 * px, 3 * atr(b)), reason=f"{self.name} 4h target 1")
        if t == 0 and p == 1: return Signal('sell', reason=f"{self.name} 4h target 0")
class EthEmaCross4h(_Base4h):
    name = "eth_ema50_100_4h"; SYMBOL = "ETHUSDT"
    def target(self, b):
        c = [x['c'] for x in b]; return int(_ema(c, 50)[-1] > _ema(c, 100)[-1])
class SolDonchian4h(_Base4h):
    name = "sol_donchian110_10_4h"; SYMBOL = "SOLUSDT"
    def __init__(self): super().__init__(); self.inpos = 0
    def load(self, d): super().load(d); self.inpos = d.get("inpos", 0)
    def dump(self): return {"prev": self.prev, "inpos": self.inpos}
    def target(self, b):
        c = b[-1]['c']; hi = max(x['h'] for x in b[-111:-1]); lo = min(x['l'] for x in b[-11:-1])
        if self.inpos == 0 and c > hi: self.inpos = 1
        elif self.inpos == 1 and c < lo: self.inpos = 0
        return self.inpos
ALL = [Breakout, EmaCrossATR, Momentum, RsiMeanRev, VolumeSpike, EthEmaCross4h, SolDonchian4h]
