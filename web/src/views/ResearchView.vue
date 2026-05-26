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
          <span>Factors...</span><em>{{ form.factor_names.length || '—' }} / {{ factors.length }}</em>
        </button>
        <div v-if="activeConfig === 'factors'" class="picker factor-picker">
          <div class="factor-filters">
            <select v-model="factorSideFilter" class="select-field">
              <option value="">All Directions</option>
              <option value="long">Long</option>
              <option value="short">Short</option>
            </select>
            <select v-model="factorGroupFilter" class="select-field">
              <option value="">All Groups</option>
              <option v-for="g in factorGroups" :key="g" :value="g">{{ g }}</option>
            </select>
          </div>
          <div class="mini-actions">
            <button @click="checkVisibleFactors">Check Visible</button>
            <button @click="clearVisibleFactors">Clear Visible</button>
            <button @click="selectAllFactors">All</button>
            <button @click="form.factor_names = []">Clear</button>
          </div>
          <button v-for="f in visibleFactors" :key="f.name"
                  :title="f.description"
                  :class="{ active: form.factor_names.includes(f.name) }"
                  @click="toggleFactor(f.name)">
            {{ f.name }}<span v-if="f.requires_ticks"> [tick]</span>
          </button>
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
      <div v-if="result && resultStatus" class="hint text-up">{{ resultStatus }}</div>
    </aside>

    <main class="research-main">
      <nav class="tabs">
        <button v-for="t in tabs" :key="t" :class="{ active: activeTab === t }" @click="activeTab = t">{{ t }}</button>
      </nav>
      <div class="result-toolbar">
        <label>Regime</label>
        <select v-model="activeRegime" class="select-field">
          <option v-for="key in regimeKeys" :key="key" :value="key">{{ key }}</option>
        </select>
        <span v-if="selectedResult" class="text-dim">
          rows={{ Number(selectedResult.rows || 0).toLocaleString() }}
          · factors={{ selectedResult.summary?.length || 0 }}
          · unavailable={{ selectedResult.unavailable?.length || 0 }}
        </span>
        <button class="btn-ghost compact-action" :disabled="!selectedResult" @click="exportSelectedCsv">Export CSV</button>
      </div>

      <section class="tab-body">
        <div v-if="!selectedResult" class="empty-state">No research result loaded.</div>

        <template v-else-if="activeTab === 'Regime Matrix'">
          <div class="matrix-toolbar">
            <label>Metric</label>
            <select v-model="matrixMetric" class="select-field">
              <option v-for="opt in matrixMetricOptions" :key="opt.key" :value="opt.key">{{ opt.label }}</option>
            </select>
            <span class="text-dim">{{ regimeKeys.length }} regimes × {{ regimeMatrixRows.length }} factors</span>
          </div>
          <table class="dense-table">
            <thead>
              <tr>
                <th>Factor</th>
                <th v-for="key in regimeKeys" :key="key" @click="activeRegime = key">{{ displayRegimeKey(key) }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in regimeMatrixRows" :key="row.factor">
                <td>{{ row.factor }}</td>
                <td v-for="key in regimeKeys" :key="key" :class="[icColor(row.values[key]?.value), { selected: activeRegime === key }]" @click="activeRegime = key">
                  {{ fmtIC(row.values[key]?.value) }}
                  <span class="cell-sample">n={{ Number(row.values[key]?.n || 0).toLocaleString() }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="!regimeMatrixRows.length" class="empty-state compact">No matrix rows for this result.</div>
        </template>

        <template v-else-if="activeTab === 'Factor Ranking'">
          <ResultTable :rows="tabRows" />
        </template>

        <template v-else-if="activeTab === 'Orthogonal Ranking'">
          <ResultTable :rows="tabRows" />
        </template>

        <template v-else-if="activeTab === 'IC Time Series'">
          <svg viewBox="0 0 900 420" preserveAspectRatio="none" class="chart-svg">
            <line v-for="g in [60, 140, 220, 300, 380]" :key="g" x1="34" :y1="g" x2="886" :y2="g" stroke="#2a2e3966" />
            <line v-if="icTrainCutoffX != null" :x1="icTrainCutoffX" y1="20" :x2="icTrainCutoffX" y2="400" stroke="#ffca28" stroke-width="1" stroke-dasharray="5 4" />
            <path v-for="line in icLines" :key="line.key" :d="line.path" fill="none" :stroke="line.color" stroke-width="2" />
            <text v-for="(line, i) in icLines" :key="'l' + line.key" :x="42 + (i % 4) * 190" :y="24 + Math.floor(i / 4) * 16" :fill="line.color" font-size="11">
              {{ line.key }}
            </text>
          </svg>
          <div v-if="!icLines.length" class="empty-state compact">No IC time-series values for this result.</div>
        </template>

        <template v-else-if="activeTab === 'Visualization'">
          <div class="viz-scroll">
            <div class="viz-row">
              <div class="panel tight">
                <div class="panel-header">
                  <h2 class="panel-title">Monthly IC Heatmap</h2>
                  <div class="viz-ctrl">
                    <label>H</label>
                    <select v-model="vizMonthHorizon" class="viz-select">
                      <option v-for="h in vizHorizons(selectedResult?.stability_monthly)" :key="h" :value="h">{{ h }}</option>
                    </select>
                    <label>M</label>
                    <select v-model="vizMonthMetric" class="viz-select">
                      <option v-for="m in vizMetricOptions" :key="m.key" :value="m.key">{{ m.label }}</option>
                    </select>
                  </div>
                </div>
                <GridHeatmap
                  :rows="vizFilterRows(selectedResult?.stability_monthly, vizMonthHorizon)"
                  row-key="factor" col-key="period"
                  :value-key="vizMonthMetric"
                  :split-map="vizSplitMap(selectedResult?.stability_monthly)"
                />
              </div>
              <div class="panel tight">
                <div class="panel-header">
                  <h2 class="panel-title">Yearly IC Heatmap</h2>
                  <div class="viz-ctrl">
                    <label>H</label>
                    <select v-model="vizYearHorizon" class="viz-select">
                      <option v-for="h in vizHorizons(selectedResult?.stability_yearly)" :key="h" :value="h">{{ h }}</option>
                    </select>
                    <label>M</label>
                    <select v-model="vizYearMetric" class="viz-select">
                      <option v-for="m in vizMetricOptions" :key="m.key" :value="m.key">{{ m.label }}</option>
                    </select>
                  </div>
                </div>
                <GridHeatmap
                  :rows="vizFilterRows(selectedResult?.stability_yearly, vizYearHorizon)"
                  row-key="factor" col-key="period"
                  :value-key="vizYearMetric"
                  :split-map="vizSplitMap(selectedResult?.stability_yearly)"
                />
              </div>
            </div>
            <div class="viz-row">
              <div class="panel tight" style="grid-column: 1 / -1;">
                <div class="panel-header">
                  <h2 class="panel-title">Per-Year IC Bar Chart</h2>
                  <div class="viz-ctrl">
                    <label>H</label>
                    <select v-model.number="vizBarHorizon" class="viz-select">
                      <option v-for="h in vizHorizons(selectedResult?.stability_yearly)" :key="h" :value="h">{{ h }}</option>
                    </select>
                    <label>M</label>
                    <select v-model="vizBarMetric" class="viz-select">
                      <option v-for="m in vizMetricOptions" :key="m.key" :value="m.key">{{ m.label }}</option>
                    </select>
                  </div>
                </div>
                <YearlyICBar
                  :rows="selectedResult?.stability_yearly || []"
                  :horizon="vizBarHorizon"
                  :metric-key="vizBarMetric"
                  :split-map="vizSplitMap(selectedResult?.stability_yearly)"
                />
              </div>
            </div>
            <div class="viz-row">
              <div class="panel tight" style="grid-column: 1 / -1;">
                <h2 class="panel-title">Correlation Matrix</h2>
                <GridHeatmap :rows="selectedResult.factor_correlations || []" row-key="factor_a" col-key="factor_b" value-key="spearman_oos" />
              </div>
            </div>
          </div>
        </template>

        <template v-else-if="activeTab === 'IC by Horizon'">
          <ResultTable :rows="tabRows" />
        </template>

        <template v-else-if="activeTab === 'Quantiles'">
          <ResultTable :rows="tabRows" />
        </template>

        <template v-else-if="activeTab === 'Monthly Stability'">
          <div class="stability-toolbar">
            <label class="check-row">
              <input v-model="stabilityOosOnly" type="checkbox" /> OOS only
            </label>
            <span class="text-dim">
              {{ stabilityOosOnly ? oosStabilityRows('monthly').length : tabRows.length }} rows
            </span>
          </div>
          <StabilityTable :rows="stabilityOosOnly ? oosStabilityRows('monthly') : tabRows" />
        </template>

        <template v-else-if="activeTab === 'Yearly Stability'">
          <div class="stability-toolbar">
            <label class="check-row">
              <input v-model="stabilityOosOnly" type="checkbox" /> OOS only
            </label>
            <span class="text-dim">
              {{ stabilityOosOnly ? oosStabilityRows('yearly').length : tabRows.length }} rows
            </span>
          </div>
          <StabilityTable :rows="stabilityOosOnly ? oosStabilityRows('yearly') : tabRows" />
        </template>

        <template v-else-if="activeTab === 'Factor Correlations'">
          <ResultTable :rows="tabRows" />
        </template>

        <template v-else-if="activeTab === 'Unavailable'">
          <ResultTable :rows="tabRows" />
        </template>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch, h } from 'vue'
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
  render() {
    const rows = Array.isArray(this.rows) ? this.rows : []
    if (!rows.length) return h('div', { class: 'empty-state compact' }, 'No rows for selected regime.')
    return h('table', { class: 'dense-table' }, [
      h('thead', [
        h('tr', this.cols.map(c => h('th', { key: c }, c))),
      ]),
      h('tbody', rows.map((row, i) => h('tr', { key: i }, this.cols.map(c => (
        h('td', { key: c, class: this.cls(row?.[c]) }, this.fmt(row?.[c]))
      ))))),
    ])
  }
}

