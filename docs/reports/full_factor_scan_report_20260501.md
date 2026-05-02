# 因子研究審計報告 (Factor Research Audit Report)
**生成時間**: 2026-05-01
**數據範圍**: BTCUSDT | 15m | 2025-01-01 -> 2026-05-01 (約 4.6 萬根 K 線)

---

## 1. 核心結論 (Executive Summary)
本次回測掃描了因子庫中的 **75 個因子**。研究發現，在 15 分鐘週期下，**價格行為 (Price Action)** 類因子的表現顯著優於傳統的技術指標 (Momentum) 與成交量因子。

*   **最強信號**: `ma_trend_alignment_crossover` 展現了極高的短線預測力 (IC 0.31)。
*   **最佳穩定性**: `sweep_pin_bar_short` 與 `atr_percentile_100` 在中長期 (Horizon 3-12) 表現出優異的預測一致性。
*   **均值回歸特徵**: 傳統指標如 RSI、Bollinger Position 在此週期呈現顯著的負相關，適合逆勢操作。

---

## 2. 頂尖因子排名 (Top 5 Alpha Factors)

| 因子名稱 | 分類 | OOS Oriented IC | 最佳 Horizon | 統計顯著性 (IR) |
| :--- | :--- | :---: | :---: | :---: |
| **ma_trend_alignment_crossover** | Price Action | **0.3109** | 1 (15m) | N/A (稀疏信號) |
| **sweep_pin_bar_short** | Price Action | **0.0741** | 3 (45m) | 0.45 (良好) |
| **reversal_bar_up** | Mean-Reversion | **0.0585** | 1 (15m) | 0.55 (穩定) |
| **atr_percentile_100** | Volatility | **0.0465** | 12 (3h) | **0.78 (優異)** |
| **reversal_bar_down** | Mean-Reversion | **0.0342** | 1 (15m) | 0.44 (有效) |

---

## 3. 深度細節分析

### 3.1 價格行為的強大預測力
*   **MA 金叉共振**: `ma_trend_alignment_crossover` 的 IC 高達 0.31。經過代碼審計，未發現未來數據洩漏。這說明在強勢趨勢排列 ($20>50>120$) 下的首次金叉回踩具有極高的勝率。
*   **上影線橫掃**: `sweep_pin_bar_short` 的 IC (0.074) 遠高於 `sweep_pin_bar_long` (0.023)。這反映出在 2025-2026 年的市場環境中，高點的流動性橫掃 (Liquidity Sweep) 後的拒絕信號比低點橫掃更具有預測價值。

### 3.2 波動率作為過濾器
*   **ATR 百分位**: `atr_percentile_100` 的 IR 高達 0.78，是全場最穩定的因子之一。這意味著「波動率的大小」本身就是一個極強的趨勢延續指標。
*   **應用建議**: 建議將 ATR 設為開倉的前提條件，只在 ATR 處於高位時執行價格行為信號。

### 3.3 失敗因子警示
*   **EMA Cross (5/20)**: 在 15m 週期下 IC 為負 (-0.0201)，說明單純的小週期均線交叉容易產生大量假信號 (Whipsaw)。
*   **Volume Z-Score**: 單純的成交量激增並未展現出強方向性 (IC 0.006)，需配合影線或 Delta 資訊使用。

---

## 4. 策略開發建議 (Next Steps)

1.  **結構化過濾 (Structural Filter)**:
    使用 `ma_trend_alignment_crossover` 的邏輯作為「大方向鎖定」，僅在趨勢排列正確時尋找入場機會。
    
2.  **空頭策略優化**:
    重點開發基於 `sweep_pin_bar_short` 的做空策略，因其在各個 Horizon 的表現均優於做多因子。
    
3.  **多因子組合 (Alpha Combination)**:
    建議組合: `Price Action (Signal)` + `ATR Percentile (Volatility Filter)` + `Buy/Sell Trade Volume (Liquidity Confirmation)`。

---
**報告結束**
