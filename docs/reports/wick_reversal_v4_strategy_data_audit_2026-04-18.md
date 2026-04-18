# Wick Reversal v4 稽核進度

- 產生時間（UTC）：2026-04-18T06:55:43+00:00
- 分段報表來源：`D:\program\OrderFlow\docs\reports\wick_reversal_v4_segment_experiments.json`

## 階段 1：資料對齊確認

### 2023/06 Sample Week

- 區間：`2023-06-05` ~ `2023-06-12`（end-exclusive）
- Tick / rebuilt bars / exchange bars：`9,317,337` / `10,080` / `10,080`
- 任一欄位不一致 bar：`2,153` (`21.36%`)
- 欄位不一致次數：`{'volume': 2148, 'taker_buy_volume': 1198, 'open': 709, 'high': 27, 'low': 31}`
- 最大差異：`{'open': 5.5, 'high': 0.9, 'low': 0.2, 'close': 0.0, 'volume': 17.781, 'taker_buy_volume': 17.781}`
- Close 全數吻合：`yes`
- first tick delay(ms)：`{'median': 80.0, 'p95': 296.0, 'max': 5292.0}`
- last tick gap(ms)：`{'median': 101.0, 'p95': 581.1, 'max': 2490.0}`

最差日期：
- `2023-06-06`: `371` bars
- `2023-06-10`: `369` bars
- `2023-06-05`: `331` bars
- `2023-06-07`: `331` bars
- `2023-06-11`: `257` bars

代表性差異 bar：
- `2023-06-09T14:19:00+00:00` diff=`{'volume': 17.781, 'taker_buy_volume': 17.781}`
- `2023-06-09T14:20:00+00:00` diff=`{'open': 0.1, 'volume': 17.781, 'taker_buy_volume': 17.781}`
- `2023-06-08T03:38:00+00:00` diff=`{'open': 0.1, 'volume': 15.546, 'taker_buy_volume': 15.546}`

### 2024/06 Sample Week

- 區間：`2024-06-03` ~ `2024-06-10`（end-exclusive）
- Tick / rebuilt bars / exchange bars：`6,204,437` / `10,080` / `10,080`
- 任一欄位不一致 bar：`1,462` (`14.50%`)
- 欄位不一致次數：`{'volume': 1461, 'taker_buy_volume': 804, 'open': 487, 'high': 22, 'low': 24}`
- 最大差異：`{'open': 12.4, 'high': 2.4, 'low': 1.0, 'close': 0.0, 'volume': 11.218, 'taker_buy_volume': 11.218}`
- Close 全數吻合：`yes`
- first tick delay(ms)：`{'median': 92.0, 'p95': 624.0, 'max': 3393.0}`
- last tick gap(ms)：`{'median': 162.0, 'p95': 909.0, 'max': 5447.0}`

最差日期：
- `2024-06-07`: `282` bars
- `2024-06-04`: `245` bars
- `2024-06-03`: `225` bars
- `2024-06-05`: `211` bars
- `2024-06-06`: `202` bars

代表性差異 bar：
- `2024-06-07T18:15:00+00:00` diff=`{'open': 12.4, 'volume': 0.022}`
- `2024-06-07T17:49:00+00:00` diff=`{'volume': 11.218, 'taker_buy_volume': 11.218}`
- `2024-06-07T17:50:00+00:00` diff=`{'open': 0.2, 'volume': 11.218, 'taker_buy_volume': 11.218}`

## 階段 2：既有分段實驗摘要

### y2023

- optimized 正分段數：`1/4`
- baseline 平均 score：`-109.738432`
- optimized 平均 score：`-36.43985`
- 平均 score 變化：`73.298582`
- 最佳分段：`{'name': 'm8_to_m4', 'optimized_test_score': 33.366268, 'profit_factor': 1.438861, 'trades': 55}`
- 最差分段：`{'name': 'h2_to_h1', 'optimized_test_score': -133.807729, 'profit_factor': 0.713692, 'trades': 210}`

### y2024

- optimized 正分段數：`3/4`
- baseline 平均 score：`1.939537`
- optimized 平均 score：`9.765968`
- 平均 score 變化：`7.826432`
- 最佳分段：`{'name': 'm4_to_m8', 'optimized_test_score': 55.053226, 'profit_factor': 1.264757, 'trades': 137}`
- 最差分段：`{'name': 'h2_to_h1', 'optimized_test_score': -52.839006, 'profit_factor': 0.877032, 'trades': 118}`

### y2025

- optimized 正分段數：`4/4`
- baseline 平均 score：`159.951917`
- optimized 平均 score：`60.971782`
- 平均 score 變化：`-98.980135`
- 最佳分段：`{'name': 'm4_to_m8', 'optimized_test_score': 88.359047, 'profit_factor': 1.326153, 'trades': 133}`
- 最差分段：`{'name': 'h1_to_h2', 'optimized_test_score': 28.80068, 'profit_factor': 1.234436, 'trades': 102}`

## 判讀

- 樣本週的 tick 重建 1m K 棒與交易所快取存在顯著差異，問題集中在 open/high/low/volume，close 全數吻合。
- 這更像是 aggTrade 缺片或極值遺漏，而不是 bar 索引錯位；時間範圍內未觀察到跨分鐘漂移。
- 分段實驗顯示 2023 年優化後仍大多為負分，2025 年則全部維持正分，策略表現確實高度 regime-dependent。
- 同一組優化流程對 2025 年平均反而降分，說明目前參數搜尋無法產出穩定跨年份解。

## 下一步測試策略

- 優先回補 BTCUSDT_20230414_20240413 的高差異日期：2023-06-06, 2023-06-10, 2023-06-05，先核對 shard/manifest，再針對原始 zip 重新匯入。
- 後續參數掃描先以 y2023 / y2024 的失敗段 (`h2_to_h1`) 當 canary，並保留 y2025 baseline 當控制組，避免對 2025 過度調參。
- 在資料回補完成前，先不要擴大全區間重優化；否則很可能只是對不完整 tick 序列做 overfit。
- 資料修正後，先重跑 `y2023:h2_to_h1` 與 `y2024:h2_to_h1`，只掃 `long/short_delta_eff_threshold` 與 `long/short_sl_offset` 的小網格。
