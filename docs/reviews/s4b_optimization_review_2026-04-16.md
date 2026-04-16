# S4B Optimization Review

Date: 2026-04-16
Target: `docs/reports/s4b_optimization.json`
Conclusion: `phase4 optimized` 不建議部署

## Review Scope

本 review 針對 `S4B` 專項優化報告做部署層面的檢討，重點不是看 train 或 full sample 是否漂亮，而是看：

- isolated `S4B` 的改善是否具備足夠樣本
- `S4B` 的改善是否能轉成 combined strategy 的 validation 改善
- 目前報告是否足以支撐部署決策

## Findings

### 1. Combined validation 明顯退化，不能作為部署版本

`phase4 optimized` 在 combined validation 的表現比 baseline 差：

- Baseline: PF `1.4939`, Return `66.30%`, Max DD `13.48%`
- Optimized: PF `1.3280`, Return `34.86%`, Max DD `15.51%`

這代表優化後版本在驗證區間呈現：

- 獲利能力下降
- 最大回撤上升
- score 明顯惡化

如果目標是部署，這個結果本身就足以否決 `phase4 optimized`。

## 2. Phase 2 / Phase 3 validation 樣本數過少，統計可信度不足

`phase2_filter` validation 只有 `3` 筆交易，`phase3_risk` validation 也只有 `3` 筆交易。

這種樣本數不足以支持：

- filter 有效
- risk 調整有效
- 結果具有穩定泛化能力

在這種條件下，即使 PF 很高，也更接近「少數交易剛好表現很好」，而不是「策略真的被優化成功」。

## 3. 這次優化主要是在壓縮交易數，不是在提升泛化品質

isolated `S4B` baseline validation 有 `10` 筆交易，PF `3.7078`；但 optimized 放回 combined validation 後，`S4B` 只剩 `3` 筆交易，雖然 PF 變高，但訊號被切得太薄。

這代表目前的優化更像：

- 保留少數極端高品質樣本
- 犧牲可用訊號覆蓋率
- 用稀疏樣本撐出漂亮數值

這對研究可以接受，但對部署不夠穩。

## 4. S4A 仍是 short 端的主要殘留風險

validation baseline 中：

- `S4A`: PF `1.0634`, net pnl `489.13`
- `S4B`: PF `3.4972`, net pnl `3325.54`

validation optimized 中：

- `S4A`: PF `1.0370`, net pnl `240.69`
- `S4B`: PF `5.3897`, net pnl `1159.67`

可以看出：

- `S4B` 單點品質變高
- 但 `S4A` 沒有同步改善
- short side 整體從 PF `1.4218` 掉到 `1.2067`

也就是說，`S4B` 的 isolated 改善沒有成功轉成 short portfolio 的改善。

## 5. 報告適合研究，不適合直接當部署報告

這份報告有研究價值，因為 phase 拆分完整，能看出：

- baseline 問題在哪
- filter 怎麼縮訊號
- risk 調整如何放大單筆績效
- 為什麼回到 combined strategy 後失效

但它缺少部署報告應有的 gate：

- minimum validation trade count
- combined validation 不得低於 baseline 的硬性條件
- walk-forward 檢驗
- out-of-sample 穩健性門檻

## Interpretation

這份結果不是在證明 `S4B` 沒價值，而是在證明目前這條優化路徑有偏差。

更精確地說：

- `S4B` 可以被篩成高品質訊號
- 但目前篩法過於激進，造成樣本過少
- 當 `S4B` 放回整體策略後，無法抵消 `S4A` 與整體 short side 的弱化

所以目前最合理的判斷不是「S4B 優化失敗」，而是：

`S4B` 仍有優化空間，但這一版優化結果不具備部署條件。

## Recommendations

下一輪 `S4B` 專項優化應加入以下硬性規則：

- validation trade count 至少 `8-10` 筆，低於門檻直接淘汰
- combined validation 必須不低於 baseline，否則不得升級為候選部署版本
- 先優化 filter quality，不要優先調 `RR / SL / TD`
- `S4A` 與 `S4B` 必須一起看 portfolio impact，不能只看 isolated `S4B`
- 增加 walk-forward 或 rolling validation，避免單一視窗偏差

建議下一輪順序：

1. 固定 risk 參數，先只測 `S4B` entry/filter
2. 設定最小交易數門檻
3. 檢查 combined validation 是否同步改善
4. 只有在 combined 改善成立後，才進入 risk tuning

## Final Verdict

這份 `s4b_optimization.json` 的核心結論是：

- `S4B` 有研究價值
- 目前優化方向抓到了一些高品質樣本
- 但樣本過少、combined validation 退化
- 因此 `phase4 optimized` 不應部署

現階段比較合理的做法是把它視為 exploratory result，保留研究結論，但不要升級成正式部署版本。
