"""基礎資料型別。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    """一根日 K。價格單位為元，成交量單位為股。"""
    date: str      # ISO，例如 "2026-08-05"
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def lots(self) -> float:
        """成交張數。"""
        return self.volume / 1000.0

    def to_row(self) -> list:
        return [self.date, self.open, self.high, self.low, self.close, self.volume]

    @classmethod
    def from_row(cls, row) -> "Bar":
        return cls(
            date=str(row[0]).strip(),
            open=float(row[1]), high=float(row[2]),
            low=float(row[3]), close=float(row[4]),
            volume=int(float(row[5])),
        )


@dataclass(frozen=True)
class Snapshot:
    """某一個交易日收盤後，所有指標的快照。"""
    date: str
    close: float
    prev_close: float | None
    change_pct: float | None
    ma_fast: float | None       # MA20
    ma_slow: float | None       # MA60
    atr: float | None           # ATR14 (Wilder)
    h_lookback: float | None    # H20：近 N 日最高收盤
    fast_stop: float | None
    slow_stop: float | None
    bias: float | None          # 收盤 / MA20 - 1
    weekly_ma: float | None
    weekly_close: float | None

    def bias_price(self, threshold: float) -> float | None:
        """乖離門檻對應的價格。"""
        return None if self.ma_fast is None else self.ma_fast * (1 + threshold)


@dataclass(frozen=True)
class Fill:
    """一筆已成交（或已判定成交）的賣出。"""
    tranche_id: str
    engine: str
    lots: int
    price: float
    date: str
    reason: str


@dataclass(frozen=True)
class PendingOrder:
    """今日收盤觸發、明日開盤才執行的單。"""
    tranche_id: str
    engine: str
    lots: int | None      # None = 剩餘全部
    signal_date: str
    reason: str
