# Mean-Reversion & Extreme Factors 詳細說明書

本文整理 `research/factors.py` 中歸類為 `GROUP_MEAN_REVERSION` 的所有因子，並補充 6 個 Stage 2 多頭 alpha 因子：

1. `sweep_low_reclaim`
2. `cvd_bullish_divergence`
3. `negative_delta_absorption`
4. `val_reclaim_long`
5. `poc_reversion_potential`
6. `return_shock_reclaim`

## 使用口徑

- 因子值與 K 線一一對齊，`factor[i]` 只使用 `klines[i]` 當下或之前的資料計算。
- 研究回測若假設下一根開盤成交，應對因子做 `entry_lag=1`，避免用收盤後才知道的資訊直接在同一根成交。
- `NaN` 代表 warm-up 不足、分母無效、tick 資料缺失，或該事件型因子當下不成立。
- Stage 2 六個 alpha 因子採用 `0.0` 表示「沒有訊號」，正數表示訊號強度。
- Long 因子值越大通常代表多頭反轉/回補機率越高；Short 因子值越大通常代表空頭反轉/回落機率越高；Long/Short 雙向因子需要靠 IC、分位數或 regime 條件決定實際方向。

## Mean-Reversion & Extreme 因子總覽

| Factor | Class | Side | Tick | 核心用途 |
|---|---|---:|---:|---|
| `lower_wick_to_body_ratio` | `LowerWickToBodyRatioFactor` | Long | No | 下影線相對實體的反轉強度 |
| `upper_wick_to_body_ratio` | `UpperWickToBodyRatioFactor` | Short | No | 上影線相對實體的反轉強度 |
| `lower_wick_delta_eff_mr` | `LowerWickDeltaEfficiencyMeanReversionFactor` | Long | Yes | 下影線區域的主動買盤吸收效率 |
| `zscore_price_20` | `ZscorePrice20Factor` | Long/Short | No | 價格相對 20 期均值的極端偏離 |
| `zscore_return_20` | `ZscoreReturn20Factor` | Long/Short | No | 單根報酬相對 20 期分布的衝擊程度 |
| `bollinger_position_20` | `BollingerPosition20Factor` | Long/Short | No | 收盤價在 20 期布林通道內的位置 |
| `bollinger_width_20` | `BollingerWidth20Factor` | Long/Short | No | 布林寬度，描述波動擴張/壓縮 |
| `rsi_14` | `Rsi14Factor` | Long/Short | No | Wilder RSI 動能過熱/過冷 |
| `stoch_k` | `StochKFactor` | Long/Short | No | 14 期高低區間內的收盤位置 |
| `distance_to_vwap` | `DistanceToVwapFactor` | Long/Short | No | 收盤價偏離 20 期 rolling VWAP 的距離 |
| `upper_wick_ratio` | `UpperWickRatioFactor` | Short | No | 上影線佔整根 range 的比例 |
| `lower_wick_ratio` | `LowerWickRatioFactor` | Long | No | 下影線佔整根 range 的比例 |
| `body_ratio` | `BodyRatioFactor` | Long/Short | No | 實體佔整根 range 的比例 |
| `range_zscore_20` | `RangeZscore20Factor` | Long/Short | No | high-low range 的極端程度 |
| `close_position_in_bar` | `ClosePositionInBarFactor` | Long/Short | No | 收盤價在單根 K 棒內的位置 |
| `reversal_bar_up` | `ReversalBarUpFactor` | Long | No | 大 range、長下影、收高的多頭反轉 bar |
| `reversal_bar_down` | `ReversalBarDownFactor` | Short | No | 大 range、長上影、收低的空頭反轉 bar |

## 共同欄位定義

以下公式使用相同符號：

