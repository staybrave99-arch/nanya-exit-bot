"""技術指標。全部是純函式，方便單元測試。

刻意不引入 pandas / numpy：資料量很小（幾百根 K），
標準庫就夠，容器可以壓到很小、冷啟動也快。
"""
from __future__ import annotations

from .models import Bar, Snapshot


def sma(values: list[float], period: int, index: int | None = None) -> float | None:
    """簡單移動平均。index 預設為最後一根。"""
    i = len(values) - 1 if index is None else index
    if i < 0 or i + 1 < period:
        return None
    window = values[i - period + 1: i + 1]
    return sum(window) / period


def true_ranges(bars: list[Bar]) -> list[float]:
    """每根 K 的真實區間。第一根沒有前收，用當日高低差。"""
    if not bars:
        return []
    out = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        out.append(max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        ))
    return out


def atr_wilder(bars: list[Bar], period: int = 14, index: int | None = None) -> float | None:
    """Wilder 平滑 ATR。

    初值 = 第 1..period 根 TR 的算術平均（跳過第 0 根，因為它沒有前收），
    之後 a = (a*(period-1) + TR) / period。
    """
    i = len(bars) - 1 if index is None else index
    if i < period:          # 需要至少 period+1 根才有意義
        return None
    tr = true_ranges(bars)
    a = sum(tr[1: period + 1]) / period
    for k in range(period + 1, i + 1):
        a = (a * (period - 1) + tr[k]) / period
    return a


def highest_close(bars: list[Bar], lookback: int, index: int | None = None) -> float | None:
    """近 N 個交易日的最高『收盤』價（不是最高價）。"""
    i = len(bars) - 1 if index is None else index
    if i < 0:
        return None
    start = max(0, i - lookback + 1)
    return max(b.close for b in bars[start: i + 1])


def weekly_bars(bars: list[Bar]) -> list[tuple[str, float]]:
    """把日 K 壓成 (該週最後交易日, 週收盤)。用 ISO 年-週分組。"""
    import datetime as _dt
    buckets: dict[tuple[int, int], tuple[str, float]] = {}
    for b in bars:
        d = _dt.date.fromisoformat(b.date)
        key = d.isocalendar()[:2]
        buckets[key] = (b.date, b.close)   # 後面的覆蓋前面的 → 留下該週最後一筆
    return [buckets[k] for k in sorted(buckets)]


def snapshot(bars: list[Bar], plan, index: int | None = None) -> Snapshot:
    """算出某一日收盤後的完整指標快照。"""
    i = len(bars) - 1 if index is None else index
    sub = bars[: i + 1]
    closes = [b.close for b in sub]

    ma_f = sma(closes, plan.ma_fast, i)
    ma_s = sma(closes, plan.ma_slow, i)
    atr = atr_wilder(sub, plan.atr_period, i)
    h20 = highest_close(sub, plan.chandelier_lookback, i)

    fast_stop = slow_stop = None
    if h20 is not None and atr is not None:
        fast_stop = h20 - plan.fast_k * atr
        slow_stop = h20 - plan.slow_k * atr

    wk = weekly_bars(sub)
    wk_closes = [c for _, c in wk]
    w_ma = sma(wk_closes, plan.weekly_ma) if len(wk_closes) >= plan.weekly_ma else None

    prev_close = sub[i - 1].close if i >= 1 else None
    return Snapshot(
        date=sub[i].date,
        close=sub[i].close,
        prev_close=prev_close,
        change_pct=None if prev_close in (None, 0) else (sub[i].close / prev_close - 1) * 100,
        ma_fast=ma_f,
        ma_slow=ma_s,
        atr=atr,
        h_lookback=h20,
        fast_stop=fast_stop,
        slow_stop=slow_stop,
        bias=None if not ma_f else sub[i].close / ma_f - 1,
        weekly_ma=w_ma,
        weekly_close=wk_closes[-1] if wk_closes else None,
    )
