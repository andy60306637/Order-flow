# Pipeline 策略系統 — 交易員開發手冊

> 適用版本：Pipeline v1.1（新增 tick 資料支援）  
> 路徑：`strategies/pipeline/`

---

## 目錄

1. [架構概覽](#1-架構概覽)
2. [核心概念](#2-核心概念)
3. [內建 Components](#3-內建-components)
4. [內建 Stages](#4-內建-stages)
5. [組建第一條 Pipeline](#5-組建第一條-pipeline)
6. [多策略管理：MultiPipelineRunner](#6-多策略管理-multipipelinerunner)
7. [接入回測引擎](#7-接入回測引擎)
8. [自定義 Component](#8-自定義-component)
9. [自定義 Stage](#9-自定義-stage)
10. [自定義 AlphaSignal](#10-自定義-alphasignal)
11. [SharedContext 快取機制](#11-sharedcontext-快取機制)
12. [手續費計算說明](#12-手續費計算說明)
13. [最佳實踐與注意事項](#13-最佳實踐與注意事項)
14. [Tick 資料整合](#14-tick-資料整合)
15. [故障排查](#15-故障排查)
16. [欄位速查表](#16-欄位速查表)

---

## 1. 架構概覽

```
MultiPipelineRunner
│
├── SharedContext  ←──────────────────────────────────────────────────┐
│   (跨 Pipeline 快取：ATR、Regime、Session、Volatility…)              │
│                                                                      │
├── PipelineDef("wick_v4",  weight=0.5)                               │
│   └── TradingPipeline                                                │
│       ├── RegimeStage([regime_comp, session_comp], allowed={...})        │
│       ├── AlphaStage([WickReversalV4Signal()], mode="AND")          │
│       ├── RRStage(min_rr=1.5, atr_comp=atr_comp)                    │
│       └── FeeStage(taker_rate=0.0005, min_net_rr=1.2)              │
│                                                          ↑ 快取共用  │
├── PipelineDef("order_flow", weight=0.5)                             │
│   └── TradingPipeline                                                │
│       ├── RegimeStage(regime_comp, allowed=["ranging"])  ────────────┘
│       ├── AlphaStage([OrderFlowSignal(), VolumeSignal()], mode="SCORE")
│       ├── RRStage(min_rr=1.2)
│       └── FeeStage(min_net_rr=1.0)
│
└── shared_risk: RiskModule  ← 全域風控（最後一道關卡）
```

### 資料流

```
klines + idx + equity
        ↓
  SharedContext.invalidate()   ← 新 K 棒，清快取
        ↓
  對每條 PipelineDef：
    PipelineContext 建立（注入 shared 參考）
        ↓
    [ Stage 1 ] → ctx or None
        ↓
    [ Stage 2 ] → ctx or None
        ↓
    [ Stage N ] → ctx or None
        ↓
  成功 → PipelineResult
        ↓
  conflict 解決（all / priority / vote）
        ↓
  shared_risk.allow_entry() 全域風控
        ↓
  list[PipelineResult]
```

---

## 2. 核心概念

### 2.1 PipelineContext — 流動狀態容器

Pipeline 的核心是一個在各 Stage 之間傳遞的 `PipelineContext`。

- 每個 Stage **讀取**前面 Stage 填入的欄位，**寫入**自己負責的欄位。
- 任一 Stage 回傳 `None` 即代表「此 K 棒不進場」，Pipeline 停止。
- `ctx.shared` 是全域共享快取（`SharedContext`），所有 Pipeline 共用同一個實例。

### 2.2 Component vs Stage — 計算與過濾的分離

| | SharedComponent | PipelineStage |
|---|---|---|
| **職責** | 昂貴的計算（ATR、Regime…） | 策略特定的過濾邏輯 |
| **快取** | 結果存入 SharedContext | 讀取 SharedContext，不快取 |
| **可共用** | 是，多個 Stage 引用同一個 | 否，每條 Pipeline 各自的過濾邏輯 |
| **策略偏好** | 無（純計算） | 有（允許哪些 regime、最低 RR…） |

**範例**：`ATRComponent` 計算 ATR 一次，`RRStage` 和 `RegimeComponent` 都可以讀取。

### 2.3 SharedContext — 跨 Pipeline 快取

```python
# 第一個 Pipeline 計算（快取 miss）—— tick_map 透過 lambda closure 傳入
result = ctx.shared.get_or_compute(
    "regime",
    lambda: regime_comp.compute(ctx.klines, ctx.idx, ctx.tick_map),
)

# 第二個 Pipeline 直接讀取（快取 hit，0 計算成本）
result = ctx.shared.get_or_compute(
    "regime",
    lambda: regime_comp.compute(ctx.klines, ctx.idx, ctx.tick_map),
)
```

`SharedContext` 在每根 K 棒開始時由 `MultiPipelineRunner` 自動清除。
同一次 `run_all()` 中所有 Pipeline 共用同一個 `tick_map`，快取鍵不含 `tick_map`，不會發生混用。

---

## 3. 內建 Components

所有 Component 都在 `strategies/pipeline/component.py`。

**Tick 支援說明**：所有 Component 的 `compute()` 均接受 `tick_map=None`。
- 標記 **tick-first** 的 Component 在有 tick 時精確計算，無 tick 時自動 kline fallback。
- 標記 **純 kline** 的 Component 忽略 `tick_map`，計算結果與有無 tick 無關。

| Component | component_id | Tick 支援 |
|-----------|-------------|----------|
| `ATRComponent` | `atr_{period}` | 純 kline |
| `RegimeComponent` | `regime` | 純 kline |
| `SessionComponent` | `session` | 純 kline（時間戳） |
| `VolatilityComponent` | `volatility_{period}` | 純 kline |
| `MicroVolatilityComponent` | `micro_volatility_{period}_l{N}` | L2 snapshot-first + kline fallback |
| `TickDeltaComponent` | `tick_delta` | **tick-first** + kline fallback |
| `TickVWAPComponent` | `tick_vwap` | **tick-first** + kline fallback |

---

### ATRComponent

```python
ATRComponent(period=14)
```

| 回傳鍵 | 說明 |
|--------|------|
| `atr` | 絕對 ATR（price unit） |
| `atr_pct` | ATR 占收盤價的百分比 |

`component_id = "atr_{period}"`，同 period 的實例共用同一個快取槽。

---

### RegimeComponent

```python
RegimeComponent(
    ema_period=50,
    atr_period=14,
    slope_threshold=0.0003,   # EMA 斜率判趨勢門檻
    vol_threshold_pct=3.0,    # ATR% 超過此值判 volatile
)
```

| 回傳鍵 | 說明 |
|--------|------|
| `regime` | `"trending_bull"` / `"trending_bear"` / `"ranging"` / `"volatile"` |
| `ema_slope` | 5 bar EMA 相對斜率 |
| `ema` | 當前 EMA 值 |
| `atr_pct` | ATR% |

`component_id = "regime"`

---

### SessionComponent

```python
SessionComponent()
```

| 回傳鍵 | 說明 |
|--------|------|
| `session` | `"asian"` / `"london"` / `"ny"` / `"overlap"` / `"off"` |
| `active_sessions` | 所有重疊時段的列表 |
| `utc_hour` | UTC 小時數 |

`component_id = "session"`

---

### VolatilityComponent

```python
VolatilityComponent(period=20, lookback=100)
```

| 回傳鍵 | 說明 |
|--------|------|
| `realized_vol` | 滾動 realized volatility（log return std） |
| `vol_percentile` | 目前波動率在歷史中的百分位（0~100） |

`component_id = "volatility_{period}"`

---

### MicroVolatilityComponent

```python
from core.micro_volatility import MicroVolatilityEngine
from strategies.pipeline import MicroVolatilityComponent

# 實盤：每個 L2 / trade 切片直接餵 engine，O(1) rolling update。
engine = MicroVolatilityEngine(window_size=15, normalization_window=100, top_n=10)
mfi = engine.update(orderbook_snapshot, trade_snapshot)

# Pipeline / 回測：用 open_time 對齊快照。
micro_vol_comp = MicroVolatilityComponent(
    period_label="15m",
    window_size=15,
    normalization_window=100,
    top_n=10,
    snapshot_map={
        kline_open_time: {
            "orderbook": {
                "best_bid_price": 50000.0,
                "best_ask_price": 50000.5,
                "bids_volume_top_N": 120.0,
                "asks_volume_top_N": 110.0,
            },
            "trade": {
                "taker_buy_volume": 35.0,
                "taker_sell_volume": 20.0,
            },
        },
    },
)
```

預設 `window_size=15` 代表 1m K 線下的 15 分鐘窗口；如果輸入是 tick 或秒級切片，請把 `window_size` 改成該週期內的樣本數。三個子指標方向已統一為「越大越脆弱」：

| 子指標 | 計算 |
|--------|------|
| `spread_variance` | `best_ask_price - best_bid_price` 的 rolling std |
| `depth_depletion` | `max(prev_total_depth - total_depth, 0)`，深度快速抽離越大越脆弱 |
| `ofi_variance` | L1/top-N book OFI + taker buy/sell imbalance 的 rolling std |

| 回傳鍵 | 說明 |
|--------|------|
| `micro_fragility_index` | 加權後的 Micro-Fragility Index |
| `spread_zscore` | spread variance 的標準化值 |
| `depth_depletion_zscore` | depth depletion 的標準化值 |
| `ofi_zscore` | OFI variance 的標準化值 |
| `spread` | 當前 bid/ask spread |
| `total_depth` | bid/ask top-N 總深度 |
| `ofi` | 當前 order flow imbalance |
| `source` | `"snapshot"` / `"kline_fallback"` / `"missing_snapshot"` |

`component_id = "micro_volatility_{period}_l{N}"`，例如預設為 `micro_volatility_15m_l10`。沒有 L2 快照時，component 會用 K 線做低精度 fallback，`source` 會明確標記；若要避免 fallback，設定 `use_kline_fallback=False`。

---

### TickDeltaComponent

```python
TickDeltaComponent()
```

tick-first：有 tick 時逐筆計算精確 delta；無 tick 時用 `taker_buy_volume` 估算。

| 回傳鍵 | 說明 | 有 tick | 無 tick |
|--------|------|---------|---------|
| `delta` | 買壓 − 賣壓 | 精確 | taker_buy_vol 估算 |
| `buy_vol` | 買方主動成交量 | 精確 | `taker_buy_volume` |
| `sell_vol` | 賣方主動成交量 | 精確 | `volume - taker_buy_volume` |
| `imbalance` | delta / total，-1~1 | 精確 | 估算 |
| `source` | 資料來源 | `"tick"` | `"kline_fallback"` |

`component_id = "tick_delta"`

> `is_buyer_maker=True` → 買方掛單（被動），賣方主動吃單 → sell aggressor  
> `is_buyer_maker=False` → 賣方掛單（被動），買方主動吃單 → buy aggressor

---

### TickVWAPComponent

```python
TickVWAPComponent()
```

tick-first：有 tick 時計算精確成交量加權均價；無 tick 時用 `(H+L+C)/3` 近似。

| 回傳鍵 | 說明 | 有 tick | 無 tick |
|--------|------|---------|---------|
| `vwap` | 成交量加權均價 | 精確 | (H+L+C)/3 近似 |
| `vwap_dev` | (close-vwap)/vwap | 精確 | 近似 |
| `tick_count` | 本根 K 棒 tick 數 | 實際值 | `0` |
| `source` | 資料來源 | `"tick"` | `"kline_fallback"` |

`component_id = "tick_vwap"`

---

## 4. 內建 Stages

### RegimeStage

```python
# 單一 trend regime 維度，保留舊用法
RegimeStage(
    component=RegimeComponent(),
    allowed_regimes=["trending_bull", "trending_bear"],
)

# 多維度 regime：trend + session
RegimeStage(
    components=[RegimeComponent(), SessionComponent()],
    allowed={
        "trend": ["trending_bull", "trending_bear"],
        "session": ["london", "ny", "overlap"],
    },
)
```

- 讀取對應 component 的 SharedContext cache，不符合白名單則阻斷。
- `SessionComponent` 是 `RegimeClassifier` 的一個維度，不再有獨立 `session stage`。
- trend 維度填入 `ctx.regime` 與 `ctx.regime_meta["regime"]`。
- session 維度填入 `ctx.regime_meta["session"]`，並同步記錄在 `ctx.regime_meta["regime_dimensions"]["session"]`。

---

### AlphaStage

```python
# AND 模式（預設）：所有模組同意才過
AlphaStage(
    modules=[SignalA(), SignalB()],
    mode="AND",
)

# SCORE 模式：加權投票
AlphaStage(
    modules=[SignalA(), SignalB(), SignalC()],
    mode="SCORE",
    min_score=0.6,          # 得票比例門檻
    weights=[2.0, 1.0, 1.0], # 各模組投票權重
)
```

- 填入 `ctx.direction`、`ctx.entry_price`、`ctx.stop_price`、`ctx.alpha_score`。
- SCORE 模式下 `entry_price` 是同方向模組的**加權平均**成交價。
- 任一模組方向衝突（AND 模式）→ 阻斷。

---

### RRStage

```python
RRStage(
    exit_cfg=ExitConfig(tp_rr_ratio=2.0),
    capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
    min_rr=1.5,
    atr_component=ATRComponent(14),  # 可選，ATR 輔助 TP 計算
    use_atr_tp=False,                # True：取 config RR 與 ATR 推算的較大值
)
```

- 填入 `ctx.tp_price`、`ctx.expected_rr`、`ctx.qty`、`ctx.risk_amount`。
- `expected_rr < min_rr` 則阻斷。
- `qty = None`（資金不足以支撐最小倉位）則阻斷。

---

### FeeStage

```python
FeeStage(
    taker_rate=0.0005,     # Binance USDT Perp taker 費率 (0.05%)
    slippage_rate=0.0002,  # 單邊滑點保守估計 (0.02%)
    min_net_rr=1.2,        # 扣費後最低 net RR
)
```

- 估算雙邊費用：`(entry_notional + exit_notional) × (taker_rate + slippage_rate)`
- 填入 `ctx.expected_fee`、`ctx.net_reward`、`ctx.fee_approved`。
- `net_rr < min_net_rr` 則阻斷。

---

### TickFactorStage

```python
from strategies.pipeline import TickDeltaComponent, TickVWAPComponent, TickFactorStage

TradingPipeline([
    RegimeStage(...),
    TickFactorStage(component=TickDeltaComponent()),   # 計算並快取 tick_delta
    TickFactorStage(component=TickVWAPComponent()),    # 計算並快取 tick_vwap
    AlphaStage(modules=[MyTickSignal()]),              # 從 SharedContext 讀取
    RRStage(...),
    FeeStage(...),
])
```

- **不阻斷**：永遠回傳 `ctx`，僅確保 tick 因子在 `SharedContext` 中就位。
- 計算結果同時存入 `ctx.alpha_meta["tick_factors"][component_id]`，方便日誌讀取。
- 需要阻斷邏輯請使用自定義 Stage 直接呼叫 `ctx.shared.get_or_compute()`（見第 14 章）。

---

## 5. 組建第一條 Pipeline

```python
from strategies.pipeline import (
    TradingPipeline, PipelineDef,
    RegimeComponent, ATRComponent, SessionComponent,
    RegimeStage, AlphaStage, RRStage, FeeStage,
)
from strategies.modules import ExitConfig, CapitalConfig
from strategies.modules.signal_trigger import StrategySignalModule
from strategies.wick_reversal_v4 import WickReversalV4Strategy

# ── 步驟 1：建立共享 Component 實例（實例化一次，多 Pipeline 共用）──
regime_comp  = RegimeComponent(ema_period=50, slope_threshold=0.0003)
atr_comp     = ATRComponent(period=14)
session_comp = SessionComponent()

# ── 步驟 2：建立 Alpha Signal ──────────────────────────────────────────
wick_signal = StrategySignalModule(WickReversalV4Strategy())

# ── 步驟 3：組建 Pipeline ─────────────────────────────────────────────
pipeline = TradingPipeline([
    RegimeStage(
        components=[regime_comp, session_comp],
        allowed={
            "trend": ["trending_bull", "trending_bear"],
            "session": ["london", "ny", "overlap"],
        },
    ),
    AlphaStage(
        modules=[wick_signal],
        mode="AND",
    ),
    RRStage(
        exit_cfg=ExitConfig(tp_rr_ratio=2.0),
        capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
        min_rr=1.5,
        atr_component=atr_comp,
    ),
    FeeStage(taker_rate=0.0005, slippage_rate=0.0002, min_net_rr=1.2),
])

# ── 步驟 4：包裝為 PipelineDef ────────────────────────────────────────
defn = PipelineDef(
    name="wick_v4_trend",
    pipeline=pipeline,
    allocation_weight=1.0,
    tags=["trend", "reversal"],
)
```

---

## 6. 多策略管理：MultiPipelineRunner

```python
from strategies.pipeline import MultiPipelineRunner
from strategies.modules import RiskConfig, RiskModule

runner = MultiPipelineRunner(
    defs=[
        PipelineDef(name="wick_trend",   pipeline=pipeline_a, allocation_weight=0.5),
        PipelineDef(name="order_flow",   pipeline=pipeline_b, allocation_weight=0.5),
        PipelineDef(name="mean_rev",     pipeline=pipeline_c, allocation_weight=0.3,
                    direction_filter="long", enabled=False),  # 暫時關閉
    ],
    shared_risk=RiskModule(RiskConfig(
        max_daily_loss_pct=5.0,
        max_drawdown_pct=15.0,
    )),
    conflict="all",   # "all" | "priority" | "vote"
)

# 執行
results = runner.run_all(klines=klines, idx=i, equity=10_000.0)
for r in results:
    print(f"[{r.pipeline_name}] {r.ctx.direction} @ {r.ctx.entry_price:.2f}"
          f"  RR={r.ctx.expected_rr:.2f}  fee={r.ctx.expected_fee:.2f}")

# 交易結束後更新風控
runner.update_risk(trade_pnl=-50.0)

# 每日收盤後重置
runner.reset_daily_risk()
```

### conflict 模式說明

| 模式 | 行為 | 適用場景 |
|------|------|----------|
| `"all"` | 所有通過的 Pipeline 都執行 | 多策略獨立組合，各自倉位 |
| `"priority"` | 每個方向只取 defs 順序第一個 | 策略有優先級，同方向避免重複 |
| `"vote"` | 超過半數同意才執行（取第一個） | 保守型，強調確認 |

---

## 7. 接入回測引擎

### 方式 A：MultiPipelineStrategy（推薦）

```python
from strategies.pipeline import MultiPipelineStrategy

strategy = MultiPipelineStrategy(
    runner=runner,
    exit_mod=ExitModule(ExitConfig(tp_rr_ratio=2.0, use_trailing_stop=True)),
    initial_equity=10_000.0,
)

# 與現有策略完全相同的介面
signals = strategy.on_history(klines, tick_map=None)
stats   = strategy.compute_stats(signals)
```

### 方式 B：直接使用 runner（進階）

```python
# 在自定義回測迴圈中使用
for i in range(1, len(klines)):
    results = runner.run_all(klines, i, equity=current_equity)
    for r in results:
        # 自行管理持倉、出場、資金
        ...
```

---

## 8. 自定義 Component

只需繼承 `SharedComponent`，實作 `compute()` 即可。
簽名必須包含 `tick_map=None`，即使不使用 tick 也要保留此參數。

```python
from collections.abc import Mapping
from typing import Optional
import numpy as np
from strategies.pipeline.component import SharedComponent
from core.data_types import Kline

TickBarMap = Mapping[int, np.ndarray]

class CVDRollingComponent(SharedComponent):
    """跨 K 棒累積 Delta（CVD）。tick-first，kline fallback。"""

    def __init__(self, lookback_bars: int = 20) -> None:
        self.lookback = lookback_bars
        self.component_id = f"cvd_rolling_{lookback_bars}"  # 必須全域唯一

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,   # ← 必須宣告，即使不用
    ) -> dict:
        start  = max(0, idx - self.lookback + 1)
        window = klines[start : idx + 1]
        cumulative_delta = 0.0

        for k in window:
            if tick_map is not None and k.open_time in tick_map:
                ticks    = tick_map[k.open_time]
                is_bm    = ticks[:, 3].astype(bool)
                buy_vol  = float(np.sum(ticks[~is_bm, 2]))
                sell_vol = float(np.sum(ticks[is_bm,  2]))
            else:
                buy_vol  = k.taker_buy_volume
                sell_vol = k.volume - k.taker_buy_volume
            cumulative_delta += buy_vol - sell_vol

        total_vol = sum(k.volume for k in window)
        return {
            "cvd":            cumulative_delta,
            "cvd_normalized": cumulative_delta / (total_vol + 1e-10),
        }
```

**使用規則**：
- `component_id` 必須全域唯一，作為 `SharedContext` 的快取鍵。
- `compute()` 必須是純函式：同樣的輸入必然得到相同結果。
- `tick_map=None` 時必須提供合理 fallback，不得拋出例外。
- 不要在 `compute()` 內修改 `klines` 或任何外部狀態。

---

## 9. 自定義 Stage

繼承 `PipelineStage`，實作 `process(ctx)`：

```python
from strategies.pipeline.stages import PipelineStage
from strategies.pipeline.context import PipelineContext
from typing import Optional

class VolatilityFilterStage(PipelineStage):
    """只在波動率百分位在指定範圍內才進場。"""

    name = "VolatilityFilterStage"

    def __init__(self, component: VolatilityComponent,
                 min_pct: float = 20.0, max_pct: float = 80.0) -> None:
        self.component = component
        self.min_pct   = min_pct
        self.max_pct   = max_pct

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        result = ctx.shared.get_or_compute(
            self.component.component_id,
            lambda: self.component.compute(ctx.klines, ctx.idx),
        )
        pct = result["vol_percentile"]
        if self.min_pct <= pct <= self.max_pct:
            ctx.regime_meta["vol_percentile"] = pct
            return ctx
        return None
```

**Stage 開發守則**：
1. 只讀取上游 Stage 已填入的欄位，只寫入本 Stage 負責的欄位。
2. 過濾失敗時回傳 `None`，不要 raise exception。
3. 不持有 klines 的參考或任何跨 K 棒狀態。
4. 昂貴計算必須透過 `ctx.shared.get_or_compute()` 快取。

---

## 10. 自定義 AlphaSignal

實作 `SignalModule`（現有介面，完全不變）：

```python
from strategies.modules.signal_trigger import SignalModule
from strategies.base import StrategySignal
from core.data_types import Kline
from typing import Optional

class OrderFlowSignal(SignalModule):
    """基於訂單流失衡的進場訊號。"""

    name = "OrderFlow"

    def __init__(self, of_component: OrderFlowComponent,
                 imbalance_threshold: float = 0.3) -> None:
        self._comp      = of_component
        self._threshold = imbalance_threshold

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        # 注意：detect_k0 拿不到 ctx.shared，需自行計算或接受 component 傳入
        result = self._comp.compute(klines, idx)
        imbalance = result["imbalance"]
        if imbalance > self._threshold:
            return {"direction": "long",  "k0_idx": idx, "imbalance": imbalance}
        if imbalance < -self._threshold:
            return {"direction": "short", "k0_idx": idx, "imbalance": imbalance}
        return None

    def entry_conditions(self, klines, k0_idx, k0_meta, tick_map=None) -> Optional[StrategySignal]:
        k = klines[k0_idx]
        direction = k0_meta["direction"]
        stop_price = k.low if direction == "long" else k.high
        return StrategySignal(
            open_time   = k.open_time,
            price       = k.close,
            signal_type = "long_entry" if direction == "long" else "short_entry",
            label       = self.name,
            stop_price  = stop_price,
            fill_price  = k.close,
        )
```

> **提示**：若 `SignalModule` 需要共享快取，可在建構時接受 component 實例，
> 在 `detect_k0` / `entry_conditions` 內直接呼叫 `component.compute()`。
> 雖然不走 SharedContext，但 component 自身可實作輕量快取（若需要）。

---

## 11. SharedContext 快取機制

### 快取生命週期

```
K 棒 N：
  invalidate() → 清空所有快取
  Pipeline A 的 RegimeStage → 計算並快取 "regime"
  Pipeline A 的 RRStage     → 計算並快取 "atr_14"
  Pipeline B 的 RegimeStage → 快取命中 "regime"（0 計算成本）
  Pipeline B 的 RRStage     → 快取命中 "atr_14"（0 計算成本）

K 棒 N+1：
  invalidate() → 清空所有快取
  ...（重新計算）
```

### component_id 命名規則

| 格式 | 範例 |
|------|------|
| 無參數 | `"regime"`, `"session"` |
| 含期數 | `"atr_14"`, `"volatility_20"` |
| 自定義 | `"order_flow"`, `"funding_rate"` |

**衝突警告**：不同 component 不得使用相同 `component_id`，否則會讀到錯誤快取。

### 手動操作快取（測試用）

```python
ctx.shared.set("regime", {"regime": "trending_bull", "ema_slope": 0.001, ...})
cached = ctx.shared.get("atr_14")
exists = ctx.shared.has("session")
```

---

## 12. 手續費計算說明

FeeStage 使用**雙邊 taker + 雙邊滑點**的保守估計：

```
entry_notional = entry_price × qty
exit_notional  = tp_price    × qty          ← 假設 TP 出場，保守起見用 tp
total_fee      = (entry_notional + exit_notional) × (taker_rate + slippage_rate)

expected_reward = |tp_price - entry_price| × qty
net_reward      = expected_reward - total_fee
net_rr          = net_reward / (|stop_price - entry_price| × qty)
```

### 常用費率參考

| 場景 | taker_rate | slippage_rate |
|------|-----------|---------------|
| Binance USDT Perp（一般用戶） | 0.0005 | 0.0002 |
| Binance USDT Perp（VIP 3+） | 0.00036 | 0.0001 |
| 回測保守估計（含衝擊成本） | 0.0005 | 0.0005 |

### 獨立使用 FeeModule

Pipeline 外場景（CompositeStrategy、手動計算）：

```python
from strategies.modules import FeeModule, FeeConfig

fee = FeeModule(FeeConfig(taker_rate=0.0005, slippage_rate=0.0002, min_net_rr=1.2))
approved, detail = fee.approve(
    entry_price=50_000.0,
    stop_price=49_500.0,
    tp_price=51_000.0,
    qty=0.1,
)
print(approved)            # True / False
print(detail["net_rr"])    # 扣費後 net RR
```

---

## 13. 最佳實踐與注意事項

### Component 共用

```python
# ✅ 正確：同一個 component 實例傳給多個 Stage
atr  = ATRComponent(14)
defA = PipelineDef("a", TradingPipeline([..., RRStage(atr_component=atr)]))
defB = PipelineDef("b", TradingPipeline([..., RRStage(atr_component=atr)]))

# ❌ 錯誤：不同實例雖然 component_id 相同，仍共用快取，但語意混亂
atr_a = ATRComponent(14)
atr_b = ATRComponent(14)   # 與 atr_a 使用同一快取槽（component_id 相同）
```

### allocation_weight 總和

`allocation_weight` 各 Pipeline 加總可以超過 1.0（代表多個策略同時滿倉）。
如果希望強制總風險上限，應透過 `CapitalConfig.max_risk_pct` 控制每筆交易風險，
或在 `shared_risk` 的 `RiskModule` 設定整體回撤上限。

### Stage 順序建議

```
1. RegimeStage   ← 最輕量的過濾，放最前
2. AlphaStage                   ← 訊號偵測（可能較重）
3. RRStage                      ← 需要 Alpha 的結果
4. FeeStage                     ← 最後，需要 RR 的結果
```

### 不要在 Stage 內維護跨 K 棒狀態

```python
# ❌ 錯誤：Stage 不是持倉追蹤器
class BadStage(PipelineStage):
    def __init__(self):
        self.last_signal = None  # 跨 K 棒狀態放在 Stage 是錯的

# ✅ 正確：跨 K 棒狀態由 MultiPipelineStrategy 或自定義引擎管理
```

---

## 14. Tick 資料整合

### 14.1 Tick 資料在 Pipeline 中的流動路徑

```
runner.run_all(klines, idx, equity, tick_map=tick_map)
        ↓
PipelineContext.tick_map = tick_map
        ↓
Stage.process(ctx) 內：
  ctx.shared.get_or_compute(
      component_id,
      lambda: component.compute(ctx.klines, ctx.idx, ctx.tick_map)  ← 傳入
  )
        ↓
AlphaStage → SignalModule.entry_conditions(klines, k0_idx, k0_meta, ctx.tick_map)
```

**重要**：`tick_map` 在同一次 `run_all()` 中對所有 Pipeline 是同一個物件，快取不會因為 tick_map 不同而衝突。

### 14.2 tick_map 格式

```python
# TickBarMap: open_time_ms → ndarray(N, 4)
# 欄位：[trade_time_ms, price, qty, is_buyer_maker]
#
# is_buyer_maker 語意（Binance）：
#   True  (1.0) → 買方為掛單方（maker），賣方主動吃單 → sell aggressor
#   False (0.0) → 賣方為掛單方（maker），買方主動吃單 → buy aggressor

ticks = tick_map[kline.open_time]   # ndarray shape (N, 4)
buy_vol  = np.sum(ticks[ticks[:, 3] == 0, 2])   # is_buyer_maker=False → buy
sell_vol = np.sum(ticks[ticks[:, 3] == 1, 2])   # is_buyer_maker=True  → sell
```

### 14.3 內建 Tick Components

#### TickDeltaComponent

```python
from strategies.pipeline import TickDeltaComponent, TickFactorStage

delta_comp = TickDeltaComponent()

pipeline = TradingPipeline([
    RegimeStage(...),
    TickFactorStage(component=delta_comp),   # 計算並快取 tick delta
    AlphaStage(modules=[MyDeltaSignal()]),   # 從 SharedContext 讀取
    RRStage(...),
    FeeStage(...),
])
```

| 回傳鍵 | 說明 | 有 tick | 無 tick |
|--------|------|---------|---------|
| `delta` | 買壓 − 賣壓 | 精確 | taker_buy_vol 估算 |
| `buy_vol` | 買方主動成交量 | 精確 | taker_buy_volume |
| `sell_vol` | 賣方主動成交量 | 精確 | volume - taker_buy_volume |
| `imbalance` | delta / total，-1~1 | 精確 | 估算 |
| `source` | 資料來源 | `"tick"` | `"kline_fallback"` |

#### TickVWAPComponent

```python
from strategies.pipeline import TickVWAPComponent, TickFactorStage

vwap_comp = TickVWAPComponent()
```

| 回傳鍵 | 說明 | 有 tick | 無 tick |
|--------|------|---------|---------|
| `vwap` | 成交量加權均價 | 精確 | (H+L+C)/3 近似 |
| `vwap_dev` | (close-vwap)/vwap | 精確 | 近似 |
| `tick_count` | 本根 K 棒 tick 數 | 實際值 | 0 |
| `source` | 資料來源 | `"tick"` | `"kline_fallback"` |

### 14.4 在 AlphaSignal 內讀取 Tick Factors

`TickFactorStage` 將結果存入 `ctx.alpha_meta["tick_factors"]`，但 `SignalModule.detect_k0()` 沒有 ctx 參數。推薦的讀取方式：

**方式 A：Component 直接傳入 Signal（推薦）**

```python
class TickDeltaSignal(SignalModule):
    name = "TickDelta"

    def __init__(self, component: TickDeltaComponent, threshold: float = 0.3):
        self._comp      = component
        self.threshold  = threshold

    def detect_k0(self, klines, idx) -> Optional[dict]:
        # detect_k0 沒有 tick_map，先只做 K 線結構判斷
        k = klines[idx]
        if abs(k.close - k.open) < (k.high - k.low) * 0.3:
            return None  # 十字星，跳過
        direction = "long" if k.close > k.open else "short"
        return {"direction": direction, "k0_idx": idx}

    def entry_conditions(self, klines, k0_idx, k0_meta, tick_map=None):
        # entry_conditions 有 tick_map，可做精確判斷
        result = self._comp.compute(klines, k0_idx, tick_map)
        imbalance = result["imbalance"]
        direction = k0_meta["direction"]

        if direction == "long"  and imbalance < self.threshold:
            return None  # 買壓不足
        if direction == "short" and imbalance > -self.threshold:
            return None  # 賣壓不足

        k = klines[k0_idx]
        return StrategySignal(
            open_time   = k.open_time,
            price       = k.close,
            signal_type = f"{direction}_entry",
            label       = self.name,
            stop_price  = k.low if direction == "long" else k.high,
            fill_price  = k.close,
            meta        = {"imbalance": imbalance, "source": result["source"]},
        )
```

**方式 B：直接從 SharedContext 讀取（進階）**

```python
# 若 TickFactorStage 已在前面執行，結果已在快取
# 但 detect_k0 / entry_conditions 拿不到 ctx.shared，
# 因此此方式只適合自定義 Stage
class TickFilterStage(PipelineStage):
    def __init__(self, component: TickDeltaComponent, min_imbalance: float = 0.2):
        self._comp = component
        self.min_imbalance = min_imbalance

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        result = ctx.shared.get_or_compute(
            self._comp.component_id,
            lambda: self._comp.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )
        direction = ctx.direction  # AlphaStage 已填入
        if direction == "long"  and result["imbalance"] < self.min_imbalance:
            return None
        if direction == "short" and result["imbalance"] > -self.min_imbalance:
            return None
        ctx.alpha_meta["tick_imbalance"] = result["imbalance"]
        return ctx
```

### 14.5 自定義 Tick Component

```python
class CVDRollingComponent(SharedComponent):
    """跨 K 棒累積 Delta（CVD）。"""

    def __init__(self, lookback_bars: int = 20) -> None:
        self.lookback = lookback_bars
        self.component_id = f"cvd_rolling_{lookback_bars}"

    def compute(self, klines, idx, tick_map=None) -> dict:
        start = max(0, idx - self.lookback + 1)
        window = klines[start : idx + 1]
        cumulative_delta = 0.0

        for k in window:
            if tick_map is not None and k.open_time in tick_map:
                ticks = tick_map[k.open_time]
                is_bm    = ticks[:, 3].astype(bool)
                sell_vol = float(np.sum(ticks[is_bm,  2]))
                buy_vol  = float(np.sum(ticks[~is_bm, 2]))
            else:
                buy_vol  = k.taker_buy_volume
                sell_vol = k.volume - k.taker_buy_volume
            cumulative_delta += buy_vol - sell_vol

        return {
            "cvd":           cumulative_delta,
            "cvd_normalized": cumulative_delta / (sum(k.volume for k in window) + 1e-10),
        }
```

### 14.6 回測 vs 實盤的 tick_map 行為

| 場景 | tick_map | Component 行為 |
|------|----------|----------------|
| 回測（有歷史 tick） | `{open_time: ndarray}` | 精確計算，`source="tick"` |
| 回測（僅 kline） | `None` | kline fallback，`source="kline_fallback"` |
| 實盤（快速模式） | `None` | kline fallback，結果略有差異 |
| 實盤（完整模式） | 即時 tick buffer | 精確計算 |

所有內建 tick component 都保證在 `tick_map=None` 時仍能執行，**不會拋出例外**。

---

## 15. 故障排查

### Pipeline 永遠回傳 None

逐一確認各 Stage 的過濾條件：

```python
# 臨時除錯：在 Stage 前插入印出 Stage
class DebugStage(PipelineStage):
    name = "DebugStage"
    def __init__(self, label: str):
        self.label = label
    def process(self, ctx):
        print(f"[{self.label}] regime={ctx.regime}, dir={ctx.direction}, "
              f"entry={ctx.entry_price}, rr={ctx.expected_rr}")
        return ctx

pipeline = TradingPipeline([
    RegimeStage(...),
    DebugStage("after_regime"),
    AlphaStage(...),
    DebugStage("after_alpha"),
    ...
])
```

### SharedContext 快取不命中

確認多個 Pipeline 使用的是**同一個 Component 實例**（`id()` 相同），且 `component_id` 字串一致。

```python
regime_a = RegimeComponent()
regime_b = RegimeComponent()
print(regime_a.component_id == regime_b.component_id)  # True（字串相同，快取共用）
print(regime_a is regime_b)                             # False（不同實例，但無影響）
```

### FeeStage 阻斷太頻繁

調低 `min_net_rr` 或增大 RR（`tp_rr_ratio`）；確認費率設定符合實際帳戶等級。

### allocation_weight 校驗失敗

`allocation_weight` 必須在 `(0, 1]` 之間，`0.0` 不允許（代表無資金）。

---

## 15. 欄位速查表

### PipelineContext 欄位

| 欄位 | 類型 | 填入者 | 說明 |
|------|------|--------|------|
| `klines` | `list[Kline]` | Runner | 完整 K 棒序列 |
| `idx` | `int` | Runner | 當前 K 棒索引 |
| `equity` | `float` | Runner | 分配給此 Pipeline 的資金 |
| `tick_map` | `Optional[TickBarMap]` | Runner | 逐 tick 資料（可 None） |
| `pipeline_name` | `str` | Runner | PipelineDef.name |
| `pipeline_weight` | `float` | Runner | PipelineDef.allocation_weight |
| `shared` | `SharedContext` | Runner | 跨 Pipeline 共享快取（by ref） |
| `regime` | `Optional[str]` | RegimeStage | 市場狀態 |
| `regime_meta` | `dict` | RegimeStage | Regime 完整結果（含 session） |
| `direction` | `Optional[str]` | AlphaStage | `"long"` / `"short"` |
| `entry_price` | `Optional[float]` | AlphaStage | 計畫進場價 |
| `stop_price` | `Optional[float]` | AlphaStage | 計畫停損價 |
| `alpha_score` | `float` | AlphaStage | 0~1，SCORE 模式投票比例 |
| `alpha_meta` | `dict` | AlphaStage | Alpha 詳情 |
| `tp_price` | `Optional[float]` | RRStage | 計算的止盈價 |
| `expected_rr` | `Optional[float]` | RRStage | 預期 RR 倍數 |
| `qty` | `Optional[float]` | RRStage | 合約數量 |
| `risk_amount` | `Optional[float]` | RRStage | 風險金額（USD） |
| `expected_fee` | `Optional[float]` | FeeStage | 預期總費用（USD） |
| `net_reward` | `Optional[float]` | FeeStage | 扣費後預期獲利（USD） |
| `fee_approved` | `bool` | FeeStage | 費用核算通過標記 |

### PipelineResult 欄位

| 欄位 | 說明 |
|------|------|
| `pipeline_name` | 來源 Pipeline 名稱 |
| `ctx` | 完整 PipelineContext（所有 Stage 結果） |
| `entry_signal` | 可直接送入引擎的 StrategySignal |
| `tags` | 繼承自 PipelineDef.tags |

### StrategySignal.meta 欄位（Pipeline 版）

| meta 鍵 | 來源 |
|---------|------|
| `pipeline` | Pipeline 名稱 |
| `regime` | 市場狀態 |
| `session` | 交易時段 |
| `alpha_score` | Alpha 投票分數 |
| `expected_rr` | 預期 RR |
| `qty` | 倉位大小 |
| `tp_price` | 止盈價 |
| `expected_fee` | 預期費用 |
| `net_reward` | 扣費後獲利 |
