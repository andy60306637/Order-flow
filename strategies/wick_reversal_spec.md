# Wick Reversal 1m 策略規格（對齊 `wick_reversal.py`）

## 1) 策略定位

- 策略名稱：`Wick Reversal 1m`
- 交易方向：雙向（做多 / 做空）
- 資料粒度：1m K 線（使用 K 線層級 Delta）
- 持倉限制：同時最多 1 筆
- 版本：v3（同棒觸發、同棒成交語義）

---

## 2) 參數

| 參數 | 預設值 | 說明 |
|---|---:|---|
| `zoom_bars` | `5` | K0 後允許突破進場的最大觀察 K 棒數 |
| `sl_offset` | `10.0` | 進場後固定停損位移（USDT） |
| `rr_ratio` | `1.0` | 初始風報比（1.0 = 1:1） |
| `long_delta_eff_threshold` | `0.6` | 做多進場的 Delta Efficiency 最低門檻（0 ~ 1） |
| `long_vol_sma_period` | `20` | 做多成交量 SMA 窗期；0 = 不過濾 |
| `long_vol_sma_mult` | `1.2` | 做多成交量門標倍率（volume > SMA × mult） |
| `short_delta_eff_threshold` | `0.6` | 做空進場的 Delta Efficiency 最低門檻（0 ~ 1） |
| `short_vol_sma_period` | `20` | 做空成交量 SMA 窗期；0 = 不過濾 |
| `short_vol_sma_mult` | `1.2` | 做空成交量門標倍率（volume > SMA × mult） |
| `td_consec_bars` | `2` | 追蹤模式下需連續幾根反向 delta 才觸發 TD 出場 |

---

## 3) Delta 定義

策略使用 Binance K 線的 `volume` 與 `taker_buy_volume`：

```text
delta     = 2 * taker_buy_volume - volume
delta_eff = delta / volume          # 介於 -1 ~ +1，volume = 0 時視為 0
```

- `delta > 0` / `delta_eff > 0`：買方主動量較強
- `delta < 0` / `delta_eff < 0`：賣方主動量較強
- 進場條件使用 `delta_eff`，以標準化不同 volume 量級的行情；出場（TD）判斷使用原始 `delta`

---

## 4) 訊號型別與標籤

- `k0_long` / `k0_short`：僅圖表標記用，不直接開倉
- `long_entry` / `short_entry`：進場訊號
- `long_exit` / `short_exit`：出場訊號
- 出場標籤：
  - `SL`：固定停損
  - `TP`：1:1 停利
  - `TS`：追蹤停損（stop 已移至 1:1 位）
  - `TD`：追蹤模式下 Delta 反轉出場（同棒以 close 出場）

---

## 5) K0 判定條件

### 做多 K0

```text
close < open
close >= (high + low) / 2
(close - low) > abs(close - open)
high - low > 0
```

### 做空 K0

```text
close > open
close <= (high + low) / 2
(high - close) > abs(close - open)
high - low > 0
```

說明：
- K0 代表「長下影（做多）/ 長上影（做空）」的潛在反轉棒。
- 出現新 K0 時，內部狀態只保留最新一根（舊 K0 被覆蓋）。

---

## 6) 每根 K 棒執行順序（與程式一致）

程式對每根 K 棒按下列順序處理：

### Step 0：K0 標記（圖示）

- 條件：`not in_position` 且該棒滿足 K0 形態
- 動作：發出 `k0_long` / `k0_short` 標記訊號
- 注意：這一步只做標記，不更新 K0 指標狀態

### Step 1：持倉管理（僅 `in_position=True`）

#### 做多持倉優先順序

1. `k.low <= stop_price` -> `SL`（若 `trailing=True` 則標 `TS`）
2. 若 `trailing=True` 且 `delta <= 0` -> `td_consec += 1`；若 `td_consec >= td_consec_bars` -> `TD`，價格=`k.close`；否則重設 `td_consec = 0`
3. 若 `k.high >= target_price`：
   - `delta > 0` -> 切換 `trailing=True`，`stop_price = target_price`，`td_consec = 0`
   - 否則 -> `TP`，價格=`target_price`

#### 做空持倉優先順序

1. `k.high >= stop_price` -> `SL`（若 `trailing=True` 則標 `TS`）
2. 若 `trailing=True` 且 `delta >= 0` -> `td_consec += 1`；若 `td_consec >= td_consec_bars` -> `TD`，價格=`k.close`；否則重設 `td_consec = 0`
3. 若 `k.low <= target_price`：
   - `delta < 0` -> 切換 `trailing=True`，`stop_price = target_price`，`td_consec = 0`
   - 否則 -> `TP`，價格=`target_price`

#### Step 1 的流程分支

- 若出場成功：本棒會繼續執行 Step 2 / Step 3
- 若未出場：`continue`，本棒不再執行 Step 2 / Step 3

### Step 2：K0 + Zoom 進場判定

先決條件：
- 有 K0（`k0 is not None`）
- `i > k0_idx`
- `bars_after = i - k0_idx <= zoom_bars`

#### 做多分支

1. 若 `k.low < k0.low` -> K0 失效
2. 否則若以下三個條件**同時**成立 -> 進場：
   - `k.high >= k0.high`
   - `delta_eff > long_delta_eff_threshold`
   - `k.volume > vol_sma(long_vol_sma_period) × long_vol_sma_mult`（`long_vol_sma_period = 0` 時略過）

進場定義：

```text
entry  = k0.high
stop   = k0.low - sl_offset
risk   = entry - stop
target = entry + risk * rr_ratio
```

> Vol SMA 窗期含當根，使用當前索引前推 `period` 根 K 棒（含第 i 根）

