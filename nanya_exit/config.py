"""出場計畫的參數定義。

所有「策略長什麼樣」的決定都集中在這裡，其他模組只讀不改。
想調參數，改 plan.json 即可，不用動程式碼。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass(frozen=True)
class LadderRung:
    """引擎 1：一階限價賣單。"""
    id: str
    price: float
    lots: int
    note: str = ""


@dataclass(frozen=True)
class BiasStep:
    """引擎 2：一段乖離過熱賣出。"""
    id: str
    threshold: float          # 收盤 / MA20 - 1 的門檻，例如 0.25
    lots: int


@dataclass(frozen=True)
class Plan:
    # ── 標的與部位 ─────────────────────────────
    symbol: str = "2408"
    name: str = "南亞科"
    total_lots: int = 60
    start_date: str = "2026-08-05"      # 計畫起算日（當日收盤價視為成本基準點）
    start_close: float = 445.0

    # ── 引擎 1：限價階梯 ───────────────────────
    ladder: tuple[LadderRung, ...] = (
        LadderRung("R1", 470.0, 6, "8/5 高點 470.5"),
        LadderRung("R2", 495.0, 6, "7/15 波段高 489.5"),
        LadderRung("R3", 520.0, 6, "站上歷史高 505"),
        LadderRung("R4", 550.0, 6, "券商目標價下緣"),
        LadderRung("R5", 585.0, 6, "券商目標價中段"),
    )

    # ── 引擎 2：乖離過熱 ───────────────────────
    bias_steps: tuple[BiasStep, ...] = (
        BiasStep("B25", 0.25, 6),
        BiasStep("B32", 0.32, 6),
    )

    # ── 引擎 3：移動停利（吊燈出場）─────────────
    chandelier_lookback: int = 20       # H20：近 N 日最高「收盤」價
    atr_period: int = 14
    fast_k: float = 2.0
    fast_lots: int = 9
    slow_k: float = 3.0                 # 慢線觸發 → 剩餘全出

    # ── 引擎 4：循環結束總開關 ─────────────────
    hard_floor: float = 322.0           # 收盤跌破即無條件清空（7/30 低點）
    ma_fast: int = 20
    ma_slow: int = 60
    weekly_ma: int = 8                  # 週線連兩週收在 N 週均線下 → 清空
    weekly_breach_weeks: int = 2

    # ── 執行假設 ───────────────────────────────
    # 停利訊號一律「當日收盤判斷、隔日開盤成交」
    stop_executes_next_open: bool = True

    def rung(self, rung_id: str) -> LadderRung | None:
        return next((r for r in self.ladder if r.id == rung_id), None)

    def bias_step(self, step_id: str) -> BiasStep | None:
        return next((b for b in self.bias_steps if b.id == step_id), None)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ladder"] = [asdict(r) for r in self.ladder]
        d["bias_steps"] = [asdict(b) for b in self.bias_steps]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        d = dict(d)
        d["ladder"] = tuple(LadderRung(**r) for r in d.get("ladder", []))
        d["bias_steps"] = tuple(BiasStep(**b) for b in d.get("bias_steps", []))
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Plan":
        """有 plan.json 就讀它，沒有就用上面的預設值。"""
        path = Path(path or os.getenv("PLAN_PATH", "plan.json"))
        if path.is_file():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls()


@dataclass(frozen=True)
class Settings:
    """執行環境設定（全部可用環境變數覆寫）。"""
    state_path: str = field(default_factory=lambda: os.getenv("STATE_PATH", "state.json"))
    seed_csv: str = field(default_factory=lambda: os.getenv("SEED_CSV", "data/seed_history.csv"))
    cache_csv: str = field(default_factory=lambda: os.getenv("CACHE_CSV", ""))  # 空 = 放 state 旁邊
    ntfy_server: str = field(default_factory=lambda: os.getenv("NTFY_SERVER", "https://ntfy.sh"))
    ntfy_topic: str = field(default_factory=lambda: os.getenv("NTFY_TOPIC", "Exit2408"))
    ntfy_token: str = field(default_factory=lambda: os.getenv("NTFY_TOKEN", ""))
    chart_url: str = field(default_factory=lambda: os.getenv(
        "CHART_URL", "https://staybrave99-arch.github.io/nanya-exit-bot/"))
    timezone: str = field(default_factory=lambda: os.getenv("TZ_NAME", "Asia/Taipei"))
    run_at: str = field(default_factory=lambda: os.getenv("RUN_AT", "20:00"))
    http_timeout: int = field(default_factory=lambda: int(os.getenv("HTTP_TIMEOUT", "30")))

    def resolved_cache_csv(self) -> str:
        if self.cache_csv:
            return self.cache_csv
        return str(Path(self.state_path).with_name("price_cache.csv"))
