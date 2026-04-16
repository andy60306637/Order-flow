# S4B 專項優化 Plan

## 目的

針對 `strategies/wick_reversal_v4.py` 的 `S4B` 做空訊號進行獨立優化，目標是在不破壞目前 `long` 與 `S4A` 已建立穩定性的前提下，提升 `S4B` 的：

- Validation profit factor
- Validation total return
- Full-sample consistency
- Drawdown control

目前觀察到的問題是：

- `S4B` 在部分區段表現很好，但在訓練區的穩定性不如 `S4A`
- `S4B` 與 `S4A` 共用大部分 short 邏輯，可能導致 `S4B` 沒有被用最適合自己的 entry / exit / risk 規則管理

---

## 現況假設

### 已知前提

- `S4C` 已關閉
- `S4A` 已新增 `short_a_min_upper_wick_pct`
- 目前 short 只剩 `S4A + S4B`
- `S4B` 在 validation 相對乾淨，但 train 端品質仍可再提升

### 核心假設

`S4B` 的問題比較像「管理方式不對」，而不是「訊號本身完全失效」。

優化方向應優先放在：

- `S4B` 專屬 entry gate
- `S4B` 專屬 RR / stop / trailing
- `S4B` 專屬 regime filter

不優先做的事：

- 再次對整個 short 全局暴力掃參
- 同時動 `S4A` 與 `S4B`，避免混淆因果

---

## 優化目標

### Primary

- 提升 `S4B` short-only validation PF 至 `> 1.35`
- 提升 `S4B` short-only full-sample PF 至 `> 1.20`
- 控制 `S4B` short-only max drawdown 不高於目前 short defaults

### Secondary

- 提升 combined strategy 的 validation PF
- 若交易數下降，必須換到更高的單筆品質，而不是單純把 trade count 壓小

### 不接受的結果

- Train 明顯變好，但 validation 明顯惡化
- Combined 表現提升只來自 trade 數大幅減少
- 需要用過多 hard-coded regime 才成立

---

## 研究切分

維持目前同一套資料切分，避免和既有報告口徑不一致：

- Train: `2025-04-14` ~ `2026-01-31`
- Validation: `2026-02-01` ~ `2026-04-13`
- Data: tick-level backtest
- Assumptions:
  - initial capital `10000 USDT`
  - leverage `20x`
  - fee `0.032%`
  - slippage `0.2 bps`

---

## Phase 1: S4B 結構拆解

目標：先把 `S4B` 從整體 short 行為中獨立出來。

### 任務

1. 產出 `S4B` short-only baseline 報表
2. 拆出 `S4B` 在 train / validation / full 的：
   - trades
   - win rate
   - PF
   - avg win / avg loss
   - exit label 分布 (`SL/TP/TS/TD`)
3. 檢查 `S4B` 的 k0 特徵分布：
   - upper wick 絕對百分比
   - wick/body ratio
   - k0 volume
   - upper-wick absorption 強度
   - entry bar delta_eff
4. 檢查 `S4B` 在不同 regime 的表現：
   - 高波動 / 低波動
   - 急拉後 / 非急拉後
   - 高量 / 低量

### 產出

- `S4B` baseline 指標表
- `S4B` 特徵差異摘要
- `S4B` 失敗樣本類型整理

---

## Phase 2: S4B Filter 設計

目標：定義最小且可解釋的 `S4B` 專屬 filter。

### 優先測試候選

1. `S4B` 最小上影線絕對幅度
2. `S4B` 最小 upper-wick / body ratio
3. `S4B` 最小前置拉升幅度
4. `S4B` 最小 k0 成交量
5. `S4B` 最小 upper-wick absorption volume ratio
6. `S4B` 僅在高波動 regime 啟用

### 設計原則

- 每次只加一層 filter
- 先驗證 validation，再看 full
- filter 必須有市場結構上的解釋，不接受純 curve-fit

---

## Phase 3: S4B Risk Management 專屬化

目標：讓 `S4B` 不再完全沿用 `S4A` 的 short 管理方式。

### 優先測試參數

1. `short_rr_wick_b`
2. `short_sl_offset`
3. `short_td_consec_bars`
4. `short_min_fee_cover_ratio`
5. `short_vol_sma_mult`

### 方法

- 鎖住 `S4A` 現況不動
- 鎖住 `S4B` filter
- 只對 `S4B` 管理規則做小範圍搜尋

### 原則

- 先做低維搜尋
- 若 validation 不提升，直接回退
- 不把 `S4B` 調成極低頻策略來美化 PF

---

## Phase 4: Combined 驗證

目標：確認 `S4B` 的改動真的能改善最終策略，而不是只改善 isolated backtest。

### 驗證項目

1. Combined train / validation / full
2. `L4A/L4B/L4C/S4A/S4B` label breakdown
3. 總 trade 數變化
4. 總 drawdown 變化
5. `S4B` 是否與 `S4A` 產生互相干擾

### 通過標準

- Combined validation PF 提升，或
- Combined validation return 不降太多但 drawdown 明顯下降

---

## 技術改動清單

預計可能需要在 `wick_reversal_v4.py` 增加：

- `enable_short_wick_b`
- `short_b_min_upper_wick_pct`
- `short_b_min_wick_body_ratio`
- `short_b_min_runup_pct`
- `short_rr_wick_b` 的獨立驗證流程
- 若有必要，新增 `S4B` 專屬 regime gate helper

若分析工具不夠，則在 `utils/optimize_wick_reversal_v4.py` 補：

- `S4B` isolated runner
- `S4B` label-level feature export
- `S4B` 專用 validation summary

---

## 風險

### 主要風險

- `S4B` 本質上可能是低樣本、高變異訊號
- `S4B` 在 validation 好，不代表在未來仍會穩
- 過度特化 `S4B` 可能讓策略整體複雜度上升

### 控制方式

- 優先使用可解釋 filter
- 每次只增加一個結構條件
- 保留 baseline / filtered / optimized 三組對照

---

## 執行順序

1. 先做 `S4B` short-only baseline 與特徵拆解
2. 找出最小有效 `S4B` filter
3. 再做 `S4B` 專屬 risk management 微調
4. 最後回到 combined strategy 驗證
5. 只有 validation 與 full 都合理時，才寫回預設值

---

## 完成標準

以下條件同時成立，才視為 `S4B` 專項優化完成：

- 有一組明確的 `S4B` 專屬規則
- validation 沒有惡化
- full-sample PF / drawdown 至少有一項明確改善
- 有書面報告說明：
  - 做了哪些假設
  - 哪些 filter 有效
  - 哪些 filter 無效
  - 最後為何採用該版本
