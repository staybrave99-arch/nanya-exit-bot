# 南亞科 2408 分批出場機器人

> **目前沒有實際部署**：Fly.io 排程器、GitHub Actions 圖表自動更新、
> GitHub Pages 都已經下線，程式碼保留在這個 repo 供之後需要時重新部署。
> 移除前的最後持倉狀態備份在 [backups/](backups/)。

每個交易日 20:00（台北）自動抓證交所收盤資料、重算指標、判斷該建議賣哪一批，
把「賣出建議」推到 ntfy。訊息一律用建議語氣、張數用佔總部位的百分比——這支
程式沒有真的連券商，不代表已經真的成交。**內部仍是有狀態**：賣到第幾批、
每批成交價、今天觸發但明天才執行的單，全部存在 `state.json`，供 `status`
指令查詢與 `mark-filled`/`dequeue`/`unfill` 校正，只是不會每天推播出來。

```
收 445.0（+2.06%）
MA20 411.2｜ATR14 37.7｜乖離 +8.2%
──建議：明日掛單──
R1 470×10%　R2 495×10%　R3 520×10%
R4 550×10%　R5 585×10%
停利 405.6 建議賣 15%／367.9 全部
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
python -m nanya_exit dequeue SLOW --reason "..."   # 撤掉還在排隊、明日開盤還沒執行的單
python -m nanya_exit unfill R1 --reason "..."      # 撤銷已標記成交、但實際沒成交的批次
python -m nanya_exit chart-data --out docs/chart-data.json   # 產生圖表資料 JSON
python -m nanya_exit backtest --paths 200          # 用合成路徑比較候選策略
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
pytest -q          # 56 個測試
```

指標部分對照人工算過的答案：MA20 411.2、MA60 388.0、ATR14 37.7、H20 481.0
（皆為 2026-08-05 收盤）。引擎部分用合成 K 棒逐條驗證觸發、成交價、跨日排隊、
冪等、張數不超賣。

---

## 部署到 Fly.io

> **目前未部署**（app、machine、volume 都已移除）。下面是重新部署要跑的步驟；
> `fly.toml`／`Dockerfile`／`scheduler.py` 都還在，程式碼不用改。

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

## 圖表（GitHub Pages，目前未發布）

`docs/index.html` 是一頁式的收盤價 + 出場價位圖，資料來自 `docs/chart-data.json`
（現在是移除部署前最後一次產生的靜態快照，不會再自動更新）。之前的自動化是
用 `.github/workflows/update-chart.yml` 每個交易日 19:00（台北，早於排程器的
20:00 巡檢——這樣通知裡附的圖表連結一點開就是當天資料）重新產生並 commit，
push 到 `main` 讓 Pages（從 `main:/docs` 部署）自動重新發布；這個 workflow
已經移除，Pages 也關掉了。要重新啟用的話：把 `chart-data` 這段排程邏輯的
workflow 加回來，並在 repo 設定裡把 GitHub Pages 重新指到 `main:/docs`。

`chart-data.json` 只讀市場資料算指標，跟 `state.json`／實際持倉無關；本地要
手動更新一次可以直接跑：

```bash
python -m nanya_exit chart-data --out docs/chart-data.json
```

---

## 策略回測（`backtest.py`）

`python -m nanya_exit backtest` 拿目前的四引擎方案跟 `nanya_exit/strategies.py`
裡的對照策略（目前只有 `pure_trailing_stop`：拿掉階梯與乖離，全倉交給移動停利）
互相比較，跟實盤完全獨立，不會動 `state.json`。

**評估方法**：這檔股票近一年漲了近 10 倍，只有這一條真實歷史路徑，直接拿來下
結論風險很高（換一種劇本結果可能完全不同）。所以用 **block bootstrap**：把歷史
K 棒切成連續區塊（預設 10 個交易日一塊）隨機抽樣重組成很多條合成路徑，並且
**去除每個區塊自己的方向性趨勢**（只留日內振幅/日對日相對走勢這種「形狀」），
避免單純複利串接把價格越滾越誇張——也就是說,合成路徑刻意假設「不再繼續複製
過去那種漲十倍的走勢」,只測試策略在正常波動下的表現。每個策略都跑過同一組
合成路徑（成對比較），主要看 **capture ratio 的變異係數（CV）**：這個數字越小,
代表策略在不同劇本下的結果越穩定,是目前排序的依據（相對地,不是挑均值最高的
那個）。

```bash
python -m nanya_exit backtest --paths 200 --length 180 --block-size 10
```

輸出裡的 `capture 均` = 已實現（+ 未實現部分用最後一天收盤 mark-to-market）
均價 ÷ 該路徑期間最高收盤，越接近 1 代表越接近賣在高點；`vs持有均` = 跟
「全程抱著不賣」比較的平均超額報酬。

**方向敏感度**：預設會把「偏空／中性／偏多」三種日均漂移假設（`SCENARIOS`，
`nanya_exit/backtest.py`）都跑一遍，而不是只信一個中性假設——這不是預測未來
會怎麼走，是讓你自己判斷要相信哪個情境，同時看得出結論對「未來方向」這個
假設有多敏感。三種情境如果排名結果一致，代表結論比較站得住腳；如果不一致，
代表該不該執行這套出場計畫，取決於你自己對後市的判斷，不是規則能幫你決定的。
`--scenarios 中性` 可以只跑其中一種。

---

## 已知限制

- 停利單假設**隔日開盤成交**。真的開跌停鎖死時賣不掉，實際成交價會比程式記的差。
  遇到這種情況用 `mark-filled` 修正。
- 台股一般委託單當日有效，階梯要掛**券商的長效條件單**（多數可設 30~90 天），到期要重掛。
- 階梯全數成交後（>585）程式會提醒，但不會自動生成新一組——需要人工決定新價位。
- 純價格規則，不看基本面。引擎 4 是唯一的基本面代理，而且是落後訊號。
- 證交所 API 沒有正式版本承諾，改版會讓抓取失敗；`run` 會回非 0 錯誤碼。
- `backtest` 的合成路徑刻意去除歷史趨勢（零漂移假設），只測試「正常波動下」的
  穩健度，不代表對未來漲跌方向的預測；也沒有計入手續費、證交稅、滑價。

---

## 免責

這是規則執行工具，不是投資建議。所有價位由公開資料與 `config.py` 的公式產生，
下單前請自行確認即時報價。