// StabilityTable — same as ResultTable but row background differs by IS/OOS split
const StabilityTable = {
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
    numCls(v) {
      return typeof v === 'number' && Math.abs(v) <= 1
        ? (v > 0.05 ? 'text-up' : v < -0.05 ? 'text-down' : 'text-text')
        : 'text-text'
    },
    rowCls(row) {
      const sp = row?.split || ''
      return sp === 'test' ? 'row-oos' : sp === 'train' ? 'row-is' : ''
    },
  },
  render() {
    const rows = Array.isArray(this.rows) ? this.rows : []
    if (!rows.length) return h('div', { class: 'empty-state compact' }, 'No rows for selected regime.')
    return h('table', { class: 'dense-table' }, [
      h('thead', [
        h('tr', this.cols.map(c => h('th', { key: c }, c))),
      ]),
      h('tbody', rows.map((row, i) => h('tr', { key: i, class: this.rowCls(row) }, this.cols.map(c => (
        h('td', { key: c, class: this.numCls(row?.[c]) }, this.fmt(row?.[c]))
      ))))),
    ])
  }
}

const GridHeatmap = {
  props: {
    rows: Array,
    rowKey: String,
    colKey: String,
    valueKey: { type: String, default: 'rank_ic' },
    splitMap: { type: Object, default: () => ({}) },
  },
  computed: {
    safeRows() { return Array.isArray(this.rows) ? this.rows : [] },
    rowLabels() { return [...new Set(this.safeRows.map(r => r[this.rowKey]).filter(Boolean))] },
    colLabels() {
      return [...new Set(this.safeRows.map(r => r[this.colKey]).filter(Boolean))].sort()
    },
    cells() {
      const vk = this.valueKey
      const maxAbs = Math.max(1e-9, ...this.safeRows.map(r => Math.abs(Number(r[vk] || 0))))
      return this.safeRows.map(r => ({
        x: this.colLabels.indexOf(r[this.colKey]),
        y: this.rowLabels.indexOf(r[this.rowKey]),
        v: Number(r[vk] || 0),
        key: `${r[this.rowKey]}:${r[this.colKey]}`,
        color: Number(r[vk] || 0) >= 0
          ? `rgba(38,166,154,${0.18 + Math.abs(Number(r[vk] || 0)) / maxAbs * 0.75})`
          : `rgba(239,83,80,${0.18 + Math.abs(Number(r[vk] || 0)) / maxAbs * 0.75})`
      }))
    }
  },
  render() {
    if (!this.safeRows.length) return h('div', { class: 'empty-state compact' }, 'No heatmap rows for selected regime.')
    const cellMap = new Map(this.cells.map(cell => [cell.key, cell]))
    const splitMap = this.splitMap || {}
    const children = [
      h('span'),
      ...this.colLabels.map(c => {
        const split = splitMap[c] || ''
        const isTest = split === 'test'
        return h('b', {
          key: `h:${c}`,
          class: isTest ? 'col-test' : 'col-train',
          title: isTest ? 'OOS (test)' : split === 'train' ? 'IS (train)' : split,
        }, c)
      }),
    ]
    for (const r of this.rowLabels) {
      children.push(h('b', { key: `r:${r}` }, r))
      for (const c of this.colLabels) {
        const cell = cellMap.get(`${r}:${c}`)
        const split = splitMap[c] || ''
        children.push(h(
          'span',
          {
            key: `${r}:${c}`,
            style: { background: cell?.color || '#151c2a' },
            class: split === 'test' ? 'cell-test' : split === 'train' ? 'cell-train' : '',
            title: cell ? `${r} × ${c}\n${this.valueKey}: ${Number(cell.v).toFixed(4)}` : '—',
          },
          cell ? Number(cell.v).toFixed(3) : '—',
        ))
      }
    }
    return h(
      'div',
      {
        class: 'grid-heatmap',
        style: { gridTemplateColumns: `120px repeat(${Math.max(1, this.colLabels.length)}, minmax(34px, 1fr))` },
      },
      children,
    )
  }
}

