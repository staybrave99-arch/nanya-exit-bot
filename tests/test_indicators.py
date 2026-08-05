"""指標正確性：對照 2026-08-05 收盤已知的人工計算結果。"""
import datetime as dt

import pytest

from nanya_exit.config import Plan
from nanya_exit.indicators import (atr_wilder, highest_close, sma, snapshot,
                                   true_ranges, weekly_bars)
from nanya_exit.models import Bar
from nanya_exit.twse import read_csv

SEED = "data/seed_history.csv"


@pytest.fixture(scope="module")
def bars():
    b = read_csv(SEED)
    assert b, "找不到種子資料"
    return b


def test_seed_range(bars):
    assert bars[0].date == "2025-09-01"
    assert bars[-1].date == "2026-08-05"
    assert len(bars) == 224
    assert len({b.date for b in bars}) == len(bars), "日期不可重複"


def test_seed_sorted_and_sane(bars):
    for i, b in enumerate(bars):
        assert b.low <= b.close <= b.high, f"{b.date} 收盤不在高低之間"
        assert b.low <= b.open <= b.high, f"{b.date} 開盤不在高低之間"
        assert b.volume > 0
        if i:
            assert bars[i - 1].date < b.date


def test_last_bar_is_20260805(bars):
    b = bars[-1]
    assert (b.open, b.high, b.low, b.close) == (469.0, 470.5, 441.5, 445.0)


# ── 對照人工算過的數字（容差 0.05）──────────────────────────
def test_ma20_matches_known_value(bars):
    assert sma([b.close for b in bars], 20) == pytest.approx(411.2, abs=0.05)


def test_ma60_matches_known_value(bars):
    assert sma([b.close for b in bars], 60) == pytest.approx(388.0, abs=0.05)


def test_atr14_matches_known_value(bars):
    assert atr_wilder(bars, 14) == pytest.approx(37.7, abs=0.05)


def test_h20_is_the_0715_close(bars):
    assert highest_close(bars, 20) == pytest.approx(481.0, abs=0.001)


def test_stops_match_plan(bars):
    plan = Plan()
    s = snapshot(bars, plan)
    assert s.fast_stop == pytest.approx(481.0 - 2.0 * 37.68, abs=0.1)   # ≈405.6
    assert s.slow_stop == pytest.approx(481.0 - 3.0 * 37.68, abs=0.1)   # ≈367.9
    assert s.bias == pytest.approx(0.0821, abs=0.0005)


# ── 純函式性質 ──────────────────────────────────────────────
def test_sma_returns_none_when_not_enough_data():
    assert sma([1.0, 2.0], 5) is None
    assert sma([1.0, 2.0, 3.0], 3) == 2.0


def test_true_range_uses_previous_close():
    bars = [Bar("2026-01-01", 10, 11, 9, 10, 1),
            Bar("2026-01-02", 10, 12, 11, 11, 1)]   # 向上跳空
    tr = true_ranges(bars)
    assert tr[0] == 2                                # 第一根：高−低
    assert tr[1] == 2                                # max(1, |12−10|, |11−10|)


def test_atr_needs_period_plus_one_bars():
    bars = [Bar(f"2026-01-{i+1:02d}", 10, 11, 9, 10, 1) for i in range(14)]
    assert atr_wilder(bars, 14) is None              # 只有 14 根 → 不足
    bars.append(Bar("2026-01-15", 10, 11, 9, 10, 1))
    assert atr_wilder(bars, 14) == pytest.approx(2.0)


def test_highest_close_uses_close_not_high(bars):
    """H20 錨點必須是收盤價。7/15 收 481，但 7/15 最高 489.5、8/5 最高 470.5。"""
    assert highest_close(bars, 20) == 481.0
    assert max(b.high for b in bars[-20:]) == 489.5   # 若誤用最高價會得到這個


def test_weekly_bars_take_last_close_of_week(bars):
    wk = weekly_bars(bars)
    last_date, last_close = wk[-1]
    assert last_date == "2026-08-05"
    assert last_close == 445.0
    # 6/22 那週最後一個交易日是 6/26，收 449
    match = [c for d, c in wk if d == "2026-06-26"]
    assert match and match[0] == 449.0


def test_snapshot_at_earlier_index(bars):
    """指定 index 時只能看到當日以前的資料，不可偷看未來。"""
    i = next(i for i, b in enumerate(bars) if b.date == "2026-06-22")
    s = snapshot(bars, Plan(), i)
    assert s.date == "2026-06-22"
    assert s.close == 505.0
    assert s.h_lookback == 505.0                     # 當天就是最高收盤
