# Research Factors Reference

本文依據 `research/factors.py` 中已註冊的 `FactorBase` class 整理。每個因子都回傳與 K 線對齊的數值序列，研究框架再用 forward return、IC、分位數報酬與 out-of-sample 表現驗證 alpha 是否存在。

## 解讀方式

- `Side` 表示研究時預設的方向：`Long` 為數值越高越偏多，`Short` 為數值越高越偏空，`Long/Short` 代表方向需由樣本 IC 或分位數結果判斷。
- `Tick` 表示是否需要逐筆成交資料。大多數因子只需要 K 線欄位，`lower_wick_delta_eff` 需要 tick map。
- `Alpha 相關性` 是交易假說，不是保證。加密貨幣合約的槓桿、資金費率、強平與 24/7 交易結構會讓這些訊號比現貨或傳統市場更容易出現 regime dependence。

## Micro-structure & Order Flow Factors

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `LowerWickDeltaEfficiencyFactor` | `lower_wick_delta_eff` | Long | Yes | 只看下影線區域的 tick，計算 `(2 * buy_qty - total_qty) / total_qty`。 | 下探區若主動買量占優，代表低位吸收賣壓，對永續合約的短線反彈有正向 alpha 假說。 |
| `DeltaEfficiencyLongFactor` | `delta_eff_long` | Long | No | 用 K 線 taker buy volume 估計主動買賣不平衡：`(2 * taker_buy_volume - volume) / volume`。 | 主動買盤效率越高，短線 order-flow continuation 機率越高；在高槓桿市場容易推動追價與止損觸發。 |
| `DeltaEfficiencyShortFactor` | `delta_eff_short` | Short | No | `delta_eff_long` 的反向值。 | 主動賣盤效率越高，代表賣方吃單壓力強，對短線下跌或多頭去槓桿有 alpha。 |
| `BuyTradeVolumeFactor` | `buy_trade_volume_1m` | Long | No | 直接取 `taker_buy_volume`。 | 主動買成交量上升常代表多方急迫性，但需搭配總量、價格位置或波動過濾避免只捕捉雜訊。 |
| `SellTradeVolumeFactor` | `sell_trade_volume_1m` | Short | No | `volume - taker_buy_volume` 估計主動賣成交量。 | 主動賣量擴大可對應短線壓力與多頭強平風險，尤其在破位或高波動 regime 更有意義。 |
| `TradeVolumeDeltaFactor` | `trade_volume_delta_1m` | Long/Short | No | `2 * taker_buy_volume - volume`，保留原始量級。 | 正值代表買方吃單占優，負值代表賣方吃單占優；適合與價格變化比較，辨識推動是否有效。 |
| `TradeVolumeDeltaRatioFactor` | `trade_volume_delta_ratio_1m` | Long/Short | No | trade volume delta 除以總成交量。 | 標準化後可跨不同成交量時段比較 order-flow 方向，通常比原始 delta 更穩定。 |
| `TakerBuyRatioFactor` | `taker_buy_ratio_1m` | Long/Short | No | `taker_buy_volume / volume`。 | 高於 0.5 偏主動買，低於 0.5 偏主動賣；在合約市場可衡量短線攻防，但極端值也可能是 exhaustion。 |

