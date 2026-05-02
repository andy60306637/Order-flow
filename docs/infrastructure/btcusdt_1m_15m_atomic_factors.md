# BTCUSDT 1m / 15m 原子級別因子清單

> 目標：整理適用於 BTCUSDT 1m / 15m 級別交易研究的原子級別因子。  
> 欄位包含：因子名稱、Group、定義。

---

## Group 1：微觀結構與訂單流因子  
**Micro-structure & Order Flow Factors**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `buy_trade_volume_1m` | Micro-structure & Order Flow | 1 分鐘內主動買成交量。 |
| `sell_trade_volume_1m` | Micro-structure & Order Flow | 1 分鐘內主動賣成交量。 |
| `trade_count_1m` | Micro-structure & Order Flow | 1 分鐘內成交筆數。 |
| `avg_trade_size_1m` | Micro-structure & Order Flow | 1 分鐘成交量除以成交筆數，用來衡量平均單筆成交大小。 |
| `large_trade_count_1m` | Micro-structure & Order Flow | 1 分鐘內大於指定門檻的成交筆數。 |
| `large_buy_trade_volume_1m` | Micro-structure & Order Flow | 1 分鐘內大額主動買成交量。 |
| `large_sell_trade_volume_1m` | Micro-structure & Order Flow | 1 分鐘內大額主動賣成交量。 |
| `trade_volume_delta_1m` | Micro-structure & Order Flow | 主動買成交量減去主動賣成交量。 |
| `trade_volume_delta_ratio_1m` | Micro-structure & Order Flow | `(buy_trade_volume_1m - sell_trade_volume_1m) / total_trade_volume_1m`，標準化買賣成交壓力。 |
| `taker_buy_ratio_1m` | Micro-structure & Order Flow | 1 分鐘內 taker buy volume / total volume。 |
| `best_bid_price` | Micro-structure & Order Flow | 訂單簿第一檔買價。 |
| `best_ask_price` | Micro-structure & Order Flow | 訂單簿第一檔賣價。 |
| `mid_price` | Micro-structure & Order Flow | `(best_bid_price + best_ask_price) / 2`。 |
| `spread` | Micro-structure & Order Flow | `best_ask_price - best_bid_price`。 |
| `spread_bps` | Micro-structure & Order Flow | `spread / mid_price * 10000`，以 bps 表示的買賣價差。 |
| `bid_depth_l1` | Micro-structure & Order Flow | 訂單簿第 1 檔 bid 掛單數量。 |
| `ask_depth_l1` | Micro-structure & Order Flow | 訂單簿第 1 檔 ask 掛單數量。 |
| `bid_depth_l5` | Micro-structure & Order Flow | 訂單簿前 5 檔 bid 掛單數量總和。 |
| `ask_depth_l5` | Micro-structure & Order Flow | 訂單簿前 5 檔 ask 掛單數量總和。 |
| `bid_depth_l10` | Micro-structure & Order Flow | 訂單簿前 10 檔 bid 掛單數量總和。 |
| `ask_depth_l10` | Micro-structure & Order Flow | 訂單簿前 10 檔 ask 掛單數量總和。 |
| `orderbook_imbalance_l1` | Micro-structure & Order Flow | `(bid_depth_l1 - ask_depth_l1) / (bid_depth_l1 + ask_depth_l1)`。 |
| `orderbook_imbalance_l5` | Micro-structure & Order Flow | `(bid_depth_l5 - ask_depth_l5) / (bid_depth_l5 + ask_depth_l5)`。 |
| `orderbook_imbalance_l10` | Micro-structure & Order Flow | `(bid_depth_l10 - ask_depth_l10) / (bid_depth_l10 + ask_depth_l10)`。 |
| `micro_price` | Micro-structure & Order Flow | `(best_ask_price * bid_depth_l1 + best_bid_price * ask_depth_l1) / (bid_depth_l1 + ask_depth_l1)`。 |
| `micro_price_diff` | Micro-structure & Order Flow | `micro_price - mid_price`，衡量微觀價格偏移。 |
| `ofi_l1_1m` | Micro-structure & Order Flow | 1 分鐘內 L1 order flow imbalance，用來衡量最上層掛單變動造成的買賣壓力。 |
| `bid_qty_change_l1` | Micro-structure & Order Flow | L1 bid 掛單數量變化。 |
| `ask_qty_change_l1` | Micro-structure & Order Flow | L1 ask 掛單數量變化。 |
| `bid_price_move_flag` | Micro-structure & Order Flow | bid price 是否上移的布林或方向標記。 |
| `ask_price_move_flag` | Micro-structure & Order Flow | ask price 是否下移的布林或方向標記。 |
| `cancel_pressure_bid` | Micro-structure & Order Flow | bid depth 減少但沒有對應成交時，用來估計買方撤單壓力。 |
| `cancel_pressure_ask` | Micro-structure & Order Flow | ask depth 減少但沒有對應成交時，用來估計賣方撤單壓力。 |

