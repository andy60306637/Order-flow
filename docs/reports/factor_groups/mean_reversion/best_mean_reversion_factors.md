# Best Mean-Reversion Factors

整理日期: 2026-05-05

資料來源:
- 因子定義: `research/factors.py`
- 表現資料: `docs/reports/factor_groups/mean_reversion/summary.csv`

排序口徑:
- 主要使用 `rank_score`，也就是有明確 Long/Short 方向的 OOS Oriented Rank IC。
- `orientation = 0` 的 Long/Short 雙向因子在正式排名中會被設為 `rank_score = -1e9`，因此另列為輔助候選。

## 可直接按方向使用的候選因子

| Rank | Factor | Side | OOS Horizon | OOS Oriented Rank IC | OOS IR | OOS t-stat | OOS Samples | 訊號原理 |
| ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| 1 | `reversal_bar_down` | Short | 1 | 0.018461 | 0.246 | 1.394 | 3,093 | 大 range K 棒，上影線占比 >= 0.5，且收盤位置偏低。代表上衝後被賣回來，是追多失敗或上方流動性被掃後回落的空方反轉訊號。 |
| 2 | `reversal_bar_up` | Long | 1 | 0.017042 | 0.195 | 1.101 | 3,490 | 大 range K 棒，下影線占比 >= 0.5，且收盤位置偏高。代表下探後被買回來，是低位吸收或清算後反彈的多方反轉訊號。 |
| 3 | `upper_wick_to_body_ratio` | Short | 6 | 0.007193 | 0.442 | 2.539 | 93,085 | 上影線 / 實體。上影線相對實體越長，代表高位拒絕越明顯，偏短線回落。這個因子的 t-stat 是本組候選中最高。 |
| 4 | `upper_wick_ratio` | Short | 6 | 0.006812 | 0.233 | 1.338 | 93,139 | 上影線 / 整根 high-low range。衡量整根 K 裡高位拒絕的占比，邏輯與 `upper_wick_to_body_ratio` 接近，但較不受小實體放大影響。 |
| 5 | `lower_wick_to_body_ratio` | Long | 1 | 0.006375 | 0.300 | 1.726 | 93,085 | 下影線 / 實體。下影線相對實體越長，代表低位被買回越強，偏短線反彈。 |
| 6 | `lower_wick_ratio` | Long | 1 | 0.006174 | 0.344 | 1.975 | 93,139 | 下影線 / 整根 high-low range。衡量整根 K 裡低位吸收的占比，t-stat 接近 2，穩定性優於大部分小樣本事件因子。 |

## 輔助候選因子

這些因子 raw OOS Oriented Rank IC 為正，但在 `research/factors.py` 中是 Long/Short 雙向因子，缺少明確方向，因此正式 `rank_score` 不採用。比較適合作為 regime filter、權重調整或重新定義方向後再測。

| Factor | Side | OOS Horizon | OOS Oriented Rank IC | OOS IR | OOS t-stat | OOS Samples | 訊號原理 |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| `bollinger_width_20` | Long/Short | 12 | 0.019188 | 0.200 | 1.146 | 93,139 | 20 期布林帶寬度 / MA。代表波動擴張或收縮狀態；本身不是清楚的均值回歸方向，更適合判斷是否處於高波動/擴張環境。 |
| `body_ratio` | Long/Short | 6 | 0.004333 | 0.291 | 1.670 | 93,139 | 實體 / high-low range。衡量 K 棒趨勢實體占比；可當作反轉訊號的品質濾網，例如影線反轉時避免實體過大造成訊號互斥。 |
| `range_zscore_20` | Long/Short | 1 | 0.000759 | 0.062 | 0.355 | 93,139 | high-low range 的 20 期 z-score。表現很弱，較適合作為波動環境描述，不建議單獨交易。 |

## 優先順序

1. 優先研究 `upper_wick_to_body_ratio`、`lower_wick_ratio`、`lower_wick_to_body_ratio`：樣本數大，t-stat 相對好，較適合做穩定策略基底。
2. `reversal_bar_down`、`reversal_bar_up` 分數最高，但樣本約 3k，屬於事件型反轉訊號，應先檢查年度穩定性、交易頻率與成本後再進策略。
3. `bollinger_width_20` 不應直接當方向因子，可用來過濾高波動時段，或搭配影線反轉因子測試是否能提升命中率。
4. `zscore_price_20`、`bollinger_position_20`、`rsi_14`、`stoch_k`、`distance_to_vwap` 在這批 OOS 方向表現偏負，不建議按目前定義直接使用。
