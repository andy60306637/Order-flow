# 因子分析使用者手冊

## 目錄

1. [系統架構概覽](#1-系統架構概覽)
2. [如何進行因子分析](#2-如何進行因子分析)
   - 2.1 [開啟 Research Lab](#21-開啟-research-lab)
   - 2.2 [設定資料來源](#22-設定資料來源)
   - 2.3 [選擇時間區間](#23-選擇時間區間)
   - 2.4 [選擇因子](#24-選擇因子)
   - 2.5 [設定研究參數](#25-設定研究參數)
   - 2.6 [執行分析](#26-執行分析)
3. [如何解讀分析結果](#3-如何解讀分析結果)
   - 3.1 [Factor Ranking（因子排名）](#31-factor-ranking因子排名)
   - 3.2 [IC by Horizon（分 Horizon 指標）](#32-ic-by-horizon分-horizon-指標)
   - 3.3 [Quantiles（分位數分析）](#33-quantiles分位數分析)
   - 3.4 [Monthly / Yearly Stability（穩定度）](#34-monthly--yearly-stability穩定度)
   - 3.5 [Factor Correlations（因子相關性）](#35-factor-correlations因子相關性)
   - 3.6 [Unavailable（無法執行的因子）](#36-unavailable無法執行的因子)
4. [標準判讀流程](#4-標準判讀流程)
5. [常見問題](#5-常見問題)

---

## 1. 系統架構概覽

```
research/
├── base.py       因子基礎類別、欄位分類常數、工具函式
├── factors.py    已實作的具體因子（全部自動注冊）
├── registry.py   因子注冊表
└── runner.py     分析引擎（IC 計算、分位數、穩定度、相關性）
```

**資料流向：**
```
K 線資料 + Tick 資料
        ↓
   factor.compute()          ← 每根 K 線產出一個浮點數（因子值）
        ↓
   _forward_return()         ← 計算每根 K 線對應的未來報酬
        ↓
   IC / Rank IC / IR / t-stat
   分位數分析（in/out-of-sample）
   月 / 年穩定度
   因子相關矩陣
```

---

## 2. 如何進行因子分析

### 2.1 開啟 Research Lab

在主視窗頂部分頁列找到 **Research Lab** 標籤，點擊進入。

---

### 2.2 設定資料來源

| 欄位 | 說明 |
|------|------|
| **Symbol** | 交易對，例如 `BTCUSDT` |
| **Interval** | K 線週期，例如 `1m`、`5m`、`1h` |
| **Use tick-derived factors when available** | 勾選後，系統會載入 Tick 資料並啟用需要 Tick 的因子。若本地無 Tick 快取，這些因子會進入 Unavailable 列表，不影響 K 線因子。 |

**建議：**
- 初始研究先用 `1m`，取得足夠樣本數（30,000 根以上）。
- Tick 因子計算量較大，第一次可以先取消勾選，確認 K 線因子有效後再補。

---

### 2.3 選擇時間區間

在 **Time Slice** 區塊選取想分析的月份。

**注意事項：**
- 避免橫跨市場制度差異過大的時段（例如 2020 年 COVID 暴跌期間 + 2021 年多頭）混在一起分析，會污染 IC。
- 建議至少選取 **6 個月** 以上，讓月度穩定度分析有意義（需至少 3 個月有效期間）。
- 若資料跨越幣種上架 / 下架，建議分開跑。

---

### 2.4 選擇因子

**篩選器：**

| 篩選器 | 說明 |
|--------|------|
| **Side** | `Long`：篩出預測多頭機會的因子。`Short`：篩出預測空頭機會的因子。`All`：全部顯示。 |
| **Group** | 按因子所屬類別篩選（微結構、動能、均值回歸…）。 |
| **Check Visible / Clear Visible** | 一鍵勾選 / 取消目前篩選結果中的所有因子。 |

**目前已實作的因子：**

| 因子名稱 | Side | Group | 說明 |
|----------|------|-------|------|
| `lower_wick_to_body_ratio` | Long | Mean-Reversion | 下影線 / 實體比。比例越高，代表 K 線收盤前被強力買盤頂回，均值回歸多頭信號。 |
| `upper_wick_to_body_ratio` | Short | Mean-Reversion | 上影線 / 實體比。比例越高，代表賣壓強，均值回歸空頭信號。 |
| `lower_wick_delta_eff` | Long | Micro-structure | 下影線區域 Tick 的買賣差值效率（需 Tick）。正值越大代表下影線區有強力淨買入（absorption）。 |
| `upper_wick_delta_eff` | Short | Micro-structure | 上影線區域 Tick 的買賣差值效率（需 Tick）。負值越大代表上影線區有強力淨賣出。 |
| `lower_wick_volume_ratio` | Long | Micro-structure | 下影線 Tick 成交量 / 該根 K 線總成交量（需 Tick）。比例越高代表成交量集中在下影線區。 |
| `upper_wick_volume_ratio` | Short | Micro-structure | 上影線 Tick 成交量 / 總成交量（需 Tick）。比例越高代表成交量集中在上影線區。 |

**建議初始跑法：**
全選所有 K 線因子（不含 tick），確認基礎信號後再加入 tick 類。

---

### 2.5 設定研究參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| **Forward Horizons (bars)** | `1,3,6,12` | 因子預測的未來 K 線數。例如 `1m` 週期配 `12`，代表預測 12 根後（12 分鐘）的報酬。用逗號分隔可同時測多個 horizon。 |
| **Quantiles** | `5` | 把因子值切成幾個等份（分位數）分析。建議 5（五分位）或 10（十分位）。樣本太少時降到 3 或 4。 |

**ResearchConfig 進階參數（程式碼層級調整）：**

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `entry_lag` | `1` | 信號產生後幾根 K 線才進場。`1` 代表信號在 K 線 i 收盤產生，在 K 線 i+1 收盤進場（避免使用收盤瞬間成交的假設）。設為 `0` 只在你確定能在收盤當下成交時使用。 |
| `min_period_samples` | `30` | 月度/年度穩定度每個區間要有至少幾個有效樣本才計算 IC。低於此值的月份直接跳過。 |
| `train_ratio` | `0.5` | 分位數 Out-of-Sample 分析的 in-sample 比例。前 50% 計算分桶邊界，後 50% 評估表現。 |
| `ic_period_granularity` | `"month"` | 計算 IC IR 時用月或年切分子期間。通常保持 `"month"`。 |

---

### 2.6 執行分析

1. 點擊 **Run Research**。
2. 狀態列顯示 `Running vectorized research...`，等待計算完成。
3. 完成後狀態列顯示 `Done | rows=N | factors=M`，結果分散在右側各分頁。
4. 可點擊 **Export** 匯出為 JSON（完整結果）或 CSV 資料夾（每個表一個 CSV 檔）。

---

## 3. 如何解讀分析結果

### 3.1 Factor Ranking（因子排名）

**每個因子一列，依 `oriented_rank_ic` 由高到低排序（最強信號在最上方）。**

| 欄位 | 說明 | 判讀標準 |
|------|------|----------|
| `factor` | 因子名稱 | — |
| `side` | Long / Short / Long/Short | — |
| `group` | 因子所屬類別 | — |
| `orientation` | +1（多頭因子）/ -1（空頭因子）/ 0（雙向） | — |
| `best_horizon` | `oriented_rank_ic` 最高時對應的 horizon | 代表因子在哪個預測期最有效 |
| `best_rank_ic` | 最佳 horizon 的原始 Rank IC 值（有正負號） | 正代表「值越高 → 未來報酬越高」，負則相反 |
| `oriented_rank_ic` | Rank IC × orientation（正值永遠代表「符合設計方向的信號強度」） | **主排序依據**。> 0.03 有參考價值，> 0.05 值得深入研究 |
| `ic_ir` | IC Information Ratio = 月度 Rank IC 均值 / 標準差 | **統計穩定性指標**。> 0.5 算穩定，> 1.0 優秀 |
| `ic_t_stat` | IR × √(有效月數) | > 2.0 表示統計顯著（95% 信心水準） |
| `avg_abs_rank_ic` | 所有 horizon 的 \|Rank IC\| 平均 | 衡量因子在各期的綜合強度 |
| `sample_count` | 有效樣本數 | 低於 1,000 的結論要保持懷疑 |

**快速篩選邏輯：**
```
oriented_rank_ic > 0.03  →  信號有方向性
ic_t_stat > 2.0          →  統計顯著，非偶然
ic_ir > 0.5              →  穩定性夠，月度之間不亂飄
→ 三條件同時滿足才列入候選因子
```

---

### 3.2 IC by Horizon（分 Horizon 指標）

**每個（因子 × horizon）組合一列。用來觀察信號隨時間的衰減結構。**

| 欄位 | 說明 |
|------|------|
| `ic` | Pearson IC：因子值與未來報酬的線性相關係數 |
| `rank_ic` | Spearman Rank IC：因子排名與報酬排名的相關。**比 IC 更穩健**，不受極端值影響，優先看這個 |
| `oriented_rank_ic` | 修正方向後的 Rank IC（正值代表信號有效） |
| `ic_period_mean` | 月度 Rank IC 的均值 |
| `ic_period_std` | 月度 Rank IC 的標準差（越小越穩定） |
| `ic_ir` | 月度 IC 的 IR（見上方說明） |
| `ic_t_stat` | t 統計量（見上方說明） |
| `ic_periods` | 納入計算的有效月份數（< 3 則 IR/t-stat 為 0，不可信） |
| `sample_count` | 有效樣本數 |

**信號衰減判讀：**

理想因子的 `oriented_rank_ic` 應隨 horizon 增加而衰減（而非隨機跳動），例如：

```
horizon=1  →  0.060
horizon=3  →  0.045
horizon=6  →  0.028
horizon=12 →  0.012  ← 自然衰減，信號有效
```

若各 horizon 值亂跳（例如 h=1 是 0.01，h=6 突然跳到 0.08），代表可能是樣本噪音而非真實 alpha。

---

### 3.3 Quantiles（分位數分析）

**核心表格，驗證「因子值越高/低，未來報酬是否單調遞增/遞減」。**

每個（因子 × horizon × 分位數 × 樣本類型）一列。

| 欄位 | 說明 |
|------|------|
| `sample` | `in_sample`：全樣本排序切桶（參考用）。`out_of_sample`：用前 50% 計算邊界，後 50% 評估（**主要看這個**） |
| `quantile` | 分位桶編號（1 = 因子值最低，N = 因子值最高） |
| `mean_return` | 該分位桶的平均未來報酬。正數代表此 horizon 期間平均上漲 |
| `win_rate` | 報酬 > 0 的比例（%）。> 55% 才有操作意義 |
| `sample_count` | 桶內樣本數 |
| `spread_qhigh_qlow` | 最高分位 mean_return − 最低分位 mean_return |
| `oriented_spread` | spread × orientation（正值代表符合設計方向的單調性） |

**判讀要點：**

1. **Long 因子（orientation=+1）**：應看到分位數越高 → `mean_return` 越正的單調遞增關係：
   ```
   Q1（最低值）→  mean_return ≈ -0.0003
   Q2           →  mean_return ≈ -0.0001
   Q3           →  mean_return ≈  0.0002
   Q4           →  mean_return ≈  0.0004
   Q5（最高值）→  mean_return ≈  0.0008  ← 最強信號
   ```

2. **Short 因子（orientation=-1）**：應看到分位數越高 → `mean_return` 越負（因子值越大 → 越空頭）：
   ```
   Q5（最高值）→  mean_return ≈ -0.0008  ← 最強信號
   ```

3. **`out_of_sample` 的單調性是關鍵**：若 OOS 分位數分布亂跳、不單調，代表 in_sample 的結果可能是過擬合。

4. **`oriented_spread` > 0 且絕對值越大越好**：代表因子能有效區分強弱報酬。

---

### 3.4 Monthly / Yearly Stability（穩定度）

**驗證 IC 是否跨時間段一致，而非只在特定市場環境有效。**

每個（因子 × horizon × 期間）一列。

| 欄位 | 說明 |
|------|------|
| `period` | 月份（格式 `YYYY-MM`）或年份（`YYYY`） |
| `ic` | 該期間的 Pearson IC |
| `rank_ic` | 該期間的 Rank IC |
| `spread_qhigh_qlow` | 該期間的分位數報酬極差 |
| `sample_count` | 該期間有效樣本數（低於 `min_period_samples=30` 的月份不出現） |

**判讀要點：**

- **月度 Rank IC 方向一致性**：好因子的月度 IC 應 **大多同號**（多頭因子多數月份為正，空頭因子多數月份為負）。如果 IC 正負各半，代表因子不穩定。
- **參考比率**：IC 同號比例 > 60% 才算穩定；> 70% 算優良。
- **看熊市與牛市是否都有效**：若因子只在某種市場趨勢下有效，使用時需加入 regime filter。

---

### 3.5 Factor Correlations（因子相關性）

**避免選到高度相關的因子組合，防止把同一個 alpha 當兩個獨立因子。**

每個因子對一列，依 `|spearman|` 由高到低排序。

| 欄位 | 說明 |
|------|------|
| `factor_a` / `factor_b` | 因子名稱 |
| `pearson` | 線性相關係數 |
| `spearman` | 排名相關係數（更穩健，優先看這個） |
| `sample_count` | 兩因子同時有有效值的樣本數 |

**決策規則：**

```
|spearman| > 0.7  →  高度相關，兩個因子選一個即可（通常選 IC IR 較高的）
|spearman| 0.4~0.7  →  中度相關，可並存但需注意多頭配置時的集中風險
|spearman| < 0.4  →  低相關，視為獨立因子，並存合理
```

---

### 3.6 Unavailable（無法執行的因子）

| 欄位 | `reason` 說明 |
|------|---------------|
| `factor` | 因子名稱 |
| `reason` | `not_registered`：因子名稱不在 registry 中（名稱拼錯或忘了 import factors.py）。`tick_data_unavailable`：因子需要 tick 資料但本地無快取或勾選了「不使用 tick」。 |

---

## 4. 標準判讀流程

按以下順序判讀，效率最高：

```
Step 1  Factor Correlations
        → 先刪掉 |spearman| > 0.7 的重複因子
        → 留下低相關的候選集

Step 2  Factor Ranking（看 out_of_sample 指標）
        → oriented_rank_ic > 0.03？
        → ic_t_stat > 2.0？
        → ic_ir > 0.5？
        → 三條件同時滿足 → 進入 Step 3

Step 3  IC by Horizon
        → 找最佳 horizon（oriented_rank_ic 最高的期數）
        → 確認隨 horizon 增加有自然衰減（非隨機跳動）

Step 4  Quantiles（重點看 out_of_sample 列）
        → 分位數單調性是否成立？
        → OOS oriented_spread > 0？
        → OOS Q5（或 Q1 for short）的 win_rate > 55%？

Step 5  Monthly Stability
        → 月度 IC 同號比例 > 60%？
        → 是否在熊市 / 牛市都有效？
        → IC 是否在某段時間突然失效（可能碰到制度轉換）？

→ 通過全部 Step 的因子才納入策略考量
```

---

## 5. 常見問題

**Q: IC 值 0.03 很低，代表因子沒用嗎？**
A: 不。量化金融中 IC > 0.05 已屬優秀，IC > 0.10 在高頻市場幾乎不存在。關鍵是 IC 的**穩定性**（IC IR）而非絕對大小。IC=0.03 但月月正值（IC IR=1.2）遠比 IC=0.08 但正負亂跳（IC IR=0.3）更有價值。

**Q: in_sample 和 out_of_sample 的分位數差很多，怎麼辦？**
A: 這代表 in_sample 結果有過擬合跡象，要以 `out_of_sample` 為準。如果 OOS 的分位數仍有一定單調性，因子還是可用，只是強度要往下打折估計。若 OOS 完全沒規律，這個因子在當前資料上不可信。

**Q: ic_periods 只有 2，IC IR 顯示 0，怎麼辦？**
A: 有效月份不足 3 個時，IR / t-stat 不計算（顯示 0）。需要增加資料時間範圍，或降低 `min_period_samples` 讓更多月份納入計算（但小心樣本噪音增加）。

**Q: 某個 tick 因子一直在 Unavailable 裡？**
A: 確認：(1) 有勾選「Use tick-derived factors」；(2) 所選時間區間在本地有 tick 快取；(3) 在 Research Lab 狀態欄確認載入資料量（rows=N）正常。

**Q: entry_lag 設 0 和 1 差多少？**
A: `entry_lag=0` 假設能在 K 線收盤瞬間以收盤價成交，這在實務中幾乎不可能（收盤觸發信號時已無法以該價格進場）。`entry_lag=1` 是較保守且現實的假設，會讓 IC 略低但更接近真實可執行的 alpha。若你用限價單且通常能在下一根 K 線早期成交，`entry_lag=1` 是合理的。
