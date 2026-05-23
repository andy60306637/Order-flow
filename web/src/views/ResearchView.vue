<template>
  <div class="research-root bg-bg">
    <aside class="research-sidebar">
      <div class="panel">
        <h2 class="panel-title">Research Dataset</h2>
        <div class="field-grid">
          <label>Symbol</label>
          <select v-model="form.symbol" class="select-field">
            <option v-for="s in availableSymbols" :key="s" :value="s">{{ s }}</option>
          </select>
          <label>Interval</label>
          <select v-model="form.interval" class="select-field">
            <option v-for="i in availableIntervals" :key="i" :value="i">{{ i }}</option>
          </select>
        </div>
        <label class="check-row solo"><input v-model="form.use_tick_features" type="checkbox" /> Use tick-derived factors</label>
      </div>

      <div class="panel">
        <h2 class="panel-title">Analysis Configuration</h2>
        <button class="config-row" @click="activeConfig = activeConfig === 'months' ? '' : 'months'">
          <span>Time Slice...</span><em>{{ form.selected_months.length || '—' }}</em>
        </button>
        <div v-if="activeConfig === 'months'" class="picker">
          <button v-for="m in availableMonths" :key="m"
                  :class="{ active: form.selected_months.includes(m) }"
                  @click="toggleMonth(m)">{{ m }}</button>
        </div>

        <button class="config-row" @click="activeConfig = activeConfig === 'factors' ? '' : 'factors'">
          <span>Factors...</span><em>{{ form.factor_names.length || '—' }}</em>
        </button>
        <div v-if="activeConfig === 'factors'" class="picker factor-picker">
          <div class="mini-actions">
            <button @click="selectAllFactors">All</button>
            <button @click="form.factor_names = []">Clear</button>
          </div>
          <button v-for="f in visibleFactors" :key="f.name"
                  :title="f.description"
                  :class="{ active: form.factor_names.includes(f.name) }"
                  @click="toggleFactor(f.name)">{{ f.name }}</button>
        </div>

        <button class="config-row" @click="activeConfig = activeConfig === 'params' ? '' : 'params'">
          <span>Parameters...</span><em>{{ horizonsInput }}</em>
        </button>
        <div v-if="activeConfig === 'params'" class="param-grid">
          <label>Horizons</label><input v-model="horizonsInput" class="input-field" />
          <label>Quantiles</label><input v-model.number="form.quantiles" type="number" class="input-field" min="2" max="10" />
          <label>Entry Lag</label><input v-model.number="form.entry_lag" type="number" class="input-field" min="0" max="5" />
          <label>Train Ratio</label><input v-model.number="form.train_ratio" type="number" class="input-field" step="0.1" min="0.1" max="0.9" />
        </div>

        <button class="config-row" @click="activeConfig = activeConfig === 'regime' ? '' : 'regime'">
          <span>Regime...</span><em>{{ regimeSummary }}</em>
        </button>
        <div v-if="activeConfig === 'regime'" class="regime-panel">
          <div class="slice-modes">
            <button v-for="m in regimeOptions.modes" :key="m"
                    :class="{ active: form.regime_filter.mode === m }"
                    @click="form.regime_filter.mode = m">{{ m }}</button>
          </div>
          <div v-for="dim in regimeOptions.dimensions" :key="dim.key" class="regime-dim">
            <label class="check-row solo">
              <input v-model="dimState(dim.key).enabled" type="checkbox" />
              {{ dim.label }}
            </label>
            <div class="picker compact-picker" :class="{ muted: !dimState(dim.key).enabled }">
              <button v-for="lbl in dim.labels" :key="lbl"
                      :class="{ active: dimState(dim.key).selected_labels.includes(lbl) }"
                      @click="toggleRegimeLabel(dim.key, lbl)">{{ lbl }}</button>
            </div>
            <div v-if="Object.keys(dimState(dim.key).params).length" class="param-grid compact-param">
              <template v-for="(_, key) in dimState(dim.key).params" :key="key">
                <label>{{ key }}</label>
                <input v-model.number="dimState(dim.key).params[key]" type="number" step="0.01" class="input-field" />
              </template>
            </div>
          </div>
        </div>
      </div>

      <div class="action-row">
        <button class="btn-primary" :disabled="running" @click="runResearch">{{ running ? 'Running' : 'Run Research' }}</button>
        <button class="btn-ghost" :disabled="!result" @click="exportJson">Export</button>
        <label class="btn-ghost import-label">
          Import
          <input type="file" accept="application/json" @change="importJson" />
        </label>
      </div>
      <div v-if="progress" class="hint">{{ progress }}</div>
      <div v-if="error" class="hint text-down">錯誤：{{ error }}</div>
    </aside>

    <main class="research-main">
      <nav class="tabs">
        <button v-for="t in tabs" :key="t" :class="{ active: activeTab === t }" @click="activeTab = t">{{ t }}</button>
      </nav>

      <section class="tab-body">
        <div v-if="!selectedResult" class="empty-state">No research result loaded.</div>

        <template v-else-if="activeTab === 'Regime Matrix'">
          <table class="dense-table">
            <thead><tr><th>Regime</th><th>Rows</th><th>Top Factor</th><th>Best IC</th><th>Best IR</th><th>Unavailable</th></tr></thead>
            <tbody>
              <tr v-for="row in regimeRows" :key="row.key" @click="activeRegime = row.key" :class="{ selected: activeRegime === row.key }">
                <td>{{ row.key }}</td><td>{{ row.rows }}</td><td>{{ row.factor }}</td>
                <td :class="icColor(row.ic)">{{ fmtIC(row.ic) }}</td>
                <td :class="icColor(row.ir)">{{ fmtIC(row.ir) }}</td><td>{{ row.unavailable }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-else-if="activeTab === 'Factor Ranking'">
          <ResultTable :rows="selectedResult.summary" />
        </template>

        <template v-else-if="activeTab === 'Orthogonal Ranking'">
          <ResultTable :rows="selectedResult.orthogonal_summary || []" />
        </template>

        <template v-else-if="activeTab === 'IC Time Series'">
          <svg viewBox="0 0 900 420" preserveAspectRatio="none" class="chart-svg">
            <path v-for="line in icLines" :key="line.key" :d="line.path" fill="none" :stroke="line.color" stroke-width="2" />
          </svg>
        </template>

        <template v-else-if="activeTab === 'Visualization'">
          <div class="viz-grid">
            <div class="panel tight">
              <h2 class="panel-title">Monthly IC Heatmap</h2>
              <GridHeatmap :rows="selectedResult.stability_monthly || []" row-key="factor" col-key="period" value-key="IC" />
            </div>
            <div class="panel tight">
              <h2 class="panel-title">Correlation Matrix</h2>
              <GridHeatmap :rows="selectedResult.factor_correlations || []" row-key="factor_a" col-key="factor_b" value-key="corr" />
            </div>
          </div>
        </template>

        <template v-else-if="activeTab === 'IC by Horizon'">
          <ResultTable :rows="selectedResult.metrics || []" />
        </template>

        <template v-else-if="activeTab === 'Quantiles'">
          <ResultTable :rows="selectedResult.quantiles || []" />
        </template>

        <template v-else-if="activeTab === 'Monthly Stability'">
          <ResultTable :rows="selectedResult.stability_monthly || []" />
        </template>

        <template v-else-if="activeTab === 'Yearly Stability'">
          <ResultTable :rows="selectedResult.stability_yearly || []" />
        </template>

        <template v-else-if="activeTab === 'Factor Correlations'">
          <ResultTable :rows="selectedResult.factor_correlations || []" />
        </template>

        <template v-else-if="activeTab === 'Unavailable'">
          <ResultTable :rows="selectedResult.unavailable || []" />
        </template>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { researchApi, backtestApi, settingsApi } from '@/api/client.js'

const ResultTable = {
  props: { rows: { type: Array, default: () => [] } },
  computed: {
    cols() {
      const keys = new Set()
      for (const row of this.rows) Object.keys(row || {}).forEach(k => keys.add(k))
      return [...keys]
    }
  },
  methods: {
    fmt(v) {
      return typeof v === 'number' ? v.toFixed(Math.abs(v) < 10 ? 4 : 2) : (v ?? '—')
    },
    cls(v) {
      return typeof v === 'number' && Math.abs(v) <= 1
        ? (v > 0.05 ? 'text-up' : v < -0.05 ? 'text-down' : 'text-text')
        : 'text-text'
    }
  },
  template: `
    <table class="dense-table">
      <thead><tr><th v-for="c in cols" :key="c">{{ c }}</th></tr></thead>
      <tbody>
        <tr v-for="(row, i) in rows" :key="i">
          <td v-for="c in cols" :key="c" :class="cls(row[c])">{{ fmt(row[c]) }}</td>
        </tr>
      </tbody>
    </table>
  `
}

const GridHeatmap = {
  props: { rows: Array, rowKey: String, colKey: String, valueKey: String },
  computed: {
    rowLabels() { return [...new Set((this.rows || []).map(r => r[this.rowKey]).filter(Boolean))] },
    colLabels() { return [...new Set((this.rows || []).map(r => r[this.colKey]).filter(Boolean))] },
    cells() {
      const maxAbs = Math.max(1e-9, ...this.rows.map(r => Math.abs(Number(r[this.valueKey] || 0))))
      return this.rows.map(r => ({
        x: this.colLabels.indexOf(r[this.colKey]),
        y: this.rowLabels.indexOf(r[this.rowKey]),
        v: Number(r[this.valueKey] || 0),
        key: `${r[this.rowKey]}:${r[this.colKey]}`,
        color: Number(r[this.valueKey] || 0) >= 0
          ? `rgba(38,166,154,${0.18 + Math.abs(Number(r[this.valueKey] || 0)) / maxAbs * 0.75})`
          : `rgba(239,83,80,${0.18 + Math.abs(Number(r[this.valueKey] || 0)) / maxAbs * 0.75})`
      }))
    }
  },
  template: `
    <div class="grid-heatmap" :style="{ gridTemplateColumns: '120px repeat(' + Math.max(1, colLabels.length) + ', minmax(34px, 1fr))' }">
      <span></span><b v-for="c in colLabels" :key="c">{{ c }}</b>
      <template v-for="r in rowLabels" :key="r">
        <b>{{ r }}</b>
        <span v-for="c in colLabels" :key="c" :style="{ background: (cells.find(x => x.key === r + ':' + c) || {}).color || '#151c2a' }">
          {{ ((cells.find(x => x.key === r + ':' + c) || {}).v ?? 0).toFixed(3) }}
        </span>
      </template>
    </div>
  `
}

const tabs = [
  'Regime Matrix', 'Factor Ranking', 'Orthogonal Ranking', 'IC Time Series',
  'Visualization', 'IC by Horizon', 'Quantiles', 'Monthly Stability',
  'Yearly Stability', 'Factor Correlations', 'Unavailable'
]
const activeTab = ref('Regime Matrix')
const activeConfig = ref('months')
const activeRegime = ref('(all)')
const factors = ref([])
const regimeOptions = ref({ modes: ['filter', 'matrix', 'cross_matrix'], dimensions: [], defaults: {} })
const running = ref(false)
const progress = ref('')
const error = ref('')
const result = ref(null)
const horizonsInput = ref('1,3,6,12')
const klineRecords = ref([])
const settingsReady = ref(false)
let saveTimer = null
let restoringSettings = false

const form = ref({
  symbol: 'BTCUSDT',
  interval: '1m',
  selected_months: [],
  factor_names: [],
  quantiles: 5,
  entry_lag: 1,
  train_ratio: 0.5,
  use_tick_features: true,
  regime_filter: {
    mode: 'matrix',
    dimensions: [],
  },
})

const visibleFactors = computed(() => factors.value)
const selectedResult = computed(() => {
  if (!result.value) return null
  return result.value[activeRegime.value] || Object.values(result.value)[0] || null
})
const regimeRows = computed(() => {
  if (!result.value) return []
  return Object.entries(result.value).map(([key, res]) => {
    const top = (res.summary || [])[0] || {}
    const h = resultHorizons(res)[0]
    return {
      key,
      rows: res.rows || 0,
      factor: top.factor || '—',
      ic: h ? top[`IC_h${h}`] : null,
      ir: h ? top[`IR_h${h}`] : null,
      unavailable: (res.unavailable || []).length,
    }
  })
})
const regimeSummary = computed(() => {
  const active = (form.value.regime_filter.dimensions || []).filter(d => d.enabled && d.selected_labels.length)
  const labels = active.reduce((n, d) => n + d.selected_labels.length, 0)
  return labels ? `${form.value.regime_filter.mode} / ${labels} labels` : 'off'
})

const availableMonths = computed(() => {
  const rec = klineRecords.value.find(r => r.symbol === form.value.symbol && r.interval === form.value.interval)
  let startMs = rec ? rec.start_ms : Date.UTC(2021, 0, 1)
  let endMs = rec ? rec.end_ms : Date.now()
  const months = []
  let cur = new Date(startMs)
  cur.setUTCDate(1); cur.setUTCHours(0, 0, 0, 0)
  const end = new Date(endMs)
  while (cur <= end) {
    months.push(`${cur.getUTCFullYear()}${String(cur.getUTCMonth() + 1).padStart(2, '0')}`)
    cur.setUTCMonth(cur.getUTCMonth() + 1)
  }
  return months
})
const availableSymbols = computed(() => klineRecords.value.length ? [...new Set(klineRecords.value.map(r => r.symbol))] : ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
const availableIntervals = computed(() => klineRecords.value.length
  ? klineRecords.value.filter(r => r.symbol === form.value.symbol).map(r => r.interval)
  : ['1m', '3m', '5m', '15m', '30m', '1h'])

const icLines = computed(() => {
  const ts = selectedResult.value?.timeseries_ic || {}
  const entries = Object.entries(ts).slice(0, 8)
  const colors = ['#26a69a', '#ef5350', '#42a5f5', '#ffca28', '#ab47bc', '#66bb6a', '#ff7043', '#8d6e63']
  return entries.map(([key, arr], idx) => ({
    key,
    color: colors[idx % colors.length],
    path: seriesPath(Array.isArray(arr) ? arr.map(pointValue) : [], 900, 420),
  }))
})

watch(() => form.value.symbol, () => {
  if (restoringSettings) return
  const avail = availableIntervals.value
  if (!avail.includes(form.value.interval) && avail.length) form.value.interval = avail[0]
})
watch(form, scheduleSaveSettings, { deep: true })
watch(horizonsInput, scheduleSaveSettings)

function toggleMonth(m) {
  const i = form.value.selected_months.indexOf(m)
  if (i >= 0) form.value.selected_months.splice(i, 1)
  else form.value.selected_months.push(m)
}
function toggleFactor(name) {
  const i = form.value.factor_names.indexOf(name)
  if (i >= 0) form.value.factor_names.splice(i, 1)
  else form.value.factor_names.push(name)
}
function selectAllFactors() { form.value.factor_names = factors.value.map(f => f.name) }
function ensureRegimeDimensions() {
  const current = new Map((form.value.regime_filter.dimensions || []).map(d => [d.dimension, d]))
  form.value.regime_filter.dimensions = (regimeOptions.value.dimensions || []).map(dim => {
    const existing = current.get(dim.key)
    if (existing) return existing
    return {
      dimension: dim.key,
      enabled: false,
      selected_labels: [],
      params: { ...(regimeOptions.value.defaults?.[dim.key] || {}) },
    }
  })
}
function dimState(key) {
  let dim = form.value.regime_filter.dimensions.find(d => d.dimension === key)
  if (!dim) {
    dim = { dimension: key, enabled: false, selected_labels: [], params: { ...(regimeOptions.value.defaults?.[key] || {}) } }
    form.value.regime_filter.dimensions.push(dim)
  }
  return dim
}
function toggleRegimeLabel(dimKey, label) {
  const dim = dimState(dimKey)
  if (!dim.enabled) dim.enabled = true
  const i = dim.selected_labels.indexOf(label)
  if (i >= 0) dim.selected_labels.splice(i, 1)
  else dim.selected_labels.push(label)
}
function researchSettingsPayload() {
  return {
    symbol: form.value.symbol,
    interval: form.value.interval,
    use_tick_features: form.value.use_tick_features,
    horizons: horizonsInput.value,
    quantiles: form.value.quantiles,
    entry_lag: form.value.entry_lag,
    train_ratio: form.value.train_ratio,
    factors: form.value.factor_names,
    selected_months: form.value.selected_months,
    regime_filter: form.value.regime_filter,
  }
}
function scheduleSaveSettings() {
  if (!settingsReady.value || restoringSettings) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(async () => {
    try {
      await settingsApi.update({ research_lab_config: researchSettingsPayload() })
    } catch { /* persistence is best-effort */ }
  }, 300)
}
function restoreResearchSettings(saved) {
  if (!saved || typeof saved !== 'object') return
  Object.assign(form.value, {
    symbol: saved.symbol ?? form.value.symbol,
    interval: saved.interval ?? form.value.interval,
    selected_months: Array.isArray(saved.selected_months) ? saved.selected_months : form.value.selected_months,
    factor_names: Array.isArray(saved.factors) ? saved.factors : (Array.isArray(saved.factor_names) ? saved.factor_names : form.value.factor_names),
    quantiles: saved.quantiles ?? form.value.quantiles,
    entry_lag: saved.entry_lag ?? form.value.entry_lag,
    train_ratio: saved.train_ratio ?? form.value.train_ratio,
    use_tick_features: saved.use_tick_features ?? form.value.use_tick_features,
    regime_filter: saved.regime_filter ?? form.value.regime_filter,
  })
  horizonsInput.value = saved.horizons ?? horizonsInput.value
}
function resultHorizons(res) {
  if (!res?.summary?.length) return []
  return Object.keys(res.summary[0]).filter(k => k.startsWith('IC_h')).map(k => parseInt(k.replace('IC_h', ''))).sort((a, b) => a - b)
}
function fmtIC(v) { return typeof v === 'number' ? v.toFixed(4) : '—' }
function icColor(v) {
  if (v == null) return 'text-dim'
  if (v > 0.05) return 'text-up'
  if (v < -0.05) return 'text-down'
  return 'text-text'
}
function pointValue(p) {
  if (Array.isArray(p)) return Number(p[1])
  if (p && typeof p === 'object') return Number(p.ic ?? p.IC ?? p.value ?? 0)
  return Number(p)
}
function seriesPath(vals, w, h) {
  const clean = vals.filter(Number.isFinite)
  if (!clean.length) return ''
  const min = Math.min(...clean)
  const max = Math.max(...clean)
  return clean.map((v, i) => {
    const x = 20 + i * ((w - 40) / Math.max(1, clean.length - 1))
    const y = 20 + (1 - (v - min) / Math.max(1e-9, max - min)) * (h - 40)
    return `${i ? 'L' : 'M'}${x} ${y}`
  }).join(' ')
}

async function runResearch() {
  if (running.value) return
  if (!form.value.selected_months.length) { error.value = '請先選擇月份'; return }
  if (!form.value.factor_names.length) { error.value = '請先選擇因子'; return }
  running.value = true
  error.value = ''
  progress.value = 'Submitting research job...'
  result.value = null
  try {
    const horizons = horizonsInput.value.split(',').map(Number).filter(Boolean)
    const { data } = await researchApi.run({ ...form.value, horizons })
    await pollJob(data.job_id)
  } catch (e) {
    error.value = e.message
    running.value = false
  }
}
async function pollJob(jobId) {
  while (true) {
    await new Promise(r => setTimeout(r, 2000))
    const { data } = await researchApi.getJob(jobId)
    progress.value = data.progress || ''
    if (data.status === 'done') {
      result.value = data.result
      activeRegime.value = Object.keys(data.result || {})[0] || '(all)'
      running.value = false
      progress.value = ''
      return
    }
    if (data.status === 'error') {
      error.value = data.error
      running.value = false
      progress.value = ''
      return
    }
  }
}
function exportJson() {
  const blob = new Blob([JSON.stringify(result.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `research-${form.value.symbol}-${form.value.interval}.json`
  a.click()
  URL.revokeObjectURL(url)
}
async function importJson(ev) {
  const file = ev.target.files?.[0]
  if (!file) return
  result.value = JSON.parse(await file.text())
  activeRegime.value = Object.keys(result.value || {})[0] || '(all)'
  ev.target.value = ''
}

onMounted(async () => {
  try {
    restoringSettings = true
    const [fRes, adRes, rgRes, settings] = await Promise.all([
      researchApi.factors(),
      backtestApi.availableData(),
      researchApi.regimeOptions(),
      settingsApi.get(),
    ])
    factors.value = fRes.data.factors || []
    regimeOptions.value = rgRes.data || regimeOptions.value
    klineRecords.value = adRes.data.klines || []
    if (factors.value.length) form.value.factor_names = factors.value.slice(0, 24).map(f => f.name)
    if (availableSymbols.value.length) form.value.symbol = availableSymbols.value[0]
    form.value.selected_months = availableMonths.value.slice(-3)
    restoreResearchSettings(settings.data?.research_lab_config)
    ensureRegimeDimensions()
    if (!availableIntervals.value.includes(form.value.interval) && availableIntervals.value.length) {
      form.value.interval = availableIntervals.value[0]
    }
  } catch { /* ignore */ }
  finally {
    restoringSettings = false
    settingsReady.value = true
  }
})
</script>

<style scoped>
.research-root { height: calc(100vh - 44px); display: grid; grid-template-columns: 340px minmax(0, 1fr); overflow: hidden; }
.research-sidebar { border-right: 1px solid #263245; padding: 8px; overflow-y: auto; background: #101621; }
.research-main { min-width: 0; display: grid; grid-template-rows: 38px minmax(0, 1fr); overflow: hidden; }
.panel { background: #151c2a; border: 1px solid #263245; border-radius: 6px; padding: 10px; margin-bottom: 8px; min-width: 0; }
.panel.tight { margin: 0; height: 100%; overflow: auto; }
.panel-title { color: #8fe7d8; font-size: 12px; font-weight: 700; margin-bottom: 8px; }
.field-grid, .param-grid { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 6px; align-items: center; }
.field-grid label, .param-grid label, .hint { color: #8f96a8; font-size: 11px; }
.check-row { color: #8f96a8; font-size: 12px; }
.solo { display: block; margin-top: 10px; }
.config-row { width: 100%; display: flex; justify-content: space-between; align-items: center; background: #20283a; border: 1px solid #334058; border-radius: 6px; color: #dce3ee; padding: 7px 9px; margin-bottom: 6px; font-size: 12px; }
.config-row em { color: #8f96a8; font-style: normal; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.disabled-row { opacity: 0.75; }
.picker { display: flex; flex-wrap: wrap; gap: 4px; max-height: 160px; overflow: auto; margin: 0 0 8px; padding: 4px; border: 1px solid #263245; background: #101621; }
.factor-picker { max-height: 220px; }
.compact-picker { max-height: 86px; }
.compact-picker.muted { opacity: 0.45; }
.picker button, .mini-actions button { font-size: 10px; border: 1px solid #334058; color: #8f96a8; padding: 2px 6px; border-radius: 4px; }
.picker button.active { border-color: #26a69a; color: #f2f5f9; background: #1f6f6644; }
.mini-actions { width: 100%; display: flex; gap: 4px; }
.slice-modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin-bottom: 8px; }
.slice-modes button { border: 1px solid #334058; color: #8f96a8; border-radius: 4px; padding: 3px 4px; font-size: 10px; }
.slice-modes button.active { border-color: #26a69a; background: #1f6f6644; color: #f2f5f9; }
.regime-panel { border: 1px solid #263245; background: #101621; padding: 6px; margin-bottom: 8px; }
.regime-dim { border-top: 1px solid #263245; padding-top: 6px; margin-top: 6px; }
.compact-param { grid-template-columns: 96px minmax(0, 1fr); margin-bottom: 4px; }
.action-row { display: grid; grid-template-columns: 1fr 74px 74px; gap: 6px; }
.import-label { text-align: center; cursor: pointer; }
.import-label input { display: none; }
.tabs { display: flex; overflow-x: auto; background: #151c2a; border-bottom: 1px solid #263245; }
.tabs button { color: #8f96a8; border-right: 1px solid #263245; padding: 0 12px; font-size: 12px; white-space: nowrap; }
.tabs button.active { color: #f2f5f9; background: #20283a; border-top: 2px solid #26a69a; }
.tab-body { min-height: 0; overflow: auto; padding: 8px; }
.empty-state { height: 100%; display: grid; place-items: center; color: #8f96a8; }
.dense-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.dense-table th { color: #aab3c2; background: #182132; position: sticky; top: 0; z-index: 1; }
.dense-table th, .dense-table td { padding: 5px 7px; border: 1px solid #263245; text-align: right; white-space: nowrap; }
.dense-table th:first-child, .dense-table td:first-child { text-align: left; }
.dense-table tr.selected { background: #23423f; }
.chart-svg { width: 100%; height: 100%; min-height: 360px; background: #131722; border: 1px solid #263245; }
.viz-grid { height: 100%; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.grid-heatmap { display: grid; gap: 1px; font-size: 10px; min-width: 520px; }
.grid-heatmap b, .grid-heatmap span { min-height: 22px; padding: 4px; color: #d1d4dc; background: #101621; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grid-heatmap b { color: #8f96a8; background: #182132; }
@media (max-width: 980px) {
  .research-root { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
  .research-sidebar { max-height: 45vh; border-right: 0; border-bottom: 1px solid #263245; }
  .viz-grid { grid-template-columns: 1fr; }
}
</style>
