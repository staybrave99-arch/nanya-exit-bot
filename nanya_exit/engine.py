"""四引擎決策邏輯。

每個交易日收盤後跑一次 `process_day`，順序是固定的：

  0. 先執行「昨天收盤觸發、今天開盤成交」的排隊單
  1. 引擎 1 限價階梯 —— 當日最高價碰到就成交（限價單，成交在掛單價）
  2. 引擎 2 乖離過熱 —— 收盤乖離達標，成交在收盤價
  3. 引擎 4 循環結束總開關 —— 成立就排隊「明日全部出清」
  4. 引擎 3 移動停利 —— 慢線優先於快線；排隊「明日開盤賣出」

引擎 3、4 一律「收盤判斷、隔日開盤執行」，因為盤中假跌破在這檔股票上
太常見（ATR 佔股價 8.5%）。這也是為什麼需要跨日狀態。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import Plan
from .indicators import snapshot
from .models import Bar, Fill, PendingOrder, Snapshot
from .state import State


@dataclass
class DayResult:
    date: str
    snap: Snapshot
    fills: list[Fill] = field(default_factory=list)
    queued: list[PendingOrder] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def triggered(self) -> bool:
        """今天有任何成交或排隊 → 值得用高優先度推播。"""
        return bool(self.fills or self.queued)


def _regime_break(snap: Snapshot, plan: Plan, bars: list[Bar], i: int) -> str | None:
    """引擎 4：循環結束總開關。回傳觸發原因，沒觸發回 None。"""
    if snap.close < plan.hard_floor:
        return f"收盤 {snap.close:.1f} 跌破硬地板 {plan.hard_floor:.0f}"
    if snap.ma_fast is not None and snap.ma_slow is not None and snap.ma_fast < snap.ma_slow:
        return f"MA{plan.ma_fast} {snap.ma_fast:.1f} 跌破 MA{plan.ma_slow} {snap.ma_slow:.1f}（死亡交叉）"
    if snap.weekly_ma is not None:
        from .indicators import weekly_bars, sma
        wk = weekly_bars(bars[: i + 1])
        closes = [c for _, c in wk]
        need = plan.weekly_breach_weeks
        if len(closes) >= plan.weekly_ma + need - 1:
            breached = 0
            for j in range(len(closes) - 1, -1, -1):
                m = sma(closes, plan.weekly_ma, j)
                if m is not None and closes[j] < m:
                    breached += 1
                else:
                    break
                if breached >= need:
                    return f"週線連 {need} 週收在 {plan.weekly_ma} 週均線之下"
    return None


def process_day(state: State, bars: list[Bar], plan: Plan, index: int) -> DayResult:
    """處理第 index 根 K（收盤後）。會就地修改 state。"""
    bar = bars[index]
    snap = snapshot(bars, plan, index)
    res = DayResult(date=bar.date, snap=snap)

    if state.closed:
        res.skipped = True
        res.notes.append("部位已全部出清，無動作")
        return res
    if bar.date <= plan.start_date:
        # 起算日當天掛單還沒送出去，不可能成交。少了這道守衛，
        # 8/5 的最高價 470.5 會讓 R1(470) 憑空成交。
        res.skipped = True
        res.notes.append(f"{bar.date} 為計畫起算日（含）之前，不處理")
        return res
    if state.already_processed(bar.date):
        res.skipped = True
        res.notes.append(f"{bar.date} 已處理過，跳過（冪等保護）")
        return res

    # ── 0. 執行昨日排隊、今日開盤成交的單 ─────────────────────
    for order in state.pending_orders():
        if order.signal_date >= bar.date:
            continue                                    # 還沒到執行日
        lots = order.lots if order.lots is not None else state.remaining
        if lots <= 0:
            continue
        f = state.fill(order.tranche_id, lots, bar.open, bar.date,
                       f"{order.reason}（{order.signal_date} 訊號，今日開盤 {bar.open:.1f} 執行）")
        res.fills.append(f)
    state.clear_next_session()
    if state.closed:
        state.mark_processed(bar.date)
        return res

    # ── 1. 引擎 1：限價階梯 ───────────────────────────────────
    for t in list(state.pending_tranches("ladder")):
        price = t["trigger"]["price"]
        if bar.high >= price:
            f = state.fill(t["id"], t["lots"], price, bar.date,
                           f"最高價 {bar.high:.1f} 觸及階梯 {price:.0f}")
            res.fills.append(f)
    if state.closed:
        state.mark_processed(bar.date)
        return res

    # ── 2. 引擎 2：乖離過熱 ───────────────────────────────────
    if snap.bias is not None:
        for t in sorted(state.pending_tranches("bias"),
                        key=lambda x: x["trigger"]["threshold"]):
            if snap.bias >= t["trigger"]["threshold"]:
                f = state.fill(t["id"], t["lots"], bar.close, bar.date,
                               f"乖離 {snap.bias:+.1%} ≥ {t['trigger']['threshold']:+.0%}")
                res.fills.append(f)
    if state.closed:
        state.mark_processed(bar.date)
        return res

    # ── 3. 引擎 4：循環結束總開關（優先於停利）─────────────────
    reason = _regime_break(snap, plan, bars, index)
    if reason:
        slow = state.tranche("SLOW")
        if slow and slow["status"] == "pending":
            o = PendingOrder("SLOW", "regime_break", None, bar.date, f"循環結束訊號：{reason}")
            state.queue_next_session(o)
            res.queued.append(o)
            state.cancel_remaining_tranches(f"循環結束：{reason}")
            res.notes.append(f"⚠ 引擎 4 觸發：{reason}")
            state.mark_processed(bar.date)
            return res

    # ── 4. 引擎 3：移動停利（慢線優先）─────────────────────────
    if snap.slow_stop is not None and bar.close < snap.slow_stop:
        slow = state.tranche("SLOW")
        if slow and slow["status"] == "pending":
            o = PendingOrder("SLOW", "chandelier_slow", None, bar.date,
                             f"收盤 {bar.close:.1f} 跌破慢停利線 {snap.slow_stop:.1f}")
            state.queue_next_session(o)
            res.queued.append(o)
            state.cancel_remaining_tranches("慢停利線觸發")
            res.notes.append(f"⚠ 慢停利線觸發，明日開盤全部出清（剩 {state.remaining} 張）")
            state.mark_processed(bar.date)
            return res

    if snap.fast_stop is not None and bar.close < snap.fast_stop:
        fast = state.tranche("FAST")
        if fast and fast["status"] == "pending":
            lots = min(fast["lots"], state.remaining)
            o = PendingOrder("FAST", "chandelier_fast", lots, bar.date,
                             f"收盤 {bar.close:.1f} 跌破快停利線 {snap.fast_stop:.1f}")
            state.queue_next_session(o)
            res.queued.append(o)
            res.notes.append(f"⚠ 快停利線觸發，明日開盤賣出 {lots} 張")

    state.mark_processed(bar.date)
    return res


def replay(state: State, bars: list[Bar], plan: Plan,
           upto: str | None = None) -> list[DayResult]:
    """從計畫起算日的『次一個交易日』開始逐日重播到 upto（含）。

    起算日當天不處理 —— 那天是計畫成立的日子，掛單還沒送出去。
    """
    results: list[DayResult] = []
    start = dt.date.fromisoformat(plan.start_date)
    for i, b in enumerate(bars):
        d = dt.date.fromisoformat(b.date)
        if d <= start:
            continue
        if upto and b.date > upto:
            break
        results.append(process_day(state, bars, plan, i))
    return results
