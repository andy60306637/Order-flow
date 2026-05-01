# Tick ZIP 新增與整合操作流程

## 1. 目的

這份文件說明當未來新增新的 `tick.zip` 資料時，從：

- 新增 zip 檔
- 解析
- 合併到既有 tick cache
- 更新 shard

直到讓回測系統可讀到最新資料的完整操作流程。

適用對象：

- `BTCUSDT` 或其他已建立 tick cache 的 symbol
- 目前使用 legacy `NPZ` + shard 並存的系統版本

---

## 2. 目前資料結構

目前系統中，同一個 symbol 的 tick 資料有兩層：

### 2.1 Legacy 主檔

- `data/ticks/{SYMBOL}_ticks.npz`
- `data/ticks/{SYMBOL}_manifest.json`

這一層是：

- 由 `tick_cache_worker.py` 或 `rebuild_tick_cache_once.py` 維護
- 所有原始 tick 的主快取

### 2.2 Shard 分片

- `data/ticks/{SYMBOL}_shards.json`
- `data/ticks/shards/{SYMBOL}/{SYMBOL}_YYYYMM.npy`

這一層是：

- 由 `rebuild_tick_shards_once.py` 生成
- 供 `load_range()` 與優化後的短區間回測路徑優先使用

---

## 3. 重點原則

目前系統是兩段式流程：

1. 先更新 legacy `NPZ`
2. 再重建 shard

原因是：

- `tick_cache_worker.py` 目前只會更新 `NPZ`
- shard 不會自動跟著更新

所以如果你只做第 1 步、不做第 2 步，會出現：

- 舊路徑可讀到新資料
- shard-aware 路徑仍可能停留在舊資料

---

## 4. 日常增量更新流程

這是最常見的情境：你只新增了幾天或幾週的新 zip。

### Step 1: 放入新的 zip 檔

例如：

```text
tick_data/binance/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-04-15.zip
tick_data/binance/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-04-16.zip
```

檔名必須符合：

```text
{SYMBOL}-aggTrades-YYYY-MM-DD.zip
```

例如：

```text
BTCUSDT-aggTrades-2026-04-15.zip
```

---

### Step 2: 用 worker 解析並合併進既有 NPZ

```bash
python utils/tick_cache_worker.py --symbol BTCUSDT --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"
```

這一步會：

- 掃描 `tick_dir`
- 找出新的 zip
- 解析 CSV
- merge 進 `data/ticks/BTCUSDT_ticks.npz`
- 更新 `data/ticks/BTCUSDT_manifest.json`

如果你是長時間背景監看，也可以用：

```bash
python utils/tick_cache_worker.py --symbol BTCUSDT --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" --watch --interval 60
```

---

### Step 3: 重建 shard

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT --overwrite
```

這一步會：

- 讀取 `data/ticks/BTCUSDT_ticks.npz`
- 重新切成每月 shard
- 更新 `data/ticks/BTCUSDT_shards.json`
- 覆蓋既有 shard 檔

這一步是必要的，因為目前 shard 不會自動增量同步。

---

### Step 4: 驗證資料是否可被最新回測路徑讀到

可先跑 benchmark 或短區間回測確認：

```bash
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1,7 --tick-access map --load-mode auto
```

或：

```bash
python utils/tick_data_backtest.py --symbol BTCUSDT --strategy "Wick Reversal 1m v4" --tick-dir tick_data
```

如果這些路徑可正常跑，代表 shard-aware 讀取已成功接上。

---

## 5. 大範圍補歷史資料的流程

如果你不是補幾天，而是：

- 補很長一段歷史
- 或想重建整段資料
- 或懷疑現有 `NPZ` 有污染 / 缺漏

則不要只靠 `tick_cache_worker.py` 逐日 merge，建議直接重建。

### Step 1: 用一次性重建工具生成完整 NPZ

```bash
python utils/rebuild_tick_cache_once.py ^
  --symbol BTCUSDT ^
  --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" ^
  --from-date 2025-04-14 ^
  --to-date 2026-04-30
```

如果你想先做測試版，不碰正式 cache，可用：

```bash
python utils/rebuild_tick_cache_once.py ^
  --symbol BTCUSDT ^
  --output-symbol BTCUSDT_TMP ^
  --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" ^
  --from-date 2025-04-14 ^
  --to-date 2026-04-30
```

---

### Step 2: 重建 shard

若你重建的是正式 `BTCUSDT`：

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT --overwrite
```

若你重建的是 `BTCUSDT_TMP`：

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT_TMP --overwrite
```

---

### Step 3: 用 benchmark 驗證

```bash
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1,7 --tick-access map --load-mode auto
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1,7 --tick-access map --load-mode legacy
```

檢查：

- `auto` 與 `legacy` 結果是否一致
- 載入時間是否正常
- 記憶體是否正常

---

## 6. 標準 SOP

如果你問「最標準、最穩定的新增 zip SOP」，目前就是這兩條：

```bash
python utils/tick_cache_worker.py --symbol BTCUSDT --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT --overwrite
```

這是目前最推薦的日常操作方式。

---

## 7. 資料流簡圖

```text
新 zip 檔
  ↓
tick_cache_worker.py / rebuild_tick_cache_once.py
  ↓
data/ticks/{SYMBOL}_ticks.npz
data/ticks/{SYMBOL}_manifest.json
  ↓
rebuild_tick_shards_once.py
  ↓
data/ticks/{SYMBOL}_shards.json
data/ticks/shards/{SYMBOL}/{SYMBOL}_YYYYMM.npy
  ↓
load_range() / UI / benchmark / optimized backtest
```

---

## 8. 目前系統限制

需要記住以下限制：

### 8.1 shard 不是自動同步

目前新增 zip 後：

- `NPZ` 會更新
- shard 不會自動更新

所以必須手動再跑一次：

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT --overwrite
```

### 8.2 有些研究腳本仍直接讀 `load_raw()`

部分研究腳本還是直接走 legacy 路徑，所以：

- 它們可能不會直接體現 shard 的效能優勢
- 但資料結果仍應一致

### 8.3 重建 shard 會增加磁碟空間使用

因為 shard 是額外產物，不會取代 legacy `NPZ`。

---

## 9. 常見錯誤情境

### 9.1 新 zip 放對了，但回測還是讀不到最新資料

常見原因：

- 只更新了 `NPZ`
- 沒有重建 shard

解法：

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT --overwrite
```

---

### 9.2 大量補資料後效能還是很差

常見原因：

- 仍在跑 `legacy` 路徑
- 或 shard 尚未生成成功

可檢查：

```bash
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1 --tick-access map --load-mode auto
```

---

### 9.3 不想直接覆蓋正式 cache

請改用：

```bash
--output-symbol BTCUSDT_TMP
```

先重建測試版，再驗證。

---

## 10. 建議操作順序總結

### 日常新增少量資料

1. 放入新 zip
2. 跑 `tick_cache_worker.py`
3. 跑 `rebuild_tick_shards_once.py --overwrite`
4. 跑 benchmark 或短區間回測確認

### 大範圍重建

1. 跑 `rebuild_tick_cache_once.py`
2. 跑 `rebuild_tick_shards_once.py --overwrite`
3. 跑 `benchmark_tick_backtest.py` 驗證

---

## 11. 建議維護方向

如果未來想把流程再簡化，最值得做的是新增一支整合工具，例如：

```text
sync_tick_cache_and_shards.py
```

讓流程從：

1. 更新 `NPZ`
2. 重建 shard

變成一條指令完成。

在目前版本中，這個流程仍是手動兩段式。
