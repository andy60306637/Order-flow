# /backtest — 回測執行 & 報告生成

使用者透過此指令指定策略、時間區間、費率等參數，由 agent 執行回測並產出 Markdown 報告。

## 使用說明

```
/backtest [參數]
```

### 支援參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `strategy` | 策略名稱（見下方清單）| 必填 |
| `start` | 起始日期 `YYYY-MM-DD` | 必填 |
| `end` | 結束日期 `YYYY-MM-DD` | 必填 |
| `symbol` | 交易對 | `BTCUSDT` |
| `interval` | K 線週期 | `1m` |
| `mode` | 回測模式 `tick` / `kline` | `tick` |
| `capital` | 初始資金 (USDT) | `10000` |
| `leverage` | 槓桿倍數 | `20` |
| `fee` | 手續費率（小數，如 0.00032） | `0.00032` |
| `slippage` | 滑價 BPS | `0.2` |
| `max_loss_pct` | 每筆最大虧損比例 | `0.02` |
| `compound` | 複利模式 `true/false` | `true` |

### 範例

```
/backtest strategy="MR Reclaim v1" start=2026-01-01 end=2026-03-01
/backtest strategy="Wick Reversal 1m v6.1" start=2026-01-01 end=2026-02-01 mode=kline fee=0.0005 capital=50000
/backtest strategy="MR Pipeline v1" start=2025-10-01 end=2026-01-01 symbol=ETHUSDT leverage=10
```

---

## Agent 執行協議

當使用者呼叫 `/backtest` 時，依照以下步驟執行：

### Step 0 — 解析參數

從使用者訊息中解析出所有參數。必填欄位缺失時，直接詢問使用者補充。

若使用者未指定策略，先執行以下指令列出可用策略清單，再請使用者選擇：

```python
python -c "
from strategies import STRATEGY_REGISTRY
for name in sorted(STRATEGY_REGISTRY.keys()):
    print(f'  • {name}')
"
```

### Step 1 — 準備輸出路徑

在 `docs/reports/backtest/` 下建立報告：

```python
import os, datetime
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
symbol = "BTCUSDT"          # 替換為實際值
strategy_slug = strategy.replace(" ", "_").replace("/", "-")
out_json = f"docs/reports/backtest/{symbol}_{strategy_slug}_{ts}.json"
out_csv  = f"docs/reports/backtest/{symbol}_{strategy_slug}_{ts}_trades.csv"
os.makedirs("docs/reports/backtest", exist_ok=True)
```

### Step 2 — 執行回測

使用 Bash 執行 `utils/fast_backtest.py`，傳入所有解析到的參數：

```bash
python utils/fast_backtest.py \
  --strategy "<strategy>" \
  --symbol <symbol> \
  --interval <interval> \
  --mode <mode> \
  --start <start> \
  --end <end> \
  --capital <capital> \
  --leverage <leverage> \
  --fee <fee> \
  --slippage <slippage> \
  --out <out_json> \
  --csv <out_csv>
```

**若出現錯誤**，診斷原因並告知使用者，不要靜默失敗。

### Step 3 — 讀取 JSON 結果

```python
import json
with open(out_json) as f:
    report = json.load(f)
meta  = report["meta"]
stats = report["stats"]
```

### Step 4 — 產出 Markdown 報告

依照以下模板格式化輸出，**直接在對話中顯示**（不需另存 .md 除非使用者要求）：

---

````markdown
## 回測報告 — {strategy}

**{symbol} | {interval} | {mode.upper()} mode | {start} → {end}**

### 設定
| 項目 | 值 |
|------|-----|
| 初始資金 | {capital} USDT |
| 槓桿 | {leverage}x |
| 手續費率 | {fee*100:.3f}% |
| 滑價 | {slippage} BPS |
| 複利 | {compound} |

---

### 總體績效
| 指標 | 值 |
|------|-----|
| 交易次數 | {trades} |
| 勝率 | {win_rate:.2f}% |
| 獲利因子 | {profit_factor:.3f} |
| 總淨利 | {total_net_pnl:+.2f} USDT |
| 總報酬率 | {total_return_pct:+.2f}% |
| 最大回撤 | {max_drawdown_pct:.2f}% |
| 最大連虧 | {max_consec_loss} 筆 |
| Sharpe Ratio | {sharpe_ratio:.4f} |
| 最終資金 | {final_equity:.2f} USDT |

---

### 多空分離
| 方向 | 交易數 | 勝率 | 獲利因子 |
|------|--------|------|----------|
| 多單 | {long_trades} | {long_win_rate:.2f}% | {long_profit_factor:.3f} |
| 空單 | {short_trades} | {short_win_rate:.2f}% | {short_profit_factor:.3f} |

---

### 出場方式分佈
| 出場類型 | 交易數 | 勝率 | 淨利 | 獲利因子 |
|----------|--------|------|------|----------|
| SL (停損) | ... | ... | ... | ... |
| TP (止盈) | ... | ... | ... | ... |
| TS (移動停利) | ... | ... | ... | ... |
| TD (時間到期) | ... | ... | ... | ... |

> 出場統計來自 `stats["exit_stats"]`

---

### 費用摘要
| 項目 | 值 |
|------|-----|
| 總手續費 | {total_fees:.2f} USDT |
| 平均勝筆 | {avg_win:.2f} USDT |
| 平均虧筆 | {avg_loss:.2f} USDT |
| 費用覆蓋比 | {total_net_pnl/total_fees if total_fees>0 else 'N/A':.2f}x |

---

### 交易明細
已儲存至：`{out_csv}`

**前 10 筆交易樣本：**

| # | 方向 | 進場時間 | 出場時間 | 進場價 | 出場價 | 淨利 | 出場標籤 |
|---|------|----------|----------|--------|--------|------|----------|
| ... |

---

### 診斷建議

根據以上結果，對策略表現給出 2-3 條具體觀察：
- 若 `profit_factor < 1.2`：提示策略邊際獲利能力薄弱
- 若 `max_drawdown_pct > 30`：提示需要控制倉位或停損
- 若 `win_rate < 40`：提示需要改善 RR 比
- 若 `total_fees / abs(total_net_pnl) > 0.3`：提示費用侵蝕嚴重，建議換更優費率
- 若 `long_trades` 或 `short_trades` 為 0：提示策略單邊偏斜

```
````

---

### Step 5 — 儲存報告（可選）

若使用者要求存檔（如加上 `--save` 或說「存下來」），將完整 Markdown 報告寫入：

```
docs/reports/backtest/{symbol}_{strategy_slug}_{ts}_report.md
```

---

## 注意事項

- **tick 模式**需要本地 tick cache 存在，若資料不存在請提示使用者先執行資料下載。
- **策略名稱**大小寫敏感，與 `STRATEGY_REGISTRY` key 完全匹配。
- 若回測交易數為 0，需診斷原因（常見：日期範圍無資料、策略參數過濾過嚴）。
- `docs/reports/backtest/` 若不存在，執行前先建立。
- JSON report 的 `trade_list` 欄位被排除在 `stats` 外，若需要完整明細請讀 CSV。
- 回測完成後主動告知：JSON 路徑、CSV 路徑、執行耗時。
