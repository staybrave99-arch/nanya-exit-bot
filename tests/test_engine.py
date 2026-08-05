"""四引擎與跨日狀態的行為測試。

用合成 K 棒，才能精準控制每個觸發條件。
"""
import pytest

from nanya_exit.config import BiasStep, LadderRung, Plan
from nanya_exit.engine import process_day, replay
from nanya_exit.models import Bar
from nanya_exit.state import State

START = "2026-01-01"


def mkplan(**kw) -> Plan:
    base = dict(
        total_lots=60, start_date=START, start_close=100.0,
        ladder=(LadderRung("R1", 110.0, 6), LadderRung("R2", 120.0, 6)),
        bias_steps=(BiasStep("B25", 0.25, 6),),
        chandelier_lookback=5, atr_period=3,
        fast_k=2.0, fast_lots=9, slow_k=3.0,
        hard_floor=50.0, ma_fast=3, ma_slow=5, weekly_ma=99,
    )
    base.update(kw)
    return Plan(**base)


def flat(n, price=100.0, start_day=1):
    """n 根平盤 K，日期從 2026-01-{start_day} 起。"""
    return [Bar(f"2026-01-{start_day+i:02d}", price, price, price, price, 1000)
            for i in range(n)]


def run_all(plan, bars, state=None):
    st = state or State.new(plan)
    out = [process_day(st, bars, plan, i) for i in range(len(bars))
           if bars[i].date > plan.start_date]
    return st, out


# ── 引擎 1：限價階梯 ────────────────────────────────────────
def test_ladder_fills_at_trigger_price_not_close():
    plan = mkplan()
    bars = flat(5) + [Bar("2026-01-06", 100, 115, 99, 101, 1000)]   # 盤中衝到 115 收 101
    st, res = run_all(plan, bars)
    f = st.data["fills"]
    assert len(f) == 1
    assert f[0]["tranche_id"] == "R1"
    assert f[0]["price"] == 110.0, "限價單必須成交在掛單價，不是收盤價"
    assert st.remaining == 54


def test_ladder_two_rungs_same_day():
    plan = mkplan()
    bars = flat(5) + [Bar("2026-01-06", 100, 125, 99, 124, 1000)]
    st, _ = run_all(plan, bars)
    assert {f["tranche_id"] for f in st.data["fills"]} == {"R1", "R2"}
    assert st.remaining == 48


def test_ladder_does_not_refill():
    plan = mkplan()
    bars = flat(3) + [Bar(f"2026-01-{d:02d}", 100, 115, 99, 101, 1000) for d in (4, 5, 6)]
    st, _ = run_all(plan, bars)
    assert len([f for f in st.data["fills"] if f["tranche_id"] == "R1"]) == 1


# ── 引擎 2：乖離 ────────────────────────────────────────────
def test_bias_fills_at_close():
    plan = mkplan(ladder=())                       # 關掉階梯以隔離
    bars = flat(4, 100.0) + [Bar("2026-01-05", 100, 200, 100, 200, 1000)]
    st, _ = run_all(plan, bars)
    f = st.data["fills"]
    assert len(f) == 1 and f[0]["tranche_id"] == "B25"
    assert f[0]["price"] == 200.0, "乖離觸發成交在收盤價"


# ── 引擎 3：移動停利（跨日狀態）────────────────────────────
def test_fast_stop_queues_today_and_fills_next_open():
    plan = mkplan(ladder=(), bias_steps=())
    bars = flat(6, 100.0)
    bars.append(Bar("2026-01-07", 100, 100, 60, 60, 1000))      # 收盤暴跌 → 觸發
    bars.append(Bar("2026-01-08", 70, 75, 65, 70, 1000))        # 隔日開 70
    plan_state = State.new(plan)

    # 第 7 天：只排隊，不成交
    for i in range(len(bars) - 1):
        if bars[i].date > plan.start_date:
            r = process_day(plan_state, bars, plan, i)
    assert plan_state.remaining == 60, "訊號日不該成交"
    assert len(plan_state.data["pending_next_session"]) >= 1

    # 第 8 天：以開盤價成交
    process_day(plan_state, bars, plan, len(bars) - 1)
    fills = plan_state.data["fills"]
    assert fills, "隔日必須執行"
    assert fills[0]["price"] == 70.0, "停利單成交在隔日開盤價"


