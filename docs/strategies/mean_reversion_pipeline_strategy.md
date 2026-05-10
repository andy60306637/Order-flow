# 均值回歸 Pipeline 策略系統文件

**檔案：** `strategies/pipeline/mean_reversion.py`
**類型：** 僅做多、均值回歸、K棒內 Tick 執行
**時間框架：** 1 分鐘 K 線（主要回測目標）
**標的：** 加密貨幣永續合約（USDT 本位，taker 手續費市場）

---

## 架構概覽

策略建立在無狀態的 `TradingPipeline` 框架上。每根新 K 棒，Pipeline 按順序執行各 Stage。任何 Stage 回傳 `None` 即阻斷 Pipeline，不產生訊號。若所有 Stage 通過，Pipeline 輸出一個完整的交易設定（`PipelineContext`）。

```
PositionGateStage
    ↓ (已有倉位 >= max_positions → 阻斷)
CooldownStage
    ↓ (距上次訊號 < 5 分鐘 → 阻斷)
RegimeStage           ← Stage 1
    ↓ (市場非 MEAN_REVERSION 或不在低乖離區或非有效時段 → 阻斷)
AlphaStage            ← Stage 2（OR：三因子取第一個成立者）
    ↓ (無訊號 → 阻斷)
EntryManagementStage  ← Stage 3
    ↓ (ATR 停損無效 → 阻斷)
RRStage
    ↓ (倉位大小為 0 → 阻斷)
FeeCoverRatioStage    ← Stage 4
    ↓ (毛利 < 1.2 × 往返費用 → 阻斷)
→ 訊號輸出
```

兩個 Gate Stage 放在最前端，避免在已有倉位或冷卻期內做無謂的昂貴計算。

---

## Stage 1：Regime 過濾（RegimeStage）

三個獨立維度必須全部通過，Pipeline 才繼續執行。每個維度透過 `RegimeClassifier` 計算出一個 label，Stage 將 label 與允許清單比對。

### 維度 A — 市場波動率 Regime

**元件：** `MarketVolatilityRegimeComponent`
**維度鍵：** `market_vol_regime`
**必要 label：** `MEAN_REVERSION`

該元件使用已實現波動率比值、ATR 比值、效率比值（方向位移 / 路徑長度）及 ADX 的綜合評分，將市場分為四種 Regime。MEAN_REVERSION 在價格呈震盪（低方向效率、低 ADX、中等波動）時發出。這是最主要的環境閘門 — 趨勢市場或高波動市場不交易。

**預設參數：**
```
rv_period=60, atr_short=10, atr_long=60, er_period=30, adx_period=14, lookback=100
```

### 維度 B — VWAP 乖離區帶

**元件：** `VWAPDeviationRegimeComponent`（包裝 `VWAPDeviationComponent`）
**維度鍵：** `vwap_dev`
**必要 label：** `extended_low` 或 `overextended_low`

元件計算最近 120 根 K 棒（約 2 小時）的滾動 VWAP，再以 300 根 K 棒的分佈對當前收盤偏差做 z-score 標準化。z-score 決定區帶：

| 區帶 | z-score 範圍 | 方向 |
|------|------------|------|
| `normal` | \|z\| < 1.0 | — |
| `extended_low` | 1.0 ≤ \|z\| < 2.0 | 收盤低於 VWAP |
| `extended_high` | 1.0 ≤ \|z\| < 2.0 | 收盤高於 VWAP |
| `overextended_low` | 2.0 ≤ \|z\| ≤ 2.5 | 收盤低於 VWAP |
| `overextended_high` | 2.0 ≤ \|z\| ≤ 2.5 | 收盤高於 VWAP |
| `extreme_low` | \|z\| > 2.5 | 收盤低於 VWAP |
| `extreme_high` | \|z\| > 2.5 | 收盤高於 VWAP |

策略只在價格低於 VWAP（`extended_low` 或 `overextended_low`）時進場。`extreme_low` 刻意排除——極端乖離可能代表真正的行情崩跌而非均值回歸機會。

**預設參數：**
```
vwap_window=120, vwap_lookback=300, overextended_low=2.0, overextended_high=2.5
```

**1 分鐘 K 線的窗口選擇依據：** `window=24`（24 分鐘）雜訊過高；`window=120`（2 小時）能捕捉日內均值行為。`lookback=300`（5 小時）提供足夠的分佈樣本做 z-score 標準化。

### 維度 C — 交易時段