- `O, H, L, C`: open、high、low、close
- `range = H - L`
- `body = abs(C - O)`
- `body_hi = max(O, C)`
- `body_lo = min(O, C)`
- `upper_wick = H - body_hi`
- `lower_wick = body_lo - L`
- `safe_divide(a, b)`: 分母為 0 或無效時輸出 `NaN`
- `rolling_mean(x, n)`, `rolling_std(x, n)`, `rolling_min(x, n)`, `rolling_max(x, n)`: n 期 rolling 統計
- `zscore(x, n) = (x - rolling_mean(x, n)) / rolling_std(x, n)`

## `lower_wick_to_body_ratio`

**公式**

```text
lower_wick_to_body_ratio = (body_lo - L) / abs(C - O)
```

**交易語意**

衡量下影線相對 K 棒實體的長度。值越大，代表價格曾向下探測，但收盤前被買回；在均值回歸語境中通常解讀為下方流動性被掃掉後，賣壓失敗或買盤吸收。

**使用方式**

- 適合作為 Long 反轉候選。
- 可搭配 `close_position_in_bar > 0.6` 過濾「只插針但沒有收回」的弱訊號。
- 可搭配 `range_zscore_20 > 0` 或 `volume_zscore_20 > 0` 要求事件足夠顯著。

**注意事項**

- 實體很小時，分母很小，數值會被放大；需要搭配 `body_ratio` 或最小 range 條件。
- 十字線、極小實體 bar 可能造成高分但不可交易。

## `upper_wick_to_body_ratio`

**公式**

```text
upper_wick_to_body_ratio = (H - body_hi) / abs(C - O)
```

**交易語意**

衡量上影線相對 K 棒實體的長度。值越大，代表價格曾向上衝高，但收盤前被賣回；在均值回歸語境中通常解讀為高位拒絕、追多失敗或上方流動性掃蕩後回落。

**使用方式**

- 適合作為 Short 反轉候選。
- 可搭配 `close_position_in_bar < 0.4` 確認收盤偏低。
- 若位於前高、VAH、布林上緣或 VWAP 上方，訊號語意更完整。

**注意事項**

- 小實體會放大比值，應用時建議加上最小 range 或最小成交量條件。
- 在強趨勢突破中，上影線也可能只是中途換手，需搭配 regime filter。

## `lower_wick_delta_eff_mr`

**公式**

此因子繼承 `LowerWickDeltaEfficiencyFactor`，需要 tick 資料：

```text
zone = ticks where tick_price <= body_lo
buy_qty = sum(qty where buyer_side tick)
total_qty = sum(qty in zone)
lower_wick_delta_eff_mr = (2 * buy_qty - total_qty) / total_qty
```

**交易語意**

只看下影線區域內的成交方向。如果價格插到下方，但下影線內主動買量佔優，代表下探區域可能出現承接或吸收，較符合多頭均值回歸。

**使用方式**

- 適合作為 `lower_wick_ratio`、`reversal_bar_up` 的 order-flow 確認。
- 值接近 1 表示下影線區域買方主導；接近 -1 表示賣方仍主導。
- 可用於剔除「形態像反轉，但下影線內仍是主動賣壓」的訊號。

**注意事項**

- `requires_ticks = True`，沒有 tick_map 時整列輸出 `NaN`。
- tick side 定義依現有 tick 資料格式，若交易所欄位語意改變，需要重新驗證。

## `zscore_price_20`

**公式**

```text
ma20 = rolling_mean(C, 20)
std20 = rolling_std(C, 20)
zscore_price_20 = (C - ma20) / std20
```

**交易語意**

衡量收盤價偏離 20 期均值的標準差倍數。負值代表低於均值，正值代表高於均值。它本身不保證反轉，可能同時代表「過度反應」或「趨勢正在展開」。

**使用方式**

- Long 反轉：常見條件是 `zscore_price_20 < -1.5` 或 `< -2.0` 後搭配 reclaim。
- Short 反轉：常見條件是 `zscore_price_20 > 1.5` 或 `> 2.0` 後搭配 rejection。
- 更適合作為背景狀態，而不是單獨進場訊號。

**注意事項**

