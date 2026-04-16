# Tick Backtest Optimization Plan

## 1. 目標

本計畫的目標不是單純「換檔案格式」，而是系統性降低 tick 級回測在以下三個階段的成本：

- Tick 資料載入
- Tick 對應到 bar 的預處理
- 策略執行期間的 tick 存取與記憶體壓力

適用範圍：

- `core/tick_cache.py`
- `utils/tick_data_backtest.py`
- `strategies/base.py`
- 依賴 `tick_map` 的 tick-first 策略，例如 `strategies/wick_reversal_v4.py`

---

## 2. 現況摘要

目前系統的主要瓶頸不是單一點，而是三段成本疊加：

### 2.1 載入成本

- `load_raw()` 會一次把整個 symbol 的 tick cache 載入記憶體。
- 對長區間回測，單次載入可能是數 GB 級別。
- 現行 `.npz` 壓縮格式節省磁碟，但不一定適合高頻率、大區間的反覆回測。

### 2.2 預處理成本

- `build_bar_map()` 會為每根 bar 建立 `dict[open_time] -> np.ndarray slice`。
- 雖然 slice 本身不一定複製資料，但大量小物件仍會造成 Python 物件管理與 GC 壓力。

### 2.3 策略存取模型成本

- 現有策略大量依賴 `tick_map.get(k.open_time)`。
- 這種介面對策略撰寫方便，但會把預處理成本提前支付到所有 bar。

---

## 3. 問題定義

這份優化計畫要解決的不是單純 I/O，而是下列問題：

1. 是否可以避免「每次都全量載入整段 tick」？
2. 是否可以避免「為每根 bar 預先建好一個 tick slice 物件」？
3. 是否需要重構存儲格式，或只改資料存取模型就已足夠？

---

## 4. 設計原則

### 4.1 先量測，再重構

任何優化都必須先有 baseline benchmark，否則無法判斷改善來自哪一層。

### 4.2 先改資料存取模型，再改存儲格式

若 `tick_map` / slice model 本身就是主要瓶頸，先換成 Parquet 不一定能解決核心問題。

### 4.3 小步可回退

每個 Phase 都必須能獨立驗證，也必須能單獨回退，不做一次性大改。

### 4.4 維持策略正確性優先

效能改善若造成 tick-first 策略行為改變，應視為失敗。

---

## 5. Benchmark 規格

在任何重構開始前，先建立固定 benchmark harness。

### 5.1 測試區間

至少固定三組：

- `7 days`
- `90 days`
- `365 days`

### 5.2 測試模式

每組都量測：

- cold start
- warm cache

### 5.3 指標

每次 benchmark 至少輸出：

- tick load time
- bar build time
- tick-to-bar mapping build time
- strategy execution time
- backtest simulate time
- peak RSS / resident memory
- final trade count
- final PnL / PF / max drawdown

### 5.4 Baseline 實作

先以目前版本為 baseline，比對後續每個 Phase。

---

## 6. Phase 設計

## Phase 0: Benchmark Harness

### 目標

建立可以重複執行、結果可比較的效能基準。

### 實作

- 在 `utils/` 新增 benchmark script
- 封裝：
  - `load_raw()`
  - `_build_klines_from_ticks()`
  - `build_bar_map()`
  - `strategy.on_history()`
  - `simulate_trades()`

### 成功條件

- 同一資料集重複執行的時間誤差可接受
- 能清楚切開每一段耗時

### 失敗條件

- benchmark 只能量總時間，無法拆段

---

## Phase 1: Index-Range Mapping

### 目標

先不碰存儲格式，只替換 `tick_map` 的建立方式。

### 核心想法

把：

- `dict[int, np.ndarray]`

改成：

- `dict[int, tuple[start_idx, end_idx]]`

或更進一步的 view accessor。

策略在需要某根 bar 的 tick 時，再對原始 ticks 做 slicing。