// ── Yearly IC Bar Chart ────────────────────────────────────────────────────
const YearlyICBar = {
  props: {
    rows:      { type: Array, default: () => [] },
    horizon:   { type: Number, default: 1 },
    metricKey: { type: String, default: 'oriented_rank_ic' },
    splitMap:  { type: Object, default: () => ({}) },
  },
  computed: {
    filtered() {
      return (this.rows || []).filter(r => r.horizon === this.horizon)
    },
    years() {
      return [...new Set(this.filtered.map(r => r.period))].sort()
    },
    factors() {
      return [...new Set(this.filtered.map(r => r.factor))]
    },
    dataMap() {
      const m = {}
      for (const r of this.filtered) {
        if (!m[r.factor]) m[r.factor] = {}
        m[r.factor][r.period] = { v: Number(r[this.metricKey] ?? 0), split: r.split || '' }
      }
      return m
    },
  },
  methods: {
    factorColor(idx) {
      const palette = ['#26a69a','#42a5f5','#ffca28','#ab47bc','#ff7043','#66bb6a','#8d6e63','#ef5350']
      return palette[idx % palette.length]
    },
  },
  render() {
    const { years, factors, dataMap } = this
    if (!years.length || !factors.length) {
      return h('div', { class: 'empty-state compact' }, 'No yearly IC data for this horizon.')
    }
    const W = 900, H = 300
    const pad = { top: 32, right: 16, bottom: 48, left: 50 }
    const chartW = W - pad.left - pad.right
    const chartH = H - pad.top - pad.bottom

    const allVals = factors.flatMap(f => years.map(y => dataMap[f]?.[y]?.v ?? 0))
    const maxAbs  = Math.max(0.01, ...allVals.map(Math.abs))
    const yScale  = v => pad.top + chartH * (1 - (v + maxAbs) / (2 * maxAbs))
    const yZero   = yScale(0)

    const groupW   = chartW / Math.max(1, years.length)
    const barW     = Math.max(2, (groupW * 0.8) / Math.max(1, factors.length))
    const groupPad = groupW * 0.1

    const elems = []

    // Y grid + labels
    for (const frac of [0, 0.25, 0.5, 0.75, 1]) {
      const v  = maxAbs * (1 - 2 * frac)
      const yp = yScale(v)
      elems.push(h('line', { x1: pad.left, y1: yp, x2: pad.left + chartW, y2: yp, stroke: '#2a2e3966', 'stroke-width': 1 }))
      elems.push(h('text', { x: pad.left - 4, y: yp + 4, 'text-anchor': 'end', fill: '#6b7280', 'font-size': 9 }, v.toFixed(2)))
    }
    // Zero line
    elems.push(h('line', { x1: pad.left, y1: yZero, x2: pad.left + chartW, y2: yZero, stroke: '#4a5568', 'stroke-width': 1, 'stroke-dasharray': '4 3' }))

    // Bars
    years.forEach((yr, yi) => {
      const gx = pad.left + groupPad + yi * groupW
      factors.forEach((fac, fi) => {
        const d    = dataMap[fac]?.[yr]
        const v    = d?.v ?? 0
        const sp   = d?.split || ''
        const isOOS = sp === 'test'
        const color = this.factorColor(fi)
        const bx   = gx + fi * barW
        const by   = v >= 0 ? yScale(v) : yZero
        const bh   = Math.abs(yScale(v) - yZero)
        elems.push(h('rect', {
          x: bx, y: by, width: barW - 1, height: Math.max(1, bh),
          fill: color,
          opacity: isOOS ? 1.0 : 0.45,
          stroke: isOOS ? color : 'none',
          'stroke-width': isOOS ? 1 : 0,
          rx: 1,
        }))
        elems.push(h('title', {}, `${fac} / ${yr}\n${this.metricKey}: ${v.toFixed(4)}\n${isOOS ? 'OOS' : 'IS'}`))
      })
      elems.push(h('text', {
        x: gx + (factors.length * barW) / 2, y: H - pad.bottom + 14,
        'text-anchor': 'middle', fill: '#8f96a8', 'font-size': 10,
      }, yr))
    })

    // Legend
    factors.forEach((fac, fi) => {
      const lx = pad.left + fi * 120
      elems.push(h('rect', { x: lx, y: 6, width: 10, height: 10, fill: this.factorColor(fi), rx: 2 }))
      elems.push(h('text', { x: lx + 14, y: 15, fill: '#8f96a8', 'font-size': 9, 'text-anchor': 'start' }, fac))
    })
    // IS/OOS legend
    elems.push(h('text', { x: W - 120, y: 15, fill: '#8f96a8', 'font-size': 9 }, '■ full = OOS  ■ dim = IS'))

    return h('svg', { viewBox: `0 0 ${W} ${H}`, class: 'bar-svg', preserveAspectRatio: 'xMidYMid meet' }, elems)
  }
}

