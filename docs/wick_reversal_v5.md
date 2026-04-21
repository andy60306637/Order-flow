# Wick Reversal v5 策略文檔

## 概述

Wick Reversal v5（`WickReversalV5Strategy`）是基於 v4 的延伸版本，加入了**價格區間感知（Price-Regime Switching）**機制，針對不同價格帶自動套用不同的參數組，同時支援多空雙向交易與 Tick 級精確進出場。

---

## 核心邏輯

### k0 蠟燭判定

#### 做多 k0（下影線反轉）
- 實體位於整根 K 棒**上半部**（`body_low >= mid`）
- 下影線長度 > 實體長度（`lower_wick > body`）
- 下影線區域出現**吸收**：delta_eff ≤ 0（賣壓被承接）
- 成交量 ≥ `long_k0_vol_gate`

#### 做空 k0（上影線反轉）
- 實體位於整根 K 棒**下半部**（`body_high <= mid`）
- 上影線長度 > 實體長度（`upper_wick > body`）
- 上影線區域出現**吸收**：delta_eff ≥ 0（買壓被承接）
- 成交量 ≥ `short_k0_vol_gate`
- 通過 Wick 類型啟用判斷（A/B/C 各自可獨立開關）

### Zoom 進場窗口

k0 確認後，在接下來 `long_zoom_bars`（或 `short_zoom_bars`）根 K 棒內等待進場觸發：

| 方向 | 進場條件 | 守護線（失效條件） |
|------|----------|-------------------|
| 做多 | 價格突破 k0 實體高點 + 累積 delta_eff > 門檻 | `k.low < k0_body_low` |
| 做空 | 價格跌破 k0 實體低點 + 累積 delta_eff < -門檻 | `k.high > k0_body_high` |

### 停損計算

```
做多停損距離 = max(entry × pct_floor, lower_wick × wick_mult)
             上限 = entry × pct_cap
做多停損價   = k0.low - 停損距離

做空停損距離 = max(entry × pct_floor, upper_wick × wick_mult)
             上限 = entry × pct_cap
做空停損價   = k0.high + 停損距離
```

停損距離會依影線大小動態伸縮，短影線用百分比下限保底，長影線不會超過上限。

### Wick 類型分類與動態 RR

| 類型 | 判斷條件（影線 / 實體比） |
|------|--------------------------|
| A 級 | ≥ `wick_type_a_threshold`（預設 4.0） |
| B 級 | ≥ `wick_type_b_threshold`（預設 3.0） |
| C 級 | 其餘 |

每個類型對應不同的盈虧比（RR），由 `long_rr_wick_a/b/c` 與 `short_rr_wick_a/b/c` 控制。

### 出場機制

| 標籤 | 觸發條件 |
|------|----------|
| **SL** | 價格觸及停損線 |
| **TP** | 價格觸及目標價，且當下 delta 方向不支持持續 |
| **TS（Trailing Stop）** | 價格觸及目標後 delta 方向仍有利，停損移至目標價鎖利 |
| **TD（Trailing Delta）** | Trailing 狀態下，連續 N 根 K 棒反向 delta，市價出場 |

---

## Regime 切換系統

### 三層優先查找（`_rp(name, price)`）

```
1. Band 模式（10k 帶）：b{idx}_{name}  ← 優先
2. Legacy Regime（R0/R1/R2）：r{0|1|2}_{name}
3. Global 預設：{name}                  ← 最後 fallback
```

### Legacy Regime 分界

| Regime | 價格區間 | 切換參數 |
|--------|----------|---------|
| R0 | < `regime_price_break_0`（預設 50,000） | `r0_*` |
| R1 | 50,000 ~ `regime_price_break_1`（85,000） | `r1_*` |
| R2 | > 85,000 | `r2_*` |

### Band 模式

- `regime_band_size`：每帶寬度（預設 10,000）
- `regime_band_floor`：起始基準（預設 0）
- Band 索引 = `int((price - band_floor) // band_size)`
- 若對應 `b{idx}_{name}` 屬性存在，優先使用

---

## 已校準參數（3 年 Tick 資料，2023-04 ~ 2026-04）

### Legacy Regime

| Regime | Long vol_gate | Long RR (A/B/C) | Short fee_cover |
|--------|:-------------:|:---------------:|:---------------:|
| R0 <50k | 1200 | 3.0 / 2.0 / 1.0 | 1.2 |
| R1 50k-85k | 1200 | 2.5 / 2.5 / 2.0 | 2.0 |
| R2 >85k | 800 | 3.0 / 1.5 / 1.0 | 2.0 |

### Band 覆蓋（已接受）

| Band | 方向 | 主要調整 |
|------|------|---------|
| b5（50k-60k） | 空單 | RR: 2.5 / 1.5 / 0.8（保守化） |
| b8（80k-90k） | 多單 | RR: 3.0 / 2.0 / 1.0 |
| b11（110k-120k） | 多單 | RR: 3.0 / 1.5 / 1.0，SL pct_cap: 0.2% |

---

## 回測結果（Regime Mode）

