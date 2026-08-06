"""block bootstrap 合成路徑與策略比較的行為測試。"""
import random

import pytest

from nanya_exit.backtest import (
    SCENARIOS, block_bootstrap_bars, compare_strategies, run_plan, _metrics, _summary,
)
from nanya_exit.config import BiasStep, LadderRung, Plan
from nanya_exit.models import Bar
from nanya_exit.state import State
from nanya_exit.strategies import STRATEGIES, pure_trailing_stop


def mkplan(**kw) -> Plan:
    base = dict(
        total_lots=60, start_date="2026-01-01", start_close=100.0,
        ladder=(LadderRung("R1", 110.0, 6), LadderRung("R2", 120.0, 6)),
        bias_steps=(BiasStep("B25", 0.25, 6),),
        chandelier_lookback=5, atr_period=3,
        fast_k=2.0, fast_lots=9, slow_k=3.0,
        hard_floor=50.0, ma_fast=3, ma_slow=5, weekly_ma=99,
    )
    base.update(kw)
    return Plan(**base)


def real_ish_bars(n=60, start_price=100.0):
    """帶一點鋸齒振幅的合成歷史，給 bootstrap 抽樣用（不是平盤，才有東西可抽）。"""
    out = []
    price = start_price
    for i in range(n):
        price *= 1 + ((i % 7) - 3) * 0.01
        o = price * 0.995
        c = price
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        out.append(Bar(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", o, h, l, c, 100000))
    return out


def test_block_bootstrap_length_and_ohlc_validity():
    source = real_ish_bars(60)
    rng = random.Random(1)
    path = block_bootstrap_bars(source, length=100, block_size=10, rng=rng)
    assert len(path) == 100
    dates = [b.date for b in path]
    assert dates == sorted(set(dates)) and len(set(dates)) == len(dates)
    for b in path:
        assert b.high >= max(b.open, b.close, b.low)
        assert b.low <= min(b.open, b.close, b.high)


def test_block_bootstrap_is_deterministic_with_seed():
    source = real_ish_bars(60)
    p1 = block_bootstrap_bars(source, 50, 10, random.Random(42))
    p2 = block_bootstrap_bars(source, 50, 10, random.Random(42))
    assert [b.close for b in p1] == [b.close for b in p2]


def test_block_bootstrap_anchors_to_last_close_by_default():
    source = real_ish_bars(60)
    rng = random.Random(7)
    path = block_bootstrap_bars(source, 20, 10, rng)
    # 第一根開盤應該接近 anchor（source 最後一天收盤）；去趨勢會有些微誤差
    assert path[0].open == pytest.approx(source[-1].close, rel=0.05)


def test_block_bootstrap_rejects_block_size_larger_than_source():
    source = real_ish_bars(10)
    with pytest.raises(ValueError):
        block_bootstrap_bars(source, 20, 50, random.Random(1))


def test_block_bootstrap_detrends_strong_historical_growth():
    """回歸測試：這檔股票近一年漲了近 10 倍，naive 複利串接區塊會讓合成
    路徑指數爆炸（180 天內變好幾倍），去趨勢後不應該再有這種系統性偏移。
    """
    strong_uptrend = []
    price = 46.75
    for i in range(224):
        price *= 1.0102          # 約等於真實歷史的日均漲幅
        strong_uptrend.append(Bar(f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
                                  price * 0.99, price * 1.01, price * 0.98,
                                  price, 100000))
    anchor = strong_uptrend[-1].close
    worst_ratio = 0.0
    for seed in range(20):
        path = block_bootstrap_bars(strong_uptrend, length=180, block_size=10,
                                    rng=random.Random(seed), anchor_close=anchor)
        worst_ratio = max(worst_ratio, max(b.close for b in path) / anchor)
    assert worst_ratio < 3.0, f"合成路徑不該系統性複利爆炸，最壞倍數 {worst_ratio:.1f}x"


def test_target_drift_zero_matches_default_neutral_path():
    source = real_ish_bars(60)
    p_default = block_bootstrap_bars(source, 50, 10, random.Random(5))
    p_zero = block_bootstrap_bars(source, 50, 10, random.Random(5), target_drift=0.0)
    assert [b.close for b in p_default] == [b.close for b in p_zero]


def test_target_drift_bull_ends_higher_than_bear_with_same_seed():
    source = real_ish_bars(60)
    bull = block_bootstrap_bars(source, 60, 10, random.Random(3),
                                target_drift=SCENARIOS["偏多"])
    bear = block_bootstrap_bars(source, 60, 10, random.Random(3),
                                target_drift=SCENARIOS["偏空"])
    assert bull[-1].close > bear[-1].close
    # 两者第一天應該仍接近同一個 anchor（漂移是逐日累積的，第一天差異很小）
    assert bull[0].close == pytest.approx(bear[0].close, rel=0.01)


def test_metrics_fully_closed_matches_realised_avg():
    plan = mkplan(hard_floor=0.0)      # 避免被硬地板提早清空干擾
    bars = real_ish_bars(40, start_price=100.0)
    state = State.new(plan)
    state.fill("SLOW", 60, 130.0, bars[-1].date, "測試：全部出清")
    m = _metrics(state, bars, plan)
    assert m["realised_avg"] == 130.0
    assert m["portfolio_per_lot"] == pytest.approx(130.0)
    assert m["capture_ratio"] == pytest.approx(130.0 / m["period_high"])


def test_metrics_nothing_sold_equals_buy_hold():
    plan = mkplan()
    bars = real_ish_bars(5, start_price=100.0)
    state = State.new(plan)
    m = _metrics(state, bars, plan)
    assert m["realised_avg"] is None
    assert m["portfolio_per_lot"] == pytest.approx(bars[-1].close)
    assert m["vs_buy_hold"] == pytest.approx(0.0)


def test_summary_handles_empty_and_single_value():
    assert _summary([])["mean"] is None
    s = _summary([5.0])
    assert s["mean"] == 5.0 and s["std"] == 0.0 and s["cv"] == 0.0


def test_pure_trailing_stop_has_no_ladder_or_bias():
    variant = pure_trailing_stop(Plan())
    assert variant.ladder == () and variant.bias_steps == ()
    ids = {t["id"] for t in State.new(variant).data["tranches"]}
    assert ids == {"FAST", "SLOW"}


def test_compare_strategies_runs_both_and_reports_summary_shape():
    plan = mkplan()
    strategies = {name: fn(plan) for name, fn in STRATEGIES.items()}
    source = real_ish_bars(80, start_price=100.0)
    result = compare_strategies(strategies, source, n_paths=5, path_length=30,
                                block_size=5, seed=123)
    assert set(result.keys()) == set(strategies.keys())
    for name, r in result.items():
        assert r["n_paths"] == 5
        assert 0.0 <= r["closed_rate"] <= 1.0
        assert set(r["capture_ratio"].keys()) >= {"mean", "std", "cv", "p10", "p50", "p90"}


def test_compare_strategies_is_deterministic_with_seed():
    plan = mkplan()
    strategies = {"current": plan}
    source = real_ish_bars(80, start_price=100.0)
    r1 = compare_strategies(strategies, source, n_paths=5, path_length=30, seed=99)
    r2 = compare_strategies(strategies, source, n_paths=5, path_length=30, seed=99)
    assert r1["current"]["capture_ratio"]["mean"] == r2["current"]["capture_ratio"]["mean"]