const tabs = [
  'Regime Matrix', 'Factor Ranking', 'Orthogonal Ranking', 'IC Time Series',
  'Visualization', 'IC by Horizon', 'Quantiles', 'Monthly Stability',
  'Yearly Stability', 'Factor Correlations', 'Unavailable'
]
const activeTab = ref('Regime Matrix')
const activeConfig = ref('months')

// ── Visualization tab state ────────────────────────────────────────────────
const vizMonthHorizon = ref('')
const vizMonthMetric  = ref('oriented_rank_ic')
const vizYearHorizon  = ref('')
const vizYearMetric   = ref('oriented_rank_ic')
const vizBarHorizon   = ref(1)
const vizBarMetric    = ref('oriented_rank_ic')

// ── Stability tab state ────────────────────────────────────────────────────
const stabilityOosOnly = ref(false)
function oosStabilityRows(granularity) {
  const key = granularity === 'yearly' ? 'stability_yearly' : 'stability_monthly'
  return (selectedResult.value?.[key] || []).filter(r => r.split === 'test')
}
const vizMetricOptions = [
  { key: 'oriented_rank_ic', label: 'Oriented Rank IC' },
  { key: 'rank_ic',          label: 'Rank IC' },
  { key: 'ic',               label: 'Pearson IC' },
  { key: 'spread_qhigh_qlow', label: 'Q-Spread' },
]
function vizHorizons(rows) {
  if (!Array.isArray(rows)) return []
  return [...new Set(rows.map(r => r.horizon))].sort((a, b) => a - b)
}
function vizFilterRows(rows, horizon) {
  if (!Array.isArray(rows)) return []
  if (!horizon && rows.length) {
    const h = Math.min(...rows.map(r => r.horizon))
    return rows.filter(r => r.horizon === h)
  }
  return rows.filter(r => r.horizon === Number(horizon))
}
function vizSplitMap(rows) {
  if (!Array.isArray(rows)) return {}
  const map = {}
  for (const r of rows) {
    if (r.period && !(r.period in map)) map[r.period] = r.split || ''
  }
  return map
}
const activeRegime = ref('(all)')
const matrixMetric = ref('oos_oriented_rank_ic')
const factors = ref([])
const factorSideFilter = ref('')
const factorGroupFilter = ref('')
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