- 趨勢行情中，z-score 可長時間維持極端。
- rolling window 為 20，前 19 根或標準差無效時會是 `NaN`。

## `zscore_return_20`

**公式**

```text
ret1 = C / C[-1] - 1
zscore_return_20 = zscore(ret1, 20)
```

**交易語意**

衡量單根 close-to-close 報酬是否異常。極端負值常見於急跌、清算或流動性真空；極端正值常見於急拉或空頭回補。

**使用方式**

- 負向衝擊後若同根或下一根出現收回，可作為多頭 exhaustion reclaim 條件。
- 正向衝擊後若上影線明顯，可作為空頭過熱條件。
- 適合與 `close_position_in_bar`、`lower_wick_ratio`、`upper_wick_ratio` 組合。

**注意事項**

- 單根衝擊可代表新資訊進場，不一定反轉。
- 對低流動性時間段敏感，建議搭配 session 或 volume filter。

## `bollinger_position_20`

**公式**

```text
ma20 = rolling_mean(C, 20)
std20 = rolling_std(C, 20)
upper = ma20 + 2 * std20
lower = ma20 - 2 * std20
bollinger_position_20 = (C - lower) / (upper - lower)
```

**交易語意**

描述收盤價位於 20 期布林通道中的相對位置。`0` 附近表示接近下緣，`1` 附近表示接近上緣，低於 0 或高於 1 代表突破布林帶。

**使用方式**

- `bollinger_position_20 < 0` 可視為下方極端，但需要 reclaim 或下影線確認。
- `bollinger_position_20 > 1` 可視為上方極端，但需要 rejection 確認。
- 可與 `bollinger_width_20` 判斷「窄帶突破」或「寬帶極端」。

**注意事項**

- 布林位置是相對指標，不知道趨勢方向。
- 在波動突然擴張時，通道更新落後，訊號可能延遲。

## `bollinger_width_20`

**公式**

```text
bollinger_width_20 = 4 * rolling_std(C, 20) / rolling_mean(C, 20)
```

**交易語意**

衡量 20 期布林上下緣的相對寬度，也就是價格波動狀態。高值代表波動擴張，低值代表壓縮。

**使用方式**

- 高寬度環境：反轉訊號若成立，通常目標與風險都較大。
- 低寬度環境：更適合等待突破或避免過早均值回歸。
- 可作為策略 regime filter，不建議單獨當方向因子。

**注意事項**

- `research/factors.py` 另有 `bb_width_20` 在 Volatility 組，公式相同但分組不同。
- 高寬度可能是崩跌或突破延續，不必然反轉。

## `rsi_14`

**公式**

```text
delta = C - C[-1]
avg_gain = WilderSmooth(max(delta, 0), 14)
avg_loss = WilderSmooth(max(-delta, 0), 14)
RS = avg_gain / avg_loss
rsi_14 = 100 - 100 / (1 + RS)
```

**交易語意**

衡量近期上漲與下跌幅度的相對強弱。低 RSI 常被視為超賣，高 RSI 常被視為超買。

**使用方式**

- Long 反轉：常見條件 `rsi_14 < 30` 後等待價格收回。
- Short 反轉：常見條件 `rsi_14 > 70` 後等待上影線或跌回。
- 更適合搭配趨勢/震盪 regime 使用。

**注意事項**

- 強趨勢中 RSI 可能長時間鈍化。
- 此實作使用 Wilder smoothing，與簡單 rolling RSI 結果不同。

## `stoch_k`

**公式**

```text
low14 = rolling_min(L, 14)
high14 = rolling_max(H, 14)
stoch_k = (C - low14) / (high14 - low14) * 100
```

**交易語意**

描述收盤價位於最近 14 期高低區間的位置。接近 0 表示收在近期低位附近，接近 100 表示收在近期高位附近。

**使用方式**