#### 做空分支

1. 若 `k.high > k0.high` -> K0 失效
2. 否則若以下三個條件**同時**成立 -> 進場：
   - `k.low <= k0.low`
   - `delta_eff < -short_delta_eff_threshold`
   - `k.volume > vol_sma(short_vol_sma_period) × short_vol_sma_mult`（`short_vol_sma_period = 0` 時略過）

進場定義：

```text
entry  = k0.low
stop   = k0.high + sl_offset
risk   = stop - entry
target = entry - risk * rr_ratio
```

#### Step 2 補充

- 進場後會 `continue`，本棒不再執行 Step 3
- 若超過 `zoom_bars`，K0 直接失效

### Step 3：更新 K0 狀態指標

- 條件：`not in_position` 且 `high - low > 0`
- 動作：若當前棒符合做多/做空 K0 條件，則覆蓋 `k0 / k0_idx / k0_dir`

---

## 7) 價格語義（回測對接）

### Bar 模式

- 進場價（`signal.price`）：做多=`k0.high`，做空=`k0.low`
- `SL/TS`：`stop_price`
- `TP`：`target_price`
- `TD`：當根 `k.close`
- 策略不填 `fill_price`，回測引擎以 `signal.price` 計算損益

### Tick 模式

- `signal.price`（圖表基準）：做多=`k0.high`，做空=`k0.low`（同 Bar 模式）
- `signal.fill_price`（實際成交）：觸發條件成立時的 tick 實際成交價（可能穿越 k0 邊界）
- `SL/TS fill_price`：止損觸及時的 tick 實際價（可能比 stop_price 更差）
- 回測引擎應優先使用 `fill_price` 計算損益

---

## 8) 狀態機摘要

- `in_position`: 是否持倉
- `pos_dir`: `"long"` / `"short"`
- `stop_price`: 當前有效停損
- `target_price`: 1:1 目標價
- `trailing`: 是否進入 Delta Trail 模式
- `k0`, `k0_idx`, `k0_dir`: 當前待突破的 K0

---

## 9) 重要行為與限制

1. 策略採同棒觸發與同棒成交語義（含 TD 用 close），非 next-bar 執行。
2. 同棒若同時滿足多種條件，依 Step 1 / Step 2 的程式判斷順序決定結果。
3. SL/TS 判定優先於 TP/TD（保守路徑）。
4. 若歷史資料最後仍有未平倉，該筆不會形成完整 round-trip 統計。
5. TD 需連續 `td_consec_bars`（預設 2）根反向 delta 才出場，單根反向 delta 不觸發。

---

## 10) Tick 模式（tick_map 存在時）

當 `on_history` 收到非空的 `tick_map` 時，進場與出場均切換為 tick-by-tick 精度。

### 10-1 Tick 進場（`_tick_entry`）

1. **Vol SMA 前置檢查**：使用前一根已收棒成交量（`klines[i-1].volume`）對比 SMA，避免 look-ahead。Vol SMA 計算窗期為 `[i - period, i)` 共 `period` 根已收棒。
2. 遍歷該棒所有 aggTrade，逐步累計 `cum_buy_vol` / `cum_vol`：
   - `cum_delta_eff = (2 * cum_buy_vol - cum_vol) / cum_vol`
3. 做多：若 `price < k0.low` -> 立即返回失敗；若 `price >= k0.high` 且 `cum_delta_eff > long_delta_eff_threshold` -> 以該 tick 實際價入場
4. 做空：若 `price > k0.high` -> 立即返回失敗；若 `price <= k0.low` 且 `cum_delta_eff < -short_delta_eff_threshold` -> 以該 tick 實際價入場
5. `fill_price = tick.price`（圖表標記仍用 `k0.high/low`）
6. 若遍歷完仍未觸發 -> 未進場

### 10-2 Tick 出場（`_tick_exit`）

1. 若該棒無 tick 資料，回退使用 `_bar_exit_simple`（K 棒邊界估算）
2. 遍歷所有 tick，逐步累計 `cum_delta`：
   - **SL/TS**：`price <= stop_price`（做多）/ `price >= stop_price`（做空）-> 立即出場，`fill_price = tick.price`
   - **TP**（未進入 trailing）：`price >= target_price`（做多）/ `price <= target_price`（做空）
     - 此時 `cum_delta > 0`（做多）/ `< 0`（做空）-> 切換 `trailing=True`，`stop_price = target_price`
     - 否則 -> `TP` 出場
   - **Trailing 模式**：tick 遍歷中僅判斷 SL；TD 判斷延後至本棒所有 tick 跑完後
3. 棒末 TD 判斷：用本棒所有 tick 的 `cum_delta`（非 K 棒整體 delta）判斷方向，更新 `td_consec`，達到 `td_consec_bars` 則以 `k.close` 出場

### 10-3 Bar vs Tick 主要差異

| 面向 | Bar 模式 | Tick 模式 |
|---|---|---|
| 進場 delta 判斷 | 整根 K 棒 delta_eff（含後半段資訊，有 look-ahead） | 累計至觸發 tick 的即時 delta_eff |
| 進場 Vol SMA | 含當根 volume | 用前一根已收棒 volume，無 look-ahead |
| 進場價格 | 固定 `k0.high/low` | 實際 tick 成交價（可能更差） |
| SL/TS 出場 | 以 `stop_price` 理想成交 | 以實際穿越 tick 價成交（可能更差） |
| TP/TD delta 判斷 | 整根 K 棒 delta（結束後已知，有 look-ahead） | TP 觸及當下的累計 delta；TD 用全棒 tick 累計 delta |
| 無 tick 資料時 | — | 回退至 `_bar_exit_simple` K 棒邊界估算 |