const factorNameSet = computed(() => new Set(factors.value.map(f => f.name)))
const factorGroups = computed(() => [...new Set(factors.value.map(f => f.group).filter(Boolean))])
const visibleFactors = computed(() => factors.value.filter(f => {
  if (factorSideFilter.value && !(f.sides || []).includes(factorSideFilter.value)) return false
  if (factorGroupFilter.value && f.group !== factorGroupFilter.value) return false
  return true
}))
const selectedResult = computed(() => {
  if (!result.value) return null
  if (isSingleResult(result.value)) return result.value
  return result.value[activeRegime.value] || Object.values(result.value).find(v => isSingleResult(v)) || null
})
const resultStatus = computed(() => {
  if (!result.value) return ''
  const regimes = isSingleResult(result.value) ? 1 : Object.keys(result.value).length
  const rows = Object.values(normalizeResultPayload(result.value)).reduce((sum, res) => sum + Number(res.rows || 0), 0)
  const factorsN = selectedResult.value?.summary?.length || 0
  return `Done | ${regimes} regime${regimes > 1 ? 's' : ''} | rows=${rows.toLocaleString()} | factors=${factorsN}`
})
const regimeRows = computed(() => {
  if (!result.value) return []
  return Object.entries(result.value).map(([key, res]) => {
    const top = (res.summary || [])[0] || {}
    return {
      key,
      rows: res.rows || 0,
      factor: top.factor || '—',
      ic: top.oos_oriented_rank_ic ?? top.oos_best_rank_ic ?? top.oriented_rank_ic ?? top.best_rank_ic,
      ir: top.oos_oriented_ic_ir ?? top.oos_ic_ir ?? top.oriented_ic_ir ?? top.ic_ir,
      unavailable: (res.unavailable || []).length,
    }
  })
})
const regimeKeys = computed(() => Object.keys(normalizeResultPayload(result.value)))
const matrixMetricOptions = [
  { label: 'OOS Rank IC', key: 'oos_oriented_rank_ic', nKey: 'oos_sample_count' },
  { label: 'OOS IC IR', key: 'oos_oriented_ic_ir', nKey: 'oos_sample_count' },
  { label: 'OOS t-stat', key: 'oos_oriented_ic_t_stat', nKey: 'oos_sample_count' },
  { label: 'IS Rank IC', key: 'oriented_rank_ic', nKey: 'sample_count' },
  { label: 'IS IC IR', key: 'oriented_ic_ir', nKey: 'sample_count' },
]
const regimeMatrixRows = computed(() => {
  const normalized = normalizeResultPayload(result.value)
  const factorNames = []
  const seen = new Set()
  for (const key of regimeKeys.value) {
    for (const row of normalized[key]?.summary || []) {
      if (row?.factor && !seen.has(row.factor)) {
        seen.add(row.factor)
        factorNames.push(row.factor)
      }
    }
  }
  const opt = matrixMetricOptions.find(o => o.key === matrixMetric.value) || matrixMetricOptions[0]
  return factorNames.map(factor => {
    const values = {}
    for (const key of regimeKeys.value) {
      const row = (normalized[key]?.summary || []).find(r => r.factor === factor) || {}
      values[key] = {
        value: Number.isFinite(Number(row[opt.key])) ? Number(row[opt.key]) : null,
        n: row[opt.nKey] || 0,
      }
    }
    return { factor, values }
  })
})
const tabRows = computed(() => {
  const res = selectedResult.value
  if (!res) return []
  const key = {
    'Factor Ranking': 'summary',
    'Orthogonal Ranking': 'orthogonal_summary',
    'IC by Horizon': 'metrics',
    'Quantiles': 'quantiles',
    'Monthly Stability': 'stability_monthly',
    'Yearly Stability': 'stability_yearly',
    'Factor Correlations': 'factor_correlations',
    'Unavailable': 'unavailable',
  }[activeTab.value]
  return key ? (res[key] || []) : []
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
  const entries = Object.entries(ts.factors || {}).slice(0, 8)
  const colors = ['#26a69a', '#ef5350', '#42a5f5', '#ffca28', '#ab47bc', '#66bb6a', '#ff7043', '#8d6e63']
  return entries.map(([key, arr], idx) => ({
    key,
    color: colors[idx % colors.length],
    path: seriesPath(Array.isArray(arr) ? arr.map(pointValue) : [], 900, 420),
  }))
})
const icTrainCutoffX = computed(() => {
  const ts = selectedResult.value?.timeseries_ic || {}
  const timestamps = ts.timestamps || []
  const cutoff = Number(ts.train_cutoff_ts || 0)
  if (!timestamps.length || !cutoff) return null
  let idx = timestamps.findIndex(t => Number(t) >= cutoff)
  if (idx < 0) idx = timestamps.length - 1
  return 20 + idx * (860 / Math.max(1, timestamps.length - 1))
})

