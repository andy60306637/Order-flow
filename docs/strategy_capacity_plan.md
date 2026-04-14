# 策略容量（Strategy Capacity）實作規格

> 修訂日期：2026-04-14  
> 適用範圍：目前專案實作與資料結構  
> 相關模組：`backtest/engine.py`、`core/kline_cache.py`、`core/tick_cache.py`、`core/data_types.py`

---

## 一、目的

本功能的目標，是在現有回測框架上提供一個可落地的「容量估算」能力，用來回答：

- 這個策略在資金放大後，績效會如何衰減？
- 什麼資金水位開始出現明顯容量瓶頸？
- 單筆交易的市場參與率是否已高到不可忽略？

本版本只做：

- `Taker-only`
- `Volume-based`
- `Tier-2` 容量估算

本版本不做：

- 歷史 L2 訂單簿回放
- Maker queue position 建模
- 交易衝擊反饋到訊號生成本身的完整撮合模擬

---

## 二、現況校正

以下幾點必須先釐清，否則原始部署計畫會高估目前系統的現成度。

### 2.1 已存在的能力

- Tick 快取已存在，格式為 `data/ticks/{SYMBOL}_ticks.npz`，欄位為 `trade_time, price, qty, is_buyer_maker`
- K 線快取已存在，raw `.npy` 檔包含 `quote_volume`、`count` 等欄位
- `tick_cache.build_bar_map()` 已可把 tick 切回 K 棒
- 回測引擎已有固定 `slippage_bps`
- `wick_reversal_v4` 已有 entry label，例如 `L4A/L4B/L4C`、`S4A/S4B/S4C`

### 2.2 尚不存在的能力

- `simulate_trades()` 目前不支援每筆交易動態滑價
- `trade_list` 目前沒有 `entry_notional`、`applied_slippage_bps`、`impact_bps`、`entry_label`
- `Kline` dataclass 本身沒有 `quote_volume`、`count`
- 回測統計目前沒有 `sharpe`

### 2.3 對實作的直接影響

- 容量功能不能直接建立在 `List[Kline]` 之上，需要直接讀 `kline_cache` raw rows
- 容量分析若要做 per-trade breakdown，必須先擴充 `engine.py` 的 trade record
- UI 不應該先做，必須等 engine 契約與容量輸出口徑先固定

---

## 三、MVP 模型定義

### 3.1 市場衝擊模型

MVP 採用平方根市場衝擊模型：

```text
impact_bps = eta * sigma_daily_frac * sqrt(Q / ADV) * 10000
```

定義：

- `eta`：可調係數，預設 `1.0`
- `sigma_daily_frac`：日報酬率標準差，使用日線 close-to-close return 計算，單位為小數
- `Q`：單筆進場名目值，單位 USDT
- `ADV`：日均成交額，單位 USDT

說明：

- `impact_bps` 輸出單位固定為 bps
- `sigma_daily_frac` 不使用「價格標準差 / mean」混合寫法，避免單位不清
- 若缺少日線快取，先由既有 interval K 線聚合成日資料

### 3.2 成交量參與率

```text
VPR = position_qty / bar_volume_qty
```

MVP 口徑：

- 先用 K 線 `volume` 作為 bar volume
- 進階版再切換成 tick 累加的真實棒量

預設門檻：

- `warn`: `1%`
- `cap`: `5%`

### 3.3 掃描邏輯

對同一份 `signals` 做多組資金掃描，量化在不同資本水位下的績效衰減。

MVP 採用：

- 固定訊號路徑
- 動態滑價只影響成交價、部位大小、損益
- 不回頭改變策略本身的訊號產生

這代表它是「容量估算」，不是完整市場模擬。

---

## 四、MVP 指標

MVP 先以目前引擎已經有的統計為主：

- `profit_factor`
- `max_drawdown_pct`
- `total_net_pnl`
- `total_return_pct`
- `trades`
- `win_rate`

`sharpe` 不列入第一版必做，原因：

- 目前 `engine.py` 沒有回傳 `sharpe`
- 若要新增，必須先定義報酬序列口徑：per-trade、daily equity、或 bar-by-bar equity

若第二版需要 `sharpe`，再單獨補一個明確規格。