- 低位區配合 `lower_wick_ratio` 或 `sweep_low_reclaim` 可作多頭反轉候選。
- 高位區配合 `upper_wick_ratio` 或 `reversal_bar_down` 可作空頭反轉候選。
- 可作為區間市場中的超買/超賣濾網。

**注意事項**

- 單獨使用容易在趨勢行情中逆勢。
- 若 14 期 range 太小，分母無效會產生 `NaN`。

## `distance_to_vwap`

**公式**

```text
rolling_vwap20 = sum(typical_price * volume, 20) / sum(volume, 20)
typical_price = (H + L + C) / 3
distance_to_vwap = (C - rolling_vwap20) / rolling_vwap20
```

**交易語意**

衡量價格偏離近期成交量加權均價的百分比。低於 VWAP 可能代表折價或弱勢，高於 VWAP 可能代表溢價或強勢。

**使用方式**

- Long 反轉：價格低於 VWAP 後出現 reclaim 或吸收。
- Short 反轉：價格高於 VWAP 後出現 rejection。
- 可與 volume profile 的 POC/VAL 形成多層公平價參照。

**注意事項**

- 這裡是 rolling VWAP，不是 session VWAP。
- 方向需要由其他結構確認，不能只因偏離 VWAP 就逆勢。

## `upper_wick_ratio`

**公式**

```text
upper_wick_ratio = (H - body_hi) / (H - L)
```

**交易語意**

衡量上影線佔整根 K 棒 range 的比例。值越大，代表高位拒絕佔比越高。

**使用方式**

- 適合作為 Short 反轉形態的基礎特徵。
- 相比 `upper_wick_to_body_ratio`，較不容易因極小實體被過度放大。
- 可搭配 `range_zscore_20` 找「大波動上影線」。

**注意事項**

- 如果 close 仍然接近高位，上影線比例未必足以代表空頭。
- 建議搭配 `close_position_in_bar < 0.4`。

## `lower_wick_ratio`

**公式**

```text
lower_wick_ratio = (body_lo - L) / (H - L)
```

**交易語意**

衡量下影線佔整根 K 棒 range 的比例。值越大，代表低位被買回的佔比越高。

**使用方式**

- 適合作為 Long 反轉形態的基礎特徵。
- 相比 `lower_wick_to_body_ratio`，對小實體的放大較少。
- 可搭配 `close_position_in_bar > 0.6` 和成交量放大條件。

**注意事項**

- 若 close 仍在低位，下影線比例可能只是波動噪音。
- 強下跌趨勢中長下影可能連續失敗，需要 regime filter。

## `body_ratio`

**公式**

```text
body_ratio = abs(C - O) / (H - L)
```

**交易語意**

衡量實體佔整根 range 的比例。值高代表方向性強、收盤遠離開盤；值低代表影線或十字線主導。

**使用方式**

- 可作為反轉品質濾網：過高的 `body_ratio` 代表單邊趨勢 bar，反轉難度較高。
- 可用來區分「影線反轉」與「實體突破」。
- 與 `upper_wick_ratio`、`lower_wick_ratio` 互補。

**注意事項**

- 本身沒有固定 Long/Short 方向。
- 高 body ratio 在 momentum 策略中可能是好事，在 mean-reversion 策略中可能是風險。

## `range_zscore_20`

**公式**

```text
range_zscore_20 = zscore(H - L, 20)
```

**交易語意**

衡量當前 K 棒振幅相對最近 20 期是否異常。高值表示事件 bar、清算 bar 或波動擴張。

**使用方式**

- 可作為事件顯著性濾網，例如只交易 `range_zscore_20 > 0` 或 `> 1` 的反轉形態。
- 可與 `reversal_bar_up/down` 的大 range 條件互相驗證。
- 適合用於調整止損/目標距離。

**注意事項**

- 大 range 可以是反轉，也可以是趨勢啟動。
- 本身不是方向訊號。

## `close_position_in_bar`

**公式**

```text
close_position_in_bar = (C - L) / (H - L)
```

**交易語意**