watch(() => form.value.symbol, () => {
  if (restoringSettings) return
  const avail = availableIntervals.value
  if (!avail.includes(form.value.interval) && avail.length) form.value.interval = avail[0]
})
watch(form, scheduleSaveSettings, { deep: true })
watch(horizonsInput, scheduleSaveSettings)
watch(factorSideFilter, scheduleSaveSettings)
watch(factorGroupFilter, scheduleSaveSettings)
watch(regimeKeys, selectInitialRegime)

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
function checkVisibleFactors() {
  const set = new Set(form.value.factor_names)
  for (const f of visibleFactors.value) set.add(f.name)
  form.value.factor_names = [...set].filter(name => factorNameSet.value.has(name))
}
function clearVisibleFactors() {
  const visible = new Set(visibleFactors.value.map(f => f.name))
  form.value.factor_names = form.value.factor_names.filter(name => !visible.has(name))
}
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
    factor_side_filter: factorSideFilter.value,
    factor_group_filter: factorGroupFilter.value,
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
    selected_months: Array.isArray(saved.selected_months) && saved.selected_months.length ? saved.selected_months : form.value.selected_months,
    factor_names: Array.isArray(saved.factors) ? saved.factors : (Array.isArray(saved.factor_names) ? saved.factor_names : form.value.factor_names),
    quantiles: saved.quantiles ?? form.value.quantiles,
    entry_lag: saved.entry_lag ?? form.value.entry_lag,
    train_ratio: saved.train_ratio ?? form.value.train_ratio,
    use_tick_features: saved.use_tick_features ?? form.value.use_tick_features,
    regime_filter: saved.regime_filter ?? form.value.regime_filter,
  })
  horizonsInput.value = saved.horizons ?? horizonsInput.value
  factorSideFilter.value = saved.factor_side_filter ?? factorSideFilter.value
  factorGroupFilter.value = saved.factor_group_filter ?? factorGroupFilter.value
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
function isSingleResult(value) {
  return !!value && typeof value === 'object' && Array.isArray(value.summary) && Array.isArray(value.metrics)
}
function normalizeResultPayload(value) {
  if (!value || typeof value !== 'object') return {}
  if (isSingleResult(value)) return { '(all)': value }
  const out = {}
  for (const [key, res] of Object.entries(value)) {
    if (isSingleResult(res)) out[key] = res
  }
  return out
}
function normalizedRegimeFilter() {
  const rf = form.value.regime_filter
  const active = (rf?.dimensions || []).some(d => d.enabled && d.selected_labels?.length)
  return active ? rf : null
}
function validSelectedFactors() {
  return form.value.factor_names.filter(name => factorNameSet.value.has(name))
}
function selectInitialRegime() {
  const normalized = normalizeResultPayload(result.value)
  const keys = Object.keys(normalized)
  if (!keys.length) {
    activeRegime.value = '(all)'
    return
  }
  if (normalized[activeRegime.value]) return
  activeRegime.value = keys.find(key => {
    const res = normalized[key]
    return (res.summary || []).length || (res.metrics || []).length || (res.quantiles || []).length
  }) || keys[0]
}
function displayRegimeKey(key) {
  return key.split('+').map(part => {
    const [dim, label] = part.split('=')
    const short = { session: 'Sess', market_vol: 'MktVol', vwap_zone: 'VWAP', vol_profile: 'VP' }[dim] || dim
    return label ? `${short}: ${label}` : part
  }).join(' × ')
}

