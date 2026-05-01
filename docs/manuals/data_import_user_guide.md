# OrderFlow Data Import Guide

本文件說明如何切換 OrderFlow 的資料根目錄、匯入 BTCUSDT tick/kline 資料，以及如何快速檢查目前資料是否能被系統讀到。

## 1. Data Root 是什麼

OrderFlow 現在不再固定只能讀取專案內的 `data/`。你可以把大型資料放在外部磁碟，例如：

```text
D:\OrderFlowData
```

系統解析 data root 的順序是：

1. CLI/UI 本次操作指定的路徑。
2. `ORDERFLOW_DATA_ROOT` 環境變數。
3. `.ui_settings.json` 裡的 `data_root`。
4. 專案內建 fallback：`<repo>/data`。

也就是說，沒有任何設定時，舊的 `data/` 仍會照常運作。

## 2. 第一次建立外部資料根目錄

建議先初始化資料根目錄：

```powershell
python utils/check_data_root.py --data-root "D:\OrderFlowData" --init
```

這會建立：

```text
D:\OrderFlowData\
  DATA_LAYOUT.md
  manifests\
    data_root.json
```

`DATA_LAYOUT.md` 是給使用者、工具與未來 agent 讀的資料結構說明；`manifests/data_root.json` 是機器可讀的格式宣告。

## 3. 選擇 Data Root

### UI

主畫面工具列有 `Data` 區塊，可以看到目前 active data root，按 `...` 可選擇資料根目錄。回測面板的 `Tick Cache` 區塊也會顯示目前 data root，並提示 tick/kline 是否缺資料。

UI 選擇後會寫入：

```text
.ui_settings.json[data_root]
```

### CLI

單次工具執行可用 `--data-root`：

```powershell
python utils/rebuild_tick_cache_once.py ^
  --data-root "D:\OrderFlowData" ^
  --symbol BTCUSDT ^
  --tick-dir "D:\BinanceVision\futures\um\daily\aggTrades\BTCUSDT" ^
  --from-date 2025-01-01 ^
  --to-date 2025-12-31
```

或用環境變數：

```powershell
$env:ORDERFLOW_DATA_ROOT="D:\OrderFlowData"
python utils/check_data_root.py
```

## 4. 目前支援的 Tick/Kline Cache 位置

目前 tick/kline cache 仍維持 legacy 讀寫位置，以保持相容：

```text
{DATA_ROOT}/ticks/
  BTCUSDT_ticks.npz
  BTCUSDT_manifest.json
  BTCUSDT_shards.json
  shards/BTCUSDT/BTCUSDT_YYYYMM.npy

{DATA_ROOT}/klines/
  BTCUSDT_1m.npy
  BTCUSDT_15m.npy
```

未來擴充資料則使用新的 self-describing layout：

```text
{DATA_ROOT}/futures_um/
  metrics/BTCUSDT/raw/
  metrics/BTCUSDT/cache/
  fundingRate/BTCUSDT/raw/
  fundingRate/BTCUSDT/cache/
  premiumIndexKlines/BTCUSDT/1m/raw/
  premiumIndexKlines/BTCUSDT/1m/cache/
  liquidationSnapshot/BTCUSDT/raw/
  liquidationSnapshot/BTCUSDT/cache/
```

## 5. 匯入 Tick 資料

### 增量匯入 daily aggTrades zip

```powershell
python utils/tick_cache_worker.py ^
  --data-root "D:\OrderFlowData" ^
  --symbol BTCUSDT ^
  --tick-dir "D:\BinanceVision\futures\um\daily\aggTrades\BTCUSDT"
```

### 整段乾淨重建 tick NPZ

```powershell
python utils/rebuild_tick_cache_once.py ^
  --data-root "D:\OrderFlowData" ^
  --symbol BTCUSDT ^
  --tick-dir "D:\BinanceVision\futures\um\daily\aggTrades\BTCUSDT" ^
  --from-date 2025-01-01 ^
  --to-date 2025-12-31
```

### 產生 monthly tick shards

```powershell
python utils/rebuild_tick_shards_once.py ^
  --data-root "D:\OrderFlowData" ^
  --symbol BTCUSDT ^
  --overwrite
```

回測讀取 tick 時會優先使用 shard manifest；如果 shards 不完整，會 fallback 到 legacy NPZ。

## 6. 匯入延伸資料

`core/market_data_cache.py` 已提供基礎 cache helper，支援：

- `metrics`
- `fundingRate`
- `premiumIndexKlines`
- `liquidationSnapshot`

原始檔建議放在各 dataset 的 `raw/`，正規化後的 `.npz` 與 manifest 放在 `cache/`。目前這層是資料格式基礎，尚未建立完整 Binance Vision 批次下載器。

## 7. 快速檢查

檢查目前 active data root：

```powershell
python utils/check_data_root.py
```

檢查指定外部 root：

```powershell
python utils/check_data_root.py --data-root "D:\OrderFlowData"
```

初始化後檢查：

```powershell
python utils/check_data_root.py --data-root "D:\OrderFlowData" --init
```

輸出 JSON：

```powershell
python utils/check_data_root.py --data-root "D:\OrderFlowData" --json
```

判讀方式：

- `OK`：檔案存在且 manifest 可讀。
- `MISSING`：目前 data root 找不到該資料。
- `WARN`：資料存在但格式檢查不完整，或缺少 layout/manifest。

## 8. 常見問題

### UI 選了新 data root，但回測找不到資料

先執行：

```powershell
python utils/check_data_root.py --data-root "你的路徑"
```

確認 `{DATA_ROOT}/ticks` 和 `{DATA_ROOT}/klines` 裡是否有 BTCUSDT cache。

### 是否可以直接搬移舊 `data/`

可以。建議把整個 `data/` 內容搬到外部 root，再執行：

```powershell
python utils/check_data_root.py --data-root "D:\OrderFlowData" --init
```

tick shard manifest 使用相對路徑，所以只要 `ticks/` 整包一起搬，通常可以直接讀。

### 大型資料會不會被 git 追蹤

`.gitignore` 已保留 `data/DATA_LAYOUT.md` 可追蹤，並忽略大型 cache/raw 檔。不要把 `.npy`、`.npz`、tick zip/raw 檔提交到 git。