## Crypto Derivatives & Alternative Factors

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `FundingRateFactor` | `funding_rate` | Long/Short | No | 對齊 funding rate 快取欄位 `last_funding_rate`。 | 正 funding 代表多頭付費，常反映多方擁擠；可做趨勢確認，也可在極端時做反向擁擠交易。 |
| `FundingRateChangeFactor` | `funding_rate_change` | Long/Short | No | funding rate 的一階差分。 | funding 快速升高代表槓桿多頭需求升溫；快速轉弱可能暗示多頭退潮或空頭增加。 |
| `FundingRateZscore30dFactor` | `funding_rate_zscore_30d` | Long/Short | No | funding rate 相對 30 日 rolling mean/std 的 z-score。 | 衡量資金費率是否處於歷史極端；加密合約中常用於 crowded long/short 與反轉風險判斷。 |
| `OpenInterestFactor` | `open_interest` | Long/Short | No | 對齊 metrics 快取欄位 `sum_open_interest`。 | OI 上升代表槓桿倉位增加；需與價格方向合用，單獨看只能說明參與度與潛在強平燃料。 |
| `OpenInterestDelta5mFactor` | `open_interest_delta_5m` | Long/Short | No | OI 的 5 分鐘差分。 | 短時間 OI 增加代表新倉進場；若伴隨價格突破，常強化 momentum alpha。 |
| `OpenInterestDelta15mFactor` | `open_interest_delta_15m` | Long/Short | No | OI 的 15 分鐘差分。 | 較 5m 更平滑，適合辨識一段行情中的槓桿累積或倉位撤離。 |
| `OpenInterestDeltaRatio15mFactor` | `open_interest_delta_ratio_15m` | Long/Short | No | `(oi - oi_15m_ago) / oi_15m_ago`。 | 用比例衡量 OI 擴張速度，能跨價格水位與不同市場階段比較槓桿流入強度。 |
| `OpenInterestZscore30dFactor` | `open_interest_zscore_30d` | Long/Short | No | OI 相對 30 日 rolling mean/std 的 z-score。 | OI 歷史高位代表槓桿擁擠，趨勢延續與清算反轉都可能增強，方向需由 IC 驗證。 |
| `LongLiquidationVolume1mFactor` | `long_liquidation_volume_1m` | Short | No | 取 long liquidation notional。 | 多頭強平量大表示下跌壓力與去槓桿正在發生，短線偏空；極端後也可能出現反彈。 |
| `ShortLiquidationVolume1mFactor` | `short_liquidation_volume_1m` | Long | No | 取 short liquidation notional。 | 空頭強平量大表示上漲擠壓，短線偏多；若出現在過熱區，也可能是趨勢尾端。 |
| `LiquidationImbalance1mFactor` | `liq_imbalance_1m` | Long/Short | No | `short_liq_notional - long_liq_notional`。 | 正值代表空頭強平占優，偏多；負值代表多頭強平占優，偏空。 |

## Regime & Condition Filters

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `AdxFactor` | `adx_15m` | Long/Short | No | 以 Wilder smoothing 計算 14 期 ADX。 | ADX 衡量趨勢強度而非方向；在加密合約中可用來決定採用 breakout/momentum 或 mean-reversion。 |
| `ChoppinessFactor` | `chop_index_15m` | Long/Short | No | `100 * log10(sum(TR) / range) / log10(n)`。 | 高 choppiness 代表盤整，趨勢訊號容易失效；低值代表 directional move 更乾淨。 |
| `RangePositionFactor` | `range_position_15m` | Long/Short | No | close 在 20 期 high-low 區間的位置。 | 接近 1 代表靠近區間上緣，接近 0 代表靠近下緣；可做突破確認或區間反轉條件。 |
| `HhHlStructureFactor` | `hh_hl_structure_15m` | Long | No | 當前 high 與 low 都高於前一根時輸出 1。 | higher high + higher low 是微型上升結構，對多頭延續有正向假說。 |
| `LlLhStructureFactor` | `ll_lh_structure_15m` | Short | No | 當前 low 與 high 都低於前一根時輸出 1。 | lower low + lower high 是微型下降結構，對空頭延續有正向假說。 |
| `VolumeZscoreRegimeFactor` | `volume_zscore_15m_20` | Long/Short | No | 成交量 20 期 rolling z-score。 | 高成交量 regime 中訊號更可能被真實資金推動；低量環境容易出現假突破。 |
| `VolatilityZscoreRegimeFactor` | `volatility_zscore_15m_20` | Long/Short | No | 先算 5 期 log return 波動，再對其做 20 期 z-score。 | 波動 regime 影響止損、滑價與 alpha 半衰期；高波動常放大趨勢與清算效應。 |
| `SessionAsiaFlagFactor` | `session_asia_flag` | Long/Short | No | UTC 00:00-08:00 輸出 1。 | 亞洲時段流動性與參與者結構不同，可捕捉時段性的波動/成交量/方向偏差。 |
| `SessionLondonFlagFactor` | `session_london_flag` | Long/Short | No | UTC 07:00-16:00 輸出 1。 | 倫敦時段常帶來歐洲流動性，可能改變突破成功率與均值回歸速度。 |
| `SessionUsFlagFactor` | `session_us_flag` | Long/Short | No | UTC 13:00-22:00 輸出 1。 | 美國時段與宏觀事件、傳統市場重疊，合約市場常有較高波動與 directional flow。 |
| `WeekendFlagFactor` | `weekend_flag` | Long/Short | No | UTC 週六、週日輸出 1。 | 週末流動性較薄，假突破、急跌急拉與槓桿清算特徵可能與工作日不同。 |