描述收盤價位於單根 K 棒 range 的位置。接近 1 表示收在高位，接近 0 表示收在低位。

**使用方式**

- Long 反轉確認：`close_position_in_bar > 0.6`。
- Short 反轉確認：`close_position_in_bar < 0.4`。
- 可用來補足 wick 因子的「有沒有收回」問題。

**注意事項**

- 沒有 range 時為 `NaN`。
- 單獨使用只描述收盤位置，不描述前後文。

## `reversal_bar_up`

**公式**

```text
range = H - L
avg_range20 = rolling_mean(range, 20)
lower_wick_ratio = (body_lo - L) / range
close_pos = (C - L) / range

if range > avg_range20 and lower_wick_ratio >= 0.5 and close_pos >= 0.6:
    reversal_bar_up = lower_wick_ratio
else:
    reversal_bar_up = NaN
```

**交易語意**

這是事件型多頭反轉 bar。它要求當前 bar 振幅大於 20 期平均、下影線至少佔一半，且收盤在偏高位置。語意是「先向下掃流動性或觸發停損，再被買回」。

**使用方式**

- 可直接作為 Long 候選事件。
- 可與 `distance_to_vwap < 0`、`zscore_price_20 < 0`、`sweep_low_reclaim` 組合。
- 可用下一根開盤或回踩進場，避免同根收盤後資訊洩漏。

**注意事項**

- 事件樣本通常少，需看年度穩定性。
- 若發生在真正的崩跌段，反彈可能很短，止損需要嚴格。

## `reversal_bar_down`

**公式**

```text
range = H - L
avg_range20 = rolling_mean(range, 20)
upper_wick_ratio = (H - body_hi) / range
close_pos = (C - L) / range

if range > avg_range20 and upper_wick_ratio >= 0.5 and close_pos <= 0.4:
    reversal_bar_down = upper_wick_ratio
else:
    reversal_bar_down = NaN
```

**交易語意**

這是事件型空頭反轉 bar。它要求當前 bar 振幅大於 20 期平均、上影線至少佔一半，且收盤在偏低位置。語意是「先向上掃流動性或誘多，再被賣回」。

**使用方式**

- 可直接作為 Short 候選事件。
- 可與 `distance_to_vwap > 0`、`zscore_price_20 > 0`、前高/VAH/布林上緣組合。
- 若搭配 volume spike，可提高事件顯著性。

**注意事項**

- 在強勢突破行情中可能過早放空。
- 需要檢查交易成本與滑價，事件 bar 後的反轉空間可能很短。

## Stage 2 多頭 Alpha 因子總覽

這 6 個因子定義於 `research/mr_alpha_ic_factors.py`，皆為 Long-only、連續型 score，無訊號時輸出 `0.0`。

| Factor | Group | Side | 核心用途 |
|---|---|---:|---|
| `sweep_low_reclaim` | Liquidity Sweep & Reclaim | Long | 掃破前低後重新收回 |
| `cvd_bullish_divergence` | CVD Divergence | Long | 價格低位不改善但 CVD 改善 |
| `negative_delta_absorption` | Order Flow Absorption | Long | 強賣壓下仍收高，代表吸收 |
| `val_reclaim_long` | Volume Profile Alpha | Long | 跌破 VAL 後收回價值區 |
| `poc_reversion_potential` | Volume Profile Alpha | Long | close 到 POC 的回歸空間 |
| `return_shock_reclaim` | Exhaustion & Reclaim | Long | 極端負報酬後收盤回到高位 |

## `sweep_low_reclaim`

**參數**

- `WINDOW = 20`
- ATR 使用 14 期。

**公式**

```text
rolling_low_prev[i] = min(L[i-20], ..., L[i-1])

條件:
L[i] < rolling_low_prev[i]
C[i] > rolling_low_prev[i]
ATR14[i] > 0

score = ((rolling_low_prev - L) / ATR14) * ((C - rolling_low_prev) / ATR14)
```

**交易語意**

