"""狀態持久化：存讀往返、原子寫入、人工修正。"""
import json

import pytest

from nanya_exit.config import Plan
from nanya_exit.state import State


@pytest.fixture
def plan():
    return Plan()


def test_new_state_has_all_tranches(plan):
    st = State.new(plan)
    ids = [t["id"] for t in st.data["tranches"]]
    assert ids == ["R1", "R2", "R3", "R4", "R5", "B25", "B32", "FAST", "SLOW"]
    assert st.remaining == 60
    assert not st.closed


def test_ladder_lots_sum_to_half_the_position(plan):
    st = State.new(plan)
    ladder = sum(t["lots"] for t in st.data["tranches"] if t["engine"] == "ladder")
    bias = sum(t["lots"] for t in st.data["tranches"] if t["engine"] == "bias")
    assert ladder == 30 and bias == 12          # 50% / 20% of 60


def test_roundtrip(tmp_path, plan):
    p = tmp_path / "state.json"
    st = State.new(plan)
    st.fill("R1", 6, 470.0, "2026-08-10", "測試")
    st.save(p)
    again = State.load(p, plan)
    assert again.remaining == 54
    assert again.tranche("R1")["fill_price"] == 470.0
    assert again.data["fills"][0]["lots"] == 6


def test_save_writes_backup_and_no_tmp_left(tmp_path, plan):
    p = tmp_path / "state.json"
    State.new(plan).save(p)
    st = State.load(p, plan)
    st.fill("R1", 6, 470.0, "2026-08-10", "測試")
    st.save(p)
    assert (tmp_path / "state.json.bak").is_file()
    assert not (tmp_path / "state.json.tmp").exists(), "暫存檔應該已被 rename 掉"
    json.loads(p.read_text(encoding="utf-8"))       # 必須是完整合法 JSON


def test_double_fill_is_rejected(plan):
    st = State.new(plan)
    st.fill("R1", 6, 470.0, "2026-08-10", "測試")
    with pytest.raises(ValueError):
        st.fill("R1", 6, 470.0, "2026-08-11", "重複")


def test_fill_is_capped_by_remaining(plan):
    st = State.new(plan)
    st.fill("SLOW", 999, 400.0, "2026-08-10", "掃尾")
    assert st.remaining == 0 and st.closed
    assert st.data["fills"][0]["lots"] == 60


def test_realised_avg_is_lot_weighted(plan):
    st = State.new(plan)
    st.fill("R1", 6, 470.0, "2026-08-10", "")
    st.fill("SLOW", 54, 400.0, "2026-08-20", "")
    expected = (470 * 6 + 400 * 54) / 60
    assert st.realised_avg_price() == pytest.approx(expected)


def test_schema_version_mismatch_raises(tmp_path, plan):
    p = tmp_path / "state.json"
    st = State.new(plan)
    st.data["schema_version"] = 999
    p.write_text(json.dumps(st.data), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        State.load(p, plan)


def test_already_processed_is_date_monotonic(plan):
    st = State.new(plan)
    st.mark_processed("2026-08-10")
    assert st.already_processed("2026-08-10")
    assert st.already_processed("2026-08-07")       # 更早的也算處理過
    assert not st.already_processed("2026-08-11")


def test_cancel_remaining_keeps_filled(plan):
    st = State.new(plan)
    st.fill("R1", 6, 470.0, "2026-08-10", "")
    st.cancel_remaining_tranches("循環結束")
    assert st.tranche("R1")["status"] == "filled"
    assert st.tranche("R5")["status"] == "cancelled"


def test_open_ladder_sorted_and_excludes_filled(plan):
    st = State.new(plan)
    st.fill("R2", 6, 495.0, "2026-08-10", "")
    rungs = [t["id"] for t in st.open_ladder()]
    assert rungs == ["R1", "R3", "R4", "R5"]


def test_queue_next_session_dedupes(plan):
    from nanya_exit.models import PendingOrder
    st = State.new(plan)
    o = PendingOrder("FAST", "chandelier_fast", 9, "2026-08-10", "測試")
    st.queue_next_session(o)
    st.queue_next_session(o)
    assert len(st.data["pending_next_session"]) == 1


def test_dequeue_removes_pending_order_without_touching_fills(plan):
    from nanya_exit.models import PendingOrder
    st = State.new(plan)
    st.queue_next_session(PendingOrder("FAST", "chandelier_fast", 9, "2026-08-10", "測試"))
    result = st.dequeue_next_session("FAST", "臨時決定不賣")
    assert result["restored"] == []
    assert st.data["pending_next_session"] == []
    assert st.data["fills"] == []
    assert st.data["corrections"][0]["action"] == "dequeue"


def test_dequeue_unknown_tranche_raises(plan):
    st = State.new(plan)
    with pytest.raises(KeyError):
        st.dequeue_next_session("NOPE", "測試")


def test_dequeue_slow_restores_cascaded_cancellations(plan):
    from nanya_exit.models import PendingOrder
    st = State.new(plan)
    # 真實觸發順序（見 engine.py）：先 queue，SLOW 自己那筆 tranche 這時
    # 仍是 pending，所以隨後的 cancel_remaining_tranches 連它也一併取消。
    st.queue_next_session(PendingOrder("SLOW", "chandelier_slow", None, "2026-08-10", "跌破慢停利線"))
    st.cancel_remaining_tranches("慢停利線觸發")
    result = st.dequeue_next_session("SLOW", "券商回報漲停鎖死，沒真的賣掉")
    assert set(result["restored"]) == {"R1", "R2", "R3", "R4", "R5", "B25", "B32", "FAST", "SLOW"}
    for t in st.data["tranches"]:
        assert t["status"] == "pending"
        assert "已取消" not in t["note"]


def test_unfill_reverses_fill_and_reopens_position(plan):
    st = State.new(plan)
    st.fill("SLOW", 60, 400.0, "2026-08-10", "掃尾")
    assert st.closed
    result = st.unfill("SLOW", "沒真的成交")
    assert result["removed"]["lots"] == 60
    assert not st.closed
    assert st.data["closed_reason"] is None
    assert st.remaining == 60
    assert st.tranche("SLOW")["status"] == "pending"
    assert st.data["fills"] == []
    assert st.data["corrections"][0]["action"] == "unfill"


def test_unfill_pending_tranche_raises(plan):
    st = State.new(plan)
    with pytest.raises(ValueError):
        st.unfill("R1", "還沒成交，不能撤")


def test_unfill_unknown_tranche_raises(plan):
    st = State.new(plan)
    with pytest.raises(KeyError):
        st.unfill("NOPE", "測試")
