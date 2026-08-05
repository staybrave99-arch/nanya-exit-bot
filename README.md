# 南亞科 2408 分批出場機器人

每個交易日 20:00（台北）自動抓證交所收盤資料、重算指標、判斷該賣哪一批，
把「明天該掛哪些單」推到 ntfy。**有狀態**：賣到第幾批、每批成交價、
今天觸發但明天才執行的單，全部存在 `state.json`。

```
收 445.0（+2.06%）
MA20 411.2｜ATR14 37.7｜乖離 +8.2%
──明日掛單──
R1 470×6　R2 495×6　R3 520×6
R4 550×6　R5 585×6
停利 405.6 賣 9 張／367.9 全出
──狀態──
已出 0／60 張，剩 60 張
```

---

## 策略：四個引擎

南亞科的 ATR14 佔股價 **8.5%**、平均日振幅 **7%**、年化波動 **126%**。
在這種波動下，單一移動停利會被洗到報廢（2026/05–08 實測，2.5×ATR 觸發 5 次、
3×ATR 觸發 4 次），而跌停鎖死時你根本賣不掉。所以往上賣的權重必須大於往下停利。

| 引擎 | 觸發 | 成交價 | 張數 |
|---|---|---|---|
| **1 限價階梯** | 最高價觸及 470 / 495 / 520 / 550 / 585 | 掛單價 | 各 6 |
| **2 乖離過熱** | 收盤 ÷ MA20 − 1 ≥ +25% / +32% | 收盤價 | 各 6 |
| **3 移動停利** | 收盤跌破 H20 − 2.0×ATR14（快）／ −3.0×ATR14（慢） | **隔日開盤** | 9／剩餘 |
| **4 循環總開關** | MA20<MA60、收盤<322、或週線連 2 週破 8 週均 | **隔日開盤** | 剩餘全部 |

引擎 3、4 一律「收盤判斷、隔日開盤執行」——盤中假跌破在這檔股票上太常見。
這也是為什麼需要跨日狀態。

參數全部集中在 `nanya_exit/config.py`；要改就在專案根目錄放一份 `plan.json`
（格式見 `python -m nanya_exit status --json`），程式碼不用動。

---

## 為什麼是有狀態，不是從歷史回推

回推（「某天最高價碰過 495 就當 R2 成交了」）看似省事，但有三個洞：

1. **漲停／跌停鎖死時價格碰得到、單子成交不了。** 回推會虛報成交。
2. **無法表達跨日狀態。** 「今天收盤跌破停利線 → 明天開盤賣」在回推裡沒地方放。
3. **實際成交價和規則價會有落差。** 有狀態才能用 `mark-filled` 人工修正。

`state.json` 是純 JSON，人可讀可改。寫入是原子的（先寫 `.tmp` 再 `os.replace`），
並保留一份 `.bak`，機器在寫入中途被回收也不會產生半截檔案。

---

## 用法

```bash
pip install -r requirements-dev.txt

python -m nanya_exit run                 # 每日主流程（排程呼叫這個）
python -m nanya_exit run --dry-run       # 不推播、不寫狀態
python -m nanya_exit run --offline       # 不連網，只用本地資料
python -m nanya_exit status              # 看目前賣到第幾批
python -m nanya_exit status --json       # 完整狀態
python -m nanya_exit replay --write      # 從起算日重建狀態
python -m nanya_exit test-notify         # 送一則測試推播
python -m nanya_exit mark-filled R1 --price 471.5 --date 2026-08-07   # 人工修正
```

`run` 的行為：

- 今天沒有新收盤資料（休市／證交所還沒更新）→ **不推播**，直接結束。
- 同一天重複執行 → 冪等保護，不會重複扣張數（要重跑加 `--force`）。
- ntfy 推播失敗 → 錯誤碼 1，並把訊息內容印在 stdout 供人工轉貼。

### 資料來源

| 用途 | API | 說明 |
|---|---|---|
| 今日 K 棒 | `MI_INDEX?type=24` | 當日各產業行情，半導體業。收盤後就有。 |
| 歷史 | `STOCK_DAY` | 個股月檔。**當天晚上常常還沒補上今日資料**，不能拿來當今日來源。 |

（2026-08-05 20:45 實測：`STOCK_DAY` 仍只到 08-04，`MI_INDEX` 已有 08-05。）

`data/seed_history.csv` 內含 2025-09-01 ~ 2026-08-05 共 224 根日 K，
確保 MA60 / ATR14 一開機就有足夠暖機資料。抓到的新資料會併入 `price_cache.csv`。

---

## 測試

```bash
pytest -q          # 39 個測試
```

指標部分對照人工算過的答案：MA20 411.2、MA60 388.0、ATR14 37.7、H20 481.0
（皆為 2026-08-05 收盤）。引擎部分用合成 K 棒逐條驗證觸發、成交價、跨日排隊、
冪等、張數不超賣。

---

## 部署到 Fly.io

```bash
fly launch --no-deploy                       # 會問 app 名稱，改掉 fly.toml 的 app
fly volumes create nanya_data --size 1 --region nrt
fly secrets set NTFY_TOPIC=Exit2408          # 有 token 再加 NTFY_TOKEN
fly deploy
fly logs                                     # 確認排程器起來了
```

**volume 一定要建**，否則機器重建時 `state.json` 會消失、「賣到第幾批」歸零。

手動觸發一次：

```bash
fly ssh console -C "python -m nanya_exit run --force"
fly ssh console -C "python -m nanya_exit status"
```

把狀態抓回本機看：

```bash
fly ssh sftp get /data/state.json ./state.json
```

### 為什麼不用 Fly 內建的 machine schedule

它只支援 `hourly` / `daily` / `weekly`，**不能指定幾點**。收盤巡檢必須在
20:00 跑，所以用一台常駐 `shared-cpu-1x` + APScheduler。記憶體 256MB 夠用。

---

## 已知限制

- 停利單假設**隔日開盤成交**。真的開跌停鎖死時賣不掉，實際成交價會比程式記的差。
  遇到這種情況用 `mark-filled` 修正。
- 台股一般委託單當日有效，階梯要掛**券商的長效條件單**（多數可設 30~90 天），到期要重掛。
- 階梯全數成交後（>585）程式會提醒，但不會自動生成新一組——需要人工決定新價位。
- 純價格規則，不看基本面。引擎 4 是唯一的基本面代理，而且是落後訊號。
- 證交所 API 沒有正式版本承諾，改版會讓抓取失敗；`run` 會回非 0 錯誤碼。

---

## 免責

這是規則執行工具，不是投資建議。所有價位由公開資料與 `config.py` 的公式產生，
下單前請自行確認即時報價。
