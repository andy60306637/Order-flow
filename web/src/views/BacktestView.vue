<template>
  <div class="bt-root bg-bg">
    <aside class="bt-sidebar">
      <div class="panel">
        <h2 class="panel-title">Backtest Dataset</h2>
        <div class="field-grid">
          <label>Symbol</label>
          <select v-model="form.symbol" class="select-field">
            <option v-for="s in availableSymbols" :key="s" :value="s">{{ s }}</option>
          </select>
          <label>Interval</label>
          <select v-model="form.interval" class="select-field">
            <option v-for="i in availableIntervals" :key="i" :value="i">{{ i }}</option>
          </select>
          <label>Start</label>
          <input v-model="form.start_date" type="date" class="input-field" />
          <label>End</label>
          <input v-model="form.end_date" type="date" class="input-field" />
        </div>
        <div v-if="klineRange" class="hint">
          K 線 {{ klineRange.start }} → {{ klineRange.end }} / {{ klineRange.count.toLocaleString() }} bars
        </div>
        <div v-else class="hint text-down">找不到本機 K 線資料</div>
        <div class="hint">
          Tick <span v-if="tickRange">{{ msToDate(tickRange.start_ms) }} → {{ msToDate(tickRange.end_ms) }}</span>
          <span v-else>無覆蓋</span>
        </div>
      </div>

      <div class="panel">
        <h2 class="panel-title">Time Slice</h2>
        <div class="slice-modes">
          <button :class="{ active: form.slice_mode === 'range' }" @click="form.slice_mode = 'range'">Range</button>
          <button :class="{ active: form.slice_mode === 'multi_select' }" @click="form.slice_mode = 'multi_select'">Multi-select</button>
          <button :class="{ active: form.slice_mode === 'walk_forward' }" @click="form.slice_mode = 'walk_forward'">Walk-forward</button>
        </div>
        <div v-if="form.slice_mode !== 'range'" class="month-picker">
          <button v-for="m in availableMonths" :key="m"
                  :class="{ active: form.selected_months.includes(m) }"
                  @click="toggleMonth(m)">{{ m }}</button>
        </div>
        <div v-if="form.slice_mode === 'walk_forward'" class="field-grid mt-2">
          <label>Segments</label>
          <input v-model.number="form.wf_segments" type="number" min="2" max="20" class="input-field" />
          <label>OOS Fraction</label>
          <input v-model.number="form.wf_oos_fraction" type="number" min="0.1" max="0.5" step="0.05" class="input-field" />
          <label>Anchored</label>
          <label class="check-inline"><input v-model="form.wf_anchored" type="checkbox" /> expanding IS</label>
        </div>
        <div class="hint">{{ sliceSummary }}</div>
      </div>

      <div class="panel">
        <h2 class="panel-title">Strategy</h2>
        <div class="field-grid">
          <label>Strategy</label>
          <select v-model="form.strategy_name" class="select-field">
            <option v-for="s in strategies" :key="s" :value="s">{{ s }}</option>
          </select>
          <label>Initial USDT</label>
          <input v-model.number="form.initial_capital" type="number" class="input-field" />
          <label>Max Loss %</label>
          <input v-model.number="form.max_loss_pct" type="number" class="input-field" step="0.1" />
          <label>Leverage</label>
          <input v-model.number="form.leverage" type="number" class="input-field" min="1" max="125" />
          <label>Fee %</label>
          <input v-model.number="form.custom_fee_rate_pct" type="number" class="input-field" step="0.001" />
          <label>Slippage bps</label>
          <input v-model.number="form.slippage_bps" type="number" class="input-field" step="0.1" />
        </div>
        <div class="check-row">
          <label><input v-model="form.use_tick_mode" type="checkbox" /> Tick mode</label>
          <label><input v-model="form.compound" type="checkbox" /> Compound</label>
        </div>
      </div>

      <div class="panel">
        <h2 class="panel-title">Tick Cache</h2>
        <input v-model="tickImportFolder" type="text" class="input-field" placeholder="/data/binance/.../aggTrades/BTCUSDT" />
        <div class="grid grid-cols-2 gap-2 mt-2">
          <button class="btn-ghost" :disabled="tickImporting" @click="importTicksFromFolder">
            {{ tickImporting ? 'Importing' : 'Import' }}
          </button>
          <button class="btn-ghost" :disabled="tickImporting" @click="clearTickCache">Clear</button>
        </div>
      </div>

      <button class="btn-primary w-full" :disabled="running" @click="runBacktest">
        {{ running ? 'Running...' : 'Run Backtest' }}
      </button>
      <div v-if="progress" class="hint">{{ progress }}</div>
      <div v-if="tickImportProgress" class="hint">{{ tickImportProgress }}</div>
      <div v-if="error" class="hint text-down">錯誤：{{ error }}</div>
    </aside>

    <main class="bt-main">
      <section class="metrics-strip">
        <MetricBox v-for="m in summaryMetrics" :key="m.label" v-bind="m" />
      </section>

      <div class="toolbar">
        <span class="text-dim">{{ resultStatus }}</span>
        <button class="btn-ghost compact" :disabled="!activeTradeList.length" @click="openSnapshot(selectedTradeIndex)">Snapshot</button>
        <button class="btn-ghost compact" :disabled="!stats" @click="resultDialogOpen = true">Result / Excel</button>
      </div>

      <section class="center-grid">
        <div class="panel chart-panel equity-panel">
          <h2 class="panel-title">Equity / Drawdown</h2>
          <svg :viewBox="`0 0 ${chartW} ${chartH}`" preserveAspectRatio="none" class="chart-svg">
            <line v-for="g in equityGridY" :key="'ey' + g" x1="42" :y1="g" :x2="chartW - 14" :y2="g" stroke="#2a2e3966" stroke-width="1" />
            <line v-for="g in equityGridX" :key="'ex' + g" :x1="g" y1="16" :x2="g" :y2="chartH - 20" stroke="#2a2e3955" stroke-width="1" />
            <path :d="drawdownAreaPath" fill="#ef535044" stroke="#ef5350" stroke-width="1.2" />
            <path :d="equityPath" fill="none" stroke="#26a69a" stroke-width="2.2" />
            <path :d="initialLinePath" fill="none" stroke="#787b86" stroke-width="1" stroke-dasharray="5 4" />
            <text x="48" y="14" fill="#d1d4dc" font-size="10">Equity (USDT)</text>
            <text x="48" :y="chartH * 0.75" fill="#ef5350" font-size="10">Drawdown %</text>
          </svg>
        </div>

        <div class="right-stack">
          <div class="panel chart-panel">
            <h2 class="panel-title">MFE / MAE</h2>
            <svg :viewBox="`0 0 ${scatterW} ${scatterH}`" preserveAspectRatio="none" class="chart-svg">
              <line v-for="g in scatterGridY" :key="'sy' + g" x1="30" :y1="g" :x2="scatterW - 12" :y2="g" stroke="#2a2e3966" />
              <line v-for="g in scatterGridX" :key="'sx' + g" :x1="g" y1="14" :x2="g" :y2="scatterH - 24" stroke="#2a2e3955" />
              <line x1="30" :y1="scatterH - 24" :x2="scatterW - 10" y2="14" stroke="#787b8655" stroke-width="1" />
              <circle v-for="(p, i) in mfeMaePoints" :key="i" :cx="p.x" :cy="p.y" r="3"
                      :fill="p.pnl >= 0 ? '#26a69a' : '#ef5350'"
                      @click="selectedTradeIndex = p.tradeIndex" />
              <text x="34" y="12" fill="#d1d4dc" font-size="9">MFE</text>
              <text :x="scatterW - 42" :y="scatterH - 7" fill="#787b86" font-size="9">MAE</text>
            </svg>
          </div>
          <div class="panel ledger-panel">
            <h2 class="panel-title">Trade Ledger</h2>
            <table class="dense-table">
              <thead>
                <tr>
                  <th>Side</th><th>Entry</th><th>Exit</th><th>PnL</th><th>R</th><th>MAE</th><th>MFE</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in activeTradeRows" :key="row.activeIndex"
                    :class="{ selected: selectedTradeIndex === row.activeIndex }"
                    @click="selectedTradeIndex = row.activeIndex"
                    @dblclick="openSnapshot(row.activeIndex)">
                  <template v-if="row.trade">
                  <td :class="row.trade.dir === 'long' ? 'text-up' : 'text-down'">{{ row.trade.dir }}</td>
                  <td>{{ fmt(row.trade.entry) }}</td>
                  <td>{{ fmt(row.trade.exit) }}</td>
                  <td :class="row.trade.net_pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtPnl(row.trade.net_pnl) }}</td>
                  <td>{{ row.trade.r_multiple != null ? row.trade.r_multiple.toFixed(2) : '—' }}</td>
                  <td>{{ fmt(absVal(row.trade.mae ?? row.trade.MAE)) }}</td>
                  <td>{{ fmt(absVal(row.trade.mfe ?? row.trade.MFE)) }}</td>
                  </template>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="bottom-grid">
        <div class="panel chart-panel">
          <h2 class="panel-title">Optimization Heatmap</h2>
          <svg :viewBox="`0 0 ${heatW} ${heatH}`" preserveAspectRatio="none" class="chart-svg">
            <rect v-for="cell in optimizationCells" :key="cell.key" :x="cell.x" :y="cell.y"
                  :width="cell.w" :height="cell.h" :fill="cell.color" />
            <text x="4" y="14" fill="#8f96a8" font-size="10">Long</text>
            <text x="4" :y="heatH / 2 + 14" fill="#8f96a8" font-size="10">Short</text>
            <text v-for="h in [0, 6, 12, 18]" :key="h" :x="h * heatW / 24 + 4" :y="heatH - 5" fill="#8f96a8" font-size="9">{{ h }} UTC</text>
          </svg>
        </div>
        <div class="panel chart-panel">
          <h2 class="panel-title">Monte Carlo</h2>
          <svg :viewBox="`0 0 ${histW} ${histH}`" preserveAspectRatio="none" class="chart-svg">
            <line v-for="g in histGridY" :key="g" x1="24" :y1="g" :x2="histW - 10" :y2="g" stroke="#2a2e3966" />
            <rect v-for="bar in monteCarloBars" :key="bar.x" :x="bar.x" :y="bar.y"
                  :width="bar.w" :height="bar.h" fill="#26a69a99" />
            <text x="28" y="14" fill="#d1d4dc" font-size="10">Final equity distribution</text>
          </svg>
        </div>
      </section>
    </main>

    <TradeSnapshot v-if="snapshot" :snapshot="snapshot" @close="snapshot = null" />

    <div v-if="resultDialogOpen" class="modal-backdrop" @click.self="resultDialogOpen = false">
      <section class="result-dialog">
        <header class="result-header">
          <div>
            <h2>回測結果</h2>
            <p>{{ resultStatus }} | {{ fmtTime(stats?.backtest_start_ms) }} ~ {{ fmtTime(stats?.backtest_end_ms) }}</p>
          </div>
          <div class="result-actions">
            <button class="btn-ghost compact" :disabled="!activeTradeList.length" @click="openSnapshot(filteredResultRows[0]?.activeIndex || 0)">圖表快照</button>
            <a v-if="stats && currentJobId" class="btn-ghost compact" :href="backtestApi.exportUrl(currentJobId)">匯出 Excel</a>
            <button class="btn-ghost compact" @click="resultDialogOpen = false">關閉</button>
          </div>
        </header>
        <div class="result-filter">
          <label>市場時區</label>
          <select v-model="resultSession" class="select-field">
            <option value="all">全時間</option>
            <option value="asia">亞洲盤</option>
            <option value="london">倫敦盤</option>
            <option value="newyork">紐約盤</option>
          </select>
          <label>月份</label>
          <select v-model="resultMonth" class="select-field">
            <option value="all">全部月份</option>
            <option v-for="m in resultMonths" :key="m" :value="m">{{ m }}</option>
          </select>
          <div class="result-summary">
            <span>Trades {{ subsetStats.trades }}</span>
            <span>Win {{ fmtPct(subsetStats.win_rate) }}</span>
            <span>PF {{ fmtNum(subsetStats.profit_factor) }}</span>
            <span :class="subsetStats.total_net_pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtPnl(subsetStats.total_net_pnl) }} USDT</span>
          </div>
        </div>
        <div class="result-table-wrap">
          <table class="dense-table result-table">
            <thead>
              <tr>
                <th>#</th><th>方向</th><th>入場時間</th><th>入場價</th><th>出場類型</th>
                <th>出場價</th><th>數量</th><th>手續費</th><th>資金費</th><th>淨利</th><th>餘額</th><th>Regime</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in filteredResultRows" :key="row.activeIndex"
                  :class="{ selected: selectedTradeIndex === row.activeIndex }"
                  @click="selectedTradeIndex = row.activeIndex"
                  @dblclick="openSnapshot(row.activeIndex)">
                <td>{{ i + 1 }}</td>
                <td :class="row.trade.dir === 'long' ? 'text-up' : 'text-down'">{{ row.trade.dir === 'long' ? '做多' : '做空' }}</td>
                <td>{{ fmtTime(row.trade.entry_time, true) }}</td>
                <td>{{ fmt(row.trade.entry) }}</td>
                <td>{{ row.trade.exit_label || '—' }}</td>
                <td>{{ fmt(row.trade.exit) }}</td>
                <td>{{ fmtQty(row.trade.qty) }}</td>
                <td>{{ fmtNum(row.trade.total_fee) }}</td>
                <td>{{ fmtNum(row.trade.funding_cost) }}</td>
                <td :class="row.trade.net_pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtPnl(row.trade.net_pnl) }}</td>
                <td>{{ fmtNum(row.trade.equity_after) }}</td>
                <td>{{ row.trade.regime || row.trade.trend_regime || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <div v-if="running" class="backtest-progress-popup" role="status" aria-live="polite">
      <div class="progress-head">
        <strong>回測執行進度</strong>
        <span>{{ progressPercent }}%</span>
      </div>
      <div class="progress-bar"><span :style="{ width: `${progressPercent}%` }"></span></div>
      <div class="progress-message">{{ progress || '回測中...' }}</div>
      <div v-if="currentJobId" class="progress-job">Job {{ shortJobId }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { backtestApi, settingsApi } from '@/api/client.js'
import TradeSnapshot from '@/components/TradeSnapshot.vue'

const BACKTEST_ACTIVE_JOB_KEY = 'orderflow.backtest.activeJobId'

const MetricBox = {
  props: { label: String, value: [String, Number], color: String },
  template: `<div class="metric-box"><span>{{ label }}</span><strong :class="color || 'text-text'">{{ value }}</strong></div>`
}

const symbols = ref([])
const intervals = ref(['1m','3m','5m','15m','30m','1h','4h'])
const strategies = ref([])
const running = ref(false)
const tickImporting = ref(false)
const progress = ref('')
const progressPct = ref(0)
const tickImportProgress = ref('')
const error = ref('')
const stats = ref(null)
const tickImportFolder = ref('')
const currentJobId = ref('')
const snapshot = ref(null)
const selectedTradeIndex = ref(0)
const resultDialogOpen = ref(false)
const resultSession = ref('all')
const resultMonth = ref('all')
const klineRecords = ref([])
const tickCoverage = ref([])
const settingsReady = ref(false)
let saveTimer = null
let restoringSettings = false
let pollGeneration = 0

const chartW = 720
const chartH = 360
const scatterW = 360
const scatterH = 170
const heatW = 420
const heatH = 180
const histW = 420
const histH = 180

const form = ref({
  symbol: 'BTCUSDT',
  interval: '1m',
  strategy_name: '',
  start_date: '2024-01-01',
  end_date: '2024-12-31',
  initial_capital: 10000,
  max_loss_pct: 2,
  leverage: 20,
  fee_mode: '自訂',
  custom_fee_rate_pct: 0.032,
  slippage_bps: 0.2,
  compound: false,
  use_tick_mode: false,
  slice_mode: 'range',
  selected_months: [],
  wf_segments: 4,
  wf_oos_fraction: 0.3,
  wf_anchored: false,
})

const tradeList = computed(() => stats.value?.trade_list || [])
const activeTradeList = computed(() => tradeList.value.filter(t => !t.skipped))
const activeTradeRows = computed(() => activeTradeList.value.map((trade, activeIndex) => ({ trade, activeIndex })))
const availableSymbols = computed(() => klineRecords.value.length ? [...new Set(klineRecords.value.map(r => r.symbol))] : symbols.value)
const availableIntervals = computed(() => klineRecords.value.length
  ? klineRecords.value.filter(r => r.symbol === form.value.symbol).map(r => r.interval)
  : intervals.value)
const klineRange = computed(() => {
  const rec = klineRecords.value.find(r => r.symbol === form.value.symbol && r.interval === form.value.interval)
  return rec ? { start: msToDate(rec.start_ms), end: msToDate(rec.end_ms), count: rec.count } : null
})
const tickRange = computed(() => tickCoverage.value.find(t => t.symbol === form.value.symbol) || null)
const klineMonths = computed(() => {
  const rec = klineRecords.value.find(r => r.symbol === form.value.symbol && r.interval === form.value.interval)
  if (!rec) return []
  const months = []
  const cur = new Date(rec.start_ms)
  cur.setUTCDate(1); cur.setUTCHours(0, 0, 0, 0)
  const end = new Date(rec.end_ms)
  while (cur <= end) {
    months.push(`${cur.getUTCFullYear()}${String(cur.getUTCMonth() + 1).padStart(2, '0')}`)
    cur.setUTCMonth(cur.getUTCMonth() + 1)
  }
  return months
})
const tickMonths = computed(() => {
  const rec = tickCoverage.value.find(t => t.symbol === form.value.symbol)
  return Array.isArray(rec?.months) ? rec.months : []
})
const availableMonths = computed(() => {
  return form.value.use_tick_mode ? tickMonths.value : klineMonths.value
})
const sliceSummary = computed(() => {
  if (form.value.slice_mode === 'range') return 'Single range from Start to End.'
  if (form.value.slice_mode === 'multi_select') return `${form.value.selected_months.length} months selected; contiguous months are merged by server.`
  return `${form.value.selected_months.length || 'range'} months / ${form.value.wf_segments} segments / OOS ${(form.value.wf_oos_fraction * 100).toFixed(0)}%`
})

const resultStatus = computed(() => {
  if (!stats.value) return 'Ready'
  return `Done - ${stats.value.trades || 0} trades | Win ${fmtPct(stats.value.win_rate)} | PF ${fmtNum(stats.value.profit_factor)}`
})
const progressPercent = computed(() => Math.max(0, Math.min(100, Math.round((progressPct.value || 0) * 100))))
const shortJobId = computed(() => currentJobId.value ? currentJobId.value.slice(0, 8) : '')

const summaryMetrics = computed(() => {
  const s = stats.value || {}
  return [
    { label: 'Win Rate', value: s.win_rate != null ? fmtPct(s.win_rate) : '—', color: s.win_rate >= 50 ? 'text-up' : 'text-down' },
    { label: 'Profit Factor', value: s.profit_factor != null ? fmtNum(s.profit_factor) : '—', color: 'text-up' },
    { label: 'Max Drawdown', value: s.max_drawdown_pct != null ? fmtPct(s.max_drawdown_pct) : '—', color: s.max_drawdown_pct > 10 ? 'text-down' : 'text-up' },
    { label: 'Sharpe Ratio', value: s.sharpe_ratio != null ? fmtNum(s.sharpe_ratio) : '—', color: s.sharpe_ratio >= 0 ? 'text-up' : 'text-down' },
    { label: 'Total Return', value: s.total_return_pct != null ? fmtPct(s.total_return_pct) : '—', color: s.total_return_pct >= 0 ? 'text-up' : 'text-down' },
    { label: 'Trades', value: s.trades ?? '—' },
  ]
})

const resultMonths = computed(() => {
  return [...new Set(activeTradeList.value.map(t => tradeMonth(t)).filter(Boolean))].sort()
})

const filteredResultRows = computed(() => {
  return activeTradeRows.value.filter(row => {
    const t = row.trade
    if (resultMonth.value !== 'all' && tradeMonth(t) !== resultMonth.value) return false
    if (resultSession.value !== 'all' && !inSession(t, resultSession.value)) return false
    return true
  })
})

const subsetStats = computed(() => computeSubsetStats(filteredResultRows.value.map(r => r.trade)))

const equityValues = computed(() => activeTradeList.value.map(t => Number(t.equity_after)).filter(Number.isFinite))
const equityGridY = computed(() => [32, 72, 112, 152, 192, chartH * 0.72, chartH * 0.84, chartH * 0.96])
const equityGridX = computed(() => [42, chartW * 0.25, chartW * 0.5, chartW * 0.75, chartW - 14])
const scatterGridY = computed(() => [28, 58, 88, 118, 146])
const scatterGridX = computed(() => [30, scatterW * 0.33, scatterW * 0.66, scatterW - 12])
const histGridY = computed(() => [36, 72, 108, 144])
const equityPath = computed(() => linePath(equityValues.value, chartW, chartH * 0.68, 18, 42, 14))
const initialLinePath = computed(() => {
  const initial = Number(stats.value?.initial_capital || form.value.initial_capital)
  const vals = equityValues.value.length ? equityValues.value : [initial]
  const y = scaleY(initial, vals, chartH * 0.68, 18, 42, 14)
  return `M42 ${y} L${chartW - 14} ${y}`
})
const drawdownAreaPath = computed(() => {
  const vals = equityValues.value
  if (!vals.length) return ''
  let peak = vals[0]
  const dd = vals.map(v => {
    peak = Math.max(peak, v)
    return peak > 0 ? (peak - v) / peak * 100 : 0
  })
  const top = chartH * 0.72
  const h = chartH * 0.24
  const pts = dd.map((v, i) => {
    const x = 42 + i * ((chartW - 56) / Math.max(1, dd.length - 1))
    const y = top + (v / Math.max(1, Math.max(...dd))) * h
    return [x, y]
  })
  return `M42 ${top} ` + pts.map(([x, y]) => `L${x} ${y}`).join(' ') + ` L${chartW - 14} ${top} Z`
})

const mfeMaePoints = computed(() => {
  const rows = activeTradeList.value.map((t, i) => ({
    mae: absVal(t.mae ?? t.MAE),
    mfe: absVal(t.mfe ?? t.MFE),
    pnl: Number(t.net_pnl || 0),
    tradeIndex: i,
  })).filter(p => Number.isFinite(p.mae) && Number.isFinite(p.mfe))
  const maxX = Math.max(1, ...rows.map(p => p.mae))
  const maxY = Math.max(1, ...rows.map(p => p.mfe))
  return rows.map(p => ({
    ...p,
    x: 30 + (p.mae / maxX) * (scatterW - 42),
    y: scatterH - 24 - (p.mfe / maxY) * (scatterH - 38),
  }))
})

const optimizationCells = computed(() => {
  const serverRows = stats.value?.optimization_heatmap
  const buckets = new Map()
  if (Array.isArray(serverRows) && serverRows.length) {
    for (const row of serverRows) {
      const side = row.side === 'short' ? 1 : 0
      const hour = Number(row.hour)
      const key = `${side}:${hour}`
      buckets.set(key, { avg: Number(row.avg_net_pnl || 0), count: Number(row.trades || 0) })
    }
  } else for (const t of activeTradeList.value) {
    const side = t.dir === 'short' ? 1 : 0
    const hour = Number.isInteger(t.session_hour) ? t.session_hour : new Date(Number(t.entry_time || 0)).getUTCHours()
    const key = `${side}:${hour}`
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(Number(t.net_pnl || 0))
  }
  const vals = [...buckets.values()].map(v => Array.isArray(v) ? v.reduce((a, b) => a + b, 0) / v.length : v.avg)
  const maxAbs = Math.max(1, ...vals.map(v => Math.abs(v)))
  const cellW = heatW / 24
  const cellH = heatH / 2
  const cells = []
  for (let side = 0; side < 2; side++) {
    for (let hour = 0; hour < 24; hour++) {
      const arr = buckets.get(`${side}:${hour}`) || []
      const avg = Array.isArray(arr) ? (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0) : arr.avg
      const t = Math.min(1, Math.abs(avg) / maxAbs)
      cells.push({
        key: `${side}:${hour}`,
        x: hour * cellW,
        y: side * cellH,
        w: cellW - 1,
        h: cellH - 1,
        color: avg >= 0 ? `rgba(38,166,154,${0.15 + t * 0.75})` : `rgba(239,83,80,${0.15 + t * 0.75})`,
      })
    }
  }
  return cells
})

const monteCarloBars = computed(() => {
  const serverFinals = stats.value?.monte_carlo?.final_equity
  let finals = Array.isArray(serverFinals) ? serverFinals.map(Number).filter(Number.isFinite) : []
  if (!finals.length) {
    const pnls = activeTradeList.value.map(t => Number(t.net_pnl || 0))
    if (!pnls.length) return []
    finals = []
    for (let i = 0; i < 300; i++) {
      let eq = Number(stats.value?.initial_capital || form.value.initial_capital)
      for (let j = 0; j < pnls.length; j++) eq += pnls[Math.floor((i * 1103515245 + j * 12345) % pnls.length)]
      finals.push(eq)
    }
  }
  const min = Math.min(...finals)
  const max = Math.max(...finals)
  const bins = 24
  const counts = Array(bins).fill(0)
  for (const v of finals) counts[Math.min(bins - 1, Math.floor(((v - min) / Math.max(1, max - min)) * bins))]++
  const maxCount = Math.max(1, ...counts)
  const w = histW / bins
  return counts.map((c, i) => ({
    x: i * w,
    y: histH - (c / maxCount) * (histH - 10),
    w: w - 1,
    h: (c / maxCount) * (histH - 10),
  }))
})

function linePath(vals, w, h, top, leftPad, rightPad) {
  if (!vals.length) return ''
  return vals.map((v, i) => {
    const x = leftPad + i * ((w - leftPad - rightPad) / Math.max(1, vals.length - 1))
    const y = scaleY(v, vals, h, top, leftPad, rightPad)
    return `${i ? 'L' : 'M'}${x} ${y}`
  }).join(' ')
}
function scaleY(v, vals, h, top, leftPad, rightPad) {
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const pad = Math.min(leftPad, rightPad, 14)
  return top + pad + (1 - (v - min) / Math.max(1e-9, max - min)) * (h - pad * 2)
}
function absVal(v) { return Math.abs(Number(v || 0)) }
function fmt(v) { return v != null && Number.isFinite(Number(v)) ? Number(v).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : '—' }
function fmtNum(v) {
  if (v === Infinity || v === 'Infinity') return '∞'
  return v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(2) : '—'
}
function fmtPct(v) { return v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(1) + '%' : '—' }
function fmtPnl(v) { return v != null ? (v >= 0 ? '+' : '') + Number(v).toFixed(2) : '—' }
function fmtQty(v) { return v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(6) : '—' }
function msToDate(ms) { return new Date(ms).toISOString().slice(0, 10) }
function dateToMs(dateStr) { return new Date(dateStr + 'T00:00:00Z').getTime() }
function fmtTime(ms, short = false) {
  if (!ms) return '—'
  const dt = new Date(Number(ms))
  return dt.toLocaleString('zh-TW', {
    timeZone: 'Asia/Taipei',
    hour12: false,
    year: short ? undefined : 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).replace(/\//g, '-')
}
function tradeMonth(t) {
  if (!t?.entry_time) return ''
  const dt = new Date(Number(t.entry_time))
  const y = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Taipei', year: 'numeric' }).format(dt)
  const m = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Taipei', month: '2-digit' }).format(dt)
  return `${y}-${m}`
}
function localHour(ms, timeZone) {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone, hour: '2-digit', hour12: false }).formatToParts(new Date(Number(ms)))
  return Number(parts.find(p => p.type === 'hour')?.value || 0)
}
function inSession(t, session) {
  if (!t?.entry_time) return true
  if (session === 'asia') {
    const h = localHour(t.entry_time, 'Asia/Taipei')
    return h >= 7 && h < 16
  }
  if (session === 'london') {
    const h = localHour(t.entry_time, 'Europe/London')
    return h >= 8 && h < 17
  }
  if (session === 'newyork') {
    const h = localHour(t.entry_time, 'America/New_York')
    return h >= 8 && h < 16
  }
  return true
}
function computeSubsetStats(trades) {
  const active = trades.filter(t => !t.skipped)
  const n = active.length
  const net = active.reduce((s, t) => s + Number(t.net_pnl || 0), 0)
  const wins = active.filter(t => Number(t.net_pnl || 0) > 0)
  const losses = active.filter(t => Number(t.net_pnl || 0) < 0)
  const gp = wins.reduce((s, t) => s + Number(t.net_pnl || 0), 0)
  const gl = Math.abs(losses.reduce((s, t) => s + Number(t.net_pnl || 0), 0))
  return {
    trades: n,
    win_rate: n ? wins.length / n * 100 : 0,
    profit_factor: gl > 0 ? gp / gl : (gp > 0 ? Infinity : 0),
    total_net_pnl: net,
  }
}
function toggleMonth(m) {
  const i = form.value.selected_months.indexOf(m)
  if (i >= 0) form.value.selected_months.splice(i, 1)
  else form.value.selected_months.push(m)
}

watch(() => form.value.symbol, () => {
  if (restoringSettings) return
  const avail = availableIntervals.value
  if (!avail.includes(form.value.interval) && avail.length) form.value.interval = avail[0]
  applyKlineRangeDefaults()
  sanitizeSelectedMonths()
})
watch(() => form.value.interval, () => {
  if (!restoringSettings) {
    applyKlineRangeDefaults()
    sanitizeSelectedMonths()
  }
})
watch(() => form.value.use_tick_mode, () => sanitizeSelectedMonths())
watch(form, scheduleSaveSettings, { deep: true })
watch(tickImportFolder, scheduleSaveSettings)
watch(currentJobId, scheduleSaveSettings)

function applyKlineRangeDefaults(force = true) {
  const r = klineRange.value
  if (!r) return
  if (force || !form.value.start_date) form.value.start_date = r.start
  if (force || !form.value.end_date) form.value.end_date = r.end
  if (!form.value.selected_months.length) form.value.selected_months = availableMonths.value.slice(-1)
}

function sanitizeSelectedMonths() {
  const avail = availableMonths.value
  if (!avail.length) return
  const allowed = new Set(avail)
  const filtered = form.value.selected_months.filter(m => allowed.has(m))
  form.value.selected_months = filtered.length ? filtered : avail.slice(-1)
}

function settingsPayload() {
  return {
    symbol: form.value.symbol,
    interval: form.value.interval,
    strategy: form.value.strategy_name,
    mode: form.value.slice_mode,
    start_date: form.value.start_date,
    end_date: form.value.end_date,
    initial_capital: form.value.initial_capital,
    leverage: form.value.leverage,
    max_risk_pct: form.value.max_loss_pct,
    fee_mode: form.value.fee_mode,
    custom_fee_pct: form.value.custom_fee_rate_pct,
    slippage_bps: form.value.slippage_bps,
    compound: form.value.compound,
    use_tick_mode: form.value.use_tick_mode,
    selected_months: form.value.selected_months,
    wf_segments: form.value.wf_segments,
    wf_oos_fraction: form.value.wf_oos_fraction,
    wf_anchored: form.value.wf_anchored,
    tick_import_folder: tickImportFolder.value,
    last_job_id: currentJobId.value,
  }
}

function scheduleSaveSettings() {
  if (!settingsReady.value || restoringSettings) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(async () => {
    try {
      await settingsApi.update({
        backtest_dashboard_config: settingsPayload(),
        backtest_dashboard_last_job_id: currentJobId.value,
      })
    } catch { /* settings persistence should not block trading workflow */ }
  }, 300)
}

function restoreForm(saved) {
  if (!saved || typeof saved !== 'object') return
  const restored = {
    symbol: saved.symbol ?? form.value.symbol,
    interval: saved.interval ?? form.value.interval,
    strategy_name: saved.strategy ?? saved.strategy_name ?? form.value.strategy_name,
    slice_mode: saved.mode ?? saved.slice_mode ?? form.value.slice_mode,
    start_date: saved.start_date ?? form.value.start_date,
    end_date: saved.end_date ?? form.value.end_date,
    initial_capital: saved.initial_capital ?? form.value.initial_capital,
    max_loss_pct: saved.max_risk_pct ?? saved.max_loss_pct ?? form.value.max_loss_pct,
    leverage: saved.leverage ?? form.value.leverage,
    fee_mode: saved.fee_mode ?? form.value.fee_mode,
    custom_fee_rate_pct: saved.custom_fee_pct ?? saved.custom_fee_rate_pct ?? form.value.custom_fee_rate_pct,
    slippage_bps: saved.slippage_bps ?? form.value.slippage_bps,
    compound: saved.compound ?? form.value.compound,
    use_tick_mode: saved.use_tick_mode ?? form.value.use_tick_mode,
    selected_months: Array.isArray(saved.selected_months) ? saved.selected_months : form.value.selected_months,
    wf_segments: saved.wf_segments ?? form.value.wf_segments,
    wf_oos_fraction: saved.wf_oos_fraction ?? form.value.wf_oos_fraction,
    wf_anchored: saved.wf_anchored ?? form.value.wf_anchored,
  }
  Object.assign(form.value, restored)
  tickImportFolder.value = saved.tick_import_folder ?? tickImportFolder.value
  currentJobId.value = saved.last_job_id ?? currentJobId.value
}

async function restoreLastResult(jobId) {
  if (!jobId || stats.value) return
  try {
    const { data } = await backtestApi.getJob(jobId)
    if ((data.status === 'running' || data.status === 'pending') && !storedBacktestJobId()) {
      setActiveBacktestJob(jobId)
      resumeStoredBacktestJob()
      return
    }
    if (data.status === 'done' && data.result) {
      currentJobId.value = jobId
      stats.value = data.result
      progress.value = ''
      progressPct.value = 1
      error.value = ''
    }
  } catch { /* server may have restarted and lost in-memory jobs */ }
}

async function runBacktest() {
  if (running.value) return
  running.value = true
  error.value = ''
  progress.value = 'Submitting backtest...'
  progressPct.value = 0.01
  stats.value = null
  snapshot.value = null
  selectedTradeIndex.value = 0
  try {
    const payload = {
      ...form.value,
      start_ms: dateToMs(form.value.start_date),
      end_ms: dateToMs(form.value.end_date),
      custom_fee_rate: form.value.custom_fee_rate_pct / 100,
    }
    const { data } = await backtestApi.run(payload)
    setActiveBacktestJob(data.job_id)
    scheduleSaveSettings()
    await pollJob(data.job_id, { immediate: true })
  } catch (e) {
    error.value = e.message
    running.value = false
    progressPct.value = 0
  }
}

async function openSnapshot(idx) {
  if (!currentJobId.value || !activeTradeList.value.length) return
  try {
    const { data } = await backtestApi.snapshot(currentJobId.value, Math.max(0, idx || 0))
    snapshot.value = data
  } catch (e) {
    error.value = e.message
  }
}

async function pollJob(jobId, { immediate = false } = {}) {
  const token = ++pollGeneration
  let consecutiveErrors = 0
  if (immediate) {
    try {
      const done = await syncBacktestJob(jobId)
      if (done || token !== pollGeneration) return
    } catch (e) {
      consecutiveErrors++
      progress.value = `Polling... (retry ${consecutiveErrors})`
    }
  }
  while (true) {
    await new Promise(r => setTimeout(r, 1500))
    if (token !== pollGeneration) return
    try {
      const done = await syncBacktestJob(jobId)
      consecutiveErrors = 0
      if (done) return
    } catch (e) {
      consecutiveErrors++
      if (consecutiveErrors >= 10) {
        error.value = e.message
        running.value = false
        progressPct.value = 0
        return
      }
      progress.value = `Polling... (retry ${consecutiveErrors})`
    }
  }
}

async function syncBacktestJob(jobId) {
  if (!jobId) return true
  const { data } = await backtestApi.getJob(jobId)
  currentJobId.value = jobId
  progress.value = data.progress || ''
  progressPct.value = Number.isFinite(Number(data.progress_pct)) ? Number(data.progress_pct) : progressPct.value
  if (data.status === 'done') {
    stats.value = data.result
    running.value = false
    progress.value = ''
    progressPct.value = 1
    clearActiveBacktestJob()
    scheduleSaveSettings()
    return true
  }
  if (data.status === 'error') {
    error.value = data.error || 'Backtest job failed.'
    running.value = false
    progress.value = ''
    progressPct.value = 0
    clearActiveBacktestJob()
    return true
  }
  running.value = data.status === 'running' || data.status === 'pending'
  return false
}

function setActiveBacktestJob(jobId) {
  currentJobId.value = jobId || ''
  if (!jobId) return
  try { localStorage.setItem(BACKTEST_ACTIVE_JOB_KEY, jobId) } catch { /* ignore */ }
}
function clearActiveBacktestJob() {
  try { localStorage.removeItem(BACKTEST_ACTIVE_JOB_KEY) } catch { /* ignore */ }
}
function storedBacktestJobId() {
  try { return localStorage.getItem(BACKTEST_ACTIVE_JOB_KEY) || '' } catch { return '' }
}
function resumeStoredBacktestJob() {
  const jobId = storedBacktestJobId()
  if (!jobId) return
  running.value = true
  currentJobId.value = jobId
  stats.value = null
  snapshot.value = null
  progress.value = 'Restoring backtest job status...'
  progressPct.value = 0.01
  pollJob(jobId, { immediate: true }).catch(e => {
    error.value = e.message
    running.value = false
    progressPct.value = 0
  })
}
function handleBacktestVisibility() {
  if (document.visibilityState !== 'visible' || !currentJobId.value || !running.value) return
  syncBacktestJob(currentJobId.value).catch(() => { /* next poll will retry */ })
}

async function refreshAvailableData() {
  const { data } = await backtestApi.availableData()
  klineRecords.value = data.klines || []
  tickCoverage.value = data.ticks || []
  sanitizeSelectedMonths()
}

async function importTicksFromFolder() {
  if (tickImporting.value) return
  if (!tickImportFolder.value.trim()) {
    error.value = '請輸入伺服器上的 Tick CSV/ZIP 資料夾路徑'
    return
  }
  tickImporting.value = true
  error.value = ''
  tickImportProgress.value = 'Submitting tick import...'
  try {
    const { data } = await backtestApi.importTicks({ symbol: form.value.symbol, folder: tickImportFolder.value })
    while (true) {
      await new Promise(r => setTimeout(r, 1500))
      const job = await backtestApi.getJob(data.job_id)
      tickImportProgress.value = job.data.progress || ''
      if (job.data.status === 'done') {
        tickImporting.value = false
        tickImportProgress.value = `Tick import done: ${job.data.result?.total_count?.toLocaleString?.() || 0}`
        await refreshAvailableData()
        return
      }
      if (job.data.status === 'error') {
        error.value = job.data.error
        tickImporting.value = false
        tickImportProgress.value = ''
        return
      }
    }
  } catch (e) {
    error.value = e.message
    tickImporting.value = false
    tickImportProgress.value = ''
  }
}

async function clearTickCache() {
  if (!confirm(`清除 ${form.value.symbol} Tick cache？`)) return
  await backtestApi.clearTicks(form.value.symbol)
  await refreshAvailableData()
}

onMounted(async () => {
  try {
    restoringSettings = true
    const [sv, stv, settings] = await Promise.all([backtestApi.symbols(), backtestApi.strategies(), settingsApi.get()])
    symbols.value = sv.data.symbols
    intervals.value = sv.data.intervals
    strategies.value = stv.data.strategies
    await refreshAvailableData()
    if (strategies.value.length) form.value.strategy_name = strategies.value[0]
    if (availableSymbols.value.length) form.value.symbol = availableSymbols.value[0]
    const saved = settings.data?.backtest_dashboard_config || {}
    restoreForm(saved)
    if (!availableIntervals.value.includes(form.value.interval) && availableIntervals.value.length) {
      form.value.interval = availableIntervals.value[0]
    }
    if (!form.value.start_date || !form.value.end_date) applyKlineRangeDefaults(false)
    sanitizeSelectedMonths()
    settingsReady.value = true
    restoringSettings = false
    await restoreLastResult(settings.data?.backtest_dashboard_last_job_id || saved.last_job_id)
    resumeStoredBacktestJob()
  } catch { /* ignore */ }
  finally {
    restoringSettings = false
    settingsReady.value = true
  }
  document.addEventListener('visibilitychange', handleBacktestVisibility)
  window.addEventListener('focus', handleBacktestVisibility)
})

onUnmounted(() => {
  pollGeneration++
  document.removeEventListener('visibilitychange', handleBacktestVisibility)
  window.removeEventListener('focus', handleBacktestVisibility)
})
</script>

<style scoped>
.bt-root { height: calc(100vh - 44px); display: grid; grid-template-columns: 340px minmax(0, 1fr); overflow: hidden; }
.bt-sidebar { border-right: 1px solid #2a2e39; padding: 8px; overflow-y: auto; background: #101621; }
.bt-main { min-width: 0; display: grid; grid-template-rows: 70px 28px minmax(0, 3fr) minmax(150px, 1fr); overflow: hidden; }
.panel { background: #151c2a; border: 1px solid #263245; border-radius: 6px; padding: 10px; margin-bottom: 8px; min-width: 0; }
.panel-title { color: #8fe7d8; font-size: 12px; font-weight: 700; margin-bottom: 8px; }
.field-grid { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 6px; align-items: center; }
.field-grid label, .hint { color: #8f96a8; font-size: 11px; }
.check-row { display: flex; gap: 14px; margin-top: 10px; color: #8f96a8; font-size: 12px; }
.check-inline { color: #8f96a8; font-size: 12px; }
.slice-modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin-bottom: 8px; }
.slice-modes button, .month-picker button { border: 1px solid #334058; color: #8f96a8; border-radius: 4px; padding: 3px 6px; font-size: 11px; }
.slice-modes button.active, .month-picker button.active { border-color: #26a69a; background: #1f6f6644; color: #f2f5f9; }
.month-picker { display: flex; flex-wrap: wrap; gap: 4px; max-height: 122px; overflow: auto; padding: 4px; border: 1px solid #263245; background: #101621; }
.metrics-strip { display: grid; grid-template-columns: repeat(6, 1fr); background: #1e222d; border-bottom: 1px solid #2a2e39; }
.metric-box { display: flex; flex-direction: column; justify-content: center; align-items: center; border-right: 1px solid #2a2e39; }
.metric-box span { color: #787b86; font-size: 10px; }
.metric-box strong { font-size: 18px; }
.toolbar { height: 28px; display: flex; align-items: center; gap: 8px; padding: 0 8px; border-bottom: 1px solid #2a2e39; font-size: 11px; }
.compact { padding: 2px 8px; font-size: 11px; margin-left: auto; }
.compact + .compact { margin-left: 0; }
.center-grid { min-height: 0; display: grid; grid-template-columns: 3fr 2fr; gap: 4px; padding: 4px; }
.right-stack { min-height: 0; display: grid; grid-template-rows: 1fr 2fr; gap: 4px; }
.bottom-grid { min-height: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 0 4px 4px; }
.chart-panel { margin: 0; display: flex; flex-direction: column; min-height: 0; }
.chart-svg { flex: 1; min-height: 0; width: 100%; height: 100%; background: #131722; border: 1px solid #20283a; }
.ledger-panel { margin: 0; overflow: hidden; display: flex; flex-direction: column; }
.dense-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.dense-table th { color: #8f96a8; background: #182132; position: sticky; top: 0; }
.dense-table th, .dense-table td { padding: 4px 6px; border-bottom: 1px solid #263245; text-align: right; white-space: nowrap; }
.dense-table th:first-child, .dense-table td:first-child { text-align: left; }
.dense-table tbody { cursor: pointer; }
.dense-table tr.selected { background: #23423f; }
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(5, 8, 12, 0.72);
  display: grid;
  place-items: center;
  padding: 20px;
}
.result-dialog {
  width: min(1180px, 96vw);
  height: min(760px, 92vh);
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
  background: #151c2a;
  border: 1px solid #334058;
  border-radius: 6px;
  overflow: hidden;
}
.result-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid #263245;
}
.result-header h2 { color: #d1d4dc; font-size: 15px; font-weight: 700; margin: 0; }
.result-header p { color: #8f96a8; font-size: 11px; margin: 2px 0 0; }
.result-actions { display: flex; gap: 6px; margin-left: auto; }
.result-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid #263245;
  color: #8f96a8;
  font-size: 12px;
}
.result-filter .select-field { width: 120px; }
.result-summary { display: flex; gap: 16px; margin-left: auto; color: #d1d4dc; }
.result-table-wrap { min-height: 0; overflow: auto; }
.result-table th, .result-table td { padding: 5px 8px; }
.backtest-progress-popup {
  position: fixed;
  right: 18px;
  bottom: 18px;
  width: min(360px, calc(100vw - 36px));
  z-index: 40;
  background: #151c2af2;
  border: 1px solid #334058;
  border-radius: 6px;
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.38);
  padding: 12px;
  color: #dce3ee;
}
.progress-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; }
.progress-head strong { color: #8fe7d8; font-size: 13px; }
.progress-bar { height: 7px; margin: 10px 0 8px; overflow: hidden; background: #0f1420; border: 1px solid #263245; border-radius: 4px; }
.progress-bar span { display: block; height: 100%; background: linear-gradient(90deg, #26a69a, #42a5f5); transition: width 180ms ease; }
.progress-message { color: #dce3ee; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.progress-job { margin-top: 5px; color: #8f96a8; font-size: 10px; }
@media (max-width: 980px) {
  .bt-root { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
  .bt-sidebar { max-height: 44vh; border-right: 0; border-bottom: 1px solid #2a2e39; }
  .bt-main { grid-template-rows: auto 28px minmax(360px, 1fr) 220px; }
  .metrics-strip { grid-template-columns: repeat(3, 1fr); }
  .center-grid, .bottom-grid { grid-template-columns: 1fr; }
  .result-filter, .result-header { flex-wrap: wrap; }
  .result-summary, .result-actions { margin-left: 0; }
}
</style>