---

## Group 2：條件過濾與狀態因子  
**Regime & Condition Filters**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `return_15m` | Regime & Condition Filters | 15 分鐘 close-to-close 報酬率。 |
| `return_1h` | Regime & Condition Filters | 1 小時 close-to-close 報酬率，用來判斷較高週期方向。 |
| `ma_slope_15m_20` | Regime & Condition Filters | 15m K 線上的 MA20 斜率。 |
| `ma_slope_1h_20` | Regime & Condition Filters | 1h K 線上的 MA20 斜率。 |
| `adx_15m` | Regime & Condition Filters | 15m ADX，衡量趨勢強度。 |
| `chop_index_15m` | Regime & Condition Filters | 15m Choppiness Index，用來判斷盤整或趨勢狀態。 |
| `range_position_15m` | Regime & Condition Filters | 當前價格在 15m rolling high/low 區間中的位置。 |
| `hh_hl_structure_15m` | Regime & Condition Filters | 15m 是否形成 higher high / higher low 的多頭結構。 |
| `ll_lh_structure_15m` | Regime & Condition Filters | 15m 是否形成 lower low / lower high 的空頭結構。 |
| `volume_zscore_15m_20` | Regime & Condition Filters | 15m 成交量相對過去 20 根的 z-score。 |
| `volatility_zscore_15m_20` | Regime & Condition Filters | 15m 波動率相對過去 20 根的 z-score。 |
| `spread_zscore_1m_20` | Regime & Condition Filters | 1m spread 相對過去 20 根的 z-score。 |
| `liquidity_score_1m` | Regime & Condition Filters | 以深度與 spread 組合出的流動性分數，常見形式為 depth / spread。 |
| `session_asia_flag` | Regime & Condition Filters | 是否位於亞洲交易時段。 |
| `session_london_flag` | Regime & Condition Filters | 是否位於倫敦交易時段。 |
| `session_us_flag` | Regime & Condition Filters | 是否位於美國交易時段。 |
| `weekend_flag` | Regime & Condition Filters | 是否為週末交易時段。 |

---

