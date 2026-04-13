# WickReversalV4 Fee Filter + Dynamic RR 修正版 Plan

**日期**: 2026-04-13  
**對象**: `strategies/wick_reversal_v4.py`

---

## 目標

本次調整有三個目的：

1. 在進場前先過濾掉「理論上連 round-trip 成本都不容易覆蓋」的低品質交易。
2. 依 `k0` wick 強度動態調整 RR，讓高品質 rejection bar 有更高的目標報酬。
3. 讓回測結果可以事後拆解，驗證「費用門檻」與「wick 分級」是否真的提升淨績效，而不是只改了表面參數。

---

## 先修正的觀念

### 1. 費率與滑價單位

假設：

- taker fee = `0.032% = 0.00032`
- slippage = `0.2 bps = 0.002% = 0.00002`

則單邊成本：

```text
fee_per_side = taker_fee_rate + slippage_rate
             = 0.00032 + 0.00002
             = 0.00034
```

round trip 成本率：

```text
round_trip_rate = 2 * fee_per_side
                = 0.00068
```

若進場價為 `P`，則 round trip 成本：

```text
round_trip_cost = P * round_trip_rate
```

以 BTC `84000` 為例：

```text
round_trip_cost = 84000 * 0.00068 = 57.12 USDT
```

所以最低可行風險距離：

```text
min_viable_risk = round_trip_cost / rr
```

例子：

- RR = 1.0 -> `57.12`
- RR = 1.5 -> `38.08`
- RR = 2.0 -> `28.56`

若加上 buffer，例如 `fee_cover_ratio = 1.2`：

```text
min_risk_with_buffer = round_trip_cost * fee_cover_ratio / rr
```

以 BTC `84000`、RR = 2 為例：

```text
57.12 * 1.2 / 2 = 34.272
```

### 2. 本計畫中的成本用途

這裡的成本參數**不是拿來取代回測引擎的真實費用計算**，而是拿來做策略內部的「進場品質門檻」。

也就是：

- 回測引擎仍然用 `backtest/engine.py` 的 fee/slippage 算 net pnl。
- 策略內新增的 cost helper，只用來判斷某筆單的 `risk` 是否太小，不值得進場。

這樣可以避免策略邏輯和回測引擎各自維護一套互相不一致的成本模型。

---

## 設計原則

### 1. 先做 Long，再擴到 Short

目前 `wick_reversal_v4.py` 預設：

```python
enable_short: bool = False
```

所以建議實作順序：

1. 先完成 long side。
2. 驗證 long side 的淨績效是否改善。
3. 再鏡像到 short side。

### 2. A/B/C 分級要真的拉開 RR

目前 long 預設：

```python
long_rr_ratio = 2
```

如果再定義：

```python
long_rr_wick_a = 2.0
long_rr_wick_b = 1.5
```

且 C 類仍然回退到 `long_rr_ratio = 2.0`，那麼 A 跟 C 沒差，分類沒有研究價值。

因此必須明確重設 baseline。建議：

- `A`: `2.0`
- `B`: `1.5`
- `C`: `1.0` 或 `1.2`

Short 同理，不要讓 A/C 使用相同 RR。

### 3. Doji / 極小實體不能直接粗暴歸類為 C

若使用：

```python
ratio = wick / body
```

當 `body == 0` 或極小時，分級會失真。建議改成：

```python
ratio = wick / max(body, body_floor)
```

`body_floor` 可先用固定值，例如：

- `1e-9`
- 或最小跳動單位 `tick_size`
- 或 `k0.close * body_floor_pct`

若當前系統沒有 `tick_size`，第一版可先用 `body_floor_pct`。

---

## 建議新增參數

### Long

```python
# cost filter
long_min_fee_cover_ratio: float = 1.2

# body floor
long_body_floor_pct: float = 0.00001

# wick classification
long_wick_type_a_threshold: float = 4.0
long_wick_type_b_threshold: float = 3.0

# dynamic RR
long_rr_wick_a: float = 2.0
long_rr_wick_b: float = 1.5
long_rr_wick_c: float = 1.0
```

### Short

```python
short_min_fee_cover_ratio: float = 1.2
short_body_floor_pct: float = 0.00001
short_wick_type_a_threshold: float = 4.0
short_wick_type_b_threshold: float = 3.0
short_rr_wick_a: float = 2.0
short_rr_wick_b: float = 1.5
short_rr_wick_c: float = 1.0
```

### Cost helper 用參數

```python
taker_fee_rate: float = 0.00032
slippage_rate: float = 0.00002
```

---

