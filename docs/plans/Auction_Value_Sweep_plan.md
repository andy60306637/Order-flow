# Plan: Auction Value Sweep Strategy

## Context

實作 `Auction Value Sweep` 策略：以昨日 24H UTC Session Volume Profile 的 VAL/POC/VAH 為框架，在 VAL 或 VAH 出現 K0（同 wick_reversal_v4_ratio 品質邏輯）時用 Tick 精確入場，以 POC 為第一目標（TP1 → trailing），並支援三種突破回踩停利模式。

---

## 新增檔案

### `strategies/auction_value_sweep.py`

**類別：** `AuctionValueSweepStrategy(StrategyBase)`  
**名稱：** `"Auction Value Sweep"`  

---

### 參數分組

#### VP 參數
| 參數 | 預設 | 說明 |
|---|---|---|
| `tick_size` | `1.0` | VP 價格分桶大小 |
| `value_area_pct` | `0.70` | Value Area 百分比 |

#### Session 過濾
| 參數 | 預設 | 說明 |
|---|---|---|
| `enable_session_filter` | `True` | 開關 |
| `session_start_utc_hour` | `0` | 允許開倉起始 UTC 時 |
| `session_end_utc_hour` | `21` | 允許開倉結束 UTC 時（含） |

#### TP 模式（突破回踩）
| 參數 | 預設 | 說明 |
|---|---|---|
| `tp_mode` | `0` | 0=A等距測幅 / 1=B引線1.618 / 2=C全Trail |

#### K0 參數（從 v4_ratio 完整移植，含 price-ratio 縮放）
- 多方：`enable_long`, `long_zoom_bars`, `long_sl_offset`, `long_k0_vol_gate`, `long_delta_eff_threshold`, `long_vol_sma_period`, `long_vol_sma_mult`, `lower_wick_absorption_*`, `long_min_fee_cover_ratio`, `long_body_floor_pct`, `long_wick_type_a/b_threshold`, `long_rr_wick_a/b/c`, `long_td_consec_bars`
- 空方：對稱鏡像同 v4_ratio
- Ratio scaling：`baseline_price=87500`, `sl_offset_map_*`, `k0_vol_gate_map_*`, `min_fee_cover_map_*`
- 費用：`taker_fee_rate=0.00032`, `slippage_rate=0.00002`

---

### 核心方法

#### 1. `_build_daily_vp_cache(klines, tick_map) → Dict[int, VolumeProfile]`
- 將 klines 依 UTC 日期分組（以 `open_time // 86400000 * 86400000` 作為 day key ms）
- 對每個 UTC 日期呼叫 `build_composite_profile(tick_map, day_open_times, tick_size, value_area_pct)`
- 回傳 `{day_start_ms: VolumeProfile}`

#### 2. `_vp_for_bar(k, daily_cache) → Optional[VolumeProfile]`
- 取 `k.open_time` 的「前一 UTC 日」VP（`day_start_ms - 86400000`）

#### 3. `_in_session(k) → bool`
- 取 `hour = (k.open_time // 3600000) % 24`（UTC）
- 回傳 `session_start_utc_hour <= hour <= session_end_utc_hour`

#### 4. `_detect_long_scenario(k, vp, ticks) → Optional[str]`
條件邏輯：
```
body_low = min(k.open, k.close)
# VAL Rejection：wick 觸碰 VAL，body 收回 VAL 上方
if k.low <= vp.val and body_low >= vp.val and _is_k0_long(k, ticks):
    return "val_reject"
# VAH Break & Retest：wick 觸碰 VAH，body 仍在 VAH 上方（突破回踩）
if k.low <= vp.vah and body_low >= vp.vah and _is_k0_long(k, ticks):
    return "vah_retest"
return None
```

#### 5. `_detect_short_scenario(k, vp, ticks) → Optional[str]`
鏡像邏輯：`"vah_reject"` | `"val_retest"` | `None`

#### 6. `_compute_long_target(scenario, vp, k0, entry) → float`
```
if scenario == "val_reject":
    return vp.poc_price
# break & retest (vah_retest):
if tp_mode == 0:   # A: equal range
    return vp.vah + (vp.vah - vp.val)
elif tp_mode == 1: # B: wick extension
    return entry + (k0.high - k0.low) * 1.618
else:              # C: pure trail → sentinel
    return entry + 1e9
```
空方 `_compute_short_target` 對稱鏡像。

#### 7. `_fee_distance_ok(entry, target, k0_close) → bool`
```
expected = abs(entry - target)
cost = _round_trip_cost(entry)
return expected >= cost * _eff_fee_cover(k0_close, long_min_fee_cover_ratio)
```
（覆蓋 v4_ratio 原本的 RR 型計算，改為 VP 距離型）

