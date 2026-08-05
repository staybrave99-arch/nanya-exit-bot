"""證交所資料來源。

兩支 API 各司其職：

  STOCK_DAY   個股月檔 —— 歷史用。**當天晚上常常還沒補上今日資料**，
              所以不能拿它當「今天收盤」的來源（2026-08-05 20:45 實測仍只到 08-04）。
  MI_INDEX    當日各產業行情 —— 今日 K 棒用。type=24 是半導體業，
              回應小、收盤後就有 2408。

抓下來的資料會併進本地 CSV 快取，之後就算 API 掛掉也還有歷史可用。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import time
from pathlib import Path

import requests

from .models import Bar

STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
SEMICONDUCTOR = "24"          # 2408 所屬產業別代碼

_HEADERS = {
    "User-Agent": "nanya-exit-bot/1.0 (+https://github.com/)",
    "Accept": "application/json",
}


class TwseError(RuntimeError):
    pass


def _roc_to_iso(s: str) -> str:
    """民國日期 '115/08/05' → '2026-08-05'。也接受已經是西元的字串。"""
    s = s.strip().replace("＊", "").replace("*", "")
    if "-" in s:
        return s
    y, m, d = s.split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def _num(s) -> float:
    """'1,234.50' → 1234.5；'--' 或空 → 0.0。"""
    s = str(s).strip().replace(",", "")
    if s in ("", "--", "---", "X"):
        return 0.0
    return float(s)


def _get(url: str, params: dict, timeout: int, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:                     # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise TwseError(f"{url} 取得失敗：{last}")


def fetch_month(stock_no: str, day: dt.date, timeout: int = 30) -> list[Bar]:
    """抓 day 所屬整個月的日 K。"""
    js = _get(STOCK_DAY, {
        "date": day.strftime("%Y%m%d"), "stockNo": stock_no, "response": "json",
    }, timeout)
    if js.get("stat") != "OK" or not js.get("data"):
        return []
    out = []
    for row in js["data"]:
        # 欄位：日期 成交股數 成交金額 開盤 最高 最低 收盤 漲跌價差 成交筆數
        try:
            out.append(Bar(
                date=_roc_to_iso(row[0]),
                open=_num(row[3]), high=_num(row[4]),
                low=_num(row[5]), close=_num(row[6]),
                volume=int(_num(row[1])),
            ))
        except (ValueError, IndexError):
            continue
    return [b for b in out if b.close > 0]


def fetch_today(stock_no: str, day: dt.date, timeout: int = 30) -> Bar | None:
    """抓 day 當天的 K 棒（走當日產業別行情，收盤後即有）。

    回傳 None 代表當天沒有這檔的資料 → 休市，或證交所還沒更新。
    """
    js = _get(MI_INDEX, {
        "date": day.strftime("%Y%m%d"), "type": SEMICONDUCTOR, "response": "json",
    }, timeout)

    # MI_INDEX 會回多張表，欄位定義放在 fields1..fields9 / tables[]
    tables = js.get("tables")
    if tables:
        candidates = [t for t in tables if t.get("data")]
    else:
        candidates = [{"fields": js.get(f"fields{i}"), "data": js.get(f"data{i}")}
                      for i in range(1, 10) if js.get(f"data{i}")]

    for tbl in candidates:
        fields = [str(f) for f in (tbl.get("fields") or [])]
        if not fields or "證券代號" not in fields:
            continue
        idx = {name: fields.index(name) for name in fields}
        try:
            c_code = idx["證券代號"]
            c_open, c_high = idx["開盤價"], idx["最高價"]
            c_low, c_close = idx["最低價"], idx["收盤價"]
            c_vol = idx.get("成交股數", 1)
        except KeyError:
            continue
        for row in tbl["data"]:
            if str(row[c_code]).strip() != stock_no:
                continue
            close = _num(row[c_close])
            if close <= 0:
                return None
            return Bar(
                date=day.isoformat(),
                open=_num(row[c_open]) or close,
                high=_num(row[c_high]) or close,
                low=_num(row[c_low]) or close,
                close=close,
                volume=int(_num(row[c_vol])),
            )
    return None


# ── 本地快取 ────────────────────────────────────────────────────
def read_csv(path: str | Path) -> list[Bar]:
    p = Path(path)
    if not p.is_file():
        return []
    with p.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    if rows[0] and rows[0][0].lower().startswith("date"):
        rows = rows[1:]
    return sorted((Bar.from_row(r) for r in rows if len(r) >= 6), key=lambda b: b.date)


def write_csv(path: str | Path, bars: list[Bar]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "open", "high", "low", "close", "volume"])
    w.writerows(b.to_row() for b in sorted(bars, key=lambda b: b.date))
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(p)


def merge(*groups: list[Bar]) -> list[Bar]:
    """多組 K 棒合併，同日期以後傳入者為準。"""
    by_date: dict[str, Bar] = {}
    for g in groups:
        for b in g:
            by_date[b.date] = b
    return [by_date[d] for d in sorted(by_date)]


def load_history(seed_csv: str | Path, cache_csv: str | Path) -> list[Bar]:
    """種子資料 + 快取，快取優先。"""
    return merge(read_csv(seed_csv), read_csv(cache_csv))


def refresh(stock_no: str, today: dt.date, seed_csv: str | Path,
            cache_csv: str | Path, timeout: int = 30,
            months_back: int = 3, offline: bool = False) -> tuple[list[Bar], bool]:
    """把歷史補到最新。

    回傳 (bars, has_today)。offline=True 時完全不連網，只讀本地檔
    （這個雲端沙箱連不出去，離線測試就靠這個）。
    """
    bars = load_history(seed_csv, cache_csv)
    if offline:
        return bars, any(b.date == today.isoformat() for b in bars)

    groups = [bars]
    cursor = today.replace(day=1)
    for _ in range(months_back):
        try:
            groups.append(fetch_month(stock_no, cursor, timeout))
        except TwseError:
            pass
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)

    has_today = False
    try:
        tb = fetch_today(stock_no, today, timeout)
        if tb is not None:
            groups.append([tb])
            has_today = True
    except TwseError:
        pass

    bars = merge(*groups)
    has_today = has_today or any(b.date == today.isoformat() for b in bars)
    write_csv(cache_csv, bars)
    return bars, has_today