## 建議新增 helper

### 1. `_round_trip_cost`

```python
def _round_trip_cost(self, price: float) -> float:
    return 2.0 * (self.taker_fee_rate + self.slippage_rate) * price
```

### 2. `_body_floor`

```python
def _long_body_floor(self, price: float) -> float:
    return max(price * self.long_body_floor_pct, 1e-9)

def _short_body_floor(self, price: float) -> float:
    return max(price * self.short_body_floor_pct, 1e-9)
```

### 3. `_classify_long_k0_wick` / `_classify_short_k0_wick`

```python
def _classify_long_k0_wick(self, k0: Kline) -> str:
    body = abs(k0.close - k0.open)
    lower_wick = min(k0.open, k0.close) - k0.low
    denom = max(body, self._long_body_floor(k0.close))
    ratio = lower_wick / denom

    if ratio >= self.long_wick_type_a_threshold:
        return "A"
    if ratio >= self.long_wick_type_b_threshold:
        return "B"
    return "C"

def _classify_short_k0_wick(self, k0: Kline) -> str:
    body = abs(k0.close - k0.open)
    upper_wick = k0.high - max(k0.open, k0.close)
    denom = max(body, self._short_body_floor(k0.close))
    ratio = upper_wick / denom

    if ratio >= self.short_wick_type_a_threshold:
        return "A"
    if ratio >= self.short_wick_type_b_threshold:
        return "B"
    return "C"
```

### 4. `_resolve_long_rr` / `_resolve_short_rr`

```python
def _resolve_long_rr(self, k0: Kline) -> float:
    wtype = self._classify_long_k0_wick(k0)
    if wtype == "A":
        return self.long_rr_wick_a
    if wtype == "B":
        return self.long_rr_wick_b
    return self.long_rr_wick_c

def _resolve_short_rr(self, k0: Kline) -> float:
    wtype = self._classify_short_k0_wick(k0)
    if wtype == "A":
        return self.short_rr_wick_a
    if wtype == "B":
        return self.short_rr_wick_b
    return self.short_rr_wick_c
```

### 5. `_risk_covers_cost`

統一 long/short、bar/tick 的邏輯，避免分支漂移。

```python
def _risk_covers_cost(self, entry_price: float, risk: float, rr: float, fee_cover_ratio: float) -> bool:
    if rr <= 0 or risk <= 0 or entry_price <= 0:
        return False
    min_risk = self._round_trip_cost(entry_price) * fee_cover_ratio / rr
    return risk >= min_risk
```

---

## 實作步驟

### Step 1. 新增 cost helper 與 RR helper

先加入：

- `_round_trip_cost`
- `_long_body_floor`
- `_short_body_floor`
- `_classify_long_k0_wick`
- `_classify_short_k0_wick`
- `_resolve_long_rr`
- `_resolve_short_rr`
- `_risk_covers_cost`

這一步只新增 helper，不改進出場行為。

### Step 2. 先改 long bar entry

修改 `_bar_entry`：

原本：

```python
risk = entry_p - stop_p
if risk <= 0:
    return False, 0.0, 0.0, 0.0
target_p = entry_p + risk * self.long_rr_ratio
```

改成：

```python
rr = self._resolve_long_rr(k0)
risk = entry_p - stop_p
if risk <= 0:
    return False, 0.0, 0.0, 0.0
if not self._risk_covers_cost(entry_p, risk, rr, self.long_min_fee_cover_ratio):
    return False, 0.0, 0.0, 0.0
target_p = entry_p + risk * rr
```

### Step 3. 再改 long tick entry

修改 `_tick_entry`：

```python
rr = self._resolve_long_rr(k0)
fill_p = price
stop_p = k0.low - self.long_sl_offset
risk = fill_p - stop_p
if risk <= 0:
    continue
if not self._risk_covers_cost(fill_p, risk, rr, self.long_min_fee_cover_ratio):
    continue
target_p = fill_p + risk * rr
```

### Step 4. 驗證 long side

確認以下幾件事：

1. 交易數是否下降。
2. net profit factor 是否上升。
3. average net pnl per trade 是否上升。
4. `A > B > C` 是否在淨績效上具有單調性。

這一步沒過，不要做 short。

### Step 5. 複製到 short side

對 `_bar_entry_short` 與 `_tick_entry_short` 做同樣調整：

```python
rr = self._resolve_short_rr(k0)
risk = stop_p - entry_p_or_fill_p
if risk <= 0:
    return False / continue
if not self._risk_covers_cost(entry_p_or_fill_p, risk, rr, self.short_min_fee_cover_ratio):
    return False / continue
target_p = entry_p_or_fill_p - risk * rr
```