價格先跌破最近 20 根的前低，觸發下方停損或吸收流動性，最後又收回前低之上。score 同時衡量「掃得多深」與「收回多強」。

**使用方式**

- 是典型 liquidity sweep + reclaim 多頭訊號。
- 可搭配 `negative_delta_absorption` 確認下方賣壓被吸收。
- 可搭配 `poc_reversion_potential` 評估回到 POC 的利潤空間。

**注意事項**

- 若只掃破但沒有收回，score 為 0。
- 若 ATR 過小，score 可能偏大；實務上可加最大 score clip 或最小 ATR filter。

## `cvd_bullish_divergence`

**參數**

- `WINDOW = 20`
- `PRICE_TOLERANCE = 0.002`

**公式**

```text
delta = 2 * taker_buy_volume - volume
CVD = cumulative_sum(delta)
prev_low_idx = index of min(L[i-20], ..., L[i-1])

條件:
L[i] <= L[prev_low_idx] * (1 + 0.002)
CVD[i] > CVD[prev_low_idx]
rolling_sum(abs(delta), 20) > 0

score = (CVD[i] - CVD[prev_low_idx]) / rolling_sum(abs(delta), 20)
```

**交易語意**

價格回到或接近前低，但 CVD 比前低時更高，代表價格沒有明顯改善、主動成交卻出現改善。這是多頭背離：低位賣壓減弱或買盤承接變強。

**使用方式**

- 適合搭配 `sweep_low_reclaim`：先有掃低，再看 CVD 是否沒有同步創低。
- 適合用於低位盤整或 liquidation wick 後。
- score 越高代表 CVD 改善幅度相對近期 delta 活動越大。

**注意事項**

- 只使用 kline taker buy volume 估算 delta，不是逐筆 tick CVD。
- tolerance 允許「接近前低」也算背離，不要求絕對破低。

## `negative_delta_absorption`

**參數**

- `ZSCORE_WINDOW = 50`

**公式**

```text
delta = 2 * taker_buy_volume - volume
delta_z = zscore(delta, 50)
close_position = (C - L) / (H - L)
lower_wick_ratio = (body_lo - L) / (H - L)

條件:
delta_z < -1.0
close_position > 0.6
lower_wick_ratio > 0.3

score = abs(delta_z) * close_position * lower_wick_ratio
```

**交易語意**

當主動賣壓異常強，但 K 棒仍收在偏高位置且有明顯下影線，代表市場可能吸收了賣單。這比單純長下影更強，因為它加入了 order-flow 壓力背景。

**使用方式**

- 可作為 Long 反轉的 order-flow confirmation。
- 與 `lower_wick_ratio`、`reversal_bar_up`、`sweep_low_reclaim` 互補。
- 適合清算或恐慌賣出後尋找承接。

**注意事項**

- 若資料的 taker buy volume 品質不穩，delta_z 會失真。
- 強趨勢下跌中也可能出現短暫吸收，但後續仍繼續下跌。

## `val_reclaim_long`

**參數**

- `WINDOW = 20`
- `N_BINS = 24`
- ATR 使用 14 期。

**公式**

```text
(POC, VAL) = rolling_volume_profile(H, L, volume, 20, 24)

條件:
L < VAL
C > VAL
ATR14 > 0

score = ((VAL - L) / ATR14) * ((C - VAL) / ATR14)
```

**交易語意**

價格跌破 rolling volume profile 的 Value Area Low，然後收回 VAL 之上。這表示價格短暫跌出價值區後被重新接受，常用於 auction market theory 的多頭均值回歸。

**使用方式**

- 可作為 auction/value based 的 Long trigger。
- 與 `poc_reversion_potential` 搭配，可形成「VAL reclaim 進場，POC 作為目標」的結構。
- 可搭配 `sweep_low_reclaim` 檢查 VAL 下方是否同時掃到結構低點。

**注意事項**

