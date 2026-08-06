"""策略回測與穩健度評估。

跟實盤完全獨立——只讀市場資料，重用 engine.replay() 跑過候選策略，
不會碰 state.json、不會推播。

方法：只有一段（約一年）真實歷史，直接拿來下結論風險很高（這條路徑
剛好長這樣，換一種劇本結果可能差很多）。所以用 block bootstrap 從這段
歷史的統計特性合成很多條新路徑（保留區塊內部的日內振幅/跳空關係，只是
重新洗牌接起來），每個候選策略都拿去跑過同一組合成路徑，比較的重點是
「結果的穩定程度」（變異係數 CV），不是單一路徑的最高報酬。
"""
from __future__ import annotations

import datetime as dt
import math
import random
import statistics

from .config import Plan
from .engine import replay as do_replay
from .models import Bar
from .state import State


def _synthetic_dates(n: int, start: dt.date) -> list[str]:
    """產生 n 個工作日日期字串，純粹當索引用，不代表真實交易日。"""
    out: list[str] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def block_bootstrap_bars(source: list[Bar], length: int, block_size: int,
                         rng: random.Random,
                         anchor_close: float | None = None,
                         target_drift: float = 0.0) -> list[Bar]:
    """把歷史 K 棒切成連續區塊、隨機抽樣重組成一條新的合成路徑。

    保留區塊內部的日內振幅/跳空關係跟日對日的相對走勢（不是逐日打散
    報酬率重建，那樣會抹掉波動叢聚的特性），但每個區塊會先「去除自己
    的平均趨勢」再接上去——原始歷史這段期間漲了近 10 倍（日均對數報酬
    約 +1%），如果照抽到的區塊原始漲跌幅直接複利串接，180 天的合成
    路徑會系統性地指數爆炸，變成怎麼比都是「早賣的都輸給抱到最後」，
    這不是策略比較，是複利本身造成的假象。去除趨勢後，區塊內部日與日
    的相對震盪（波動特性）還在，預設（target_drift=0）不會預設任何
    方向——這是刻意的：未來會漲會跌不是這個函式該猜的事。

    target_drift：每日對數報酬的目標值，用來做「如果未來偏多/偏空」
    的情境分析——不是預測，是讓你自己選一個方向假設，看策略排名會不會
    因此翻盤。實作上是在去趨勢路徑算完之後，疊加一個固定方向的漂移，
    不會跟區塊本身的去趨勢邏輯混在一起算，避免兩邊互相干擾。

    anchor_close 預設用 source 最後一天的收盤價（=「今天」的實際價格），
    讓階梯/硬地板這類絕對價位的門檻在合成路徑上仍然是有意義的相對位置。
    """
    if block_size <= 0 or block_size > len(source):
        raise ValueError(f"block_size 必須是 1..{len(source)}，收到 {block_size}")
    if anchor_close is None:
        anchor_close = source[-1].close

    dates = _synthetic_dates(length, dt.date(2030, 1, 1))
    out: list[Bar] = []
    running_close = anchor_close
    while len(out) < length:
        start = rng.randrange(0, len(source) - block_size + 1)
        block = source[start: start + block_size]
        block_open = block[0].open
        block_drift = math.log(block[-1].close / block_open) / len(block)
        anchor_scale = running_close / block_open
        for j, b in enumerate(block):
            if len(out) >= length:
                break
            scale = anchor_scale * math.exp(-block_drift * (j + 1))
            out.append(Bar(
                date=dates[len(out)],
                open=round(b.open * scale, 2), high=round(b.high * scale, 2),
                low=round(b.low * scale, 2), close=round(b.close * scale, 2),
                volume=b.volume,
            ))
        running_close = out[-1].close

    if target_drift:
        out = [Bar(
            date=b.date,
            open=round(b.open * math.exp(target_drift * (i + 1)), 2),
            high=round(b.high * math.exp(target_drift * (i + 1)), 2),
            low=round(b.low * math.exp(target_drift * (i + 1)), 2),
            close=round(b.close * math.exp(target_drift * (i + 1)), 2),
            volume=b.volume,
        ) for i, b in enumerate(out)]
    return out


def run_plan(plan: Plan, bars: list[Bar]) -> dict:
    """跑一次完整重播，回傳這條路徑上的績效指標。"""
    state = State.new(plan)
    do_replay(state, bars, plan)
    return _metrics(state, bars, plan)


def _metrics(state: State, bars: list[Bar], plan: Plan) -> dict:
    sold = state.sold_lots
    total = int(state.data["total_lots"])
    remaining = state.remaining
    final_close = bars[-1].close
    avg = state.realised_avg_price()

    # 已實現部分照實際成交均價，還沒賣的部分用最後一天收盤價估（mark-to-market）
    portfolio_per_lot = (((avg or 0.0) * sold + final_close * remaining) / total
                        if total else final_close)

    active = [b for b in bars if b.date > plan.start_date]
    period_high = max((b.close for b in active), default=final_close)

    capture_ratio = portfolio_per_lot / period_high if period_high else None
    vs_buy_hold = portfolio_per_lot / final_close - 1 if final_close else None

    return {
        "sold_lots": sold, "remaining_lots": remaining, "closed": state.closed,
        "realised_avg": avg, "portfolio_per_lot": portfolio_per_lot,
        "period_high": period_high,
        "capture_ratio": capture_ratio, "vs_buy_hold": vs_buy_hold,
    }


def _summary(values: list[float]) -> dict:
    """平均、標準差、變異係數（CV，穩健度看這個）、與 p10/p50/p90。"""
    if not values:
        return {"n": 0, "mean": None, "std": None, "cv": None,
                "p10": None, "p50": None, "p90": None}
    ordered = sorted(values)
    mean = statistics.fmean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    cv = (std / abs(mean)) if mean else None

    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
        return ordered[idx]

    return {"n": len(values), "mean": mean, "std": std, "cv": cv,
            "p10": pct(0.10), "p50": pct(0.50), "p90": pct(0.90)}


def compare_strategies(strategies: dict[str, Plan], source_bars: list[Bar],
                       n_paths: int = 200, path_length: int = 180,
                       block_size: int = 10, seed: int = 42,
                       target_drift: float = 0.0) -> dict[str, dict]:
    """每個策略都拿去跑過同一組合成路徑（成對比較，公平）。"""
    rng = random.Random(seed)
    paths = [block_bootstrap_bars(source_bars, path_length, block_size, rng,
                                  target_drift=target_drift)
             for _ in range(n_paths)]

    results: dict[str, dict] = {}
    for name, plan in strategies.items():
        capture_ratios, vs_buy_holds, closed = [], [], 0
        for path in paths:
            m = run_plan(plan, path)
            if m["capture_ratio"] is not None:
                capture_ratios.append(m["capture_ratio"])
            if m["vs_buy_hold"] is not None:
                vs_buy_holds.append(m["vs_buy_hold"])
            closed += int(m["closed"])
        results[name] = {
            "n_paths": len(paths),
            "closed_rate": closed / len(paths) if paths else None,
            "capture_ratio": _summary(capture_ratios),
            "vs_buy_hold": _summary(vs_buy_holds),
        }
    return results


# 情境標籤 → 每日對數報酬漂移。不是預測，是給敏感度分析用的固定假設；
# ±0.0015（約 ±0.15%/日）年化大約 ±35~45%，對這檔波動股不算誇張，
# 但也不到「重演過去十倍漲幅」那種極端。
SCENARIOS: dict[str, float] = {
    "偏空": -0.0015,
    "中性": 0.0,
    "偏多": 0.0015,
}