**元件：** `SessionComponent`
**維度鍵：** `session`
**必要 label：** `asian`、`london`、`ny`、`overlap`

限制在三大主要時段及其重疊時段交易。非交易時段（例如亞洲早盤前、紐約盤後）因流動性薄弱、均值回歸特性不穩定而排除。

---

## Stage 2：Alpha 因子 Stage（AlphaStage，OR 模式）

三個訊號模組按優先順序逐一評估。第一個產生有效進場訊號的模組獲勝，其餘不再評估。組合邏輯：`a || b || c`。

### 優先 1 — LowerWickDeltaEffSignal（吸收因子）

識別「賣方嘗試下壓但積極買方吸收賣壓」的 K 棒，視覺上呈長下影線搭配正淨 taker delta。

**信號棒：** `klines[idx-1]`（執行棒的前一根）

**條件（全部滿足）：**
1. `下影線 / 振幅 >= 0.40` — 下影線至少佔全幅 40%
2. `(taker_buy - taker_sell) / volume >= 0.10` — 淨買方佔比至少 10%
3. `wick_ratio × imbalance >= 0.04` — 綜合效率指標達標

**直覺意義：** 下影線代表低價被拒絕；正 delta 代表買方是積極方。兩者並存代表在乖離區帶有機構級別的吸收行為。

**預設參數：** `min_wick_ratio=0.40, min_imbalance=0.10, min_eff=0.04`

> **設計注意：** 預設值下 `min_eff = min_wick_ratio × min_imbalance = 0.04`，三個條件並非完全獨立——前兩個成立時第三個必然成立。若要讓 `min_eff` 發揮獨立過濾作用，需將其設高於兩者乘積。

### 優先 2 — CVDDivergenceSignal（CVD 背離因子）

在滾動窗口內識別「價格低點與累積成交量差額背離」的牛背離訊號，是空頭動能衰竭的經典特徵。

**信號棒：** `klines[idx-1]`

**條件：**
1. 信號棒最低價 ≤ 窗口前低 × (1 + price_tolerance)：確認價格在近期低點附近或更低
2. 信號棒的滾動 CVD > 前低點的滾動 CVD：在相同或更低的價格，買盤淨力量更強

**CVD 計算：** 在窗口起點從 0 開始，累計 `(taker_buy - taker_sell)`。背離幅度 = `cvd_k0 - cvd_prev_trough`。

**預設參數：** `window=20, price_tolerance=0.002, min_cvd_divergence=0.0`

> `min_cvd_divergence=0.0` 接受任何正背離。回測中可調高以減少噪訊。

### 優先 3 — ReversalBarUpSignal（型態因子）

經典反轉蠟燭型態：高於平均振幅、長下影線（≥50% 振幅）、強勢收盤位置（≥60% 從低點起算），代表明顯的價格拒絕。

**信號棒：** `klines[idx-1]`

**條件：**
1. 棒振幅 > 最近 SMA(20) 平均振幅 — 高於平均動能
2. 下影線比例 >= 0.50
3. 收盤位置 `(close - low) / range` >= 0.60

**預設參數：** `sma_period=20, min_lower_wick_ratio=0.5, min_close_pos=0.6`

---

## Stage 2 → Stage 3 交接：Tick 級別進場（`_mr_long_entry`）

訊號模組的 `detect_k0` 確認信號棒後，`entry_conditions` 在執行棒（`klines[idx]`）上執行。進場使用雙重觸發驗證：

**觸發線：** 信號棒的最高價（HIGH）作為觸發價格線。

**Tick-first 路徑**（有 `tick_map` 時）：
按時間順序掃描執行棒的 tick 流，逐筆累計 Micro-CVD：
- taker buy（買方為 aggressor）→ `+qty`
- taker sell（賣方為 aggressor，`is_buyer_maker=True`）→ `-qty`

同時滿足以下兩個條件的第一筆 tick 觸發進場：
- `tick_price >= trigger_price` — 價格穿越信號棒高點
- `micro_cvd > min_micro_cvd` — 執行棒的累積買盤動能達標

**Kline fallback**（無 tick 資料時）：
- `exec_bar.high >= trigger_price` — K 棒到達觸發線
- `kline_delta (taker_buy - taker_sell) > min_micro_cvd`
- `fill_price = max(exec_bar.open, trigger_price)`

**有效期：** 1 根執行棒（信號棒的緊接下一根）。若該棒未觸發，訊號失效。

**初始停損：** `k0.low - sl_offset`（由 EntryManagementStage 以 ATR 覆蓋）。

---

