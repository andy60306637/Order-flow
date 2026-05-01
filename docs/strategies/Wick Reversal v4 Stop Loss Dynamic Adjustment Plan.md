# Wick Reversal v4 停損動態化修正計畫

## 1. 目的

目前策略的停損點採用固定點數（long/short 都是固定 offset），這會導致不同年份、不同價格水位、不同波動環境下的風險尺度不一致，進而讓 RR、勝率、PF、expectancy 的跨年份比較失真。

本次修正的核心目的：

- 讓停損距離能隨當前盤面價格與型態結構動態調整
- 讓不同年份的 risk 定義更一致
- 提升跨年份回測結果的可比性
- 為 RR 參數重新校準建立正確基礎

---

## 2. 當前問題

### 2.1 固定停損點數造成跨年份風險失真

目前策略使用：

- Long: `stop = k0.low - long_sl_offset`
- Short: `stop = k0.high + short_sl_offset`

若 `long_sl_offset = 10`、`short_sl_offset = 10`，則會出現：

- BTC 在不同年份價格差異很大時，固定 10 點的相對風險完全不同
- 高價格年份中，10 點可能太小，容易被市場噪音掃掉
- 低價格年份中，10 點可能相對偏大，使停損過寬

### 2.2 成本模型與固定停損尺度不一致

目前成本（手續費、滑價）是根據價格比例計算，但停損卻是固定點數，導致：

- 成本會隨價格水位變動
- 停損風險卻不會同步變動
- RR 與 cost filter 在不同年份的意義不同

### 2.3 回測比較基礎不公平

在固定停損模式下，不同年份回測績效差異可能不是來自策略 edge 真的改變，而是：

- 同一固定停損在不同年份代表不同風險尺度
- 同一 RR 在不同年份實際上不是同一種交易結構
- 策略對高波動 / 低波動年份的適應能力被扭曲

### 2.4 現狀補充：Dynamic RR 已先行實作

Dynamic RR（`_resolve_long_rr` / `_resolve_short_rr`，依 wick 分級 A/B/C 給予不同倍率）已在先前版本完成。
此次補做動態停損，等同於補足 dynamic RR 本應建立的基礎。
實作完成後，現有 RR 參數（`long_rr_wick_a/b/c`、`short_rr_wick_a/b/c`）需重新以動態 risk 為基礎校準。

---

## 3. 修正原則

本次停損改造遵守以下原則：

### 3.1 停損必須與當前市場尺度綁定

停損不應再只依賴固定點數，而應至少考慮：

- 當前價格水位（price-normalized floor）
- k0 wick 結構（wick-aware buffer）

### 3.2 停損設計需兼顧「結構性」與「穩定性」

若只使用價格百分比，可能忽略 wick 結構本身的重要性。
若只使用 wick 長度，又可能在極短 wick 或極長 wick 下失真。

因此採混合設計：取 price-floor 與 wick-buffer 的 `max`，再加 price-cap 防止過寬。

### 3.3 修正順序與後續行動

本次修正為：補做動態停損 → 重跑全年份回測 → 重新校準 RR 參數。

Dynamic RR 雖已先行，但其最佳化結果是建立在固定 SL 上的，修正後需視為「重設基準」，重新評估 wick 分級閾值與 RR 倍率是否仍合適。

---

## 4. 修正方案

### 4.1 第一階段目標：由固定點數改為動態停損

本階段不直接引入 ATR，先以較穩健、較符合策略邏輯的方式進行修正。

採用：

- **price-normalized**：以入場價百分比設定 floor，確保跨年份比較一致
- **wick-aware**：以 k0 wick 長度的倍數作為結構性緩衝
- **帶 floor / cap 的混合式停損**：取兩者最大值後再加 cap，防止極端值

---

### 4.2 停損公式

#### Long 停損

```python
lower_wick = min(k0.open, k0.close) - k0.low
base_stop_dist = max(
    entry_price * long_sl_pct_floor,   # price-normalized floor
    lower_wick * long_sl_wick_mult,    # wick-aware buffer
)
base_stop_dist = min(base_stop_dist, entry_price * long_sl_pct_cap)  # hard cap
stop_p = k0.low - base_stop_dist
```

#### Short 停損（Long 的鏡像）