## Volume & Liquidity Factors

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `VolumeFactor` | `volume_1m` | Long/Short | No | 直接取 K 線成交量。 | 成交量代表參與度與衝擊成本；方向通常需和價格、delta 或突破條件結合。 |
| `VolumeMa20Factor` | `volume_ma_20` | Long/Short | No | 成交量 20 期均線。 | 描述背景流動性；高均量市場訊號較可交易，低均量環境滑價與假訊號較高。 |
| `VolumeZscore20Factor` | `volume_zscore_20` | Long/Short | No | 成交量相對 20 期均值與標準差的 z-score。 | 異常放量常代表資訊或強平事件進入市場，能提高 breakout 或 reversal 訊號權重。 |
| `VolumeRatio20Factor` | `volume_ratio_20` | Long/Short | No | `volume / volume_ma_20`。 | 用倍數衡量放量程度，比 z-score 更直觀；高值常與有效突破、出貨或 capitulation 有關。 |
| `VolumeChangeFactor` | `volume_change_1m` | Long/Short | No | 成交量的一階變化率。 | 突然增量可提前反映流動性衝擊；方向需要價格與 taker flow 判斷。 |
| `BuyVolumeZscore20Factor` | `buy_volume_zscore_20` | Long | No | taker buy volume 的 20 期 z-score。 | 主動買量異常放大可對應多方攻擊與空頭止損，對短線多頭 alpha 較直接。 |
| `SellVolumeZscore20Factor` | `sell_volume_zscore_20` | Short | No | taker sell volume 的 20 期 z-score。 | 主動賣量異常放大可對應空方攻擊與多頭去槓桿，對短線空頭 alpha 較直接。 |
| `AmihudIlliquidityFactor` | `amihud_illiquidity_1m` | Long/Short | No | `abs(return_1m) / volume`。 | 衡量單位成交量造成的價格衝擊；高值代表流動性差，alpha 可來自風險溢酬或反向修復，但交易成本也高。 |