---

## 五、資料契約調整

這一段是整個容量功能的前置條件，優先級高於 UI。

### Phase 0：回測引擎契約重整

**目標**：讓引擎能承載容量分析需要的 per-trade 資訊。

**修改檔案**：`backtest/engine.py`

#### 5.1 `BacktestConfig` 新增欄位

```python
@dataclass
class BacktestConfig:
    ...
    dynamic_slippage: Optional[Callable[[float, int], float]] = None
    # 簽名：dynamic_slippage(provisional_entry_notional, entry_time_ms) -> extra_bps
```

備註：

- `provisional_entry_notional` 指「依原始 entry price 先算出的 provisional 名目值」
- 這是近似法，但可避免 `qty -> impact -> qty` 的循環依賴
- `dynamic_slippage is None` 時必須完全保留舊行為

#### 5.2 `trade_list` 新增欄位

每筆成交至少新增：

```python
{
    "entry_notional": float,
    "exit_notional": float,
    "applied_slippage_bps": float,
    "impact_bps": float,
    "entry_label": str,
}
```

建議額外保留：

```python
{
    "entry_stop": float | None,
    "provisional_entry": float,
}
```

#### 5.3 `_pair_signals()` 需保留 entry metadata

目前 entry signal 的 label 沒有進入 trade record。  
容量分析若要分組比較 `L4A/L4B/L4C` 與 `S4A/S4B/S4C`，這一步必須先補。

---

## 六、容量核心模組

### Phase 1：`backtest/capacity.py`

**目標**：建立不依賴 UI 的容量分析核心。

**新增檔案**：`backtest/capacity.py`

#### 6.1 建議資料結構

```python
@dataclass
class CapacityConfig:
    impact_eta: float = 1.0
    adv_window_days: int = 30
    vpr_warn_pct: float = 0.01
    vpr_cap_pct: float = 0.05
    limit_drop_pct: float = 0.20
    capital_sweep: list[float] = field(default_factory=lambda: [
        1_000, 5_000, 10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000
    ])


@dataclass
class CapacityPoint:
    capital: float
    profit_factor: float
    max_drawdown_pct: float
    total_net_pnl: float
    total_return_pct: float
    trades: int
    avg_impact_bps: float
    max_vpr: float
    avg_vpr: float
    warning_count: int


@dataclass
class CapacityReport:
    points: list[CapacityPoint]
    baseline_capital: float
    capacity_limit_usdt: float | None
    recommended_capital: float | None
    baseline_profit_factor: float
    notes: list[str]
```

#### 6.2 `CapacityAnalyzer` 職責

```python
class CapacityAnalyzer:
    def load_raw_klines(self, symbol: str, interval: str) -> np.ndarray: ...
    def calc_adv(self, raw_klines: np.ndarray, window_days: int) -> float: ...
    def calc_daily_volatility(self, raw_klines: np.ndarray) -> float: ...
    def calc_impact_bps(self, entry_notional: float, adv: float, sigma_daily_frac: float, eta: float) -> float: ...
    def calc_vpr_from_bars(self, trade_list: list[dict], raw_klines: np.ndarray) -> list[dict]: ...
    def run_sweep(self, signals: list, base_cfg: BacktestConfig, cap_cfg: CapacityConfig, symbol: str, interval: str) -> CapacityReport: ...
```

#### 6.3 重要實作規則

- ADV 一律從 raw kline cache 的 `quote_volume` 欄位讀，不從 `Kline` dataclass 取
- 日波動率一律用 daily return std 計算
- `impact_bps` 只作為額外 entry/exit 滑價，不改 signal path
- VPR MVP 先用 bar volume；tick-level 另列 Phase 2

#### 6.4 容量上限判定

MVP 明確定義為：

```text
capacity_limit_usdt =
    最大 capital，
    使得 profit_factor >= baseline_profit_factor * (1 - limit_drop_pct)
```

補充：

- 若同時有 `max_vpr >= vpr_cap_pct`，則該 capital 視為超過容量上限
- `recommended_capital` 先定義為「所有未超限資本中，profit_factor 最高者」

這個定義比「Sharpe 或 PF 任一衰退超過 20%」更容易落地，也更符合目前引擎輸出。

