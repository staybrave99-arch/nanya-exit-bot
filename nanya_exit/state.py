"""持久化狀態。

這支程式最重要的設計就是這裡：**賣到第幾批、哪批用什麼價成交、
今天收盤觸發了什麼但要等明天開盤才執行** —— 全部寫進 state.json，
不靠任何「從歷史回推」的猜測。

為什麼不回推：
  · 回推假設「價格碰到就一定成交」，但漲停鎖死時買不到、跌停鎖死時賣不掉。
  · 回推無法表達「今天收盤觸發、明天開盤執行」這種跨日狀態。
  · 券商實際成交價與觸發價可能有落差，需要能人工修正（see: `mark-filled` 指令）。

檔案本身是純 JSON，人可讀可改。原子寫入（先寫 .tmp 再 rename），
避免 Fly machine 在寫入中途被回收造成半截檔案。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import Plan
from .models import Fill, PendingOrder

SCHEMA_VERSION = 1

PENDING = "pending"
FILLED = "filled"
CANCELLED = "cancelled"


class State:
    """出場計畫的執行狀態。"""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    # ── 建立 / 讀寫 ────────────────────────────────────────────
    @classmethod
    def new(cls, plan: Plan) -> "State":
        tranches: list[dict] = []
        for r in plan.ladder:
            tranches.append({
                "id": r.id, "engine": "ladder", "lots": r.lots,
                "trigger": {"type": "limit_high", "price": r.price},
                "status": PENDING, "fill_price": None, "fill_date": None,
                "note": r.note,
            })
        for b in plan.bias_steps:
            tranches.append({
                "id": b.id, "engine": "bias", "lots": b.lots,
                "trigger": {"type": "bias_gte", "threshold": b.threshold},
                "status": PENDING, "fill_price": None, "fill_date": None,
                "note": f"收盤/MA20-1 ≥ {b.threshold:+.0%}",
            })
        tranches.append({
            "id": "FAST", "engine": "chandelier_fast", "lots": plan.fast_lots,
            "trigger": {"type": "close_below_stop", "k": plan.fast_k},
            "status": PENDING, "fill_price": None, "fill_date": None,
            "note": f"收盤跌破 H{plan.chandelier_lookback}−{plan.fast_k}×ATR",
        })
        tranches.append({
            "id": "SLOW", "engine": "chandelier_slow", "lots": None,  # None = 剩餘全部
            "trigger": {"type": "close_below_stop", "k": plan.slow_k},
            "status": PENDING, "fill_price": None, "fill_date": None,
            "note": "剩餘全部出清",
        })
        return cls({
            "schema_version": SCHEMA_VERSION,
            "plan": plan.to_dict(),
            "total_lots": plan.total_lots,
            "remaining_lots": plan.total_lots,
            "tranches": tranches,
            "fills": [],
            "pending_next_session": [],
            "last_processed_date": None,
            "last_notified_date": None,
            "closed": False,
            "closed_reason": None,
            "runs": [],
            "corrections": [],
        })

    @classmethod
    def load(cls, path: str | os.PathLike, plan: Plan) -> "State":
        p = Path(path)
        if not p.is_file():
            return cls.new(plan)
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"state schema 版本不符：檔案是 {data.get('schema_version')}，"
                f"程式是 {SCHEMA_VERSION}。請先備份再手動遷移。"
            )
        return cls(data)

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():                       # 留一份上一版，手殘時可回溯
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)                    # 原子置換

    # ── 查詢 ──────────────────────────────────────────────────
    @property
    def remaining(self) -> int:
        return int(self.data["remaining_lots"])

    @property
    def closed(self) -> bool:
        return bool(self.data["closed"])

    @property
    def sold_lots(self) -> int:
        return int(self.data["total_lots"]) - self.remaining

    def tranche(self, tid: str) -> dict | None:
        return next((t for t in self.data["tranches"] if t["id"] == tid), None)

    def pending_tranches(self, engine: str | None = None) -> list[dict]:
        return [t for t in self.data["tranches"]
                if t["status"] == PENDING and (engine is None or t["engine"] == engine)]

    def open_ladder(self) -> list[dict]:
        """還掛在市場上、尚未成交的階梯單。"""
        return sorted(self.pending_tranches("ladder"), key=lambda t: t["trigger"]["price"])

    def pending_orders(self) -> list[PendingOrder]:
        return [PendingOrder(**o) for o in self.data["pending_next_session"]]

    def already_processed(self, date: str) -> bool:
        last = self.data.get("last_processed_date")
        return last is not None and date <= last

    # ── 變更 ──────────────────────────────────────────────────
    def fill(self, tid: str, lots: int, price: float, date: str, reason: str) -> Fill:
        t = self.tranche(tid)
        if t is None:
            raise KeyError(f"找不到批次 {tid}")
        if t["status"] == FILLED:
            raise ValueError(f"批次 {tid} 已成交過，不可重複")
        lots = min(int(lots), self.remaining)
        t["status"] = FILLED
        t["fill_price"] = round(float(price), 2)
        t["fill_date"] = date
        t["lots"] = lots
        self.data["remaining_lots"] = self.remaining - lots
        f = Fill(tranche_id=tid, engine=t["engine"], lots=lots,
                 price=round(float(price), 2), date=date, reason=reason)
        self.data["fills"].append(asdict(f))
        if self.remaining <= 0:
            self.data["closed"] = True
            self.data["closed_reason"] = self.data.get("closed_reason") or reason
        return f

    def queue_next_session(self, order: PendingOrder) -> None:
        if any(o["tranche_id"] == order.tranche_id for o in self.data["pending_next_session"]):
            return                            # 同一批不重複排隊
        self.data["pending_next_session"].append(asdict(order))

    def clear_next_session(self) -> None:
        self.data["pending_next_session"] = []

    def cancel_remaining_tranches(self, reason: str) -> None:
        for t in self.data["tranches"]:
            if t["status"] == PENDING:
                t["status"] = CANCELLED
                t["note"] = f"{t.get('note','')}｜已取消：{reason}".strip("｜")

    def dequeue_next_session(self, tid: str, reason: str) -> dict:
        """撤掉一筆『收盤已觸發、明日開盤還沒真的執行』的排隊單。

        這個階段 state 裡還沒有任何 Fill 被寫入，撤掉不會留下錯誤的成交
        歷史，是最安全的介入點。引擎 3 慢停利／引擎 4 循環結束觸發時會把
        其他還沒成交的批次一併 cancel_remaining_tranches——兩者都只會
        連著 SLOW 一起發生，撤掉 SLOW 的排隊單時一併復原這些批次。
        """
        order = next((o for o in self.data["pending_next_session"]
                     if o["tranche_id"] == tid), None)
        if order is None:
            raise KeyError(f"{tid} 目前沒有排隊中的單")
        self.data["pending_next_session"] = [
            o for o in self.data["pending_next_session"] if o["tranche_id"] != tid
        ]
        restored = []
        for t in self.data["tranches"]:
            if t["status"] == CANCELLED:
                t["status"] = PENDING
                t["note"] = re.sub(r"｜?已取消：.*$", "", t["note"]).strip("｜")
                restored.append(t["id"])
        self._log_correction("dequeue", tid, reason, order=order, restored=restored)
        return {"dequeued": order, "restored": restored}

    def unfill(self, tid: str, reason: str) -> dict:
        """撤銷一筆已經被標記成交、但實際沒有成交的批次。

        給引擎 1（限價階梯）／引擎 2（乖離過熱）用——這兩種是當場認定
        成交，沒有像排隊單那樣的緩衝期，所以要事後校正：把成交紀錄從
        fills[] 移掉、張數加回剩餘、批次退回 pending；如果這筆成交剛好
        讓部位被標記為已出清，一併復原。
        """
        t = self.tranche(tid)
        if t is None:
            raise KeyError(f"找不到批次 {tid}")
        if t["status"] != FILLED:
            raise ValueError(f"批次 {tid} 目前不是已成交狀態，無需撤銷")
        fill_idx = next((i for i in range(len(self.data["fills"]) - 1, -1, -1)
                         if self.data["fills"][i]["tranche_id"] == tid), None)
        if fill_idx is None:
            raise ValueError(f"找不到批次 {tid} 的成交紀錄，state 可能被手動改過")
        removed = self.data["fills"].pop(fill_idx)
        self.data["remaining_lots"] = self.remaining + removed["lots"]
        t["status"] = PENDING
        t["fill_price"] = None
        t["fill_date"] = None
        if self.data["closed"] and self.remaining > 0:
            self.data["closed"] = False
            self.data["closed_reason"] = None
        self._log_correction("unfill", tid, reason, removed_fill=removed)
        return {"removed": removed}

    def _log_correction(self, action: str, tid: str, reason: str, **extra) -> None:
        entry = {"action": action, "tranche_id": tid, "reason": reason, **extra}
        self.data.setdefault("corrections", []).append(entry)

    def mark_processed(self, date: str) -> None:
        self.data["last_processed_date"] = date

    def log_run(self, entry: dict) -> None:
        self.data["runs"].append(entry)
        self.data["runs"] = self.data["runs"][-400:]     # 只留最近 400 筆

    # ── 統計 ──────────────────────────────────────────────────
    def realised_avg_price(self) -> float | None:
        fills = self.data["fills"]
        lots = sum(f["lots"] for f in fills)
        if not lots:
            return None
        return sum(f["price"] * f["lots"] for f in fills) / lots