### 實作檔案

- `core/tick_cache.py`
- `strategies/base.py`
- `utils/tick_data_backtest.py`

### 風險

- 現有策略介面依賴 `np.ndarray`，需要兼容層

### 建議做法

先新增新介面，不立刻刪掉舊介面：

- `build_bar_ranges()`
- `TickSliceAccessor`

### 成功條件

- `build_bar_map()` 替代方案可明顯降低物件數
- 7d / 90d / 365d benchmark 記憶體下降

### Gate

若此階段已達成主要效能改善，後續不必急著改存儲格式。

---

## Phase 2: Lazy Range Loading

### 目標

避免長區間回測時一次把整段 tick 全載入。

### 核心想法

以時間範圍讀取 tick，而不是永遠全量讀取。

### 實作方向

- 在 `core/tick_cache.py` 增加 range-based loader
- 讓 `utils/tick_data_backtest.py` 能只載入指定期間
- 若 UI / CLI 已知回測範圍，直接在資料層切段

### 成功條件

- 長區間回測的 peak memory 顯著下降
- 7d / 90d 回測啟動時間明顯改善

### Gate

若無法在現行 `.npz` 上做有效 range loading，再進入 Phase 3。

---

## Phase 3: Sharding

### 目標

讓 range loading 在存儲層真正可行。

### 核心想法

不要維持單一巨型 tick 檔，改成按時間分片。

### 建議分片鍵

- `symbol + YYYYMM`

例如：

- `BTCUSDT_202504`
- `BTCUSDT_202505`

### 實作檔案

- `core/tick_cache.py`
- `utils/rebuild_tick_cache_once.py`
- `utils/tick_cache_worker.py`

### 成功條件

- 讀 7d / 30d / 90d 區間時，不需要掃整年資料
- 增量更新與資料重建流程仍可維持

### 風險

- manifest / metadata 複雜度提高

---

## Phase 4: Storage Format Benchmark

### 目標

用 benchmark 決定存儲格式，不預設 Parquet 一定最好。

### 必比選項

- 現行 `.npz`
- `.npy + memmap`
- Parquet

### 評估面向

- cold read
- warm read
- range read
- implementation complexity
- dependency cost
- 與 NumPy slicing 相容性

### 原則

若 `.npy + memmap` 已滿足需求，優先選更簡單方案，不為了「現代格式」強行改成 Parquet。

---

## Phase 5: Strategy Interface Cleanup

### 目標

把策略從 `tick_map.get(open_time)` 的舊式使用模式，逐步遷移到更低成本的 tick accessor。

### 實作方向

- 在 `strategies/base.py` 提供抽象化 tick accessor
- 先改最重度使用者，例如 `wick_reversal_v4.py`
- 完成後再逐步遷移其他策略

### 成功條件

- 策略碼不需要知道底層資料究竟來自 dict slice、index range、或 lazy loader

---

## 7. 驗證與回歸測試

每一個 Phase 都必須做兩類驗證：

### 7.1 效能驗證

- 與 baseline 比較時間與記憶體

### 7.2 行為驗證

- trade count 是否一致
- entry / exit label 是否一致
- final return / PF / drawdown 是否一致

若有差異，必須明確說明：

- 是數值誤差容忍範圍內
- 還是邏輯被改壞

---

## 8. 回退策略

### 回退原則

每個 Phase 都保留舊路徑，直到新路徑被 benchmark 與回歸測試證明安全。

### 建議方式

- 以 feature flag / config toggle 控制新舊路徑
- 不直接刪除舊的 `build_bar_map()`
- 新格式與舊格式共存一段時間

### 必須可回退的點

- tick cache loading
- bar mapping
- strategy tick access

---

## 9. 預期成果

合理而不是過度樂觀的目標如下：

### 短區間回測

- 7d / 30d 啟動時間顯著下降
- peak memory 明顯下降

### 中長區間回測

