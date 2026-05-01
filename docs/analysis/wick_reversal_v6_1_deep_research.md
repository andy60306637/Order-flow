# Wick Reversal v6.1 深度策略研究報告

> 報告日期：2026-04-25  
> 策略版本：`WickReversalV6_1Strategy` (繼承自 `WickReversalV6Strategy`)  
> 時間週期：15m (主策略) / 1m (子策略 `WickReversalV61_1mStrategy`)  
> 交易品種：加密貨幣期貨（以 BTCUSDT 為主要回測標的）

---

## 1. 策略概述

Wick Reversal v6.1 是一套**以訂單流 (Order Flow) 為核心驅動**的 K 棒反轉策略。核心假設是：當市場在單根 K 棒中出現**顯著下影線（多頭）或上影線（空頭）**，且該影線區域呈現機構力量的**吸收型 (Absorption)** 或**主動反轉型 (Initiative)** 訂單流特徵，代表機構在影線端建倉。策略在下一根 K 棒等待**實體回收 (Body Reclaim)** 確認後入場，搭配跨棒累積成交量 Delta 動態管理出場。

**v6.1 相對 v6 的三項核心改進：**

1. **ATR 上限入場價格控制** (`entry_atr_cap`)：避免在高波動期過度追價進場。
2. **混合型停損** (`stop_atr_mult`)：Range-based 與 ATR-based 停損取較保守值（多頭取較小，空頭取較大）。
3. **彈性 Trailing 出場模式** (`trailing_stop_mode`)：新增 `breakeven_cost` 模式，Trailing 啟動時停損移至成本線而非 TP 價位。

---

## 2. 交易哲學與理論基礎

### 2.1 市場結構假設

```
影線 = 流動性陷阱 + 機構進場區域
```

大影線的形成過程：
1. 價格突破關鍵 Swing High/Low，觸發散戶停損單（流動性）。
2. 機構在流動性湧現時以對手方身份承接，建立反向倉位。
3. 影線區域的訂單流顯示「賣壓被吸收」或「買盤主動積累」。
4. 實體回收確認機構力量取得控制權。

### 2.2 訂單流核心指標：Delta Effectiveness

```
delta_eff = (2 × buy_volume - total_volume) / total_volume
```

- 範圍：`[-1.0, +1.0]`
- `+1.0`：全部為主動買單（極端買壓）
- `-1.0`：全部為主動賣單（極端賣壓）
- `0.0`：買賣平衡

**影線 delta_eff 解讀：**

| 多頭影線 | delta_eff ≤ 0 | 賣方在低點被買方吸收 → **吸收型** |
|----------|--------------|----------------------------------|
| 多頭影線 | delta_eff ≥ +0.4 | 買方在低點主動積累 → **主動型** |
| 空頭影線 | delta_eff ≥ 0 | 買方在高點被賣方吸收 → **吸收型** |
| 空頭影線 | delta_eff ≤ -0.4 | 賣方在高點主動積累 → **主動型** |

---

## 3. 模組拆解

### 3.1 ATR / 動態 N 模組

**功能：** 衡量當前波動率、動態調整 Swing Low 回望窗口。

```python
atr_s = _atr_series(klines, period=14)   # Wilder ATR(14)
sma_s = _sma_series(atr_s, period=100)  # ATR 的 100 期 SMA

ratio = ATR(14) / SMA_ATR(100)
N = clamp(round(24 * ratio), min=12, max=48)
```

**設計邏輯：**

| 市場狀態 | ATR/SMA ratio | N 值 | 含義 |
|----------|--------------|------|------|
| 低波動（震盪） | < 1.0 | 收縮至 12 | 僅需突破近期 3 小時低點 |
| 正常 | ≈ 1.0 | 24 | 突破過去 6 小時低點 |
| 高波動（趨勢） | > 1.0 | 擴展至 48 | 必須突破過去 12 小時低點，條件更嚴格 |

**參數：**

```
atr_period     = 14
sma_atr_period = 100
base_n         = 24
min_n          = 12
max_n          = 48
```

---

### 3.2 K0 偵測模組（Wick Pattern Filter）

**功能：** 識別符合特定形態的「信號 K 棒（k0）」。

#### 多頭 K0 六項條件（缺一不可）

