# `utils/rebuild_tick_cache_once.py` 使用說明

## 1. 工具用途

`utils/rebuild_tick_cache_once.py` 是一次性重建工具，用來把指定日期區間內的 Binance `aggTrades` 日 zip 檔，重建成單一 `NPZ` tick cache。

它的目標是：

- 驗證日期區間內每日 zip 是否完整存在
- 先掃描 metadata，預估總筆數與時間範圍
- 用單次 `memmap` 寫入方式重建大檔
- 最後只做一次 `NPZ` 輸出與 manifest 更新

這個工具適合：

- 初次建立大型 tick cache
- 重新整理某段完整歷史資料
- 用 `--output-symbol` 先產生測試用 cache，避免直接覆蓋正式資料

---

## 2. 和 `tick_cache_worker.py` 的差異

`tick_cache_worker.py` 偏向增量更新；它會逐批把新 zip merge 進既有 `NPZ`。

`rebuild_tick_cache_once.py` 偏向整段重建；它會：

- 先驗證日期是否齊全
- 一次掃出總資料量
- 一次生成完整新檔

如果你要的是：

- 每天持續補資料：用 `tick_cache_worker.py`
- 把一整段歷史乾淨重建一次：用 `rebuild_tick_cache_once.py`

---

## 3. 讀入資料格式

此工具預期 `tick_dir` 內的檔名格式如下：

```text
BTCUSDT-aggTrades-2026-01-20.zip
BTCUSDT-aggTrades-2026-01-21.zip
...
```

zip 內需包含 Binance `aggTrades` CSV。

---

## 4. 輸出內容

執行成功後，會產生：

- `data/ticks/{OUTPUT_SYMBOL}_ticks.npz`
- `data/ticks/{OUTPUT_SYMBOL}_manifest.json`

其中：

- `NPZ` 內含完整 tick array 與 `meta=[start_ms, end_ms]`
- `manifest` 會記錄本次處理過哪些 zip 以及它們的 `mtime/rows`

---

## 5. 參數說明

```bash
python utils/rebuild_tick_cache_once.py ^
  --symbol BTCUSDT ^
  --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" ^
  --from-date 2025-04-14 ^
  --to-date 2026-04-14
```

參數：

- `--symbol`
  來源商品代號，例如 `BTCUSDT`
- `--output-symbol`
  輸出的 cache 名稱；預設等於 `--symbol`
- `--tick-dir`
  每日 `aggTrades` zip 所在目錄
- `--from-date`
  起始日期，含當天，格式 `YYYY-MM-DD`
- `--to-date`
  結束日期，含當天，格式 `YYYY-MM-DD`
- `--scan-workers`
  metadata 掃描平行數，預設 `4`

---

## 6. 常見使用方式

### 6.1 直接重建正式 cache

```bash
python utils/rebuild_tick_cache_once.py ^
  --symbol BTCUSDT ^
  --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" ^
  --from-date 2025-04-14 ^
  --to-date 2026-04-14
```

這會輸出：

- `data/ticks/BTCUSDT_ticks.npz`
- `data/ticks/BTCUSDT_manifest.json`

### 6.2 先重建測試版，不碰正式 cache

```bash
python utils/rebuild_tick_cache_once.py ^
  --symbol BTCUSDT ^
  --output-symbol BTCUSDT_TMP ^
  --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" ^
  --from-date 2025-04-14 ^
  --to-date 2026-04-14
```

這會輸出：

- `data/ticks/BTCUSDT_TMP_ticks.npz`
- `data/ticks/BTCUSDT_TMP_manifest.json`

適合先驗證資料量、範圍與回測結果。

---

## 7. 執行流程

工具內部流程如下：

1. 掃描 `tick_dir` 中所有符合 `{SYMBOL}-aggTrades-YYYY-MM-DD.zip` 的檔案
2. 檢查 `from-date ~ to-date` 每一天的 zip 是否齊全
3. 掃描每個 zip 的 row count / 起訖時間
4. 依預估總筆數建立 `memmap`
5. 逐日解析 CSV，排序並去重
6. 確保跨檔案時間順序單調遞增
7. 寫成單一 `NPZ`
8. 寫出 manifest

---

## 8. 資料安全與覆寫行為

需要注意：

- 若 `--output-symbol` 與正式 symbol 相同，會重建同名 `NPZ`
- 此工具會覆寫目標 `NPZ` 與對應 manifest
- 它不會自動處理 shard 檔
- 若你要保留舊檔，請改用 `--output-symbol`

建議：

- 正式重建前，先對同一批資料跑一次 `--output-symbol BTCUSDT_TMP`
- 確認回測結果與資料範圍後，再覆蓋正式 cache

---

## 9. 常見錯誤

### 9.1 缺少某天 zip

若日期區間內少一天，工具會直接失敗，例如：

```text
missing daily zip(s): ['2026-01-17']
```

這是預期行為，避免你在不完整資料上重建正式 cache。

### 9.2 檔名格式不符

若 zip 不是：

```text
{SYMBOL}-aggTrades-YYYY-MM-DD.zip
```

可能會出現 `unexpected filename`。

### 9.3 CSV 無有效資料

若 zip 內 CSV 損壞或格式異常，可能會出現：

```text
no valid rows in ...
```

### 9.4 時間順序異常

若跨日資料出現倒退，工具會報：

```text
non-monotonic tick time between files
```

這通常表示來源資料重複、損毀，或檔案內容不符合日期順序。

---

## 10. 產出後如何驗證

可先檢查：

- `NPZ` 是否存在
- `manifest` 是否存在
- 回測時是否可正常讀取

例如：

```bash
python utils/benchmark_tick_backtest.py --symbol BTCUSDT --days 1 --load-mode legacy --tick-access map
```

或用你原本的 tick 回測工具驗證結果是否合理。

---

## 11. 建議操作順序

建議如下：

1. 先確認 `tick_dir` 的 zip 日期完整
2. 先用 `--output-symbol XXX_TMP` 重建測試版
3. 用 benchmark / backtest 驗證結果
4. 再決定是否覆蓋正式 `BTCUSDT_ticks.npz`

這樣風險最低。
