# Wick Reversal v5 價格帶優化中文摘要

日期：`2026-04-18`

## 一句話結論

這次優化證明「用 BTC 價格帶來做 regime」是正確方向，但目前只有少數價格帶的參數夠穩定，可以正式導入；其餘價格帶先保留舊版 `r0/r1/r2` fallback，避免過擬合。

## 這次做了什麼

本次把 `Wick Reversal 1m v5` 從原本的「3 段大區間 regime」進一步擴充成「每 `10,000` 美元一個價格帶」的模式。

實作方式：

- 價格帶模式使用 `b{idx}_*` 參數
- 若某價格帶沒有採用的新參數，就自動回退到原本的 `r0/r1/r2`
- 使用專案現有的 3 年 tick 資料做回測與優化

使用資料：

- `BTCUSDT_20230414_20240413`
- `BTCUSDT_20240414_20250413`
- `BTCUSDT`

## 為什麼用 10,000，不用 5,000

原因很直接：  
如果切成 `5,000` 一段，交易數會被分得太碎。再進一步拆成多單/空單後，很多區段的樣本數不足，容易產生「看起來有優化、其實只是碰巧」的結果。

所以這一輪先採用 `10,000` 作為較保守、較可用的切法。

## 最後正式採用的價格帶

這次不是所有跑過的 band 都導入，而是只導入「validation 沒有明顯退步」的結果。

正式採用 3 組：

### 1. `b5_short`：`50k-60k` 空單

採用原因：

- 訓練集有改善
- 驗證集雖然樣本不多，但沒有明顯崩壞
- full 結果轉為正向

採用參數重點：

- `short_k0_vol_gate = 500`
- `short_rr_wick_a/b/c = 2.5 / 1.5 / 0.8`
- `short_min_fee_cover_ratio = 2.0`

### 2. `b8_long`：`80k-90k` 多單

採用原因：

- validation 表現明顯比 baseline 更好
- full 結果也同步提升
- 是這次最乾淨、最有信心的一組多單 band

採用參數重點：

- `long_k0_vol_gate = 800`
- `long_rr_wick_a/b/c = 3.0 / 2.0 / 1.0`

### 3. `b11_long`：`110k-120k` 多單

採用原因：

- validation 從負分翻成正分
- full 結果提升明顯
- 表示高價區在 stop loss 與 RR 上需要更保守的設定

採用參數重點：

- `long_sl_pct_floor = 0.001`
- `long_sl_pct_cap = 0.002`
- `long_k0_vol_gate = 800`
- `long_rr_wick_a/b/c = 3.0 / 1.5 / 1.0`

## 沒有採用的價格帶

雖然有測試，但最後沒有導入：

- `b2_long`
- `b2_short`
- `b3_long`
- `b6_long`
- `b6_short`
- `b7_short`
- `b9_long`
- `b9_short`
- `b10_long`

主要原因：

- 很多 case 在 train 變好
- 但一到 validation 就退步
- 這種情況很像「過擬合」，所以不導入

白話來說，就是：

這些 band 不是完全沒效果，而是還沒有穩到可以直接放進正式策略。

## 目前策略的實際狀態

現在的 `v5` 是一個「混合版本」：

- 有證據支持的價格帶，使用新的 band 參數
- 沒有足夠證據的價格帶，繼續使用舊版 3-regime 參數

所以它不是「全價格帶 fully optimized」，而是「部分價格帶已優化上線，其他區段先保守 fallback」。

## 最後三年回測結果

最終驗證指令：

```bash
python utils/backtest_dynamic_sl.py --strategy "Wick Reversal 1m v5"
```

也有再用參數包驗證一次，結果一致：

```bash
python utils/backtest_dynamic_sl.py --strategy "Wick Reversal 1m v5" --regime-params docs/reports/wick_v5_price_bands_accepted.json
```

結果如下：

| 區間 | Trades | 勝率 | PF | Net PnL | Max DD |
|---|---:|---:|---:|---:|---:|
| `2023-04 ~ 2024-04` | 370 | 38.108% | 0.847 | -1004.2094 | 72.6157% |
| `2024-04 ~ 2025-04` | 210 | 41.905% | 1.148 | 1110.5265 | 25.2784% |
| `2025-04 ~ 2026-04` | 125 | 54.400% | 1.586 | 4300.7280 | 17.0591% |

## 如何解讀這個結果

可以簡單理解成：

- 早期低價區環境仍然偏弱，Y1 還沒救起來
- 中後期表現有改善，尤其高價區比較能反映 band-based regime 的價值
- 價格帶方法是有效的，但目前還不是全面完成版

## 這次留下的報告

中文摘要：

- `docs/reviews/wick_reversal_v5_price_band_review_zh_tw_2026-04-18.md`

技術版總結：

- `docs/reviews/wick_reversal_v5_price_band_review_2026-04-18.md`

最終採用參數：

- `docs/reports/wick_v5_price_bands_accepted.json`

過程報告：

- `docs/reports/wick_v5_price_bands_opt_partial.json`
- `docs/reports/wick_v5_price_bands_opt_high_long.json`
- `docs/reports/wick_v5_price_bands_opt_misc_short.json`

## 下一步建議

如果下一階段還要繼續優化，最合理的方向不是把 grid 再放更大，而是：

1. 累積更多樣本
2. 延續現在的價格帶框架
3. 用更嚴格的 acceptance rule 再跑一次

原因是目前真正的瓶頸不是「沒有更多參數可以試」，而是「很多 band 的樣本還不夠穩」。