```
條件 1：整根 K 棒長度 > ATR(14) × atr_range_mult(1.1)
         → 確保是有意義的大 K 棒，排除細微波動

條件 2：下影線 ≥ max(實體, price×1e-5) × wick_body_ratio(3.0)
         → 下影線至少是實體的 3 倍，確認影線顯著

條件 3：上影線 < 整根 K 棒長度 × opposite_wick_cap(0.1)
         → 上影線不超過 10%，確認方向純粹

條件 4：k0.low < 過去 N 根 K 棒的最低點
         → 完成了對流動性區域的掃描（Liquidity Sweep）

條件 5：影線區域成交量 / 全棒成交量 ≥ wick_min_vol_ratio(0.15)
         → 影線必須有足夠的成交量支撐，排除假突破

條件 6：影線 delta_eff 符合 Absorption 或 Initiative 條件
         → 訂單流確認機構力量存在
```

**空頭 K0** 為多頭的完全鏡像（以上影線和高點突破為判斷基準）。

#### K0 元數據（用於後驗分析）

每個 k0 記錄以下元信息：

```python
{
    "side": "long" / "short",
    "wick_type": "Absorb" / "Initiative",
    "session_hour": 0-23,          # UTC 小時
    "atr_percentile": 0-100,       # 當前 ATR 的百分位（波動率環境）
    "trend_regime": "...",         # 透過 detect_regime() 判斷的趨勢方向
    "k0_range_atr": float,         # K 棒幅度 / ATR（量化 k0 的相對大小）
    "wick_volume_ratio": float,    # 影線成交量比例
}
```

---

### 3.3 進場模組（Entry Module）

**功能：** 在 Zoom 視窗內以 Tick 精度確認入場時機。

#### v6 vs v6.1 進場比較

| 項目 | v6 | v6.1 |
|------|-----|-------|
| 觸發條件 | price > k0_body_high | price > k0_body_high（相同）|
| 最大入場價格 | k0_high + range × 0.25 | `min(range × 0.25, ATR × 0.35)` |
| 入場參考點 | k0_body_high（高點） | k0_body_high（實體最高點）|

#### v6.1 入場邏輯（多頭）

```python
entry_cap = min(k0_rng × entry_extension_a,   # range 限制 (0.25)
                atr × entry_atr_cap)            # ATR 限制 (0.35) ← v6.1 新增
max_entry = body_high + entry_cap

# Tick 級別入場條件
for tick in ticks:
    if tick.price < k0_body_low:
        KILL_SETUP                              # 觸及體下即作廢
    if tick.price > body_high:
        if tick.price > max_entry: continue     # 超過上限跳過
        zoom_de = cum_delta_eff(zoom_window)
        if zoom_de > zoom_entry_delta_eff_threshold(0.3):
            ENTER_LONG                          # Delta 確認後入場
```

#### Zoom 視窗 Delta 過濾

```
zoom_delta_eff = (2 × zcbv - zcv) / zcv
```

- `zcv`：Zoom 視窗開啟後至當前 Tick 的累積成交量
- `zcbv`：Zoom 視窗內累積**賣方**成交量（`is_buyer_maker = False`）
- 多頭：zoom_de > **+0.3**（買方主導）
- 空頭：zoom_de < **-0.3**（賣方主導）

**Zoom 視窗定義：**

```
zoom_bars = 1   # k0 後僅 1 根 K 棒為有效進場窗口
```

---

### 3.4 停損模組（Stop Loss Module）

#### v6 vs v6.1 停損比較

| 版本 | 計算方式 |
|------|---------|
| v6 | `stop = k0_low - k0_range × stop_extension_b(0.1)` |
| v6.1 | `stop = min(range_stop, atr_stop)` — 取更保守（更低）值 |

#### v6.1 混合停損（多頭）

```python
range_stop = k0.low - k0_rng × stop_extension_b(0.10)   # 原有 Range 停損
atr_stop   = k0.low - atr × stop_atr_mult(0.25)          # 新增 ATR 停損
stop_p     = min(range_stop, atr_stop)                    # 取更低者（更保守）
```

**設計意圖：** 在小幅波動環境（ATR 小）中，`atr_stop` 可能比 `range_stop` 更緊；在大幅波動環境（ATR 大）中，`range_stop` 提供底層保護。確保停損不會因影線過大而設置過緊。

---

### 3.5 目標價與盈虧比模組

