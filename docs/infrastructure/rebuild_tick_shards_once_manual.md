# `utils/rebuild_tick_shards_once.py` 使用說明

## 1. 工具用途

`utils/rebuild_tick_shards_once.py` 用來把既有的單一 `NPZ` tick cache，轉成按月份切分的 shard 檔案。

它的目的是：

- 保留原本的 `NPZ`
- 額外生成月分片 `.npy`
- 生成 shard manifest
- 讓 `tick_cache.load_range()` 可以只讀需要的月份，而不必每次先全量載入整包 `NPZ`

這是目前 tick 回測優化計畫中 `Phase 2/3` 的關鍵工具。

---

## 2. 前置條件

執行前必須先有舊版 `NPZ` cache，例如：

- `data/ticks/BTCUSDT_ticks.npz`

如果舊 `NPZ` 不存在，工具會直接退出。

---

## 3. 輸出內容

以 `BTCUSDT` 為例，執行後會新增：

- `data/ticks/BTCUSDT_shards.json`
- `data/ticks/shards/BTCUSDT/BTCUSDT_202504.npy`
- `data/ticks/shards/BTCUSDT/BTCUSDT_202505.npy`
- `data/ticks/shards/BTCUSDT/BTCUSDT_202506.npy`
- ...

注意：

- 舊的 `data/ticks/BTCUSDT_ticks.npz` 不會被刪除
- 新 shard 是額外產物，不是 inplace 改寫

---

## 4. 參數說明

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT
```

參數：

- `--symbol`
  目標商品，例如 `BTCUSDT`
- `--overwrite`
  若 shard 已存在，允許覆蓋

---

## 5. 基本用法

### 5.1 第一次建立 shard

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT
```

如果成功，畫面會輸出類似：

```text
symbol=BTCUSDT
legacy_span=1744588800013->1776124799989
shard_dir=D:\program\OrderFlow\data\ticks\shards\BTCUSDT
manifest=D:\program\OrderFlow\data\ticks\BTCUSDT_shards.json
months=13
ticks=566,741,855
```

### 5.2 強制重建 shard

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT --overwrite
```

只有在你確定要重建 shard 時才建議使用。

---

## 6. 執行後會發生什麼

工具流程很簡單：

1. 讀取 `data/ticks/{SYMBOL}_ticks.npz`
2. 依 tick 時間排序
3. 按月份切段
4. 寫出每個月份的 `.npy`
5. 寫出 `data/ticks/{SYMBOL}_shards.json`

之後 `core/tick_cache.py` 中的：

- `load_meta()`
- `load_range()`

就會優先使用 shard manifest 與月分片資料。

---

## 7. shard manifest 內容

`{SYMBOL}_shards.json` 會記錄：

- `symbol`
- `format`
- `start_ms`
- `end_ms`
- `months`

其中 `months` 內每個月份會記錄：

- shard 檔路徑
- tick 筆數
- 該月份的起訖時間

這讓系統可以只載入需要的月份，而不是掃整年資料。

---

## 8. 對現有資料的影響

這個工具的設計是安全的：

- 不會刪除舊 `NPZ`
- 不會改寫舊 `NPZ`
- 只會新增 shard 檔與 shard manifest

但是：

- 若你加上 `--overwrite`，會覆蓋既有 shard 檔
- shard 會額外占用磁碟空間

也就是說，這個工具主要影響的是磁碟容量，不是舊資料本身。

---

## 9. 使用後的效益

建立 shard 後，走 `load_range()` 的路徑會優先讀 shard。

在目前實測中，短區間回測改善非常明顯：

- `1d legacy`：載入約數十秒、記憶體數十 GB 級
- `1d auto(shard)`：載入約 `0.03s`、記憶體約數百 MB 以下
- `7d auto(shard)`：仍是亞秒到數百毫秒等級

而且：

- 交易結果一致
- 回測指標一致

所以 shard 的主要價值在於：

- 大幅降低短區間回測啟動時間
- 大幅降低 peak memory

---

## 10. 常見錯誤

### 10.1 找不到 legacy NPZ

若舊 `NPZ` 不存在，會出現：

```text
legacy NPZ cache not found for BTCUSDT
```

代表你要先建立舊版 `NPZ`，再做 shard 切分。

### 10.2 shard 已存在

若 shard 已存在且未加 `--overwrite`，會出現類似：

```text
FileExistsError: shard already exists
```

這是保護機制，避免不小心覆蓋已存在的 shard。

---

## 11. 什麼時候需要重新執行

以下情況建議重新跑一次：

- 你重建了新的 `NPZ`
- `NPZ` 的時間範圍變大
- 來源資料修正過，想同步更新 shard

如果只是一般回測，不需要反覆執行。

---

## 12. 建議操作順序

建議如下：

1. 先用 `rebuild_tick_cache_once.py` 或既有流程建立完整 `NPZ`
2. 再執行 `rebuild_tick_shards_once.py`
3. 用 benchmark 驗證 `auto` 與 `legacy` 的結果一致

例如：

```bash
python utils/rebuild_tick_shards_once.py --symbol BTCUSDT
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1,7 --load-mode auto --tick-access map
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1,7 --load-mode legacy --tick-access map
```

---

## 13. 什麼情況不要用這個工具

以下情況不建議直接跑：

- 你還沒有確認舊 `NPZ` 是否正確
- 磁碟空間不足
- 你暫時不需要 `load_range()` 的高效能路徑

因為 shard 是效能優化產物，不是資料修復工具。
