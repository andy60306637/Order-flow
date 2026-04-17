# Wick Reversal 1m v4

## 1. 版本定位

- `v4` 是針對 `tick` 級回放重構的 long + short 研究版。
- 核心目標：把主要的訊號、觸發、成交、停損放回真實事件順序，徹底消除 look-ahead。
- **最新演進**：引入 **Wick 品質分級 (A/B/C)** 與 **動態 RR**，並針對做空側加入 **S4B (Short for B-grade)** 優化過濾器。
- `v4` 預設以 `tick_data` 重建出的 1m bars + 對應 tick map 做回測。
- 做多與做空各自獨立追蹤 k0，同時只允許一個持倉方向。

---

## 2. 核心機制

### 2.1 品質分級 (Wick Classification)
根據 `k0` 的影線長度與實體（或最小實體門檻）之比例進行分級：
- **A 級 (Elite)**：影線比例 >= `wick_type_a_threshold` (預設 4.0)
- **B 級 (Strong)**：影線比例 >= `wick_type_b_threshold` (預設 3.0)
- **C 級 (Base)**：其餘符合基本 k0 形態者。

*註：比例計算時，分母取 `max(body, body_floor)`，以避免十字星導致比例趨於無限大。*

### 2.2 動態盈虧比 (Dynamic RR)
不同級別的訊號採用不同的 RR 目標，以最大化優質訊號的獲利潛力。
- 例如做空：A 級 RR=4.5, B 級 RR=2.5, C 級 RR=2.0。

### 2.3 成本過濾 (Cost Filter)
進場前會計算預期 Risk 是否足以覆蓋「手續費 + 滑價」的特定倍率：
- `min_risk = round_trip_cost * fee_cover_ratio / RR`
- 若當前 Risk (Entry - SL) 小於此門檻，則放棄進場。

---

## 3. 策略結構

### 3.1 做多 k0（下影線吸收）
- 形態：`body_low >= mid` 且 `lower_wick > body`。
- 吸收：下影線區域 `delta_eff <= 0`。
- **Zoom 窗口**：預設 1 根。
- **守護線**：`k0_body_low`。

### 3.2 做空 k0（上影線吸收）
- 形態：`body_high <= mid` 且 `upper_wick > body`。
- 吸收：上影線區域 `delta_eff >= 0`。
- **Zoom 窗口**：預設 1 根。
- **守護線**：`k0_body_high`。

#### S4B 專屬過濾 (做空 B 級優化)
為提升做空 B 級訊號的勝率，新增以下選配過濾：
1. **最小影線幅度**：影線長度需佔價格一定比例 (e.g. 0.11%)。
2. **前置漲幅 (Runup)**：`k0` 出現前 N 根棒需有明顯漲幅。
3. **獨立成交量門檻**：B 級訊號可設定比 A 級更嚴格的成交量門檻。

---

## 4. 進場條件 (Tick 模式)

遍歷進場棒內的 aggTrade，逐步累計 delta：
`cum_delta_eff = (2 * cum_buy_vol - cum_vol) / cum_vol`

### 4.1 做多觸發
1. 若 `price < k0_body_low` → 守護線破壞，k0 失效。
2. 若 `price > k0_body_high` 且 `cum_delta_eff > long_delta_eff_threshold` → **入場**。

### 4.2 做空觸發
1. 若 `price > k0_body_high` → 守護線破壞，k0 失效。
2. 若 `price < k0_body_low` 且 `cum_delta_eff < -short_delta_eff_threshold` → **入場**。

> **Vol SMA 檢查**：使用 `klines[i-1]` 的成交量，避免 look-ahead。

---

## 5. 出場管理 (Symmetric)

1. **SL (Stop Loss)**：觸及 `stop_price`。
2. **TP (Take Profit)**：觸及 `target_price` 且 **動能已衰竭** (做多 delta<=0, 做空 delta>=0)。
3. **Trailing**：觸及 `target_price` 但動能仍強 → 將 `stop_price` 移動至 `target_price` 並開始追蹤。
4. **TD (Trend Disruption)**：Trailing 模式下，連續 `td_consec_bars` 根出現反向 delta，則以 `close` 出場。

---

## 6. 關鍵參數表 (v4 代碼現狀)

| 參數 | 預設值 (Long/Short) | 說明 |
|---|---|---|
| `zoom_bars` | `1 / 1` | k0 後允許進場的最大觀察根數 |
| `k0_vol_gate` | `500 / 300` | k0 最低成交量門檻 |
| `delta_eff_threshold` | `0.8 / 0.8` | 進場累計 delta_eff 門檻 |
| `wick_type_a_threshold` | `4.0` | A 級影線/實體比門檻 |
| `wick_type_b_threshold` | `3.0` | B 級影線/實體比門檻 |
| `rr_wick_a` | `3.0 / 4.5` | A 級預設 RR |
| `rr_wick_b` | `1.5 / 2.5` | B 級預設 RR |
| `min_fee_cover_ratio` | `1.2 / 2.0` | 預期收益需覆蓋費用的最小倍率 |
| `short_b_min_runup_pct` | `0.0` (選配) | 做空 B 級前置漲幅門檻 |

---

## 7. 執行順序 (每根 K 棒)

1. **持倉管理**：處理現有部位的出場 (SL/TP/TS/TD)。
2. **Zoom 進場判定**：檢查既存的 `long_k0` 或 `short_k0` 是否觸發。
3. **k0 偵測**：若無持倉，偵測當前棒是否構成新的 `k0`。若構成，記錄其品質分級與特徵。

---

## 8. 下一步與優化方向

- **回測驗證**：目前 `short_min_fee_cover_ratio` 設為 2.0 較為嚴格，需觀察是否過度過濾。
- **S4B 調優**：針對做空 B 級的 `runup` 參數進行網格搜索。
- **C 級訊號**：評估是否完全停用 C 級訊號以提升整體 PF。
- **動態 SL**：目前 SL 是固定位移，考慮改為基於影線長度的動態 SL。