```python
risk   = fill_price - stop_p
target = fill_price + risk × rr(2.5)
```

**盈虧比：** `rr = 2.5`（風險 1 : 獲利 2.5）

**成本覆蓋檢查（費用門檻）：**

```python
round_trip_cost = 2 × (taker_fee_rate + slippage_rate) × price
                = 2 × (0.00032 + 0.00002) × price
                = 0.00068 × price

費用通過條件: risk × rr ≥ round_trip_cost × fee_cover_ratio(3)
```

**說明：** `fee_cover_ratio = 3` 意味著目標獲利必須至少覆蓋手續費的 3 倍，確保小風險比交易不會被費用吃掉。

---

### 3.6 出場模組（Exit Module）

v6.1 出場分為三個狀態機階段：

```
狀態 A: 未觸 TP（持倉追蹤 SL）
   ↓ 觸及停損 → 出場 (SL)
   ↓ 價格到達 TP 目標區域
     → 檢查 trade_cum_delta
        ≤ 0: 直接出場 (TP)
        > 0: 進入狀態 B

狀態 B: Trailing 啟動（已觸 TP）
   ↓ 停損移至 lock_tp 或 breakeven_cost（視 trailing_stop_mode）
   ↓ 觸及停損 → 出場 (TS)
   ↓ peak_delta 回落 > drawdown_pct(30%) → 出場 (TDD)
   ↓ 連續 td_consec_bars(2) 根棒子 cum_delta 反向 → 出場 (TD)
```

#### 出場標籤說明

| 標籤 | 觸發條件 | 含義 |
|------|---------|------|
| `SL` | 價格觸及停損線（Trailing 前）| 停損出場 |
| `TP` | 達到目標價，delta ≤ 0 | 固定停利出場 |
| `TS` | Trailing 啟動後觸及移動停損 | 移動停損出場 |
| `TDD` | trade_delta 從峰值回落 > 30% | Delta 回撤出場 |
| `TD` | 連續 2 棒 cum_delta 反向 | Delta 反轉出場 |

#### v6.1 Trailing 模式

```python
trailing_stop_mode = "lock_tp"       # 預設：停損鎖定在 TP 價位
                   # "breakeven_cost" # 選項：停損移至成本 + 手續費
```

**`lock_tp` 模式：** Trailing 啟動後，最壞情況保留 RR=2.5 的獲利。  
**`breakeven_cost` 模式：** Trailing 啟動後，確保不虧損（停損 = 進場價 + 手續費）。

#### Trade-Level Cum Delta 計算

```python
# 跨棒累積（從進場 Tick 到出場 Tick）
self._tcv  += qty           # 累積成交量
self._tcbv += qty if not is_buyer_maker else 0   # 累積賣方成交量
self._tcd = 2 × _tcbv - _tcv   # 當前 trade delta

# Peak Delta 追蹤
long:  _peak_trade_delta = max(_peak_trade_delta, _tcd)
short: _peak_trade_delta = min(_peak_trade_delta, _tcd)
```

---

### 3.7 時段過濾模組（Session Filter）

```python
def _in_session(ms: int) -> bool:
    h = UTC hour of timestamp
    return h < 8 or 7 <= h < 16 or 13 <= h < 22
    # Asia: 00:00-08:00 UTC
    # London: 07:00-16:00 UTC  
    # New York: 13:00-22:00 UTC
```

**設計邏輯：** 三個主要交易時段覆蓋全球流動性高峰，排除深夜亞太尾盤到歐洲開盤前的低流動性時段（約 22:00-00:00 UTC）。

---

### 3.8 趨勢環境偵測模組（Regime Detection）

透過 `core.regime.detect_regime()` 對過去 50 根 K 棒進行趨勢分析，並記錄在 k0_meta 中（`trend_regime` 欄位）。此模組目前作為後驗分析工具，用於研究不同趨勢環境下策略的表現差異。

---

## 4. 獲利因子拆解

### 4.1 核心獲利框架

```
期望獲利 (EV) = 勝率 × 平均獲利 - (1 - 勝率) × 平均虧損
```

策略的盈虧比為 2.5:1，代表即使勝率僅 **29%** 也能達到盈虧平衡：

```
盈虧平衡勝率 = 1 / (1 + RR) = 1 / (1 + 2.5) = 28.6%
```