## Momentum & Trend Factors

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `Return1mFactor` | `return_1m` | Long/Short | No | 1 期 close-to-close 報酬。 | 捕捉最短週期 momentum 或 micro reversal；方向高度依賴交易成本與 regime。 |
| `Return3mFactor` | `return_3m` | Long/Short | No | 3 期 close-to-close 報酬。 | 平滑 1m 雜訊後觀察短線延續；合約中常受吃單與止損流推動。 |
| `Return5mFactor` | `return_5m` | Long/Short | No | 5 期 close-to-close 報酬。 | 常用於短線趨勢延續或過度反應測試，需以 IC 判斷順勢或反向。 |
| `Return10mFactor` | `return_10m` | Long/Short | No | 10 期 close-to-close 報酬。 | 反映較完整的小波段，對突破延續與清算後修復都可能敏感。 |
| `Return15mFactor` | `return_15m` | Long/Short | No | 15 期 close-to-close 報酬。 | 更偏 regime/波段動能，適合搭配 OI、funding 或波動壓縮確認。 |
| `LogReturn1mFactor` | `log_return_1m` | Long/Short | No | `log(close / prev_close)`。 | 對連續複利與統計建模更穩定，可與 realized volatility、z-score 類因子共同使用。 |
| `NormalizedReturn5mFactor` | `normalized_return_5m` | Long/Short | No | `return_5m / realized_vol_5m`。 | 將動能除以同期波動，衡量風險調整後的推動；可降低高波動雜訊。 |
| `Ma5Factor` | `ma_5_1m` | Long/Short | No | close 的 5 期 rolling mean。 | 短均線本身多作為狀態變數，與價格差、均線斜率或交叉組合才有方向性。 |
| `Ma20Factor` | `ma_20_1m` | Long/Short | No | close 的 20 期 rolling mean。 | 描述短線公平價與趨勢基準，可支援均值回歸與趨勢偏離判斷。 |
| `Ma60Factor` | `ma_60_1m` | Long/Short | No | close 的 60 期 rolling mean。 | 反映較慢的日內趨勢背景，常用於過濾短週期噪音。 |
| `PriceMaGap20Factor` | `price_ma_gap_20` | Long/Short | No | `(close - ma20) / ma20`。 | 正值代表價格高於短均線；小幅正值可順勢，極端正值可能過熱，方向需驗證。 |
| `MaSlope20Factor` | `ma_slope_20` | Long/Short | No | `ma20` 的一階變化率。 | 均線斜率代表短線趨勢方向與速度，常比單根 return 更穩定。 |
| `MaSlope60Factor` | `ma_slope_60` | Long/Short | No | `ma60` 的一階變化率。 | 較慢斜率可當 trend filter，降低逆大方向進場的噪音。 |
| `EmaCross5_20Factor` | `ema_cross_5_20` | Long/Short | No | `(ema5 - ema20) / close`。 | 快慢 EMA 差距衡量短線趨勢強度；加密合約中可跟隨快速資金流，但過熱時會失效。 |
| `TrendStrengthMaFactor` | `trend_strength_ma` | Long/Short | No | `abs(ma20_slope) / realized_vol_20`。 | 趨勢速度相對波動越高，代表 trend quality 越好；適合選擇順勢策略啟用時機。 |
| `BreakoutHigh20Factor` | `breakout_high_20` | Long | No | close 突破前 20 期最高價時輸出 1。 | 向上突破常觸發空頭止損與追多，對永續合約多頭 continuation 有直接 alpha 假說。 |
| `BreakoutLow20Factor` | `breakout_low_20` | Short | No | close 跌破前 20 期最低價時輸出 1。 | 向下破位常觸發多頭止損與強平，對空頭 continuation 有直接 alpha 假說。 |
| `DistanceToHigh20Factor` | `distance_to_high_20` | Long/Short | No | `(close - high20) / high20`。 | 衡量距離近期高點的相對位置；接近高點可作突破準備，也可能形成壓力反轉。 |
| `DistanceToLow20Factor` | `distance_to_low_20` | Long/Short | No | `(close - low20) / low20`。 | 衡量距離近期低點的相對位置；接近低點可作破位準備，也可能形成支撐反彈。 |
| `DonchianPosition20Factor` | `donchian_position_20` | Long/Short | No | `(close - low20) / (high20 - low20)`。 | close 在 Donchian 通道中的位置；高位偏趨勢強，低位偏弱勢或反彈候選。 |
| `BreakoutVolumeConfirmFactor` | `breakout_volume_confirm` | Long/Short | No | 20 期高低突破方向乘以 20 期 volume z-score。 | 放量突破通常比低量突破可靠；正值偏向上突破確認，負值偏向下突破確認。 |

