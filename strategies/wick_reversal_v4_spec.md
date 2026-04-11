# Wick Reversal 1m v4

## 1. 版本定位

- `v4` 是針對 `tick` 級回放重構的 long-only 研究版。
- 核心目標不是延續 `v3` 的 bar 樂觀回測，而是把訊號、觸發、成交、停損放回真實事件順序。
- `v4` 預設應以 `tick_data` 重建出的 1m bars + 對應 tick map 做回測；若直接混用外部 1m kline cache，可能出現 bar/tick 不一致。

## 2. 策略結構

### 2.1 k0

做多 `k0` 同時滿足：

1. 不看紅綠，只看形態。
2. 實體位於整根 K 棒上半部。
3. 下影線明顯大於實體。
4. 下影線區域必須出現吸收。
5. `k0` 本身不能是過小的噪音棒，必須通過最近區間的 range 濾網。

形態定義：

```text
range > 0
body_low >= mid
lower_wick > body
```

其中：

```text
mid        = (high + low) / 2
body_low   = min(open, close)
body_high  = max(open, close)
body       = abs(close - open)
lower_wick = body_low - low
```

### 2.2 吸收判斷

#### Tick 模式

優先使用 `k0` 對應 minute 內的 tick：

```text
wick_ticks = ticks where price <= body_low
wick_vol   = sum(qty)
total_vol  = sum(all qty)
wick_delta = 2 * wick_buy_vol - wick_vol
wick_delta_eff = wick_delta / wick_vol
```

做多吸收成立條件：

```text
wick_vol / total_vol >= lower_wick_absorption_min_vol_ratio
wick_delta_eff <= lower_wick_absorption_delta_eff_max
```

這代表下影線區域有足夠成交，且主動賣壓被承接。

#### 無 tick fallback

若沒有 tick，只能退回 bar 級近似：

```text
bar_delta <= lower_wick_absorption_bar_delta_max
```

這只是防守性近似，不應拿來當 deploy-grade 結論。

### 2.3 k_delta

`k0` 形成後，只在很短的 `zoom` 內找 `k_delta`。

做多 `k_delta` 條件：

1. `k.low >= k0.low`
2. `delta_eff > long_delta_eff_threshold`
3. `volume > SMA(previous bars) * long_vol_sma_mult`
4. `close >= open`
5. `close >= max(k0.open, k0.close)`  
   `v4` 這裡改成 reclaim `k0` 的 body high，而不是強制突破 `k0.high`
6. `close` 必須收在自身 range 的上方區域
7. `k_delta` 本身不可過度擴張，避免把延伸動能誤當成反轉確認

目前預設：

```text
zoom_bars                = 1
k_delta_close_pos_min    = 0.8
k_delta_max_range_mult   = 1.25
```

也就是：

- `k_delta` 只允許在 `k0` 後 1 根內確認
- `k_delta` 收盤需接近自身高檔
- `k_delta` range 不可明顯大於 `k0` range

### 2.4 下一根觸發進場

只有 `k_delta` 的下一根 `k_n` 可以進場。

#### Tick 模式

進場條件：

1. 先檢查是否先破壞結構  
   若 `k_n` 內先打到 `stop_price`，setup 直接失效
2. 再檢查是否首次穿越 `k_delta.close`
3. 若首次穿越價距離 trigger 太遠，也直接放棄，不追價

```text
trigger_price = k_delta.close
stop_price    = min(k0.open, k0.close)
base_risk     = trigger_price - stop_price

first tick price >= trigger_price
and price - trigger_price <= base_risk * max_entry_extension_r
```

目前預設：

```text
max_entry_extension_r = 0.15
```

這代表真正觸發價若比 `k_delta.close` 多延伸超過基礎風險的 15%，就放棄該筆交易。

#### Bar 模式

bar 模式只能做保守近似：

1. 若 `k.low <= stop_price`，直接視為 setup 失效
2. 若 `k.high < trigger_price`，不進場
3. 若 `max(k.open, trigger_price)` 超過允許延伸，也不進場

## 3. 停損與目標

做多停損：

```text
stop_price = min(k0.open, k0.close)
```

這是目前 `v4` 的主結構停損。

盈虧比：

```text
rr_ratio = 1.0
target_price = fill_price + (fill_price - stop_price) * rr_ratio
```

## 4. 出場管理

長倉管理維持既有 long-side 概念：

1. `SL`
2. `TP`
3. `TS`
4. `TD`

規則：

- 先碰 stop -> `SL` / `TS`
- 先碰 target 且 delta 不支持續抱 -> `TP`
- 先碰 target 且 delta 仍支持 -> 啟動 trailing，並把 stop 拉到 target
- trailing 後連續 `td_consec_bars` 根 delta 轉弱 -> `TD`

## 5. 與舊版 v4 的重構差異

這一版 `v4` 相較前一版，新增了三個關鍵的 tick 合理性修正：

1. `k0` 噪音濾除  
   不再接受過小 range 的微型下影線棒
2. `k_delta` 品質濾網  
   不再要求突破 `k0.high`，改成 reclaim body high，並要求 close position 與 range 品質
3. `next-bar` 真實觸發保護  
   加入 `stop-first invalidation` 與 `entry extension cap`

## 6. 回測基準

`v4` tick 研究應使用：

```bash
python utils/tick_data_backtest.py --strategy "Wick Reversal 1m v4" --symbol BTCUSDT --tick-dir tick_data --fee-mode Taker
```

此腳本會：

1. 直接讀取 `tick_data/*.zip`
2. 由 tick 重建 1m bars
3. 建立與 bars 完全一致的 `tick_map`
4. 使用專案既有 `backtest.engine` 做成交與統計

## 7. 目前結論

- 這次重構後，`v4` 在 tick-derived 1m bars 上已顯著優於 `v3` 與舊版 `v4`
- 但截至目前，結果仍未達可上線等級
- `v4` 現在比較像是「把錯誤的 bar alpha 拆掉之後，留下較乾淨的反轉原型」
- 下一步應優先繼續驗證：
  - 是否要補 short 對稱版
  - 是否要把 trailing 改成更嚴格的 microstructure exit
  - 是否要再加 session / volatility regime filter