def test_slow_stop_sweeps_everything_and_cancels_rest():
    plan = mkplan()
    bars = flat(6, 100.0)
    bars.append(Bar("2026-01-07", 100, 100, 10, 10, 1000))      # 崩到觸發慢線
    bars.append(Bar("2026-01-08", 12, 13, 11, 12, 1000))
    st, _ = run_all(plan, bars)
    assert st.remaining == 0
    assert st.closed
    assert all(t["status"] in ("filled", "cancelled") for t in st.data["tranches"])
    swept = [f for f in st.data["fills"] if f["tranche_id"] == "SLOW"]
    assert swept and swept[0]["lots"] == 60


# ── 引擎 4：總開關 ─────────────────────────────────────────
def test_hard_floor_triggers_full_exit():
    plan = mkplan(hard_floor=90.0, fast_k=99, slow_k=99)        # 關掉停利以隔離
    bars = flat(6, 100.0)
    bars.append(Bar("2026-01-07", 100, 100, 85, 85, 1000))
    bars.append(Bar("2026-01-08", 86, 87, 85, 86, 1000))
    st, _ = run_all(plan, bars)
    assert st.closed and st.remaining == 0
    assert "硬地板" in st.data["fills"][-1]["reason"]


def test_death_cross_triggers_full_exit():
    plan = mkplan(hard_floor=0.0, fast_k=99, slow_k=99, ma_fast=2, ma_slow=4)
    # 先漲後跌，讓 MA2 跌破 MA4
    prices = [100, 110, 120, 130, 140, 90, 80]
    bars = [Bar(f"2026-01-{i+1:02d}", p, p, p, p, 1000) for i, p in enumerate(prices)]
    bars.append(Bar("2026-01-08", 80, 81, 79, 80, 1000))
    st, _ = run_all(plan, bars)
    assert st.closed
    assert "死亡交叉" in " ".join(f["reason"] for f in st.data["fills"])


# ── 冪等與帳務 ─────────────────────────────────────────────
def test_same_day_processed_twice_is_noop():
    plan = mkplan()
    bars = flat(5) + [Bar("2026-01-06", 100, 115, 99, 101, 1000)]
    st, _ = run_all(plan, bars)
    before = (st.remaining, len(st.data["fills"]))
    again = process_day(st, bars, plan, len(bars) - 1)
    assert again.skipped
    assert (st.remaining, len(st.data["fills"])) == before


def test_lots_never_exceed_position():
    plan = mkplan()
    bars = flat(5) + [Bar("2026-01-06", 100, 999, 99, 999, 1000),
                      Bar("2026-01-07", 999, 999, 1, 1, 1000),
                      Bar("2026-01-08", 2, 3, 1, 2, 1000)]
    st, _ = run_all(plan, bars)
    assert st.remaining == 0
    assert sum(f["lots"] for f in st.data["fills"]) == 60


def test_closed_state_stops_processing():
    plan = mkplan()
    bars = flat(5) + [Bar("2026-01-06", 100, 999, 99, 999, 1000),
                      Bar("2026-01-07", 999, 999, 1, 1, 1000),
                      Bar("2026-01-08", 2, 3, 1, 2, 1000),
                      Bar("2026-01-09", 2, 3, 1, 2, 1000)]
    st, res = run_all(plan, bars)
    assert res[-1].skipped
    assert "已全部出清" in " ".join(res[-1].notes)


def test_replay_skips_start_date_itself():
    plan = mkplan()
    bars = [Bar(START, 100, 999, 99, 999, 1000)] + flat(3, 100.0, start_day=2)
    st = State.new(plan)
    replay(st, bars, plan)
    assert st.remaining == 60, "起算日當天不該成交（掛單還沒送出去）"


def test_process_day_refuses_start_date_itself():
    """迴歸測試：8/5 最高 470.5 曾讓 R1(470) 在計畫成立當天憑空成交。"""
    plan = mkplan()
    bars = [Bar(START, 100, 999, 99, 500, 1000)]
    st = State.new(plan)
    r = process_day(st, bars, plan, 0)
    assert r.skipped and st.remaining == 60
    assert "起算日" in " ".join(r.notes)