### 4.2 正 EV 的五個來源

#### 因子一：流動性掃描優勢（Liquidity Sweep Edge）

```
k0.low < 過去 N 根 K 棒最低點
```

策略入場點是在市場剛完成散戶停損掃描之後，此時散戶已被淘汰，反向動能最純粹。**機構在散戶被掃走後反向積累，是影線形成的根本原因。**

#### 因子二：訂單流確認優勢（Order Flow Confirmation Edge）

進場有**兩道**訂單流門檻：

1. **K0 影線 delta_eff 檢查**（吸收型 ≤ 0 或主動型 ≥ 0.4）
2. **Zoom 視窗 zoom_delta_eff > 0.3**（確認 body reclaim 時的買方動能）

雙重確認大幅降低假突破入場率。

#### 因子三：精確入場優勢（Precise Entry Edge）

Tick 級別入場 vs Bar 級別入場的差異：

```
實際成交價格（fill_price）  vs  K 棒開盤價
```

Tick 入場可以以 `body_high` 附近的最優價格進場，而非等待 K 棒收盤確認，**顯著降低入場滑點和風險寬度**。

#### 因子四：動態 Trailing 延伸獲利（Trailing Extension Edge）

當 TP 觸及且 trade_cum_delta > 0 時，不急於平倉，而是將停損鎖定在 TP 水位並繼續追蹤：

```
潛在獲利延伸 = f(trailing_持續時間, 後續動能強度)
```

Trailing 模式的三道出場機制確保不會把已到手的獲利還回去：SL（停損），TDD（Delta 回撤），TD（Delta 反轉）。

#### 因子五：費用過濾優勢（Cost Filter Edge）

```
risk × rr ≥ round_trip_cost × fee_cover_ratio(3)
```

此條件過濾掉所有「理論上盈虧比合格但實際被費用吃光」的低風險比交易，確保每筆進場交易都有足夠的獲利空間覆蓋摩擦成本。

### 4.3 風險控制因子

#### 因子六：ATR 入場上限（v6.1 新增）

```python
entry_cap = min(range × 0.25, ATR × 0.35)
```

在高波動行情中，純 range-based 的入場上限可能過寬，導致入場價過高、停損過遠。ATR cap 提供動態收緊機制。

#### 因子七：混合停損（v6.1 新增）

```python
stop_p = min(range_stop, atr_stop)   # 多頭取較低者
```

在低波動環境中，ATR 停損更緊（可能低於 range 停損），為交易提供更精確的無效化水位。

#### 因子八：Setup Kill 機制

```python
if tick.price < k0_body_low:   # 觸及體下
    KILL_SETUP                  # 立即作廢
```

一旦進場等待期間價格回撤至 k0 實體以下，代表多頭結構失敗，立即放棄此交易機會，避免在弱勢結構中勉強進場。

---

## 5. v6 vs v6.1 版本差異對比

| 特性 | v6 | v6.1 |
|------|-----|-------|
| 入場上限 | `k0_high + range × 0.25` | `body_high + min(range × 0.25, ATR × 0.35)` |
| 觸發價格 | `k0_body_high (高點)` | `k0_body_high (實體最高點)` |
| 停損計算 | `k0.low - range × 0.1` | `min(range_stop, atr_stop)` |
| Trailing 觸發點 | `target_p (fixed)` | `target_p` 或 `entry + rt_cost` |
| Trade Delta 追蹤 | 跨棒累積（但 TDD 未實作） | 跨棒累積 + Peak Delta 追蹤 + TDD 出場 |
| Bar Exit Delta 追蹤 | `bar_delta = 2×tbv - tv`（每棒重置）| 跨棒累積 `_tcv/_tcbv` |
| 新增出場 | 無 TDD | **TDD**（Peak Delta 回撤 30% 出場）|
| zoom_bars | 1 | 1 |
| rr | 2.5 | 2.5 |

---

## 6. 參數敏感性分析

### 6.1 高影響力參數