---

## 七、精確 VPR 增強

### Phase 2：Tick-Level VPR

**目標**：用 tick 真實棒量取代 K 線 volume，提升 VPR 精度。

**依賴**：Phase 1 完成

**修改檔案**：`backtest/capacity.py`

實作方式：

- 用 `tick_cache.build_bar_map()` 建立 `bar_map`
- 以 `entry_time` 對應 bar open time
- 對應 tick slice 後累加 `qty`
- 取代 bar-volume-based VPR

如果某根棒沒有 tick：

- fallback 回 K 線 `volume`
- 並在報告中累計 fallback 次數

---

## 八、UI 規格

### Phase 3：容量分析頁

**目標**：提供使用者可直接操作的容量掃描界面。

**新增檔案**：`ui/capacity_tab.py`

**修改檔案**：`ui/main_window.py`

### 8.1 UI 只展示已定稿的 backend 輸出

UI 顯示欄位以 `CapacityReport` 為準，不自行推導。

表格欄位建議：

- `capital`
- `profit_factor`
- `max_drawdown_pct`
- `total_net_pnl`
- `avg_impact_bps`
- `max_vpr`
- `warning_count`

### 8.2 背景執行

- 掃描必須放在 `QThread`
- UI 只負責顯示進度與結果
- 不在 UI thread 內直接重跑多次回測

### 8.3 UI 不是 MVP 阻塞項

若 backend 還未完成：

- 可先用 CLI / debug panel 驗證
- UI 排在 backend 穩定之後

---

## 九、測試計劃

### 9.1 引擎契約測試

- `dynamic_slippage=None` 時結果與舊版一致
- 有 `dynamic_slippage` 時，`applied_slippage_bps` 正確寫入
- `entry_notional`、`exit_notional`、`entry_label` 都會進入 `trade_list`

### 9.2 容量數學測試

- `calc_impact_bps()` 單位正確，輸出為 bps
- `ADV=0` 或資料不足時有穩定 fallback
- `sigma_daily_frac` 對應 daily return std，不混用價格標準差

### 9.3 VPR 測試

- bar-based VPR 計算正確
- tick-based VPR 計算正確
- tick 缺漏時會 fallback，且 fallback 次數可被統計

### 9.4 掃描測試

- 掃描會生成多個 `CapacityPoint`
- 基準資本正確
- `capacity_limit_usdt` 依 `profit_factor` 衰退門檻正確判定

### 9.5 回歸測試

- 原有 `wick_reversal_v4` 測試必須全數通過
- 原回測 UI 的 summary / trade table 不可被破壞

---

## 十、時程重排

| Phase | 內容 | 檔案 | 估時 |
|------|------|------|------|
| `P0` | engine 契約重整 | `backtest/engine.py` | 0.5-1 天 |
| `P1` | 容量核心模組 | `backtest/capacity.py` | 1.5-2.5 天 |
| `P2` | Tick-Level VPR | `backtest/capacity.py` | 0.5-1 天 |
| `P3` | UI 分頁 | `ui/capacity_tab.py`、`ui/main_window.py` | 1.5-2.5 天 |

**總計**：約 4-7 個工作天  
**建議順序**：`P0 -> P1 -> P2 -> P3`

---

## 十一、風險與限制

### 11.1 模型限制

- 沒有歷史 L2，無法精確估算可吃掛單深度
- 沒有 spread 歷史，隱性成本只能近似
- 沒有 signal feedback，無法模擬大資金影響後訊號本身改變

### 11.2 資料限制

- `Kline` dataclass 不含 `quote_volume` / `count`
- 因此容量模組若要用這些欄位，必須直接讀 raw cache

### 11.3 指標限制

- 第一版不做 Sharpe，避免先把 UI 與門檻建立在未定義的統計口徑上

---

## 十二、最終結論

可以做，而且值得做；但正確順序不是「直接加容量 UI」，而是：

1. 先補 `engine.py` 的動態滑價與 trade record 契約
2. 再做 `capacity.py` 的容量核心
3. 最後才做 UI

本文件定義的 MVP，是目前 codebase 上最穩妥、風險最低、且能真正交付的版本。