## Group 3：量能與流動性因子  
**Volume & Liquidity Factors**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `volume_1m` | Volume & Liquidity | 1 分鐘成交量。 |
| `volume_15m` | Volume & Liquidity | 15 分鐘成交量。 |
| `quote_volume_1m` | Volume & Liquidity | 1 分鐘以 USDT 計價的成交額。 |
| `volume_ma_20` | Volume & Liquidity | 成交量過去 20 根移動平均。 |
| `volume_zscore_20` | Volume & Liquidity | 當前成交量相對過去 20 根成交量分布的 z-score。 |
| `volume_ratio_20` | Volume & Liquidity | `volume / volume_ma_20`，衡量放量倍數。 |
| `volume_change_1m` | Volume & Liquidity | 當前成交量相對前一根的變化率。 |
| `buy_volume_zscore_20` | Volume & Liquidity | 主動買成交量相對過去 20 根的 z-score。 |
| `sell_volume_zscore_20` | Volume & Liquidity | 主動賣成交量相對過去 20 根的 z-score。 |
| `spread_bps_1m` | Volume & Liquidity | 1m 內平均 spread bps，衡量交易成本。 |
| `depth_l1_usdt` | Volume & Liquidity | L1 深度換算成 USDT 金額。 |
| `depth_l5_usdt` | Volume & Liquidity | L5 深度換算成 USDT 金額。 |
| `depth_l10_usdt` | Volume & Liquidity | L10 深度換算成 USDT 金額。 |
| `depth_imbalance_l5` | Volume & Liquidity | L5 bid/ask 深度失衡，通常為 `(bid_depth_l5 - ask_depth_l5) / (bid_depth_l5 + ask_depth_l5)`。 |
| `amihud_illiquidity_1m` | Volume & Liquidity | `abs(return_1m) / volume_1m` 或 `abs(return_1m) / quote_volume_1m`，衡量價格衝擊程度。 |
| `price_impact_buy_10k` | Volume & Liquidity | 使用 order book 模擬買入 10,000 USDT 所造成的價格衝擊。 |
| `price_impact_sell_10k` | Volume & Liquidity | 使用 order book 模擬賣出 10,000 USDT 所造成的價格衝擊。 |
| `liquidity_gap_up` | Volume & Liquidity | 上方 order book 掛單稀薄程度，用來估計向上突破空間。 |
| `liquidity_gap_down` | Volume & Liquidity | 下方 order book 掛單稀薄程度，用來估計向下跌破空間。 |

---

## Group 4：動能與趨勢因子  
**Momentum & Trend Factors**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `return_1m` | Momentum & Trend | 1 分鐘 close-to-close 報酬率。 |
| `return_3m` | Momentum & Trend | 3 分鐘 close-to-close 報酬率。 |
| `return_5m` | Momentum & Trend | 5 分鐘 close-to-close 報酬率。 |
| `return_10m` | Momentum & Trend | 10 分鐘 close-to-close 報酬率。 |
| `return_15m` | Momentum & Trend | 15 分鐘 close-to-close 報酬率。 |
| `log_return_1m` | Momentum & Trend | `log(close_t / close_{t-1})`。 |
| `cumulative_return_5m` | Momentum & Trend | 過去 5 分鐘累積報酬率。 |
| `normalized_return_5m` | Momentum & Trend | 5 分鐘報酬率除以對應期間波動率，衡量風險調整後動能。 |
| `ma_5_1m` | Momentum & Trend | 1m K 線上的 5 期移動平均。 |
| `ma_20_1m` | Momentum & Trend | 1m K 線上的 20 期移動平均。 |
| `ma_60_1m` | Momentum & Trend | 1m K 線上的 60 期移動平均。 |
| `price_ma_gap_20` | Momentum & Trend | `close / ma_20 - 1`，衡量價格相對 MA20 的偏離。 |
| `ma_slope_20` | Momentum & Trend | MA20 的斜率。 |
| `ma_slope_60` | Momentum & Trend | MA60 的斜率。 |
| `ema_cross_5_20` | Momentum & Trend | `EMA5 - EMA20`，衡量短均線與中期均線差距。 |
| `trend_strength_ma` | Momentum & Trend | `abs(ma_slope) / volatility`，衡量波動調整後的趨勢強度。 |
| `breakout_high_20` | Momentum & Trend | close 是否突破過去 20 根 high。 |
| `breakout_low_20` | Momentum & Trend | close 是否跌破過去 20 根 low。 |
| `distance_to_high_20` | Momentum & Trend | `close / rolling_high_20 - 1`，衡量距離近期高點的距離。 |
| `distance_to_low_20` | Momentum & Trend | `close / rolling_low_20 - 1`，衡量距離近期低點的距離。 |
| `donchian_position_20` | Momentum & Trend | close 在 Donchian 20 high/low 區間中的位置。 |
| `breakout_volume_confirm` | Momentum & Trend | 突破時的成交量確認因子，例如突破條件乘上 volume z-score。 |