| 參數 | 預設值 | 影響 | 備註 |
|------|--------|------|------|
| `rr` | 2.5 | 勝率 ↔ 期望值的平衡點 | 降低 rr → 勝率提升但 EV 可能下降 |
| `zoom_entry_delta_eff_threshold` | 0.3 | 進場頻率 vs 品質過濾 | 降低 → 更多進場，假訊號增加 |
| `wick_body_ratio` | 3.0 | k0 數量 vs 形態純粹度 | 提高 → k0 更稀少但更可靠 |
| `entry_atr_cap` | 0.35 | 進場入場上限收緊程度 | 降低 → 在高波動市場更嚴格 |
| `stop_atr_mult` | 0.25 | 停損緊度 | 提高 → 更寬停損，更低虧損次數但更大單次虧損 |
| `trade_delta_drawdown_pct` | 0.3 | TDD 出場靈敏度 | 降低 → 更快鎖利，但也更容易假觸 |
| `atr_range_mult` | 1.1 | k0 的最小大小門檻 | 提高 → k0 更稀少，信號品質提升 |

### 6.2 低影響力參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `stop_extension_b` | 0.10 | v6.1 混合停損中 range 停損的延伸幅度 |
| `entry_extension_a` | 0.25 | range 部分的入場上限幅度 |
| `td_consec_bars` | 2 | TD 出場需要連續多少根反向 delta 棒 |
| `fee_cover_ratio` | 3 | 費用覆蓋倍數 |

---

## 7. 策略信號流程圖

```
K 棒到來
│
├─ [持倉狀態]
│   ├─ Tick/Bar Exit 檢查
│   │   ├─ 觸及 stop_price → SL/TS 出場
│   │   ├─ Trailing 啟動中
│   │   │   ├─ peak_delta 回撤 > 30% → TDD 出場
│   │   │   └─ 連續 2 棒反向 delta → TD 出場
│   │   └─ 達到 target_p
│   │       ├─ trade_cum_delta > 0 → 啟動 Trailing
│   │       └─ trade_cum_delta ≤ 0 → TP 出場
│
├─ [Zoom 視窗等待入場]
│   ├─ 超過 zoom_bars(1) → 視窗作廢
│   ├─ tick < k0_body_low → Kill Setup
│   ├─ tick > max_entry → 跳過
│   └─ zoom_delta_eff > 0.3 → 確認進場
│       ├─ 計算混合停損（range vs ATR）
│       ├─ 費用覆蓋檢查
│       └─ 成立 → 建倉，啟動持倉狀態
│
└─ [空閒，尋找 K0]
    ├─ 時段過濾（Asia/London/NY）
    ├─ k0 形態過濾（ATR 大小、影線比例、對向影線限制）
    ├─ 動態 N 計算 → Swing Low/High 突破檢查
    └─ 影線訂單流確認（Absorb or Initiative）
        └─ 成立 → 設置 Zoom 視窗
```

---

## 8. 風險因子識別

### 8.1 策略性風險

| 風險 | 說明 | 緩解措施 |
|------|------|---------|
| **趨勢市場逆勢** | 下降趨勢中持續出現多頭 k0 但行情繼續下行 | `trend_regime` 後驗篩選，考慮趨勢過濾層 |
| **流動性不足** | k0 成交量不足，影線訂單流不可靠 | `wick_min_vol_ratio = 0.15` 過濾 |
| **Tick 缺口風險** | Tick 數據缺失時觸發 bar fallback，精度下降 | `allow_bar_fallback_in_tick_mode = False` |
| **連續虧損** | 震盪行情多次掃描後反轉失敗 | 費用覆蓋 + zoom_delta_eff 雙重過濾 |

### 8.2 執行風險

| 風險 | 說明 |
|------|------|
| **滑點超過預設** | 實際滑點 > `slippage_rate = 0.00002` |
| **費率差異** | Maker/Taker 費率環境改變 |
| **Tick 延遲** | 訂單流訊號延遲造成進場價格偏差 |

### 8.3 過度優化風險

- 策略參數在特定時間段（如 2022-2024）優化後可能在結構改變的市場失效。
- `atr_percentile` 和 `k0_range_atr` 等 meta 資訊可用於分析不同市場環境下的表現差異。

---

## 9. 優化方向

### 短期（低風險）

1. **趨勢方向過濾**：利用已有的 `trend_regime` 數據分析多空信號的勝率差異，考慮在強趨勢環境中僅做順勢 k0。
2. **會話分層分析**：按 `session_hour` 分析不同交易時段的勝率，考慮對表現差的時段調整 `zoom_entry_delta_eff_threshold`。
3. **ATR 百分位過濾**：分析高 ATR 百分位（> 80）環境下的表現，高波動環境可能需要更嚴格的 delta 門檻。

