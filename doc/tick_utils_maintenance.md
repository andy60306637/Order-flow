# Tick 工具總覽與維護規範

## 1. 文件目的

這份文件整理 `utils/` 中與 tick 級別資料下載、整合、快取重建、區間讀取、回測準備、驗證診斷相關的工具，並記錄目前系統版本下的維護狀態。

本文件的目標不是列出所有研究腳本，而是：

- 明確哪些工具屬於正式支援的 CLI
- 明確哪些工具是研究/診斷腳本
- 記錄目前版本的最小驗證方式
- 作為之後維護 tick 基礎設施時的檢查清單

驗證日期：`2026-04-16`

相關文件：

- [rebuild_tick_cache_once_manual.md](</D:/program/OrderFlow/doc/rebuild_tick_cache_once_manual.md:1>)
- [rebuild_tick_shards_once_manual.md](</D:/program/OrderFlow/doc/rebuild_tick_shards_once_manual.md:1>)
- [tick_zip_update_workflow.md](</D:/program/OrderFlow/doc/tick_zip_update_workflow.md:1>)

---

## 2. 工具分類

目前 tick 相關工具建議分成兩類：

### 2.1 正式支援 CLI

這類工具有明確參數介面，應優先使用。

- `utils/tick_cache_worker.py`
- `utils/rebuild_tick_cache_once.py`
- `utils/rebuild_tick_shards_once.py`
- `utils/tick_data_backtest.py`
- `utils/benchmark_tick_backtest.py`
- `utils/trade_snapshot.py`
- `utils/optimize_wick_reversal_v4.py`

### 2.2 研究 / 診斷腳本

這類工具偏向一次性檢查，通常有硬編碼 symbol、日期或檔案路徑，不保證穩定 CLI 介面。

- `utils/_check_data_source.py`
- `utils/_test_single_day.py`
- `utils/_test_ui_vs_cli.py`
- `utils/_test_snapshot_align.py`
- `utils/diagnose_entry_drift.py`

---

## 3. 正式支援 CLI 清單

### 3.1 `utils/tick_cache_worker.py`

用途：

- 增量掃描 `aggTrades` zip
- 解析後 merge 進 `data/ticks/{SYMBOL}_ticks.npz`
- 更新 `{SYMBOL}_manifest.json`

適合情境：

- 每日持續補資料
- 背景監看 zip 目錄

目前驗證：

- `python -m py_compile` 通過
- `python utils/tick_cache_worker.py --help` 通過

備註：

- 正式用途是維護 legacy `NPZ` cache
- 不會自動建立 shard

---

### 3.2 `utils/rebuild_tick_cache_once.py`

用途：

- 對指定日期區間做一次性完整重建
- 驗證每日 zip 是否完整
- 最後輸出單一 `NPZ` 與 manifest

適合情境：

- 初次建 cache
- 重建完整歷史區間
- 先用 `--output-symbol` 做測試版 cache

目前驗證：

- `python -m py_compile` 通過
- `python utils/rebuild_tick_cache_once.py --help` 通過

文件：

- [rebuild_tick_cache_once_manual.md](</D:/program/OrderFlow/doc/rebuild_tick_cache_once_manual.md:1>)

---

### 3.3 `utils/rebuild_tick_shards_once.py`

用途：

- 從既有 legacy `NPZ` 建立月分片 shard
- 新增 `{SYMBOL}_shards.json`
- 讓 `tick_cache.load_range()` 可優先走 shard 路徑

適合情境：

- 已經有完整 `NPZ`
- 想降低短區間回測載入時間與記憶體

目前驗證：

- `python -m py_compile` 通過
- `python utils/rebuild_tick_shards_once.py --help` 通過
- 已實際對 `BTCUSDT` 執行成功

文件：

- [rebuild_tick_shards_once_manual.md](</D:/program/OrderFlow/doc/rebuild_tick_shards_once_manual.md:1>)

---

### 3.4 `utils/tick_data_backtest.py`

用途：

- 從 zip 或 cache 載入 tick
- 重建 bar
- 建立 `tick_map` 或 range accessor
- 直接執行策略回測

重要選項：

- `--no-cache`
- `--rebuild-cache`
- `--tick-access map|range`

目前驗證：

- `python -m py_compile` 通過
- `python utils/tick_data_backtest.py --help` 通過

備註：

- 這是日常研究與回測最常用入口之一

---

### 3.5 `utils/benchmark_tick_backtest.py`

用途：

- 拆段 benchmark tick 路徑成本
- 比較 `legacy` 與 `auto(shard-aware)` 讀取模式
- 比較 `map` 與 `range` tick access

重要選項：

- `--days 1,7,90,365`
- `--tick-access map|range|both`
- `--load-mode auto|legacy`

目前驗證：

- `python -m py_compile` 通過
- `python utils/benchmark_tick_backtest.py --help` 通過
- 已實測 `auto` vs `legacy`

已知實測結果：

- `1d legacy`: 載入約 `34.3152s`，peak RSS 約 `17420.3 MB`
- `1d auto`: 載入約 `0.0332s`，peak RSS 約 `124.1 MB`
- `7d legacy`: 載入約 `33.3032s`，peak RSS 約 `17692.2 MB`
- `7d auto`: 載入約 `0.1732s`，peak RSS 約 `396.1 MB`

---

### 3.6 `utils/trade_snapshot.py`

用途：

- 針對每筆交易輸出 tick 級快照圖
- 幫助檢查 k0、entry、exit、stop/target 對齊情況