---

## Group 5：均值回歸與極端值因子  
**Mean-Reversion & Extreme Factors**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `zscore_price_20` | Mean-Reversion & Extreme | close 相對 MA20 與 rolling std 的 z-score。 |
| `zscore_return_20` | Mean-Reversion & Extreme | 當前報酬率相對過去 20 根報酬率分布的 z-score。 |
| `price_ma_gap_20` | Mean-Reversion & Extreme | `close / ma_20 - 1`，衡量價格偏離均線程度。 |
| `bollinger_position_20` | Mean-Reversion & Extreme | 價格在 Bollinger Band 上下軌之間的位置。 |
| `bollinger_width_20` | Mean-Reversion & Extreme | Bollinger Band 上下軌寬度，衡量波動擴張或壓縮。 |
| `rsi_14` | Mean-Reversion & Extreme | 14 期 RSI，用來衡量短線超買或超賣。 |
| `stoch_k` | Mean-Reversion & Extreme | Stochastic %K，用來衡量價格在近期區間中的相對位置。 |
| `distance_to_vwap` | Mean-Reversion & Extreme | `close / vwap - 1`，衡量價格相對成交均價的偏離。 |
| `upper_wick_ratio` | Mean-Reversion & Extreme | 上影線長度 / K 線總 range。 |
| `lower_wick_ratio` | Mean-Reversion & Extreme | 下影線長度 / K 線總 range。 |
| `body_ratio` | Mean-Reversion & Extreme | K 線實體長度 / K 線總 range。 |
| `range_zscore_20` | Mean-Reversion & Extreme | 當前 high-low range 相對過去 20 根的 z-score。 |
| `close_position_in_bar` | Mean-Reversion & Extreme | `(close - low) / (high - low)`，衡量收盤價在單根 K 線中的位置。 |
| `reversal_bar_up` | Mean-Reversion & Extreme | 長下影且收盤偏高的多方反轉 K 線標記。 |
| `reversal_bar_down` | Mean-Reversion & Extreme | 長上影且收盤偏低的空方反轉 K 線標記。 |

---

## Group 6：波動率與壓縮因子  
**Volatility & Compression Factors**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `realized_vol_5m` | Volatility & Compression | 過去 5 分鐘 realized volatility。 |
| `realized_vol_15m` | Volatility & Compression | 過去 15 分鐘 realized volatility。 |
| `realized_vol_1h` | Volatility & Compression | 過去 1 小時 realized volatility。 |
| `atr_14_1m` | Volatility & Compression | 1m K 線上的 ATR14。 |
| `atr_14_15m` | Volatility & Compression | 15m K 線上的 ATR14。 |
| `range_mean_20` | Volatility & Compression | high-low range 過去 20 根平均值。 |
| `range_zscore_20` | Volatility & Compression | 當前 high-low range 相對過去 20 根的 z-score。 |
| `bb_width_20` | Volatility & Compression | Bollinger Band 20 期寬度。 |
| `bb_width_percentile_100` | Volatility & Compression | BB width 在過去 100 根中的百分位。 |
| `atr_percentile_100` | Volatility & Compression | ATR 在過去 100 根中的百分位。 |
| `vol_compression_ratio` | Volatility & Compression | 短期波動率 / 長期波動率，用來判斷波動壓縮。 |
| `range_compression_count` | Volatility & Compression | 連續小 range K 線的根數。 |
| `vol_expansion_flag` | Volatility & Compression | 波動率突然放大的標記。 |
| `true_range_spike` | Volatility & Compression | True Range z-score 超過指定門檻的異常波動標記。 |

---

## Group 7：加密貨幣衍生性與替代因子  
**Crypto Derivatives & Alternative Factors**