#### 8. 複製自 v4_ratio 的 private methods（逐字 copy，無需修改）
- `_price_ratio`, `_map_with_price_ratio`, `_eff_sl_offset`, `_eff_vol_gate`, `_eff_fee_cover`
- `_is_k0_long`, `_is_k0_short`
- `_has_lower_wick_absorption`, `_has_upper_wick_absorption`
- `_classify_long_k0_wick`, `_classify_short_k0_wick`
- `_resolve_long_rr`, `_resolve_short_rr`（在 break & retest 模式 B 的 TP2 不使用，僅保留介面一致性）
- `_round_trip_cost`, `_risk_covers_cost`（`_risk_covers_cost` 在此策略被 `_fee_distance_ok` 取代）
- `_vol_sma_ok`, `_bar_entry`, `_tick_entry`（改稱 `_tick_entry_long/short`）
- `_bar_exit_long`, `_bar_exit_short`, `_tick_exit_long`, `_tick_exit_short`

---

### `on_history()` 狀態機

```
初始化：重設 _in_trade, _k0, _k0_idx, _pending, _trailing,
        _stop_price, _entry_price, _target_price, _tp1_hit,
        _td_consec, _trade_dir, _scenario

預處理：daily_vp = _build_daily_vp_cache(klines, tick_map)

主迴圈 for i, k in klines:
  1. 若 _in_trade：呼叫 _handle_exit() → 若出場則重設狀態
  2. 若 pending_entry_bar == i：呼叫 _handle_entry() → 若進場 _in_trade=True
  3. 若 idle：
       vp = _vp_for_bar(k, daily_vp)
       若 vp is None → skip
       若 not _in_session(k) → skip（enable_session_filter 開啟時）
       ticks = tick_map.get(k.open_time)
       scenario = _detect_long_scenario(k, vp, ticks) or _detect_short_scenario(k, vp, ticks)
       若 scenario：_k0=k, _scenario=scenario, pending_entry_bar=i+long_zoom_bars
```

### TP1/TP2 出場邏輯（在 _bar_exit_long / _tick_exit_long 擴充）

```
Phase 1（val_reject / vah_reject）：
  _target_price = poc_price
  當 price >= poc_price：
    _tp1_hit = True
    _trailing = True
    _stop_price = _entry_price  ← Break Even
    （不拆倉，整筆繼續 trailing）

Phase 2（trailing decay）：
  與 v4_ratio 相同：連續 N 根 k_delta <= 0 → TD 出場
```

Break & retest mode A/B：
- `_target_price` = computed TP（mode A 或 B）
- 到達 TP → 直接全倉出場（"TP" 標籤）

Break & retest mode C（純 Trail）：
- `_target_price = sentinel = entry + 1e9`（等效永不觸發 hard TP）
- 從進場即進入 trailing decay 邏輯

---

## 修改檔案

### `strategies/__init__.py`
在現有 import 區塊最後新增：
```python
from strategies import auction_value_sweep as _auction_value_sweep  # noqa
```

---

## 新增測試

### `tests/test_auction_value_sweep.py`
測試項目：
- `_build_daily_vp_cache`：UTC 日期分組正確（跨月 edge case）
- `_vp_for_bar`：回傳前一日 VP；第一天無 VP 時回傳 None
- `_in_session`：邊界時（00:00, 21:00, 21:01, 23:59 UTC）
- `_detect_long_scenario`：
  - K0 wick = VAL, body > VAL → `"val_reject"`
  - K0 wick = VAH, body > VAH → `"vah_retest"`
  - K0 wick 未碰 VAL/VAH → None
- `_detect_short_scenario`：對稱鏡像
- `_compute_long_target`：3 種 tp_mode 數值正確
- `_fee_distance_ok`：距離足夠 / 不足 兩種情境

---

## 簡化說明（與規格的差異）

| 規格描述 | 實作決策 |
|---|---|
| TP1 平倉 50%，SL 移至 BE | 簡化為整筆 trailing，SL 移至 BE（不拆倉）|
| Backtest 引擎無部分平倉支援 | 用戶確認接受此簡化 |

---

## 驗證步驟

1. `python -m pytest tests/test_auction_value_sweep.py -v` — 全測試綠燈
2. `python -m pytest tests/test_volume_profile.py -v` — 確保 VP 引擎無退化
3. 在現有 UI 的策略下拉清單中應出現 `"Auction Value Sweep"`
4. 對 BTCUSDT 1m 跑短期回測（例如 2024-01 到 2024-03），確認：
   - 有產生 long/short 訊號
   - entry_label 為 `L4A/B/C` 或 `S4A/B/C`
   - 有 TP 訊號對應 POC 價位
   - 有 TD 出場訊號（trailing decay）

---

## No Look-Ahead 強制條件

1. ticks 必須按成交時間遞增（否則會把未來 tick 提前讀到）。
2. K0 必須是「收棒後確認」，進場最早從下一根開始（本 plan 現行設計）。
3. VP 只能取前一個 UTC 日，不能退回同日 VP。

## No Look-Ahead Hard Constraints (ASCII)

1. Ticks must be strictly ordered by trade timestamp (ascending), otherwise future ticks may be consumed early.
2. K0 must be confirmed only after bar close; earliest entry is the next bar.
3. VP must use previous UTC day only; never fallback to same-day VP.
