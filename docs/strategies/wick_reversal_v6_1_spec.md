# Wick Reversal v6.1 Strategy Specification

## 1. 概述 (Overview)

Wick Reversal v6.1 是基於 v6 版本架構的改良版策略。核心目標為解決 v6 在不同價格區間 (price regime) 與波動率 (volatility) 下的風險正規化問題，並修正 trade-level order-flow (delta) 在跨 K 棒統計上的斷層，使策略更適合用於跨年份、跨環境的嚴謹量化研究與參數最佳化。

### 1.1 繼承與相容性
- **繼承**：完全繼承 `WickReversalV6Strategy` 的框架與參數，保留 `tick-first` 回測引擎、long/short 鏡像架構、以及動態 N (Dynamic N) 偵測邏輯。
- **訊號相容**：輸出的 `StrategySignal` 結構不變，以確保與既有 UI、分析腳本與回測系統相容，僅新增專屬標籤與 metadata 欄位。

---

## 2. 核心邏輯修改 (Core Modifications)

### 2.1 Entry Zone: Body Reclaim + ATR Cap (動態突破與限制)
原 v6 允許的進場點 `max_entry` 單純基於 k0 高點加上 K 棒振幅乘數，在極端長上/下影線時可能造成追高風險。

- **Long Entry**：
  - 基準點：`body_high = max(k0.open, k0.close)`
  - 進場上限限制 (Cap)：`entry_cap = min(k0_rng * entry_extension_a, atr * entry_atr_cap)`
  - 進場上限：`max_entry = body_high + entry_cap`
  - 觸發條件：`price > body_high AND price <= max_entry`

- **Short Entry**：
  - 基準點：`body_low = min(k0.open, k0.close)`
  - 進場下限限制 (Cap)：`entry_cap = min(k0_rng * entry_extension_a, atr * entry_atr_cap)`
  - 進場下限：`min_entry = body_low - entry_cap`
  - 觸發條件：`price < body_low AND price >= min_entry`

### 2.2 Hybrid Stop (混合波動率停損)
原 v6 的停損為 `k0.low - k0_rng * stop_extension_b`，無法因應極端狹幅 K 棒帶來的過度緊縮停損。

- **Long Stop**：
  - `range_stop = k0.low - k0_rng * stop_extension_b`
  - `atr_stop = k0.low - atr * stop_atr_mult`
  - 最終停損：`stop_p = min(range_stop, atr_stop)`

- **Short Stop**：
  - `range_stop = k0.high + k0_rng * stop_extension_b`
  - `atr_stop = k0.high + atr * stop_atr_mult`
  - 最終停損：`stop_p = max(range_stop, atr_stop)`

### 2.3 True Trade-Level Cumulative Delta (跨 K 棒真實累計 Delta)
修復原 v6 每次跳轉新 K 棒時 `cum_delta` 被歸零的問題。
- 新增 `self._tcv` (trade cumulative volume)、`self._tcbv` (trade cumulative buy volume)。
- 從 `_try_entry` 成功觸發進場的那一刻起歸零。
- 無論在 `_tick_exit` 或 fallback `_bar_exit` 迴圈中，每一次成交皆延續累加，真實反映從進場點開始到當下的 `trade_delta = 2.0 * _tcbv - _tcv`。

### 2.4 Trade Delta Drawdown (TDD)
正式啟用 Trade Delta 回吐的追蹤，防範順勢訂單流動能反轉。
- **追蹤極值**：維護 `self._peak_trade_delta` (Long 取 max，Short 取 min)。
- **觸發出場 (Long)**：`_peak_trade_delta > 0` 且 `_tcd < _peak_trade_delta * (1 - trade_delta_drawdown_pct)`。
- **觸發出場 (Short)**：`_peak_trade_delta < 0` 且 `_tcd > _peak_trade_delta * (1 - trade_delta_drawdown_pct)`。
- **出場標籤**：`TDD`。

### 2.5 Trailing Stop Mode (明確化移動停損行為)
原 v6 在動能有利且觸碰 TP 時，強制將停損點移至 TP (`target_p`)。v6.1 將此行為參數化。
- **觸發條件**：價格觸碰 TP (`target_p`)，且當下 Trade Delta 方向有利 (Long `_tcd > 0`, Short `_tcd < 0`)。
- **模式 1 (`lock_tp`)**：將 `stop_price` 鎖定為 `target_p`。
- **模式 2 (`breakeven_cost`)**：將 `stop_price` 鎖定為 `entry_price` 加上/減去 `round_trip_cost` (確保損益兩平)。

---

## 3. 新增參數 (New Parameters)

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `entry_atr_cap` | `0.35` | Entry Zone 距離實體邊界的最大 ATR 倍數限制。 |
| `stop_atr_mult` | `0.25` | 混合停損所使用的 ATR 乘數限制。 |
| `trailing_stop_mode` | `"lock_tp"` | 觸發 trailing stop 時的停損模式，支援 `"lock_tp"` 與 `"breakeven_cost"`。 |

*(註：原有的 `trade_delta_drawdown_pct = 0.3` 參數保留，並在此版本正式發揮作用)*

---

## 4. 訊號與 Metadata (Signals & Metadata)

為方便後續研究分析，`StrategySignal` 新增/更新了以下資訊：
- **Entry Label**：使用 `L6.1` 與 `S6.1` 以區分版本。
- **Exit Labels**：`TP` (Take Profit), `SL` (Stop Loss), `TS` (Trailing Stop), `TDD` (Trade Delta Drawdown), `TD` (Time/Delta Exhaustion)。
- **Metadata 擴充** (`signal.meta`)：
  - `final_trade_delta`：紀錄出場瞬間的最終跨 K 棒 Trade Delta。
  - `trailing_stop_mode`：紀錄當次交易使用的 Trailing 模式。

---

## 5. 類別定義 (Classes)

```python
class WickReversalV6_1Strategy(WickReversalV6Strategy):
    name = "Wick Reversal 15m v6.1"

class WickReversalV61_1mStrategy(WickReversalV6_1Strategy):
    name = "Wick Reversal 1m v6.1"
```
