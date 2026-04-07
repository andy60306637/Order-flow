# Wick Reversal 1m — 策略規格文件

## 概覽

| 項目 | 內容 |
|------|------|
| 策略名稱 | Wick Reversal 1m |
| 交易商品 | BTCUSDT（Binance USDT-M Futures） |
| 週期 | 1 分鐘 |
| 方向 | 雙向（做多 / 做空） |
| 盈虧比 | 1:1 起（Delta 順向可追蹤放大） |
| 同時持倉 | 最多 1 筆 |
| 版本 | v3 |

---

## 核心概念

### K0 — 觸發 K 棒

K0 是訊號的起點，代表一根具有**明顯引線**的反轉意圖 K 棒。

- **做多 K0**：看跌 K 棒 + 長下引線（價格被下方買盤強力承接）
- **做空 K0**：看漲 K 棒 + 長上引線（價格被上方賣盤強力壓制）

> 若出現新的 K0，**立即取代**舊的 K0，不保留前一根。

### 順向追蹤 — Delta Trail

當價格達到 1:1 停利位時，策略檢查當前 K 棒的 Delta：

- **Delta 順向**（做多 delta > 0 / 做空 delta < 0）：不出場，切換為「追蹤模式」，停損上移至原始 1:1 停利位。
- **Delta 逆向**：正常 1:1 停利出場。

追蹤模式內，持續持倉直到：
1. **Delta 轉向**：做多時 delta ≤ 0 / 做空時 delta ≥ 0 → 以該根 K 棒的 `close` 停利出場（標記 TD）
2. **追蹤停損觸發**：價格回落到原始 1:1 停利位 → 停損出場（標記 TS）

### Zoom — 觀察窗口

Zoom 為 K0 之後的第 **1 至 5** 根 K 棒，是等待突破的觀察期間。  
Zoom 期間若防守線被破，K0 立即**失效**；若突破條件+Delta 條件同時達成，**立即進場**，不等第 5 根結束。

---

## 做多策略

### Step 1 — K0 條件

```
close < open                               ← 看跌 K 棒
high - low > 0                             ← K 棒有實體長度
close >= (high + low) / 2                  ← 收盤在整體 K 棒上半部（買方實際承接）
(close - low) > (open - close)             ← 下引線長度 > 實體長度
```

$$\text{mid} = \frac{\text{high} + \text{low}}{2}$$
$$\text{body} = \text{open} - \text{close}$$
$$\text{lower\_wick} = \text{close} - \text{low} > \text{body}$$

### Step 2 — Zoom 有效性（防守線）

防守線為 **K0.low**。

| 狀況 | 條件 | 結果 |
|------|------|------|
| 有效 | zoom 內每根 `low >= k0.low` | 持續觀察 |
| 失效 | zoom 內任一根 `low < k0.low` | K0 立即失效，重新尋找 |

### Step 3 — 進場條件

zoom 內任一根 K 棒**同時**滿足：

```
high >= k0.high       ← 突破 K0 高點
delta > 0             ← 買方主動力強（taker buy > taker sell）
```

### Step 4 — 進出場價位

```
entry  = k0.high
stop   = k0.low - 10
risk   = entry - stop
target = entry + risk        (1:1 初始停利)
```

#### 追蹤模式（觸及 target 時 delta > 0）

```
trailing = True
stop ← target                (停損上移至 1:1 位置)
待 delta 轉負 → 以 close 停利
或 low <= stop(1:1) → 追蹤停損
```

---

## 做空策略

### Step 1 — K0 條件

```
close > open                               ← 看漲 K 棒
high - low > 0                             ← K 棒有實體長度
close <= (high + low) / 2                  ← 收盤在整體 K 棒下半部（賣方實際壓下）
(high - close) > (close - open)            ← 上引線長度 > 實體長度
```

$$\text{mid} = \frac{\text{high} + \text{low}}{2}$$
$$\text{body} = \text{close} - \text{open}$$
$$\text{upper\_wick} = \text{high} - \text{close} > \text{body}$$

### Step 2 — Zoom 有效性（防守線）

防守線為 **K0.high**。

| 狀況 | 條件 | 結果 |
|------|------|------|
| 有效 | zoom 內每根 `high <= k0.high` | 持續觀察 |
| 失效 | zoom 內任一根 `high > k0.high` | K0 立即失效，重新尋找 |