| 因子名稱 | Group | 定義 |
|---|---|---|
| `funding_rate` | Crypto Derivatives & Alternative | 永續合約當前資金費率。 |
| `funding_rate_zscore_30d` | Crypto Derivatives & Alternative | funding rate 相對過去 30 天分布的 z-score。 |
| `funding_rate_change` | Crypto Derivatives & Alternative | funding rate 的變化量。 |
| `time_to_funding` | Crypto Derivatives & Alternative | 距離下一次 funding 結算的時間。 |
| `funding_positive_flag` | Crypto Derivatives & Alternative | funding rate 是否大於 0。 |
| `funding_extreme_flag` | Crypto Derivatives & Alternative | funding rate 是否位於極端百分位，例如高於 95%。 |
| `open_interest` | Crypto Derivatives & Alternative | 永續合約未平倉量。 |
| `oi_change_5m` | Crypto Derivatives & Alternative | 5 分鐘 open interest 變化量或變化率。 |
| `oi_change_15m` | Crypto Derivatives & Alternative | 15 分鐘 open interest 變化量或變化率。 |
| `oi_zscore_30d` | Crypto Derivatives & Alternative | open interest 相對過去 30 天分布的 z-score。 |
| `price_up_oi_up` | Crypto Derivatives & Alternative | 價格上漲且 OI 上升，通常代表新多或新資金進場。 |
| `price_up_oi_down` | Crypto Derivatives & Alternative | 價格上漲但 OI 下降，通常可能代表空單回補。 |
| `price_down_oi_up` | Crypto Derivatives & Alternative | 價格下跌且 OI 上升，通常代表新空進場。 |
| `price_down_oi_down` | Crypto Derivatives & Alternative | 價格下跌但 OI 下降，通常代表多單平倉或去槓桿。 |
| `global_long_short_ratio` | Crypto Derivatives & Alternative | 全市場帳戶多空比。 |
| `top_trader_long_short_ratio` | Crypto Derivatives & Alternative | 大戶或頂級交易者多空比。 |
| `long_short_ratio_change` | Crypto Derivatives & Alternative | 多空比變化量。 |
| `long_short_ratio_zscore` | Crypto Derivatives & Alternative | 多空比相對歷史分布的 z-score。 |
| `long_liquidation_volume_1m` | Crypto Derivatives & Alternative | 1 分鐘內多單爆倉量。 |
| `short_liquidation_volume_1m` | Crypto Derivatives & Alternative | 1 分鐘內空單爆倉量。 |
| `liq_imbalance_1m` | Crypto Derivatives & Alternative | `short_liquidation_volume_1m - long_liquidation_volume_1m`，衡量清算方向失衡。 |
| `liq_spike_zscore` | Crypto Derivatives & Alternative | 清算量相對歷史分布的 z-score。 |
| `post_liq_reversal_flag` | Crypto Derivatives & Alternative | 大量清算後出現反轉條件的標記。 |

---

# 優先研究核心因子池

以下是建議優先進行 IC / Rank IC / 分位數收益 / regime stability 分析的核心因子。