## Stage 3：出入場管理（EntryManagementStage）

從 `alpha_meta["k0_meta"]["k0_idx"]` 定位信號棒，計算最終停損價格。

**ATR 停損計算：**
```
raw_stop = signal_bar.low − ATR(14) × k
cap_stop = entry_price × (1 − max_sl_pct)
final_stop = max(raw_stop, cap_stop)   ← 取較高者（即停損距離較小者）
```
`max()` 取兩者中更高的值，也就是距進場更近（更緊）的停損。Cap 防止高波動期 ATR 過大導致倉位接近零。

**預設參數：** `atr_period=14, atr_k=1.0, max_sl_pct=0.03`

若 `entry_price <= final_stop`（邏輯錯誤，進場在停損之下），Stage 阻斷 Pipeline。

**出場骨架**（寫入 `alpha_meta["exit_plan"]`）：

| 出場類型 | 狀態 | 說明 |
|---------|------|------|
| TP（主目標） | 已啟用 | RRStage 計算，2RR |
| SL（風險止損） | 已啟用 | ATR 基準，此 Stage 計算 |
| 時間止損 | TODO | 待因子衰退週期分析後設置 |
| 資訊止損 | TODO | 待事件驅動出場規則設計 |
| Regime 止損 | TODO | 即時交易時部屬 |

---

## Stage 4a：風險報酬 Stage（RRStage）

**輸入：** `entry_price`、`stop_price`、`direction`

**計算：**
```
risk     = |entry - stop|
tp_price = entry + risk × rr_ratio   （做多）
qty      = CapitalModule.position_size(equity, entry, stop)
```

**預設：** `rr_ratio=2.0`，倉位大小由 `CapitalConfig` 決定（預設：`CapitalConfig()`）。

若 RR 條件不符或資金配置回傳零/None，Pipeline 阻斷。

---

## Stage 4b：費用覆蓋率過濾（FeeCoverRatioStage）

對標 `WickReversalV4Strategy._risk_covers_cost()`。

**公式：**
```
round_trip_cost = 2 × (taker_fee_rate + slippage_rate) × entry_price
gross_reward    = risk × rr_ratio
通過條件：gross_reward >= round_trip_cost × fee_cover_ratio
等同：    risk >= round_trip_cost × fee_cover_ratio / rr_ratio
```

直覺：毛利（2倍風險）至少要覆蓋往返費用的 1.2 倍。

**預設參數（WickReversalV4 基準）：**
```
taker_fee_rate  = 0.00032   （0.032% taker，Binance USDT Perp）
slippage_rate   = 0.00002   （0.2 bps）
fee_cover_ratio = 1.2
```

通過後寫入：`expected_fee`、`net_reward`、`fee_approved=True`。

---

## Gate Stages

### PositionGateStage

維護內部計數器 `_open_count`。當 `open_count >= max_positions` 時阻斷 Pipeline。

**預設：** `max_positions=1`（嚴格同時間只有一筆倉位）

**外部整合契約：**
- 成交確認後：呼叫 `gate.record_open()`
- 任何出場（TP/SL/時間/資訊）後：呼叫 `gate.record_close()`
- 回測重置時：呼叫 `gate.reset()`

### CooldownStage

記錄最近一次訊號觸發的時間戳（ms）。若當前 K 棒開盤時間距上次訊號不足 `cooldown_ms`，阻斷 Pipeline。

**預設：** `cooldown_ms=300_000`（5 分鐘，1m K 線下 = 5 根棒）

**外部整合契約：**
- 任何出場後：呼叫 `cooldown.record_signal(exit_time_ms)`
- 回測重置時：呼叫 `cooldown.reset()`

**位置說明：** 兩個 Gate Stage 放在 RegimeStage 前端，避免在無效棒上計算 VWAP 和波動率 Regime。

---

## SharedContext 快取機制

運算密集的元件（VWAPDeviation、MarketVolatilityRegime、ATR）以 `component_id` 為鍵寫入 `SharedContext`。在同一根 K 棒上，即使多個 Pipeline 共用同一個 `SharedContext`，每個元件最多只計算一次。

`SharedContext.invalidate(klines, idx)` 在每根新棒開始時必須由 Runner 呼叫。`MultiPipelineRunner` 會自動處理。

---

## 訊號輸出欄位

Pipeline 全部通過後，`PipelineContext` 包含：