- 這裡是 rolling 20 根 volume profile，不是日內 session profile。
- Volume profile 使用 24 bins，是近似估算；bin 數會影響 VAL/POC 位置。

## `poc_reversion_potential`

**參數**

- `WINDOW = 20`
- `N_BINS = 24`
- `MAX_DISTANCE_ATR = 5.0`
- ATR 使用 14 期。

**公式**

```text
(POC, VAL) = rolling_volume_profile(H, L, volume, 20, 24)
raw_distance = (POC - C) / ATR14
poc_reversion_potential = clip(raw_distance, 0, 5)
```

**交易語意**

衡量當前 close 距離上方 POC 還有多少 ATR 空間。若 POC 在 close 上方，代表價格可能往成交量控制點回歸；若 POC 在 close 下方，對 Long 沒有回歸空間，score 為 0。

**使用方式**

- 不應單獨當進場訊號，更適合作為 reward potential 或 target filter。
- 可要求 `poc_reversion_potential >= 0.5` 或 `>= 1.0`，確保反彈空間足以覆蓋成本。
- 常與 `val_reclaim_long` 搭配。

**注意事項**

- POC 可能在價格下方，此時 Long 回歸潛力為 0。
- 若 ATR 很小，距離會被放大；實作已 clip 到 5 ATR。

## `return_shock_reclaim`

**參數**

- `N = 10`
- `ZSCORE_WINDOW = 100`

**公式**

```text
ret_N = (C[i] - C[i-10]) / C[i-10]
ret_N_z = zscore(ret_N, 100)
close_position = (C - L) / (H - L)

條件:
ret_N_z < -2.0
close_position > 0.6

score = abs(ret_N_z) * close_position
```

**交易語意**

最近 10 根報酬出現極端負向衝擊，但當前 K 棒收在高位。這表示市場經歷快速下跌後，當根已有明顯 reclaim，常用於 exhaustion move 後的多頭反彈。

**使用方式**

- 適合偵測急跌後的 exhaustion reclaim。
- 可搭配 `negative_delta_absorption` 檢查急跌中是否有賣壓吸收。
- 可搭配 `poc_reversion_potential` 過濾反彈空間。

**注意事項**

- 需要 100 期 z-score warm-up，早期資料多為 0 或無訊號。
- 極端下跌可能是新趨勢啟動，不能只看 reclaim 直接逆勢重倉。

## 推薦組合

### 形態反轉組合

```text
lower_wick_ratio high
close_position_in_bar > 0.6
range_zscore_20 > 0
```

用於找到有明顯下影、收回力度足夠、且事件振幅不小的 Long 反轉 bar。

### 掃低收回組合

```text
sweep_low_reclaim > 0
negative_delta_absorption > 0
poc_reversion_potential >= 0.5
```

用於找到掃破前低、賣壓被吸收，且上方 POC 仍有回歸空間的多頭 setup。

### Auction VAL 回歸組合

```text
val_reclaim_long > 0
poc_reversion_potential >= 1.0
distance_to_vwap < 0
```

用於找到跌出價值區後重新被接受，並往 POC/VWAP 回歸的 setup。

### 空頭上影反轉組合

```text
upper_wick_ratio high
close_position_in_bar < 0.4
range_zscore_20 > 0
zscore_price_20 > 0
```

用於找到高位拒絕、追多失敗或上方流動性掃蕩後回落的 Short setup。

## 研究驗證建議

1. 先以單因子 IC、分位數報酬與樣本數檢查方向，不要只看單次回測收益。
2. 對事件型因子分年、分月檢查穩定性，特別是 `reversal_bar_up/down`、`sweep_low_reclaim`、`val_reclaim_long`。
3. 對 Long/Short 雙向因子建立明確方向規則，例如 z-score 下緣只配 Long、上緣只配 Short。
4. 加入交易成本、滑價與下一根成交假設，避免同根收盤訊號高估績效。
5. 將形態、order-flow、volume profile 拆開做 ablation，確認每個模組是否真的提升 OOS。