## Mean-Reversion & Extreme Factors

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `LowerWickToBodyRatioFactor` | `lower_wick_to_body_ratio` | Long | No | 下影線長度除以實體長度。 | 長下影相對小實體代表低位被買回，對合約短線反彈有均值回歸 alpha 假說。 |
| `UpperWickToBodyRatioFactor` | `upper_wick_to_body_ratio` | Short | No | 上影線長度除以實體長度。 | 長上影相對小實體代表高位被賣回，對短線回落或多頭追價失敗有 alpha。 |
| `ZscorePrice20Factor` | `zscore_price_20` | Long/Short | No | close 相對 20 期 rolling mean/std 的 z-score。 | 價格偏離均值越大，可能是趨勢強度，也可能是短線過度反應；需用 IC 判斷順勢或反轉。 |
| `ZscoreReturn20Factor` | `zscore_return_20` | Long/Short | No | 1 期 return 相對 20 期 rolling mean/std 的 z-score。 | 異常單根報酬可捕捉衝擊後延續或回補，對清算行情特別敏感。 |
| `BollingerPosition20Factor` | `bollinger_position_20` | Long/Short | No | close 在 20 期 Bollinger 上下軌間的位置。 | 接近上軌可能代表強勢或過熱，接近下軌可能代表弱勢或超跌。 |
| `BollingerWidth20Factor` | `bollinger_width_20` | Long/Short | No | `4 * std20 / ma20`。 | Bollinger 寬度代表價格波動與壓縮/擴張狀態，可過濾 breakout 與 mean-reversion 策略。 |
| `Rsi14Factor` | `rsi_14` | Long/Short | No | 14 期 Wilder RSI。 | RSI 極端值在加密合約中可代表趨勢動能或過度擁擠，需區分 trending 與 choppy regime。 |
| `StochKFactor` | `stoch_k` | Long/Short | No | close 在 14 期 high-low 區間的位置，乘以 100。 | 高值代表收在區間上緣，低值代表收在下緣；可用於短線超買超賣或趨勢確認。 |
| `DistanceToVwapFactor` | `distance_to_vwap` | Long/Short | No | `(close - rolling_vwap20) / rolling_vwap20`。 | 偏離 VWAP 衡量當前價格相對成交成本；極端偏離可反轉，穩定正偏離可順勢。 |
| `UpperWickRatioFactor` | `upper_wick_ratio` | Short | No | 上影線長度除以整根 high-low range。 | 上影線占比高代表高位拒絕，對短線空頭或止盈回落有 alpha 假說。 |
| `LowerWickRatioFactor` | `lower_wick_ratio` | Long | No | 下影線長度除以整根 high-low range。 | 下影線占比高代表低位吸收，對短線多頭反彈有 alpha 假說。 |
| `BodyRatioFactor` | `body_ratio` | Long/Short | No | 實體長度除以整根 high-low range。 | 大實體代表單邊推動，小實體代表猶豫；方向需結合 close 位置與 order-flow。 |
| `RangeZscore20Factor` | `range_zscore_20` | Long/Short | No | high-low range 的 20 期 z-score。 | 異常大 range 常出現在清算、消息或突破；可作事件強度或反轉風險訊號。 |
| `ClosePositionInBarFactor` | `close_position_in_bar` | Long/Short | No | `(close - low) / (high - low)`。 | 收在高位代表買方收盤控制，收在低位代表賣方控制；適合與 wick 因子組合。 |
| `ReversalBarUpFactor` | `reversal_bar_up` | Long | No | range 大於 20 期均值、下影占比 >= 0.5、close position >= 0.6 時輸出下影占比。 | 放量/大波動下探後收回，是多頭反轉 bar；常對清算後反彈有效。 |
| `ReversalBarDownFactor` | `reversal_bar_down` | Short | No | range 大於 20 期均值、上影占比 >= 0.5、close position <= 0.4 時輸出上影占比。 | 上衝後收回，是空頭反轉 bar；常對追多失敗與上方流動性掃蕩後回落有效。 |

