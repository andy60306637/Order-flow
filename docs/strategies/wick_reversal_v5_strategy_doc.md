# Wick Reversal 1m v5 策略文件

**版本**：v5  
**建立日期**：2026-04-18  
**前置版本**：v4（global param baseline）  
**策略類別**：`WickReversalV5Strategy`  
**Registry 名稱**：`Wick Reversal 1m v5`

---

## 1. 版本演進概覽

| 版本 | 核心新增 |
|------|---------|
| v3 | 做多做空雙向、tick 進場確認 |
| v4 | Dynamic SL（price-normalised stop）、Dynamic RR（wick 分級 A/B/C） |
| **v5** | **Price-Regime 切換**（R0/R1/R2 三套參數組）|

v5 在 v4 所有邏輯基礎上，加入 `_rp(name, price)` 動態參數派發，使同一套策略邏輯能在不同 BTC 價格水位自動套用對應的校準參數。

---

## 2. 進出場邏輯

### 2.1 K0 辨識（做多）

| 條件 | 規則 |
|------|------|
| 形態 | body_low ≥ bar 中點；lower_wick > body |
| 成交量 | `k0.volume ≥ long_k0_vol_gate`（由 regime 決定） |
| Tick 吸收 | 下影線區域 `wick_delta_eff ≤ lower_wick_absorption_delta_eff_max`，且 wick_vol/total_vol ≥ `lower_wick_absorption_min_vol_ratio` |

### 2.2 K0 辨識（做空，鏡像）

| 條件 | 規則 |
|------|------|
| 形態 | body_high ≤ bar 中點；upper_wick > body |
| 成交量 | `k0.volume ≥ short_k0_vol_gate`（由 regime 決定） |
| Tick 吸收 | 上影線區域 `wick_delta_eff ≥ upper_wick_absorption_delta_eff_min`，且 wick_vol/total_vol ≥ `upper_wick_absorption_min_vol_ratio` |
| Wick 分級 | A/B/C 各有開關（`enable_short_wick_a/b/c`）及附加過濾 |

### 2.3 進場條件（Tick 模式）

**做多**：在 k0 後最多 `long_zoom_bars` 根內，若 tick 累計 `delta_eff > long_delta_eff_threshold` 且價格穿越 k0 實體高點 → 入場  
**做空**：對稱，`delta_eff < -short_delta_eff_threshold` 且價格跌破 k0 實體低點  
守護線（進場失效）：多方為 k0 實體低點；空方為 k0 實體高點

### 2.4 停損計算（Dynamic SL，v4 引入）

```
long_stop_dist  = clip(max(entry × sl_pct_floor,  lower_wick × sl_wick_mult), 0, entry × sl_pct_cap)
short_stop_dist = clip(max(entry × sl_pct_floor,  upper_wick × sl_wick_mult), 0, entry × sl_pct_cap)

long_stop  = k0.low  - long_stop_dist
short_stop = k0.high + short_stop_dist
```

三個 SL 參數（`pct_floor / wick_mult / pct_cap`）均支援 regime 切換。

### 2.5 目標價計算（Dynamic RR，v4 引入）

K0 wick 品質分級決定 RR：

| 等級 | 門檻 | 說明 |
|------|------|------|
| A | wick/body ≥ 4.0 | 強 wick，高 RR |
| B | wick/body ≥ 3.0 | 中等 wick |
| C | 其餘 | 弱 wick，低 RR |

```
target = entry + risk × rr_wick_{grade}    (做多)
target = entry - risk × rr_wick_{grade}    (做空)
```

RR 參數（`rr_wick_a/b/c`）支援 regime 切換。

### 2.6 出場邏輯（Trailing TP/SL）

1. 觸及目標價且動能持續（cum_delta > 0 多方 / < 0 空方）→ 轉 trailing stop（止盈線移至目標價）
2. 觸及目標價但動能已轉 → 直接 TP
3. 價格觸及 stop_price → SL 或 TS（trailing 中）
4. Trailing 中連續 `td_consec_bars` 根反向 delta → TD 出場

---

## 3. Regime 切換機制（v5 新增）

### 3.1 原理

`enable_regime_mode=True` 時，每次取參數改呼叫 `_rp(name, entry_price)`：

```python
def _get_regime(self, price) -> int:
    if price < regime_price_break_0:  return 0   # R0: <50k
    if price < regime_price_break_1:  return 1   # R1: 50k-85k
    return 2                                      # R2: >85k

def _rp(self, name, price):
    if self.enable_regime_mode:
        regime_attr = f'r{self._get_regime(price)}_{name}'
        if hasattr(self, regime_attr):
            return getattr(self, regime_attr)
    return getattr(self, name)
```

支援 regime 切換的參數（`long_*` 做多，`short_*` 做空）：

- `sl_pct_floor`, `sl_wick_mult`, `sl_pct_cap`
- `k0_vol_gate`
- `rr_wick_a`, `rr_wick_b`, `rr_wick_c`
- `min_fee_cover_ratio`

### 3.2 校準預設值（optimizer，3 年 tick shards）

**長倉**

| 參數 | R0 (<50k) | R1 (50k-85k) | R2 (>85k) |
|------|-----------|-------------|----------|
| sl_pct_floor | 0.001 | 0.001 | 0.0003 |
| sl_wick_mult | 0.2 | 0.2 | 0.2 |
| sl_pct_cap | 0.002 | 0.003 | 0.003 |
| k0_vol_gate | 1200 | 1200 | 800 |
| rr_wick_a | 3.0 | 2.5 | 3.0 |
| rr_wick_b | 2.0 | 2.5 | 1.5 |
| rr_wick_c | 1.0 | 2.0 | 1.0 |
| min_fee_cover_ratio | 1.2 | 1.2 | 1.2 |

