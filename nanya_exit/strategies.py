"""備用／對照策略——純粹拿來跟主策略（plan.json）比較績效用。

這裡的 Plan 變體不會被 cli.py 的 `run` 使用，也不會碰 state.json、
不會推播——只有 backtest.py 會讀，實盤那條路徑完全不受影響。
"""
from __future__ import annotations

import dataclasses
from typing import Callable

from .config import Plan


def pure_trailing_stop(base: Plan | None = None) -> Plan:
    """拿掉限價階梯與乖離過熱，全倉交給移動停利處理。

    快慢兩線的 k 值沿用 base 的設定，只是不再讓階梯／乖離先把部位吃掉
    一半——用來檢驗「分批鎖利」到底有沒有比單純「讓趨勢決定出場」更好。
    fast_lots 改成三分之一（20/60），slow 吃剩下三分之二，維持跟原策略
    類似的「先出一部分保護獲利、剩下讓它跑」精神。
    """
    base = base or Plan()
    return dataclasses.replace(base, ladder=(), bias_steps=(), fast_lots=20)


STRATEGIES: dict[str, Callable[[Plan], Plan]] = {
    "current": lambda base: base,
    "pure_trailing_stop": pure_trailing_stop,
}