- 90d / 365d 不再因大量小物件和全量載入而惡化
- build mapping time 與 strategy time 更可控

### 工程層面

- tick 存取模型更穩定
- 後續優化策略研究時，不需要每次先承受整段 tick I/O 成本

---

## 10. 實施順序總結

建議正式順序：

1. Phase 0: Benchmark Harness
2. Phase 1: Index-Range Mapping
3. Phase 2: Lazy Range Loading
4. Phase 3: Sharding
5. Phase 4: Storage Format Benchmark
6. Phase 5: Strategy Interface Cleanup

這個順序的好處是：

- 先處理最可能真正有效的瓶頸
- 避免一開始就重構存儲層與策略介面
- 每一步都有明確 stop/go decision

---

## 11. 結論

這份計畫的核心不是「把 NPZ 換成 Parquet」，而是：

- 先量測
- 先降低 `tick_map` / 小物件成本
- 再處理 lazy loading 與分片
- 最後才決定存儲格式

如果執行順序正確，這會是一個風險可控、可逐步落地、也最容易驗證效果的回測基礎設施優化方案。

---

## 12. Current Status / TODO

### 已完成

- `Phase 0: Benchmark Harness`
  - 已新增 `utils/benchmark_tick_backtest.py`
  - 可拆分量測 `tick load / range filter / bar build / tick-to-bar mapping / strategy / simulate`
- `Phase 1: Index-Range Mapping`
  - 已在 `core/tick_cache.py` 新增 `build_bar_ranges()` 與 `TickSliceAccessor`
  - 已在 `strategies/base.py` 將 `TickBarMap` 放寬為 mapping-like 介面
  - 已在 `utils/tick_data_backtest.py` 增加 `--tick-access map|range`
- `Phase 2: Lazy Range Loading`
  - 已在 `core/tick_cache.py` 新增 shard-aware `load_range()`
  - 目前 `load_range()` 會優先使用 shard，缺資料時回退舊 `NPZ`
- `Phase 3: Sharding`
  - 已支援 `symbol + YYYYMM` 月分片 `.npy`
  - 已新增 `utils/rebuild_tick_shards_once.py`
  - `BTCUSDT` 已實際建立 shard 並通過 benchmark 驗證

### 已驗證成果

- `1d legacy` → `1d auto(shard)`：
  - tick load 約 `34.3s` 降到 `0.03s`
  - peak RSS 約 `17.4 GB` 降到 `124 MB`
- `7d legacy` → `7d auto(shard)`：
  - tick load 約 `33.3s` 降到 `0.17s`
  - peak RSS 約 `17.7 GB` 降到 `396 MB`
- `1d / 7d` 回測結果一致：
  - trade count / PF / net pnl 無差異

### Optional TODO

- 將剩餘仍直接使用 `load_raw()` 的主要路徑逐步切到 `load_range()` / shard-aware 路徑
  - 例如 `backtest/capacity.py`
  - 例如 `utils/optimize_v4.py`
  - 例如 `utils/optimize_v4_long.py`
  - 例如 `utils/optimize_v4_short.py`
- `Phase 4: Storage Format Benchmark`
  - 正式比較 `.npz`、`.npy + memmap`、`Parquet`
  - 若 `npy + memmap` 已滿足需求，可不繼續推進 Parquet
- `Phase 5: Strategy Interface Cleanup`
  - 進一步將策略從 `tick_map.get(open_time)` 遷移到更明確的 accessor 抽象
  - 優先對象為 `strategies/wick_reversal_v4.py`
  - 此項屬於結構清理，不是當前效能瓶頸的必要前置

### 決策註記

- 目前核心優化目標已達成：
  - 短區間回測啟動時間已顯著下降
  - peak memory 已顯著下降
  - shard 路徑已可正式使用
- 因此剩餘 `Phase 4/5` 屬於「可選升級」，不是當前版本上線的必要條件