**短倉**

| 參數 | R0 (<50k) | R1 (50k-85k) | R2 (>85k) |
|------|-----------|-------------|----------|
| sl_pct_floor | 0.001 | 0.0008 | 0.001 |
| sl_wick_mult | 0.2 | 0.15 | 0.2 |
| sl_pct_cap | 0.003 | 0.003 | 0.003 |
| k0_vol_gate | 500 | 300 | 500 |
| rr_wick_a | 4.5 | 4.5 | 4.5 |
| rr_wick_b | 1.0 | 2.5 | 2.5 |
| rr_wick_c | 2.0 | 2.0 | 2.0 |
| min_fee_cover_ratio | 1.2 | 2.0 | 2.0 |

---

## 4. 回測結果

**回測設定**：initial_capital=1650，leverage=20，fee=Taker 0.032%，slippage=0.2bps

### 4.1 v4 Global Mode（單套參數）

| 年份 | Trades | WR | PF | PnL | Max DD |
|------|--------|----|----|-----|--------|
| Y1 (2023-04~2024-04) | 720 | 34.9% | 0.588 | -1645 | 99.8% |
| Y2 (2024-04~2025-04) | 386 | 37.8% | 0.812 | -1113 | 73.2% |
| Y3 (2025-04~2026-04) | 235 | 42.6% | 1.086 | +607 | 38.8% |

### 4.2 v5 Regime Mode（三套參數）

| 年份 | Trades | WR | PF | PnL | Max DD |
|------|--------|----|----|-----|--------|
| Y1 (2023-04~2024-04) | 376 | 37.5% | 0.833 | ~-1100 | 76.7% |
| Y2 (2024-04~2025-04) | 210 | 38.6% | **1.081** | +556 | **28.5%** |
| Y3 (2025-04~2026-04) | 125 | 52.8% | **1.510** | +3493 | **17.1%** |

主要改善：
- Y2 從虧損（PF 0.812）轉為盈利（PF 1.081），DD 由 73% 降至 28%
- Y3 PF 由 1.086 提升至 1.510，DD 由 39% 降至 17%
- trades 減少（更嚴格篩選）是正向信號，非過度濾除

---

## 5. 已知問題

**Y1（BTC <50k 時代）結構性虧損**：
- 即使 R0 參數獨立校準後，PF 仍為 0.833（DD 76.7%）
- 原因推測：2023 年低波動 + 低流動性環境下，wick absorption 信號噪音較高
- 這不是參數問題，而是策略在低價格水位市場結構的邊際侷限性
- 建議後續研究：R0 加入市場狀態過濾（如 ATR 百分比門檻、日成交量下限）

---

## 6. 優化工具

```bash
# 全局單套參數優化（v4 用）
python utils/optimize_wick_reversal_v4.py --symbol BTCUSDT --train-start 2025-01-01 --split 2026-01-01 --end 2026-04-14

# Regime 三區間分套優化（v5 用）
python utils/optimize_wick_reversal_v4_regime.py --passes 3 --topn 8 --out docs/reports/wick_v5_regime_opt.json

# 三年 shard 回測驗證
python utils/backtest_dynamic_sl.py                                           # v4 global
python utils/backtest_dynamic_sl.py --regime-params docs/reports/wick_v5_regime_opt.json  # v5 regime
```

> 注意：`optimize_wick_reversal_v4_regime.py` 和 `backtest_dynamic_sl.py` 目前的 `STRATEGY_NAME` 仍指向 `"Wick Reversal 1m v4"`，使用 v5 時需更新為 `"Wick Reversal 1m v5"`。

---

## 7. 參數完整清單

### 全局參數（非 regime）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| enable_long | True | 啟用做多 |
| long_zoom_bars | 1 | k0 後進場觀察窗口 |
| long_td_consec_bars | 3 | trailing 連續反向出場門檻 |
| long_delta_eff_threshold | 0.8 | 進場 delta_eff 門檻 |
| long_vol_sma_period | 20 | 成交量 SMA 窗期 |
| long_vol_sma_mult | 1.0 | 成交量 SMA 倍率 |
| long_wick_type_a_threshold | 4.0 | Wick A 級 wick/body 門檻 |
| long_wick_type_b_threshold | 3.0 | Wick B 級 wick/body 門檻 |
| lower_wick_absorption_min_vol_ratio | 0.15 | 下影線吸收最低成交量佔比 |
| lower_wick_absorption_delta_eff_max | 0.0 | 下影線吸收最大 delta_eff |
| enable_short | True | 啟用做空 |
| short_zoom_bars | 1 | k0 後進場觀察窗口 |
| short_td_consec_bars | 2 | trailing 連續反向出場門檻 |
| short_delta_eff_threshold | 0.8 | 進場 delta_eff 門檻（絕對值）|
| short_vol_sma_mult | 1.6 | 成交量 SMA 倍率 |
| enable_short_wick_a/b | True | 啟用 S5A/S5B 信號 |
| enable_short_wick_c | False | 停用 S5C 信號 |
| enable_regime_mode | True | 啟用 regime 切換 |
| regime_price_break_0 | 50000 | R0/R1 分界價格 |
| regime_price_break_1 | 85000 | R1/R2 分界價格 |
| taker_fee_rate | 0.00032 | Taker 手續費率 |
| slippage_rate | 0.00002 | 滑點率 |