| 欄位 | 類型 | 說明 |
|------|------|------|
| `direction` | `"long"` | 永遠是多單（僅做多策略） |
| `entry_price` | float | Tick 成交價或 Kline fallback |
| `stop_price` | float | ATR 基準，有上限 cap |
| `tp_price` | float | entry + 2 × risk |
| `expected_rr` | float | 當前設定永遠為 2.0 |
| `qty` | float | 倉位大小（合約數） |
| `risk_amount` | float | risk × qty（USD 風險金額） |
| `expected_fee` | float | 預估往返費用 |
| `net_reward` | float | 毛利扣除費用後的淨利 |
| `fee_approved` | bool | 永遠為 True（通過此 Stage 後） |
| `alpha_meta["module"]` | str | 觸發因子名稱 |
| `alpha_meta["k0_meta"]` | dict | 信號棒 metadata（k0_idx、k0_low、因子數值） |
| `alpha_meta["exit_plan"]` | dict | TP/SL 數值 + TODO 預留欄位 |
| `alpha_meta["fill_time"]` | int | Tick 成交時間戳 ms（Kline fallback 則為開盤時間） |
| `alpha_meta["micro_cvd"]` | float | 成交時的 Micro-CVD 累計值 |

---

## 工廠函式與整合範例

```python
from strategies.pipeline.mean_reversion import build_mean_reversion_pipeline
from strategies.pipeline import PipelineDef, MultiPipelineRunner
from strategies.modules.capital_management import CapitalConfig

# 建立 Pipeline
pipeline = build_mean_reversion_pipeline(
    allowed_vwap_zones=("extended_low", "overextended_low"),
    capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
)

# 取得 Gate Stage 以便外部回調
gate_stage     = pipeline.stages[0]   # PositionGateStage
cooldown_stage = pipeline.stages[1]   # CooldownStage

# 回測引擎整合
# 成交後：
gate_stage.record_open()
# 出場後：
gate_stage.record_close()
cooldown_stage.record_signal(exit_time_ms)
```

UI 登錄使用 `MeanReversionPipelineStrategy`，以預設參數包裝工廠函式。

---

## 回測參數優化參考

| 參數 | 預設值 | 探索範圍 | 說明 |
|------|--------|---------|------|
| `vwap_window` | 120 | 60–240 | 越小訊號越多、雜訊越高 |
| `vwap_lookback` | 300 | 120–500 | 越大 z-score 分佈越平滑 |
| `allowed_vwap_zones` | `(extended_low, overextended_low)` | 加入/移除 `overextended_low` | 移除後訊號減少但品質提升 |
| `min_micro_cvd` | 0.0 | 0–500 | 執行棒品質主要濾網 |
| `atr_k` | 1.0 | 0.5–2.0 | 停損距離乘數 |
| `max_sl_pct` | 0.03 | 0.01–0.05 | 停損距離硬性上限 |
| `cooldown_ms` | 300_000 | 60k–600k | 1 分鐘至 10 分鐘 |
| `cvd_min_divergence` | 0.0 | 0–1000 | 調高以降低 CVD 假信號 |
| `rr_ratio` | 2.0 | 1.5–3.0 | TP 目標 |

---

## 已知問題與技術債

| 問題 | 嚴重性 | 說明 |
|------|--------|------|
| 模組 docstring 第 12 行提及 VolumeAreaStage | 低 | 該 Stage 已從 Pipeline 移除，class 定義保留但未使用，docstring 應更新 |
| `VWAPDeviationRegimeComponent` 類別預設 `window=24, lookback=100` | 低 | 與工廠預設 `120/300` 不符；直接實例化類別會得到雜訊版本 |
| `FeeCoverRatioStage` 類別預設費率偏高 | 低 | 類別預設 `0.0005/0.0002`，工廠正確覆蓋為 `0.00032/0.00002` |
| `CooldownStage.remaining_ms` 回傳絕對時間戳而非剩餘時間 | 低 | 屬性名稱與語意不符，供 debug 用，不影響交易邏輯 |
| `RRStage min_rr` 檢查永遠不過濾 | 低 | `min_rr=rr_ratio=2.0` 時 expected_rr 永遠等於 2.0，檢查無效 |

---

## 未來功能規劃

| 功能 | 狀態 |
|------|------|
| 部分出場（1R 出半倉，剩餘 Trailing 至 VWAP） | 未實作 |
| 時間止損（因子衰退週期 → 棒數上限） | 待因子分析 |
| 資訊止損（事件驅動出場訊號） | 待設計 |
| Regime 變化出場 | 即時交易時部屬 |
| 空單訊號 | 暫緩 |
