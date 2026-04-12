# Wick Reversal 1m v4

## 1. 版本定位

- `v4` 是針對 `tick` 級回放重構的 long-only 研究版。
- 核心目標：把主要的訊號、觸發、成交、停損放回真實事件順序，盡量減少 v3 在 bar 模式下的 look-ahead。
- 相較 v3，v4 保留相同的 zoom 進場結構，以更嚴格的 k0 品質過濾（吸收確認）取代 v3 的形態條件。
- `v4` 預設應以 `tick_data` 重建出的 1m bars + 對應 tick map 做回測；若直接混用外部 1m kline cache，可能出現 bar/tick 不一致。
- `v4` 目前仍保留 bar fallback；因此只有在完整 tick coverage 下，才接近無 look-ahead 的研究基準。

---

## 2. 策略結構

```
k0（吸收確認，無 range 濾網）
  └─ zoom 窗口（1 ~ zoom_bars 根後，守護線 = k0 實體低點）
       └─ 進場棒：tick > k0 實體高點 且累計 delta_eff > threshold
```

### 2.1 k0

做多 `k0` 同時滿足：

1. 不看紅綠，只看形態（color-agnostic）。
2. 實體位於整根 K 棒上半部。
3. 下影線明顯大於實體。
4. 下影線區域必須出現吸收（賣壓被承接）。

> **Range 濾網**：目前停用，以最大化訊號數量，供初期探索用。

形態定義：

```text
range > 0
body_low >= mid
lower_wick > body

mid        = (high + low) / 2
body_low   = min(open, close)
body       = abs(close - open)
lower_wick = body_low - low
```

出現新 k0 時，舊 k0 被覆蓋（只保留最新一根）。

### 2.2 吸收判斷

#### Tick 模式

優先使用 `k0` 對應 minute 內的 tick：

```text
wick_ticks     = ticks where price <= body_low
wick_vol       = sum(qty)
total_vol      = sum(all qty)
wick_buy_vol   = sum(qty where is_buyer_maker == False)
wick_delta     = 2 * wick_buy_vol - wick_vol
wick_delta_eff = wick_delta / wick_vol
```

做多吸收成立條件：

```text
wick_vol / total_vol >= lower_wick_absorption_min_vol_ratio
wick_delta_eff <= lower_wick_absorption_delta_eff_max
```

代表下影線區域有足夠成交量，且主動賣壓被承接（delta 偏賣方或中性）。

#### 無 tick fallback

若沒有 tick，退回 bar 級近似：

```text
bar_delta <= lower_wick_absorption_bar_delta_max
```

這只是防守性近似，不應拿來當 deploy-grade 結論。

### 2.3 Zoom 進場

`k0` 形成後，在 `zoom_bars` 根內尋找進場棒，條件：

1. `k.low >= k0_body_low`（**守護線為 k0 實體低點**，比原本 k0.low 更寬鬆）
2. 進場條件達標（詳見下方 tick / bar 模式說明）

若守護線被破或超出 `zoom_bars`，k0 立即失效。

---

## 3. 進場條件

### 3.1 Tick 模式（優先）

遍歷進場棒內的 aggTrade，逐步累計 delta：

```text
cum_delta_eff = (2 * cum_buy_vol - cum_vol) / cum_vol
```

觸發條件（依序判斷）：

1. 若 `price < k0_body_low` → 守護線破壞，**立即返回失敗**
2. 若 `price > k0_body_high` 且 `cum_delta_eff > long_delta_eff_threshold` → **入場**

```text
fill_price   = tick.price              # 實際 tick 成交（可能穿越 k0_body_high）
signal.price = k0_body_high            # 圖表基準標記
stop_price   = k0_body_low - sl_offset
risk         = fill_price - stop_price
target_price = fill_price + risk * rr_ratio
```

> **Vol SMA 前置檢查**：使用前一根已收棒 volume（`klines[i-1].volume`）對比 SMA，
> 避免使用當根尚未收盤的 volume 造成 look-ahead。
> SMA 窗口：`klines[i-period .. i-1]`（共 `period` 根，不含當根）。

### 3.2 Bar 模式（tick 不可用時的近似）

```text
k.high >= k0_body_high
delta_eff(k) > long_delta_eff_threshold
k.volume > vol_sma(klines[i-period..i-1]) * long_vol_sma_mult
```

```text
entry_price  = k0_body_high            # 理想進場價（含輕度 look-ahead）
stop_price   = k0_body_low - sl_offset
risk         = entry_price - stop_price
target_price = entry_price + risk * rr_ratio
```

> Bar 模式的 delta_eff 使用整根已收棒（結束後才知道），仍有輕度 look-ahead。
> 僅作保守估計，不應作為 deploy-grade 基準。

---

## 4. 停損與目標