async function runResearch() {
  if (running.value) return
  if (!form.value.selected_months.length) { error.value = '請先選擇月份'; return }
  const factor_names = validSelectedFactors()
  if (!factor_names.length) { error.value = '請先選擇有效因子；目前選到的因子已不存在於 registry。'; return }
  if (factor_names.length !== form.value.factor_names.length) {
    form.value.factor_names = factor_names
  }
  running.value = true
  error.value = ''
  progress.value = 'Submitting research job...'
  result.value = null
  try {
    const horizons = horizonsInput.value.split(',').map(Number).filter(Boolean)
    const { data } = await researchApi.run({
      ...form.value,
      factor_names,
      horizons,
      regime_filter: normalizedRegimeFilter(),
    })
    await pollJob(data.job_id)
  } catch (e) {
    error.value = e.message
    running.value = false
  }
}
async function pollJob(jobId) {
  let consecutiveErrors = 0
  while (true) {
    await new Promise(r => setTimeout(r, 2000))
    try {
      const { data } = await researchApi.getJob(jobId)
      consecutiveErrors = 0
      progress.value = data.progress || ''
      if (data.status === 'done') {
        result.value = normalizeResultPayload(data.result)
        selectInitialRegime()
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
    } catch (e) {
      consecutiveErrors++
      if (consecutiveErrors >= 10) {
        error.value = e.message
        running.value = false
        return
      }
      progress.value = `Polling... (retry ${consecutiveErrors})`
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
function downloadText(filename, content, type = 'text/plain') {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
function csvEscape(value) {
  if (value == null) return ''
  const s = typeof value === 'object' ? JSON.stringify(value) : String(value)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}
function rowsToCsv(rows) {
  if (!rows?.length) return ''
  const cols = [...new Set(rows.flatMap(row => Object.keys(row || {})))]
  return [cols.join(','), ...rows.map(row => cols.map(c => csvEscape(row[c])).join(','))].join('\n')
}
function exportSelectedCsv() {
  if (!selectedResult.value) return
  const rows = activeTab.value === 'Regime Matrix'
    ? regimeMatrixRows.value.map(row => {
        const out = { factor: row.factor }
        for (const key of regimeKeys.value) {
          out[key] = row.values[key]?.value
          out[`${key}_n`] = row.values[key]?.n
        }
        return out
      })
    : (selectedResult.value[{
        'Factor Ranking': 'summary',
        'Orthogonal Ranking': 'orthogonal_summary',
        'IC by Horizon': 'metrics',
        'Quantiles': 'quantiles',
        'Monthly Stability': 'stability_monthly',
        'Yearly Stability': 'stability_yearly',
        'Factor Correlations': 'factor_correlations',
        'Unavailable': 'unavailable',
      }[activeTab.value]] || selectedResult.value.summary || [])
  const csv = rowsToCsv(rows)
  if (!csv) return
  const regime = activeRegime.value.replace(/[^a-zA-Z0-9_-]+/g, '_')
  const tab = activeTab.value.replace(/[^a-zA-Z0-9_-]+/g, '_')
  downloadText(`research-${form.value.symbol}-${form.value.interval}-${regime}-${tab}.csv`, csv, 'text/csv')
}
async function importJson(ev) {
  const file = ev.target.files?.[0]
  if (!file) return
  result.value = normalizeResultPayload(JSON.parse(await file.text()))
  selectInitialRegime()
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
    form.value.factor_names = form.value.factor_names.filter(name => factorNameSet.value.has(name))
    if (!form.value.factor_names.length && factors.value.length) {
      form.value.factor_names = factors.value.slice(0, 24).map(f => f.name)
    }
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
.research-main { min-width: 0; display: grid; grid-template-rows: 38px 34px minmax(0, 1fr); overflow: hidden; }
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
.factor-filters { width: 100%; display: grid; grid-template-columns: 1fr; gap: 4px; }
.factor-filters .select-field { width: 100%; height: 28px; font-size: 11px; padding: 2px 6px; }
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
.result-toolbar { display: flex; align-items: center; gap: 8px; padding: 4px 8px; background: #101621; border-bottom: 1px solid #263245; font-size: 11px; color: #8f96a8; }
.result-toolbar .select-field { width: 240px; height: 24px; padding: 1px 6px; font-size: 11px; }
.compact-action { margin-left: auto; padding: 2px 8px; font-size: 11px; }
.tab-body { min-height: 0; overflow: auto; padding: 8px; }
.empty-state { height: 100%; display: grid; place-items: center; color: #8f96a8; }
.empty-state.compact { height: auto; min-height: 80px; }
.matrix-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; color: #8f96a8; font-size: 11px; }
.matrix-toolbar .select-field { width: 150px; height: 26px; padding: 2px 6px; font-size: 11px; }
.dense-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.dense-table th { color: #aab3c2; background: #182132; position: sticky; top: 0; z-index: 1; }
.dense-table th, .dense-table td { padding: 5px 7px; border: 1px solid #263245; text-align: right; white-space: nowrap; }
.dense-table th:first-child, .dense-table td:first-child { text-align: left; }
.dense-table tr.selected { background: #23423f; }
.dense-table td.selected { background: #23423f; }
.cell-sample { display: block; margin-top: 2px; color: #6f7888; font-size: 10px; }
.chart-svg { width: 100%; height: 100%; min-height: 360px; background: #131722; border: 1px solid #263245; }
.viz-scroll { height: 100%; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.viz-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; min-height: 260px; }
.panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.panel-header .panel-title { margin-bottom: 0; }
.viz-ctrl { display: flex; align-items: center; gap: 4px; font-size: 10px; color: #8f96a8; }
.viz-select { height: 22px; padding: 1px 4px; font-size: 10px; background: #20283a; border: 1px solid #334058; color: #d1d4dc; border-radius: 3px; }
.grid-heatmap { display: grid; gap: 1px; font-size: 10px; min-width: 520px; }
.grid-heatmap b, .grid-heatmap span { min-height: 22px; padding: 4px; color: #d1d4dc; background: #101621; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grid-heatmap b { color: #8f96a8; background: #182132; }
.bar-svg { width: 100%; height: 300px; background: #131722; border: 1px solid #263245; border-radius: 4px; display: block; }
.stability-toolbar { display: flex; align-items: center; gap: 12px; padding: 4px 8px; font-size: 11px; color: #8f96a8; border-bottom: 1px solid #263245; background: #101621; }
.dense-table tr.row-oos td { background: #141f2a; }
.dense-table tr.row-is td { opacity: 0.70; }
.grid-heatmap b.col-test { color: #8fb8d4; background: #1e3a50; }
.grid-heatmap b.col-train { color: #5a6272; background: #182132; }
.grid-heatmap span.cell-test { outline: 1px solid #1e3a5044; }
.grid-heatmap span.cell-train { opacity: 0.80; }
@media (max-width: 980px) {
  .research-root { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
  .research-sidebar { max-height: 45vh; border-right: 0; border-bottom: 1px solid #263245; }
  .viz-row { grid-template-columns: 1fr; }
}
</style>