### 中期（中等風險）

4. **動態 RR**：根據 k0 wick type（Absorb vs Initiative）和 ATR 百分位動態調整 `rr`，主動型 k0 可考慮更高 RR。
5. **Volume Profile 整合**：利用系統已有的 `VolumeProfileEngine`（POC/VAH/VAL/HVN/LVN），在 k0 影線端存在 HVN 時加強信心。
6. **多時間週期確認**：1m 版本（`WickReversalV61_1mStrategy`）和 15m 版本信號的協同關係分析。

### 長期（高風險）

7. **機器學習 Delta 閾值優化**：針對 k0_meta 特徵（wick_type、session、regime、ATR%）訓練分類器，替換固定的 `zoom_entry_delta_eff_threshold`。
8. **Cross-session 持倉管理**：針對跨越交易時段的持倉，研究是否需要差異化的 Trailing 策略。

---

## 10. 關鍵代碼位置索引

| 功能 | 文件 | 說明 |
|------|------|------|
| K0 多頭偵測 | [wick_reversal_v6.py:325-349](strategies/wick_reversal_v6.py#L325-L349) | `_is_k0_long()` |
| K0 空頭偵測 | [wick_reversal_v6.py:351-375](strategies/wick_reversal_v6.py#L351-L375) | `_is_k0_short()` |
| 吸收型/主動型判斷 | [wick_reversal_v6.py:265-321](strategies/wick_reversal_v6.py#L265-L321) | `_abs_long()`, `_abs_short()` |
| v6.1 多頭進場 | [wick_reversal_v6_1.py:41-127](strategies/wick_reversal_v6_1.py#L41-L127) | `_try_entry_long()` |
| v6.1 空頭進場 | [wick_reversal_v6_1.py:129-215](strategies/wick_reversal_v6_1.py#L129-L215) | `_try_entry_short()` |
| Trailing 啟動 | [wick_reversal_v6_1.py:27-36](strategies/wick_reversal_v6_1.py#L27-L36) | `_activate_trailing()` |
| Tick 多頭出場 | [wick_reversal_v6_1.py:220-304](strategies/wick_reversal_v6_1.py#L220-L304) | `_tick_exit_long()` |
| Tick 空頭出場 | [wick_reversal_v6_1.py:306-390](strategies/wick_reversal_v6_1.py#L306-L390) | `_tick_exit_short()` |
| 動態 N 計算 | [wick_reversal_v6.py:259-261](strategies/wick_reversal_v6.py#L259-L261) | `_dyn_n()` |
| ATR 計算 | [wick_reversal_v6.py:70-87](strategies/wick_reversal_v6.py#L70-L87) | `_atr_series()` |
| 費用覆蓋檢查 | [wick_reversal_v6.py:191-192](strategies/wick_reversal_v6.py#L191-L192) | `_risk_ok()` |
| 時段過濾 | [wick_reversal_v6.py:64-67](strategies/wick_reversal_v6.py#L64-L67) | `_in_session()` |
| 主循環（v6.1） | [wick_reversal_v6_1.py:525-689](strategies/wick_reversal_v6_1.py#L525-L689) | `on_history()` |

---

## 11. 總結

Wick Reversal v6.1 是一套**信號稀少但品質高**的機構級訂單流策略，其核心競爭優勢在於：

1. **多層信號確認**：形態過濾 + 影線訂單流 + Zoom 視窗 Delta，三道門檻確保入場品質。
2. **Tick 精度執行**：進出場均基於逐筆成交數據，避免 K 棒級別的信息延遲。
3. **動態出場管理**：Trade-level cum delta 跨棒追蹤，在動能強勁時延伸獲利，在動能衰退時及時鎖利。
4. **費用意識設計**：費用覆蓋檢查內嵌於進場邏輯，確保每筆交易的預期回報在費用後仍為正。

**策略的核心理論假設**：影線是流動性掃描的產物，機構在其中建倉，實體回收後的延續是機構意圖的確認。只要市場結構維持「機構主導的流動性週期」特性，此策略的正 EV 基礎就依然成立。

---

*報告基於代碼版本 commit `0c4f302`，策略文件 `strategies/wick_reversal_v6.py` 和 `strategies/wick_reversal_v6_1.py`。*