```python
upper_wick = k0.high - max(k0.open, k0.close)
base_stop_dist = max(
    entry_price * short_sl_pct_floor,  # price-normalized floor
    upper_wick * short_sl_wick_mult,   # wick-aware buffer
)
base_stop_dist = min(base_stop_dist, entry_price * short_sl_pct_cap)  # hard cap
stop_p = k0.high + base_stop_dist
```

---

### 4.3 新增與移除參數

#### 移除（廢棄）

| 參數 | 說明 |
|---|---|
| `long_sl_offset: float = 10.0` | 固定停損點數，由動態公式取代 |
| `short_sl_offset: float = 10.0` | 固定停損點數，由動態公式取代 |

#### 新增（Long）

| 參數 | 預設值 | 說明 |
|---|---|---|
| `long_sl_pct_floor` | `0.0005` | 停損距離最小值（佔入場價比例，0.05%）；BTC 60k ≈ 30 pts |
| `long_sl_wick_mult` | `0.1` | 下影線乘數，作為 k0.low 下方緩衝 |
| `long_sl_pct_cap` | `0.003` | 停損距離上限（0.3%），防止極長 wick 過度展寬 |

#### 新增（Short，鏡像）

| 參數 | 預設值 | 說明 |
|---|---|---|
| `short_sl_pct_floor` | `0.0005` | 停損距離最小值（0.05%） |
| `short_sl_wick_mult` | `0.1` | 上影線乘數 |
| `short_sl_pct_cap` | `0.003` | 停損距離上限（0.3%） |

> 預設值僅為起始點，修正後需重跑回測並重新優化。

---

### 4.4 抽出 helper 方法

在策略中新增兩個 private helper，避免在 4 個進場點重複計算：

```python
def _calc_long_stop_dist(self, k0: Kline, entry_price: float) -> float:
    lower_wick = min(k0.open, k0.close) - k0.low
    dist = max(
        entry_price * self.long_sl_pct_floor,
        lower_wick * self.long_sl_wick_mult,
    )
    return min(dist, entry_price * self.long_sl_pct_cap)

def _calc_short_stop_dist(self, k0: Kline, entry_price: float) -> float:
    upper_wick = k0.high - max(k0.open, k0.close)
    dist = max(
        entry_price * self.short_sl_pct_floor,
        upper_wick * self.short_sl_wick_mult,
    )
    return min(dist, entry_price * self.short_sl_pct_cap)
```

---

## 5. 實作位置

需修改 `strategies/wick_reversal_v4.py` 共 4 處停損計算，以及參數宣告區：

| 方法 | 行號（約） | 改前 | 改後 |
|---|---|---|---|
| `_bar_entry` | L302 | `k0.low - self.long_sl_offset` | `k0.low - self._calc_long_stop_dist(k0, entry_p)` |
| `_tick_entry` | L374 | `k0.low - self.long_sl_offset` | `k0.low - self._calc_long_stop_dist(k0, fill_p)` |
| `_bar_entry_short` | L752 | `k0.high + self.short_sl_offset` | `k0.high + self._calc_short_stop_dist(k0, entry_p)` |
| `_tick_entry_short` | L824 | `k0.high + self.short_sl_offset` | `k0.high + self._calc_short_stop_dist(k0, fill_p)` |

> Tick 模式使用 `fill_p`（實際成交價）而非 `entry_p`，與現有 risk 計算邏輯一致。

---

## 6. 驗證步驟

1. **單元測試**：確認 `_calc_long_stop_dist` / `_calc_short_stop_dist` 在極端 wick（極短 / 極長）下輸出合理值，且 cap 生效
2. **快照比對**：用固定種子資料跑一次 bar 模式，確認進場訊號不變，只有 `stop_price` 數值改變
3. **全年份回測**：2023 / 2024 / 2025 分年跑，比較 risk 分布（stop_dist / entry_price）是否跨年份更一致
4. **RR 重新校準**：在動態 SL 基礎上重跑 optimize，更新 `long_rr_wick_a/b/c` 與 `short_rr_wick_a/b/c`

---

## 7. 後續行動

- [ ] 實作 `_calc_long_stop_dist` / `_calc_short_stop_dist` helper
- [ ] 替換 4 處停損計算
- [ ] 移除 `long_sl_offset` / `short_sl_offset` 參數宣告
- [ ] 跑全年份回測，檢視 risk 分布一致性
- [ ] 重新優化 RR 參數（以動態 SL 為基礎）