目前驗證：

- `python -m py_compile` 通過
- `python utils/trade_snapshot.py --help` 通過

備註：

- 屬於視覺化驗證工具
- 依賴 `matplotlib`

---

### 3.7 `utils/optimize_wick_reversal_v4.py`

用途：

- 對 `WickReversalV4` 做 tick backtest 參數優化
- 現已改成走 `load_meta() + load_range()`，可利用 shard

目前驗證：

- `python -m py_compile` 通過
- `python utils/optimize_wick_reversal_v4.py --help` 通過

備註：

- 這是研究工具，不是資料下載工具
- 但它依賴 tick 基礎設施，所以列入維護範圍

---

## 4. 研究 / 診斷腳本清單

### 4.1 `utils/_check_data_source.py`

用途：

- 快速比對 tick 與 kline 快取的時間範圍與價格範圍

狀態：

- `py_compile` 通過
- 仍可用，但會直接讀完整 `load_raw()`，不適合大型資料反覆執行

---

### 4.2 `utils/_test_single_day.py`

用途：

- 匯入單日 zip
- 存入 tick cache
- 驗證單日 tick/kline 一致性

狀態：

- `py_compile` 通過
- 仍可用，但有硬編碼 zip 路徑

注意：

- 會呼叫 `save_raw()`，可能覆寫現有 symbol 的 legacy `NPZ`

---

### 4.3 `utils/_test_ui_vs_cli.py`

用途：

- 比較 UI 路徑與 CLI 路徑的 tick 回測結果是否一致

狀態：

- `py_compile` 通過
- 仍可用，但依賴硬編碼單日 zip 與本地 cache 狀態

---

### 4.4 `utils/_test_snapshot_align.py`

用途：

- 驗證快照上下文與交易對齊是否正確

狀態：

- `py_compile` 通過
- 仍可用，但偏 UI / 快照研究用途

---

### 4.5 `utils/diagnose_entry_drift.py`

用途：

- 檢查 tick/kline 漂移
- 檢查 tick 進場 fill price 的偏移

狀態：

- `py_compile` 通過
- 仍可用，但目前預設直接讀完整 `load_raw()`，執行成本較高

---

## 5. 建議工作流程

### 5.1 日常資料整合

建議順序：

1. 用 `tick_cache_worker.py` 持續把 zip 增量整合成 legacy `NPZ`
2. 視需要定期用 `rebuild_tick_cache_once.py` 做乾淨重建
3. 在 legacy `NPZ` 穩定後，用 `rebuild_tick_shards_once.py` 產生 shard

---

### 5.2 短區間回測 / benchmark

建議順序：

1. 確認 shard 已存在
2. 用 `benchmark_tick_backtest.py --load-mode auto`
3. 用 `tick_data_backtest.py` 或 UI 回測路徑做實際策略驗證

---

### 5.3 問題診斷

建議順序：

1. `_check_data_source.py`
2. `_test_single_day.py`
3. `_test_ui_vs_cli.py`
4. `diagnose_entry_drift.py`
5. `trade_snapshot.py`

這樣可以從資料源、回測路徑一致性，一路查到交易點位視覺化。

---

## 6. 維護規範

未來只要修改以下任一層，應回來更新這份文件：

- `core/tick_cache.py`
- `utils/tick_cache_worker.py`
- `utils/rebuild_tick_cache_once.py`
- `utils/rebuild_tick_shards_once.py`
- `utils/tick_data_backtest.py`
- `utils/benchmark_tick_backtest.py`

至少要重新做這些驗證：

1. `python -m py_compile` 檢查所有相關腳本語法
2. 對正式 CLI 跑 `--help`
3. 至少跑一次 `benchmark_tick_backtest.py`
4. 若改到資料格式或讀取模型，要重新比較 `auto` vs `legacy`

---

## 7. 維護命令清單

### 7.1 語法檢查

```bash
python -m py_compile ^
  utils\tick_cache_worker.py ^
  utils\rebuild_tick_cache_once.py ^
  utils\rebuild_tick_shards_once.py ^
  utils\tick_data_backtest.py ^
  utils\benchmark_tick_backtest.py ^
  utils\trade_snapshot.py ^
  utils\diagnose_entry_drift.py ^
  utils\optimize_wick_reversal_v4.py ^
  utils\_check_data_source.py ^
  utils\_test_single_day.py ^
  utils\_test_snapshot_align.py ^
  utils\_test_ui_vs_cli.py
```

### 7.2 CLI help 檢查

```bash
python utils\tick_cache_worker.py --help
python utils\rebuild_tick_cache_once.py --help
python utils\rebuild_tick_shards_once.py --help
python utils\tick_data_backtest.py --help
python utils\benchmark_tick_backtest.py --help
python utils\trade_snapshot.py --help
python utils\optimize_wick_reversal_v4.py --help
```

### 7.3 基準測試

```bash
python utils\benchmark_tick_backtest.py --days 1,7 --tick-access map --load-mode auto
python utils\benchmark_tick_backtest.py --days 1,7 --tick-access map --load-mode legacy
```

---

## 8. 目前結論

截至 `2026-04-16`：

- 正式支援 CLI 均可正常 `--help`
- 相關腳本均可通過 `py_compile`
- shard-aware `load_range()` 已實際部署並驗證有效
- 短區間 tick 回測的載入與記憶體瓶頸已明顯下降

這代表目前 tick 級別資料下載 / 整合 / 快取 / 區間讀取工具鏈在現行版本下是可維護且可用的。