| 年度 | 期間 | 交易數 | 勝率 | PF | 最大回撤 |
|------|------|:------:|:----:|:--:|:--------:|
| Y1 | 2023-04 ~ 2024-04 | 376 | 37.5% | 0.833 | 76.7% |
| Y2 | 2024-04 ~ 2025-04 | 210 | 38.6% | 1.081 | 28.5% |
| Y3 | 2025-04 ~ 2026-04 | 125 | 52.8% | 1.510 | 17.1% |

> Y1 PF < 1 且 DD 偏高，為待改善主要目標。

---

## 參數一覽

### 全局開關

| 參數 | 預設值 | 說明 |
|------|:------:|------|
| `enable_long` | `True` | 啟用做多 |
| `enable_short` | `True` | 啟用做空 |
| `enable_regime_mode` | `True` | 啟用 Regime 切換 |
| `allow_bar_fallback_in_tick_mode` | `True` | Tick 資料缺失時退回 Bar 模式 |

### 做多核心參數

| 參數 | 預設值 | 說明 |
|------|:------:|------|
| `long_zoom_bars` | 1 | k0 後允許進場的最大 K 棒數 |
| `long_sl_pct_floor` | 0.0003 | 停損距離最小值（入場價 0.03%） |
| `long_sl_wick_mult` | 0.2 | 下影線乘數 |
| `long_sl_pct_cap` | 0.003 | 停損距離上限（入場價 0.3%） |
| `long_k0_vol_gate` | 800 | k0 最低成交量 |
| `long_delta_eff_threshold` | 0.8 | 進場 delta_eff 門檻 |
| `long_td_consec_bars` | 3 | Trailing 連續反向 delta 觸發 TD |
| `long_min_fee_cover_ratio` | 1.2 | 最低費用覆蓋倍率 |
| `long_rr_wick_a/b/c` | 3.0 / 1.5 / 1.0 | A/B/C 級盈虧比 |

### 做空核心參數

| 參數 | 預設值 | 說明 |
|------|:------:|------|
| `short_zoom_bars` | 1 | k0 後允許進場的最大 K 棒數 |
| `short_sl_pct_floor` | 0.001 | 停損距離最小值（入場價 0.1%） |
| `short_sl_wick_mult` | 0.2 | 上影線乘數 |
| `short_sl_pct_cap` | 0.003 | 停損距離上限（入場價 0.3%） |
| `short_k0_vol_gate` | 300 | k0 最低成交量 |
| `short_delta_eff_threshold` | 0.8 | 進場 delta_eff 門檻（負向） |
| `short_td_consec_bars` | 2 | Trailing 連續反向 delta 觸發 TD |
| `short_min_fee_cover_ratio` | 2.0 | 最低費用覆蓋倍率 |
| `short_rr_wick_a/b/c` | 4.5 / 2.5 / 2.0 | A/B/C 級盈虧比 |
| `enable_short_wick_a/b/c` | True / True / **False** | 各類型做空開關 |

### S4B 專屬過濾器

| 參數 | 預設值 | 說明 |
|------|:------:|------|
| `short_b_min_upper_wick_pct` | 0.0 | B 級最小上影線幅度（佔收盤價）|
| `short_b_min_k0_vol` | 0.0 | B 級最低 k0 成交量 |
| `short_b_min_runup_pct` | 0.0 | k0 前 N 根最小漲幅 |
| `short_b_runup_lookback` | 3 | 前置漲幅觀察根數 |

### 費用參數

| 參數 | 預設值 | 說明 |
|------|:------:|------|
| `taker_fee_rate` | 0.00032 | Taker 手續費率 |
| `slippage_rate` | 0.00002 | 滑價率（0.2 bps） |

費用覆蓋公式：
```
min_risk = 2 × (taker_fee_rate + slippage_rate) × entry_price × fee_cover_ratio / RR
```

---

## 與其他版本比較

| 特性 | v4_log | v4_band_files | v5 |
|------|:------:|:-------------:|:--:|
| SL 計算 | 固定 pct 偏移 | 繼承 v4 固定點數 | wick-aware（floor + mult + cap） |
| Regime 切換 | 無 | 外部 JSON（1k 帶） | Inline（10k 帶）|
| 動態 vol gate | SMA 模式 | 無 | 固定（per-regime） |
| min_rng_pct 過濾 | 有 | 無 | **無** |
| 動態 RR | 有 | 有 | 有 |
| 參數來源 | Hardcoded | JSON 檔案 | Hardcoded |

---

## 已知限制與待改善項目

1. **Y1（2023）表現差**：PF=0.833，DD=76.7%；低價區市場結構可能不適合此策略形態
2. **Band 覆蓋不完整**：只有 b5/b8/b11 有 Band 覆蓋，其他區間仍靠 Legacy regime
3. **缺少 min_rng_pct 過濾**：相比 v4_log，沒有最小 K 棒波動幅度過濾，低波動噪音 K 棒可能誤判為 k0
4. **持倉期間 SL 用進場時的 regime**：`_rp()` 每次動態計算，若跨 band 出場時 SL 參數可能與進場時不一致（v4_band_files 有 `_trade_band_key` 鎖定機制，v5 沒有）