---

## Signal 與研究欄位建議

### 不只改 label，還要保留研究資訊

只把 entry label 改成 `L4A` / `L4B` / `L4C` 不夠，因為回測後需要分組驗證。

建議：

1. `label` 可以保留做圖表顯示。
2. 額外在交易配對或回測 trade record 中保留研究欄位。

至少應保留：

- `entry_label`
- `wick_type`
- `rr_used`
- `risk_dist`
- `min_risk_required`
- `cost_gate_passed`

如果不保留這些欄位，回測後無法回答：

- A/B/C 哪類真的有效？
- 哪些單是因 cost gate 被過濾掉？
- RR 提高後是否真的提升 net pnl，而不是只是減少交易數？

### label 命名建議

可以改成：

```python
wick_type = self._classify_long_k0_wick(k0)
label = f"L4{wick_type}"
```

short 同理：

```python
label = f"S4{wick_type}"
```

但這只用於顯示，不應當作唯一研究依據。

---

## 重要注意事項

### 1. 驗證一定要看 net，不要只看 gross

本方案本質上是在處理費用與滑價後的交易品質，因此驗證應以 `backtest/engine.py` 的 net pnl 結果為準。

不要只用 `StrategyBase.compute_stats()` 做判斷，因為那邊不是這次方案的正確驗證口徑。

### 2. tick 與 bar 的 RR / cost gate 必須完全一致

目前策略在 tick entry 與 bar entry 的進場價不同：

- bar 用邊界價
- tick 用實際穿越價 `fill_price`

這是合理的，但：

- RR 解法必須一致
- cost gate 邏輯必須一致
- 只允許 entry price 不同，不允許公式不同

### 3. 不要碰 exit 邏輯

這份 plan 的重點是：

- 進場篩選
- 動態 RR
- 研究可觀測性

`TP / TS / TD` 的行為先不要改，否則很難判斷績效變化來自哪個因素。

---

## 建議測試順序

### 單元測試

1. `_round_trip_cost` 數值正確。
2. `0.2 bps` 會得到 `0.00002`。
3. `_classify_long_k0_wick` 在 A/B/C 邊界正確分類。
4. `_classify_short_k0_wick` 在 A/B/C 邊界正確分類。
5. `body` 很小時不會除以 0，也不會無限放大 ratio。
6. `_resolve_long_rr` / `_resolve_short_rr` 回傳正確 RR。
7. `_risk_covers_cost` 在臨界值上下判斷正確。

### 策略行為測試

1. long bar entry 在 risk 不足時不進場。
2. long tick entry 在 risk 不足時不進場。
3. long A/B/C 進場時 target 依不同 RR 變化。
4. short bar entry 在 risk 不足時不進場。
5. short tick entry 在 risk 不足時不進場。
6. short A/B/C 進場時 target 依不同 RR 變化。

### 回測驗證

至少輸出以下分組結果：

- 全部交易
- long only
- short only
- wick type A
- wick type B
- wick type C

每組至少看：

- trades
- win rate
- profit factor
- avg net pnl
- total net pnl
- max drawdown

---

## 實作優先順序

### Phase 1

- 新增 helper
- 改 long `_bar_entry`
- 改 long `_tick_entry`
- 補 long 相關測試

### Phase 2

- 驗證 long side 的回測結果
- 確認 cost gate 沒有把樣本砍到失去統計意義
- 確認 A/B/C 的 RR 分級有資訊價值

### Phase 3

- 複製到 short side
- 補 short 相關測試

### Phase 4

- 若結果良好，再考慮把 `wick_type` / `rr_used` 寫進 trade record，方便 UI 與報表分組分析

---

## 驗收清單

- [ ] 費率公式修正為 `0.00032 + 0.00002`
- [ ] BTC `84000` 的 round trip cost 為 `57.12`
- [ ] RR=2、fee cover ratio=1.2 時的 `min_risk` 為 `34.272`
- [ ] long A/B/C 使用不同 RR，不再發生 A=C
- [ ] short A/B/C 使用不同 RR，不再發生 A=C
- [ ] `body == 0` 或極小實體時不會除以 0
- [ ] long bar/tick 的 RR 與 cost gate 邏輯一致
- [ ] short bar/tick 的 RR 與 cost gate 邏輯一致
- [ ] 進出場 signal 仍保持既有圖表可讀性
- [ ] 回測結果可依 wick type 分組驗證
- [ ] 驗證以 net pnl / net PF 為主，不以 gross 指標替代