```text
stop_price   = k0_body_low - sl_offset
risk         = fill_price - stop_price
target_price = fill_price + risk * rr_ratio
```

`sl_offset` 預設 10.0 USDT，停損掛在 k0 實體低點下方，保留足夠彈性。

---

## 5. 出場管理

長倉管理維持既有 long-side 概念（優先順序由上至下）：

1. `SL`：`k.low <= stop_price`（trailing 前）
2. `TS`：`k.low <= stop_price`（trailing 後）
3. `TP`：`price >= target_price` 且 `cum_delta <= 0`（tick 模式用觸及 TP 瞬間的累計 delta）
4. 達到 target 且 `cum_delta > 0` → 切換 trailing，`stop_price = target_price`
5. `TD`：trailing 模式下連續 `td_consec_bars` 根反向 delta → 以 `k.close` 出場

---

## 6. 參數列表

| 參數 | 預設值 | 說明 |
|---|---:|---|
| `zoom_bars` | `5` | k0 後允許進場的最大觀察根數 |
| `k0_vol_gate` | `50.0` | k0 最低成交量門檻（volume < gate 則不視為 k0） |
| `sl_offset` | `10.0` | 固定停損位移（k0_body_low - sl_offset） |
| `rr_ratio` | `1.0` | 盈虧比 |
| `long_delta_eff_threshold` | `0.6` | 進場 delta_eff 門檻 |
| `long_vol_sma_period` | `20` | 成交量 SMA 窗期；0=不過濾 |
| `long_vol_sma_mult` | `1.2` | 成交量門標倍率 |
| `td_consec_bars` | `2` | 連續幾根反向 delta 觸發 TD 出場 |
| `lower_wick_absorption_delta_eff_max` | `0.0` | wick 區吸收 delta_eff 上限 |
| `lower_wick_absorption_min_vol_ratio` | `0.15` | wick 區成交量佔比下限 |
| `lower_wick_absorption_bar_delta_max` | `0.0` | 無 tick 時 bar 級吸收近似的 delta 上限 |

---

## 7. 與 v3 的主要差異

| 面向 | v3 | v4 |
|---|---|---|
| k0 定義 | 看跌陰線 + 收在上半部 | 不看顏色，純形態（color-agnostic） |
| k0 吸收 | 無 | tick 級 wick-zone delta 檢驗 |
| k0 range 濾網 | 無下限 | 停用（初期探索，最大化訊號數） |
| 進場基準價 | k0.high | k0 實體高點（body_high） |
| 進場觸發 | tick >= k0.high | tick > k0_body_high（strictly greater） |
| 守護線 | k0.low | k0_body_low（更寬鬆，減少 zoom 提早失效） |
| 進場 delta | 整棒 delta_eff（look-ahead） | tick 累計 delta_eff（即時，無 look-ahead） |
| 進場 Vol SMA | 含當根（look-ahead） | 使用前一根已收棒（無 look-ahead） |
| 停損 | `k0.low - sl_offset` | `k0_body_low - sl_offset` |
| zoom_bars | 3 | 5 |

---

## 8. 每根 K 棒執行順序

```
Step 1：持倉管理（若 in_position）
  ├─ tick 模式：逐 tick 判斷 SL/TP/TS/TD
  └─ bar 模式：以 K 棒邊界判斷

Step 2：k0 zoom 進場判定（若 k0 有效 且 i > k0_idx）
  ├─ bars_after > zoom_bars → k0 失效
  ├─ k.low < k0_body_low → k0 失效（實體低點守護線破壞）
  └─ 否則嘗試進場（tick / bar 模式）

Step 3：k0 偵測（若 not in_position）
  └─ 符合 k0 形態 → 覆蓋 k0 狀態，發出 k0_long 訊號
```

---

## 9. 回測基準

`v4` tick 研究應使用：

```bash
python utils/tick_data_backtest.py --strategy "Wick Reversal 1m v4" --symbol BTCUSDT --tick-dir tick_data --fee-mode Taker
```

此腳本會：

1. 直接讀取 `tick_data/*.zip`
2. 由 tick 重建 1m bars
3. 建立與 bars 完全一致的 `tick_map`
4. 使用專案既有 `backtest.engine` 做成交與統計

---

## 10. 目前結論與下一步

- 本次修改以「擴大成交數」為主要目標，簡化進場條件：
  - 移除 range SMA 濾網
  - zoom_bars 3 → 5
  - 守護線改用 k0_body_low（比 k0.low 更寬鬆）
  - 進場觸發改用 k0_body_high（比 k0.high 更低，更容易觸及）
  - 停損同步改為 k0_body_low - sl_offset
- 下一步方向（待回測結果）：
  - 觀察成交數是否顯著增加
  - 逐步重新收緊 delta_eff threshold、vol SMA 等參數
  - 考慮是否要補 short 對稱版
  - 評估 session / volatility regime filter 的必要性