| 因子名稱 | Group | 定義 |
|---|---|---|
| `return_1m` | Momentum & Trend | 1 分鐘 close-to-close 報酬率。 |
| `return_3m` | Momentum & Trend | 3 分鐘 close-to-close 報酬率。 |
| `return_5m` | Momentum & Trend | 5 分鐘 close-to-close 報酬率。 |
| `return_10m` | Momentum & Trend | 10 分鐘 close-to-close 報酬率。 |
| `return_15m` | Momentum & Trend / Regime | 15 分鐘 close-to-close 報酬率，可作為動能或 regime 方向因子。 |
| `log_return_1m` | Momentum & Trend | 1 分鐘對數報酬率。 |
| `high_low_range_1m` | Volatility & Compression | 1 分鐘 high-low range。 |
| `close_position_in_bar` | Mean-Reversion & Extreme | 收盤價在單根 K 線 range 內的位置。 |
| `upper_wick_ratio` | Mean-Reversion & Extreme | 上影線長度 / K 線總 range。 |
| `lower_wick_ratio` | Mean-Reversion & Extreme | 下影線長度 / K 線總 range。 |
| `volume_1m` | Volume & Liquidity | 1 分鐘成交量。 |
| `quote_volume_1m` | Volume & Liquidity | 1 分鐘 USDT 成交額。 |
| `volume_zscore_20` | Volume & Liquidity | 成交量 z-score。 |
| `taker_buy_volume_1m` | Micro-structure & Order Flow | 1 分鐘 taker buy volume。 |
| `taker_sell_volume_1m` | Micro-structure & Order Flow | 1 分鐘 taker sell volume。 |
| `taker_buy_ratio_1m` | Micro-structure & Order Flow | taker buy volume / total volume。 |
| `trade_volume_delta_1m` | Micro-structure & Order Flow | 主動買量減主動賣量。 |
| `trade_volume_delta_ratio_1m` | Micro-structure & Order Flow | 標準化主動買賣量差。 |
| `trade_count_1m` | Micro-structure & Order Flow | 1 分鐘成交筆數。 |
| `avg_trade_size_1m` | Micro-structure & Order Flow | 平均單筆成交量。 |
| `spread_bps` | Micro-structure & Order Flow | spread / mid price * 10000。 |
| `bid_depth_l5` | Micro-structure & Order Flow | 前 5 檔 bid 深度。 |
| `ask_depth_l5` | Micro-structure & Order Flow | 前 5 檔 ask 深度。 |
| `orderbook_imbalance_l5` | Micro-structure & Order Flow | L5 order book imbalance。 |
| `orderbook_imbalance_l10` | Micro-structure & Order Flow | L10 order book imbalance。 |
| `micro_price_diff` | Micro-structure & Order Flow | micro price - mid price。 |
| `price_impact_buy_10k` | Volume & Liquidity | 模擬買入 10,000 USDT 的價格衝擊。 |
| `price_impact_sell_10k` | Volume & Liquidity | 模擬賣出 10,000 USDT 的價格衝擊。 |
| `realized_vol_15m` | Volatility & Compression | 15 分鐘 realized volatility。 |
| `atr_14_15m` | Volatility & Compression | 15m ATR14。 |
| `bb_width_20_15m` | Volatility & Compression | 15m Bollinger Band 20 期寬度。 |
| `ma_slope_20_15m` | Regime & Condition Filters | 15m MA20 斜率。 |
| `adx_15m` | Regime & Condition Filters | 15m ADX。 |
| `range_position_15m` | Regime & Condition Filters | 價格在 15m rolling range 中的位置。 |
| `funding_rate` | Crypto Derivatives & Alternative | 永續合約資金費率。 |
| `funding_rate_zscore` | Crypto Derivatives & Alternative | funding rate z-score。 |
| `open_interest` | Crypto Derivatives & Alternative | 永續合約未平倉量。 |
| `oi_change_15m` | Crypto Derivatives & Alternative | 15 分鐘 OI 變化。 |
| `long_short_ratio` | Crypto Derivatives & Alternative | 多空帳戶比或持倉比。 |
| `long_liquidation_volume_1m` | Crypto Derivatives & Alternative | 1 分鐘多單爆倉量。 |
| `short_liquidation_volume_1m` | Crypto Derivatives & Alternative | 1 分鐘空單爆倉量。 |

---

# 使用建議

- `1m` 因子主要用於 entry timing、order flow、短線動能與流動性判斷。
- `15m` 因子主要用於 regime filter、趨勢方向、波動環境與是否允許交易。
- 每個因子建議至少測試 `future_return_1m`、`future_return_3m`、`future_return_5m`、`future_return_10m`。
- 不要只看單一 IC，應同時檢查 Rank IC、分位數收益、不同年份穩定性、不同波動 regime 下的穩定性。