## Volatility & Compression Factors

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `RealizedVol5mFactor` | `realized_vol_5m` | Long/Short | No | 5 期 log return rolling std。 | 短波動上升會放大止損與滑價，也可能代表趨勢啟動或事件衝擊。 |
| `RealizedVol15mFactor` | `realized_vol_15m` | Long/Short | No | 15 期 log return rolling std。 | 中短線 realized vol 可衡量行情活躍度，協助調整持倉與訊號門檻。 |
| `RealizedVol1hFactor` | `realized_vol_1h` | Long/Short | No | 60 期 log return rolling std。 | 小時級波動 regime 對日內策略的風險預算與 alpha 穩定度很重要。 |
| `Atr14Factor` | `atr_14_1m` | Long/Short | No | 14 期 Wilder ATR。 | ATR 反映絕對價格波動，可用於止損、倉位規模與突破強度判斷。 |
| `RangeMean20Factor` | `range_mean_20` | Long/Short | No | high-low range 的 20 期均值。 | 描述近期平均振幅；振幅擴大時趨勢與反轉信號都會被放大。 |
| `BbWidth20Factor` | `bb_width_20` | Long/Short | No | `4 * std20 / ma20`。 | 與 Bollinger width 類似，用於辨識波動壓縮與擴張。 |
| `BbWidthPercentile100Factor` | `bb_width_percentile_100` | Long/Short | No | Bollinger width 在 100 期 trailing window 中的百分位。 | 低百分位代表 squeeze，後續可能爆發；高百分位代表波動已擴張，追價風險提高。 |
| `AtrPercentile100Factor` | `atr_percentile_100` | Long/Short | No | ATR 在 100 期 trailing window 中的百分位。 | 用相對位置判斷當前波動是否極端，避免固定 ATR 門檻不適應市場狀態。 |
| `VolCompressionRatioFactor` | `vol_compression_ratio` | Long/Short | No | `realized_vol_5m / realized_vol_20m`。 | 小於 1 表示短波動低於中期波動，可能處於壓縮；大於 1 表示短波動正在擴張。 |
| `RangeCompressionCountFactor` | `range_compression_count` | Long/Short | No | 連續計數 high-low range 低於 20 期平均的 K 線數。 | 連續窄幅震盪越久，後續突破或清算擴張的能量越可能累積。 |
| `VolExpansionFlagFactor` | `vol_expansion_flag` | Long/Short | No | ATR 的 20 期 z-score 大於 2 時輸出 1。 | 標記波動突然擴張；可提高 breakout 權重，也可避免在失控波動中做均值回歸。 |
| `TrueRangeSpikeFactor` | `true_range_spike` | Long/Short | No | true range 的 20 期 z-score。 | 單根真實波幅異常代表事件或流動性衝擊，常與短線 alpha 半衰期變短相關。 |
| `HighLowRange1mFactor` | `high_low_range_1m` | Long/Short | No | 單根 K 線 `high - low`。 | 最直接的振幅因子；可衡量即時衝擊強度，但方向需搭配 close 位置與成交量。 |

## Price Action & Chart Patterns

| Class | Factor | Side | Tick | 原理 | Alpha 相關性 |
|---|---|---:|---:|---|---|
| `SweepPinBarLongFactor` | `sweep_pin_bar_long` | Long | No | range 大於 20 期均值、下影占比 >= 0.7，且 low 掃破前 20 期低點時輸出下影占比。 | 掃低點後收回代表下方流動性被拿走但賣壓失敗，對多頭反彈或 stop-run reversal 有 alpha。 |
| `SweepPinBarShortFactor` | `sweep_pin_bar_short` | Short | No | range 大於 20 期均值、上影占比 >= 0.7，且 high 掃破前 20 期高點時輸出上影占比。 | 掃高點後收回代表上方流動性被拿走但買壓失敗，對空頭回落有 alpha。 |
| `MaTrendAlignmentCrossoverFactor` | `ma_trend_alignment_crossover` | Long | No | ma20 上穿 ma50，且 ma20 > ma50 > ma120 時輸出 `(ma20 - ma50) / ma50`。 | 均線多頭排列中出現短均線上穿，代表趨勢重新加速；適合捕捉較乾淨的多頭延續。 |