### Step 3 — 進場條件

zoom 內任一根 K 棒**同時**滿足：

```
low <= k0.low         ← 跌破 K0 低點
delta < 0             ← 賣方主動力強（taker sell > taker buy）
```

### Step 4 — 進出場價位

```
entry  = k0.low
stop   = k0.high + 10
risk   = stop - entry
target = entry - risk        (1:1 初始停利)
```

#### 追蹤模式（觸及 target 時 delta < 0）

```
trailing = True
stop ← target                (停損下移至 1:1 位置)
待 delta 轉正 → 以 close 停利
或 high >= stop(1:1) → 追蹤停損
```

---

## Delta 計算方式

本策略使用 K 棒層級的訂單流 Delta，來自 Binance K 線資料中的 `taker_buy_volume`：

$$\text{delta} = 2 \times \text{taker\_buy\_volume} - \text{volume}$$

- `delta > 0`：該 K 棒買方主動量大於賣方主動量
- `delta < 0`：該 K 棒賣方主動量大於買方主動量

---

## 完整流程圖

```
每根新 K 棒出現
│
├─ 有持倉？
│   ├─ 檢查 SL：k.low <= stop      → 停損出場 (SL)
│   │       k.high >= stop     → 停損出場 (SL)
│   ├─ 追蹤模式？
│   │   ├─ [多] delta <= 0  → 以 close 停利出場 (TD)
│   │   └─ [空] delta >= 0  → 以 close 停利出場 (TD)
│   ├─ 觸及 1:1 target？
│   │   ├─ delta 順向 → 切換追蹤模式，stop 移至 target
│   │   └─ delta 逆向 → 正常 1:1 停利出場 (TP)
│   └─ 未觸發 → 繼續持倉，跳過後續
│
├─ 有 K0 且在 Zoom 窗口內 (bars_after <= 5)？
│   ├─ [多] low < k0.low  → K0 失效
│   ├─ [多] high >= k0.high AND delta > 0 → 做多進場
│   ├─ [空] high > k0.high → K0 失效
│   └─ [空] low <= k0.low AND delta < 0  → 做空進場
│
└─ 無持倉 → 尋找新 K0
    ├─ 看跌 + 收在上半部 + 下引線 > 實體 → 標記為做多 K0（取代舊 K0）
    └─ 看漲 + 收在下半部 + 上引線 > 實體 → 標記為做空 K0（取代舊 K0）
```

---

## 參數表

| 參數 | 預設值 | 說明 |
|------|--------|------|

| `zoom_bars` | `5` | K0 後的最大觀察 K 棒數 |
| `sl_offset` | `10.0` USDT | 超出 K0 引線頂端/底部的停損位移 |
| `rr_ratio` | `1.0` | 盈虧比（1.0 = 1:1） |

---

## 圖表標記說明

回測結果在 K 線圖上以以下符號呈現：

| 符號 | 顏色 | 位置 | 意義 |
|------|------|------|------|
| ◆ 菱形 | 橙色 | K 棒引線端旁 | K0 候選標記（含失效者） |
| ▲ 三角 | 綠色 | K 棒低點下方 | 做多進場 |
| ▼ 三角 | 紅色 | K 棒高點上方 | 做空進場 |
| ▲ 三角 | 綠色（標 TP/SL/TS/TD） | 對應價位 | 做多出場（停利 / 停損 / 追蹤停損 / 追蹤停利） |
| ▼ 三角 | 紅色（標 TP/SL/TS/TD） | 對應價位 | 做空出場（停利 / 停損 / 追蹤停損 / 追蹤停利） |

> 橙色菱形（K0）為全量顯示，包含最終進場的 K0 以及失效未進場的 K0，可用於判斷策略識別品質。

---

## 注意事項

1. **資料依賴**：Delta 計算需要 `taker_buy_volume`，歷史回填需確保 aggTrade 資料完整。
2. **同根進出**：持倉在 SL/TP 觸發後，同根 K 棒不會立即尋找新 K0，避免過度頻繁換倉。
3. **無出場訊號時**：若歷史資料末端仍有未平倉，統計欄會顯示「未平倉」數量，不計入勝率與 PnL。
