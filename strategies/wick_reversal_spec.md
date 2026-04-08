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

---

## 3) Delta 定義

策略使用 Binance K 線的 `volume` 與 `taker_buy_volume`：

```text
delta = 2 * taker_buy_volume - volume
```

- `delta > 0`：買方主動量較強
- `delta < 0`：賣方主動量較強

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
2. 若 `trailing=True` 且 `delta <= 0` -> `TD`，價格=`k.close`
3. 若 `k.high >= target_price`：
   - `delta > 0` -> 切換 `trailing=True`，`stop_price = target_price`
   - 否則 -> `TP`，價格=`target_price`

#### 做空持倉優先順序

1. `k.high >= stop_price` -> `SL`（若 `trailing=True` 則標 `TS`）
2. 若 `trailing=True` 且 `delta >= 0` -> `TD`，價格=`k.close`
3. 若 `k.low <= target_price`：
   - `delta < 0` -> 切換 `trailing=True`，`stop_price = target_price`
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
2. 否則若 `k.high >= k0.high` 且 `delta > 0` -> 進場

進場定義：

```text
entry  = k0.high
stop   = k0.low - sl_offset
risk   = entry - stop
target = entry + risk * rr_ratio
```

#### 做空分支

1. 若 `k.high > k0.high` -> K0 失效
2. 否則若 `k.low <= k0.low` 且 `delta < 0` -> 進場

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

- 進場價：做多=`k0.high`，做空=`k0.low`
- `SL/TS`：`stop_price`
- `TP`：`target_price`
- `TD`：當根 `k.close`

策略本身不填 `fill_price`，回測引擎將使用 `signal.price` 計算。

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
