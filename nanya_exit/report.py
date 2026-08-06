"""把當日結果組成推播訊息與終端輸出。"""
from __future__ import annotations

from .config import Plan
from .engine import DayResult
from .state import State


def _d(v, fmt="{:.1f}", dash="—"):
    return dash if v is None else fmt.format(v)


def _pct(lots: int | None, total: int) -> str:
    """張數換算成佔總部位的百分比。lots=None 代表『剩餘全部』。"""
    if lots is None or total <= 0:
        return "全部部位"
    return f"{round(lots / total * 100)}%"


def notification(res: DayResult, state: State, plan: Plan,
                 chart_url: str = "") -> tuple[str, str, str, int]:
    """回傳 (title, body, tags, priority)。priority 用 ntfy 的 1~5。

    訊息定位是**賣出建議**，不是成交回報——這支程式沒有真的連券商，
    「今日成交」只是規則判斷出來的假設，不代表你真的下了單。所以措辭
    一律用「建議」，張數也改成佔總部位的百分比（不同本金的人都看得懂），
    不顯示已出/剩多少張這種累積狀態——那是帳務追蹤該做的事，不是每日
    建議該包含的內容；真要查目前實際進度，用 `status` 指令看 state.json。

    chart_url 給了的話，本文最後會附一行連結（ntfy 客戶端會自動把網址變成可點）。
    """
    s = res.snap
    md = s.date[5:].replace("-", "/")
    chg = "" if s.change_pct is None else f"（{s.change_pct:+.2f}%）"
    total = plan.total_lots

    lines = [
        f"{plan.name} {md} 收 {s.close:.1f}{chg}",
        f"MA{plan.ma_fast} {_d(s.ma_fast)}｜ATR{plan.atr_period} {_d(s.atr)}｜"
        f"乖離 {'—' if s.bias is None else f'{s.bias:+.1%}'}",
    ]

    if state.closed:
        avg = state.realised_avg_price()
        lines += [
            "──建議：已全部出清──",
            f"建議均價 {_d(avg)}",
            state.data.get("closed_reason") or "",
        ]
        if chart_url:
            lines += ["", f"圖表：{chart_url}"]
        return f"{plan.name} {plan.symbol}｜出清完成", "\n".join(x for x in lines if x), "checkered_flag", 4

    # 今日建議賣出
    if res.fills:
        lines.append("──建議：今日賣出──")
        for f in res.fills:
            lines.append(f"{f.tranche_id} 建議賣出 {_pct(f.lots, total)}　＠{f.price:.1f}")

    # 明日建議掛單
    lines.append("──建議：明日掛單──")
    open_rungs = state.open_ladder()
    if open_rungs:
        chunk = [f"{t['id']} {t['trigger']['price']:.0f}×{_pct(t['lots'], total)}"
                for t in open_rungs]
        for i in range(0, len(chunk), 3):
            lines.append("　".join(chunk[i:i + 3]))
    else:
        lines.append("階梯已用盡，需重設新一組")

    if s.slow_stop is not None:
        fast_live = (state.tranche("FAST") or {}).get("status") == "pending"
        if fast_live and s.fast_stop is not None:
            lines.append(f"停利 {s.fast_stop:.1f} 建議賣 {_pct(plan.fast_lots, total)}"
                         f"／{s.slow_stop:.1f} 全部")
        else:
            lines.append(f"停利 {s.slow_stop:.1f} 全部（快線已執行）")

    # 乖離觸發價
    pending_bias = state.pending_tranches("bias")
    if pending_bias and s.ma_fast:
        bs = "　".join(f"{s.ma_fast * (1 + t['trigger']['threshold']):.0f}" for t in pending_bias)
        lines.append(f"乖離觸發價 {bs}")

    for o in res.queued:
        lines.append(f"⚠ 建議：明日開盤賣出 {_pct(o.lots, total)}：{o.reason}")
    for n in res.notes:
        if n.startswith("⚠") and not any(n in l for l in lines):
            lines.append(n)

    if chart_url:
        lines += ["", f"圖表：{chart_url}"]

    triggered = res.triggered
    title = f"{plan.name} {md}" + ("　⚠ 有賣出建議" if triggered else "")
    tags = "rotating_light" if triggered else "chart_with_upwards_trend"
    priority = 4 if triggered else 3
    return title, "\n".join(lines), tags, priority


def console(res: DayResult, state: State, plan: Plan) -> str:
    s = res.snap
    out = [
        "═" * 58,
        f" {plan.name} {plan.symbol}　{s.date}　收盤 {s.close:.1f}"
        + ("" if s.change_pct is None else f" ({s.change_pct:+.2f}%)"),
        "═" * 58,
        f"  MA{plan.ma_fast:<3} {_d(s.ma_fast):>8}    MA{plan.ma_slow:<3} {_d(s.ma_slow):>8}"
        f"    ATR{plan.atr_period} {_d(s.atr):>7}",
        f"  H{plan.chandelier_lookback:<4} {_d(s.h_lookback):>8}    快停利 {_d(s.fast_stop):>7}"
        f"    慢停利 {_d(s.slow_stop):>7}",
        f"  乖離  {'—' if s.bias is None else f'{s.bias:+.2%}':>8}"
        f"    週線 {_d(s.weekly_close):>8}    {plan.weekly_ma}週均 {_d(s.weekly_ma):>7}",
        "─" * 58,
    ]
    if res.skipped:
        out += [f"  {n}" for n in res.notes]
    else:
        if res.fills:
            out.append("  今日成交：")
            for f in res.fills:
                out.append(f"    {f.tranche_id:<5} {f.lots:>3} 張 @ {f.price:>7.1f}   {f.reason}")
        else:
            out.append("  今日無成交")
        for o in res.queued:
            lots = "剩餘全部" if o.lots is None else f"{o.lots} 張"
            out.append(f"  ⚠ 已排隊：明日開盤賣出 {lots} — {o.reason}")
        for n in res.notes:
            out.append(f"  {n}")

    out.append("─" * 58)
    rungs = state.open_ladder()
    out.append("  尚掛階梯：" + ("　".join(
        f"{t['id']} {t['trigger']['price']:.0f}×{t['lots']}" for t in rungs) or "（無）"))
    avg = state.realised_avg_price()
    out.append(f"  已出 {state.sold_lots}/{state.data['total_lots']} 張"
               f"　剩 {state.remaining} 張"
               + (f"　已實現均價 {avg:.2f}" if avg else ""))
    out.append("═" * 58)
    return "\n".join(out)
