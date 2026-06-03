<template>
  <div class="snap-backdrop" @click.self="$emit('close')">
    <div class="snap-window">
      <div class="snap-titlebar">
        <button class="btn-ghost nav-btn" :disabled="snapshot.trade_idx <= 0" @click="$emit('prev')" title="上一筆">&#8249;</button>
        <div class="snap-title-center">
          <div class="text-sm font-medium">Trade #{{ snapshot.trade_idx + 1 }}{{ totalTrades ? ' / ' + totalTrades : '' }} · {{ trade.dir?.toUpperCase() }}</div>
          <div class="text-xs text-dim">
            Entry {{ fmtTime(trade.entry_time) }} UTC · {{ trade.exit_label || 'Exit' }} ·
            <span :class="trade.net_pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtPnl(trade.net_pnl) }} USDT</span>
          </div>
        </div>
        <button class="btn-ghost nav-btn" :disabled="totalTrades > 0 && snapshot.trade_idx >= totalTrades - 1" @click="$emit('next')" title="下一筆">&#8250;</button>
        <button class="btn-ghost ml-4 px-2 py-0.5 text-[10px]" @click="resetZoom">重置縮放</button>
        <button class="btn-ghost ml-auto" @click="$emit('close')">關閉</button>
      </div>
      <div v-if="badgeText" class="badge-row">{{ badgeText }}</div>

      <!-- ── Main candlestick chart ── -->
      <div class="chart-sync-container"
           @mousemove="onMouseMove"
           @mouseleave="onMouseLeave"
           @wheel="onWheel"
           @mousedown="onMouseDown">
        <svg class="snap-chart" viewBox="0 0 980 500" preserveAspectRatio="none">
          <defs>
            <clipPath id="chartAreaClip">
              <rect x="46" y="0" width="912" height="500" />
            </clipPath>
          </defs>

          <line v-for="g in gridY" :key="'gy' + g" x1="46" :y1="g" x2="958" :y2="g" stroke="#2a2e3966" stroke-width="1" />
          <line v-for="g in gridX" :key="'gx' + g" :x1="g" y1="24" :x2="g" y2="480" stroke="#2a2e3955" stroke-width="1" />

          <!-- Clipped Chart Content -->
          <g clip-path="url(#chartAreaClip)">
            <rect v-if="snapshot.k0_index != null" :x="x(snapshot.k0_index) - candleW * 0.78" y="24"
                  :width="candleW * 1.56" height="456" fill="#ff980024" />
            <text v-if="snapshot.k0_index != null" :x="x(snapshot.k0_index)" y="42" text-anchor="middle" fill="#ff9800" font-size="11">k0</text>

            <!-- Sigma band polylines -->
            <g v-if="hasSigma">
              <path v-for="band in sigmaLines" :key="band.key"
                :d="band.d"
                :stroke="band.color"
                :stroke-width="band.key === 'vwap' ? 1.4 : 1.0"
                :stroke-dasharray="band.dash || undefined"
                fill="none"
                stroke-linejoin="round"
              />
            </g>

            <g v-for="(k, i) in bars" :key="k.time_ms">
              <line
                :x1="x(i)" :x2="x(i)"
                :y1="y(k.high)" :y2="y(k.low)"
                :stroke="k.close >= k.open ? '#26a69a' : '#ef5350'"
                stroke-width="1.2"
              />
              <rect
                :x="x(i) - candleW / 2"
                :y="Math.min(y(k.open), y(k.close))"
                :width="candleW"
                :height="Math.max(2, Math.abs(y(k.open) - y(k.close)))"
                :fill="k.close >= k.open ? '#26a69a' : '#ef5350'"
              />
              <rect
                :x="x(i) - candleW / 2"
                :y="volY(k.volume)"
                :width="candleW"
                :height="480 - volY(k.volume)"
                :fill="k.close >= k.open ? '#26a69a44' : '#ef535044'"
              />
            </g>

            <line v-if="stopPrice != null" x1="46" :y1="y(stopPrice)" x2="958" :y2="y(stopPrice)" stroke="#ef5350" stroke-dasharray="5 5" />
            <line v-if="tpPrice != null" x1="46" :y1="y(tpPrice)" x2="958" :y2="y(tpPrice)" stroke="#26a69a" stroke-dasharray="5 5" />

            <path :d="entryMarkerPath" fill="#2196f3" stroke="#90caf9" stroke-width="1.2" />
            <path v-if="snapshot.exit_index != null" :d="exitMarkerPath" :fill="exitColor" :stroke="exitColor" stroke-width="1.2" />

            <circle
              v-for="(t, i) in sampledTicks"
              :key="i"
              :cx="x(snapshot.entry_index) + tickOffset(i)"
              :cy="y(t.price)"
              r="2.2"
              :fill="t.is_sell ? '#ef535088' : '#26a69a88'"
            />
          </g>

          <!-- Non-Clipped Price Labels (Always visible at edges) -->
          <text v-if="stopPrice != null" x="952" :y="y(stopPrice) - 4" text-anchor="end" fill="#ef5350" font-size="11">SL {{ fmt(stopPrice) }}</text>
          <text v-if="tpPrice != null" x="952" :y="y(tpPrice) + 14" text-anchor="end" fill="#26a69a" font-size="11">{{ pocPrice != null ? 'TP/POC' : 'TP' }} {{ fmt(tpPrice) }}</text>
          <text :x="x(snapshot.entry_index) + 10" :y="y(trade.entry) - 10" fill="#90caf9" font-size="11" font-weight="600">Entry {{ fmt(trade.entry) }}</text>
          <text v-if="snapshot.exit_index != null" :x="x(snapshot.exit_index) + 10" :y="y(trade.exit) + 16" :fill="exitColor" font-size="11" font-weight="600">
            {{ trade.exit_label || 'Exit' }} {{ fmt(trade.exit) }}
          </text>

          <template v-for="band in sigmaLines" :key="'lb' + band.key">
            <text v-if="band.labelY != null" x="958" :y="band.labelY - 2" text-anchor="end" :fill="band.color" font-size="9" font-weight="600">{{ band.label }}</text>
          </template>

          <!-- Vertical Crosshair -->
          <line v-if="hoverIdx !== null" :x1="x(hoverIdx)" y1="24" :x2="x(hoverIdx)" y2="480" stroke="#ffffff66" stroke-width="1" stroke-dasharray="3 3" pointer-events="none" />
          <!-- Info box on crosshair -->
          <g v-if="hoverIdx !== null && bars[hoverIdx]">
            <rect :x="x(hoverIdx) + (hoverIdx > (viewStart + viewEnd) / 2 ? -105 : 5)" :y="mouseY - 15" width="100" height="34" rx="3" fill="#1e222dee" stroke="#334058" stroke-width="1" pointer-events="none" />
            <text :x="x(hoverIdx) + (hoverIdx > (viewStart + viewEnd) / 2 ? -100 : 10)" :y="mouseY" fill="#ffffff" font-size="10" font-weight="600" pointer-events="none">
              P: {{ fmt(bars[hoverIdx].close) }}
            </text>
            <text :x="x(hoverIdx) + (hoverIdx > (viewStart + viewEnd) / 2 ? -100 : 10)" :y="mouseY + 12" fill="#787b86" font-size="9" pointer-events="none">
              {{ fmtTime(bars[hoverIdx].time_ms).slice(11) }}
            </text>
          </g>
        </svg>

        <!-- ── CVD₁₅ₘ mini panel ── -->
        <svg v-if="hasCvd" class="cvd-chart" :viewBox="`0 0 980 ${CVD_H}`" preserveAspectRatio="none">
          <defs>
            <clipPath id="cvdAreaClip">
              <rect x="46" y="0" width="912" :height="CVD_H" />
            </clipPath>
          </defs>

          <!-- Zero reference -->
          <line x1="46" :y1="cvdZeroY" x2="958" :y2="cvdZeroY" stroke="#3a4052" stroke-width="1" stroke-dasharray="3 2" />

          <g clip-path="url(#cvdAreaClip)">
            <!-- Warmup region tint -->
            <template v-for="(f, i) in windowFeatures" :key="'wu' + i">
              <rect v-if="f?.wu" :x="x(i) - cvdBarW / 2" y="0" :width="cvdBarW" :height="CVD_H" fill="#ffa72608" />
            </template>
            <!-- CVD area fill -->
            <path v-if="cvdAreaAbove" :d="cvdAreaAbove" fill="#42a5f522" />
            <path v-if="cvdAreaBelow" :d="cvdAreaBelow" fill="#ef535022" />
            <!-- CVD line -->
            <path :d="cvdLinePath" fill="none" stroke="#64b5f6" stroke-width="1.8" stroke-linejoin="round" />
            <!-- Markers -->
            <circle v-for="(m, i) in cvdDivMarkers" :key="'div' + i" :cx="x(m.idx)" :cy="cvdY(m.val)" r="4.5" :fill="m.bull ? '#26a69a' : '#ef5350'" opacity="0.95" stroke="#ffffff44" stroke-width="0.5" />
            <polygon v-for="(m, i) in cvdAccMarkers" :key="'acc' + i" :points="accTriangle(m)" :fill="m.bull ? '#26a69acc' : '#ef5350cc'" />
          </g>

          <!-- Panel label -->
          <text x="52" y="14" fill="#546e7a" font-size="10" font-weight="600">CVD₁₅ₘ</text>
          <text v-if="lastCvdVal != null" x="958" y="14" text-anchor="end" fill="#64b5f6" font-size="10" font-weight="600">{{ fmtCvd(lastCvdVal) }}</text>

          <!-- Vertical Crosshair Sync -->
          <line v-if="hoverIdx !== null" :x1="x(hoverIdx)" y1="0" :x2="x(hoverIdx)" :y2="CVD_H" stroke="#ffffff66" stroke-width="1" stroke-dasharray="3 3" pointer-events="none" />
          <g v-if="hoverIdx !== null">
            <rect :x="x(hoverIdx) + (hoverIdx > (viewStart + viewEnd) / 2 ? -75 : 5)" :y="CVD_H / 2 - 10" width="70" height="20" rx="3" fill="#1e222dee" stroke="#334058" stroke-width="1" pointer-events="none" />
            <text :x="x(hoverIdx) + (hoverIdx > (viewStart + viewEnd) / 2 ? -70 : 10)" :y="CVD_H / 2 + 4" fill="#64b5f6" font-size="10" font-weight="600" pointer-events="none">
              {{ cvdVals[hoverIdx] ? fmtCvd(cvdVals[hoverIdx]) : '0' }}
            </text>
          </g>
        </svg>
      </div>

      <!-- ── Sigma legend ── -->
      <div v-if="hasSigma" class="sigma-legend">
        <span v-for="band in sigmaLines" :key="'leg' + band.key" class="legend-item">
          <span class="legend-swatch" :style="{ background: band.color }"></span>
          {{ band.label }}
        </span>
        <span class="legend-item" style="margin-left:auto;color:#546e7a;font-size:10px">
          滾輪縮放 · 拖曳平移 · 雙擊重置
        </span>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-2 text-xs">
        <div class="snap-stat"><span>Regime</span><b>{{ regimeLabel }}</b></div>
        <div class="snap-stat"><span>Entry</span><b>{{ fmt(trade.entry) }}</b></div>
        <div class="snap-stat"><span>Exit</span><b>{{ fmt(trade.exit) }}</b></div>
        <div class="snap-stat"><span>SL</span><b>{{ fmt(stopPrice) }}</b></div>
        <div class="snap-stat"><span>TP</span><b>{{ fmt(tpPrice) }}</b></div>
        <div class="snap-stat"><span>POC</span><b>{{ fmt(pocPrice) }}</b></div>
        <div class="snap-stat"><span>PnL</span><b :class="trade.net_pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtPnl(trade.net_pnl) }}</b></div>
        <div class="snap-stat"><span>Ticks</span><b>{{ snapshot.ticks?.length || 0 }}</b></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'

const props = defineProps({
  snapshot: { type: Object, required: true },
  totalTrades: { type: Number, default: 0 },
})
defineEmits(['close', 'prev', 'next'])

const hoverIdx = ref(null)
const mouseY = ref(0)
const viewStart = ref(0)
const viewEnd = ref(0)
const vStart = ref(null)
const vEnd = ref(null)
const isPanning = ref(false)
let panStartX = 0
let panStartY = 0
let panStartRange = [0, 0]
let panStartPriceRange = [0, 0]

// ── Base data (Defined first to avoid initialization errors) ────────────────
const trade = computed(() => props.snapshot.trade || {})
const bars = computed(() => props.snapshot.window || [])
const windowFeatures = computed(() => props.snapshot.window_features || [])
const hasSigma = computed(() => windowFeatures.value.some(f => f?.vwap != null))
const hasCvd = computed(() => windowFeatures.value.some(f => f?.cvd15 != null))

const cvdVals = computed(() => windowFeatures.value.map(f => f?.cvd15 ?? null))

const regimeLabel = computed(() => {
  const t = trade.value
  const m = props.snapshot.entry_signal?.meta || {}
  const wt = t.wick_type || m.wick_type || ''
  if (wt.includes('reclaim')) return '均值回歸 (Reclaim)'
  if (wt.includes('breakout')) return '趨勢突破 (Breakout)'
  return t.regime || t.trend_regime || m.regime || m.trend_regime || '—'
})

// ── Watchers & Handlers ──────────────────────────────────────────────────────
watch(() => bars.value.length, (n) => {
  viewStart.value = 0
  viewEnd.value = Math.max(0, n - 1)
}, { immediate: true })

function onMouseMove(e) {
  const svg = e.currentTarget.querySelector('.snap-chart')
  if (!svg) return
  const rect = svg.getBoundingClientRect()
  const xRel = e.clientX - rect.left

  const s = viewStart.value
  const e_idx = viewEnd.value
  const n_visible = Math.max(e_idx - s, 1)

  const chartAreaWidth = rect.width * (912 / 980)
  const leftOffset = rect.width * (46 / 980)

  let localFrac = (xRel - leftOffset) / chartAreaWidth
  let currentIdx = s + localFrac * n_visible

  hoverIdx.value = Math.round(currentIdx)
  if (hoverIdx.value < 0 || hoverIdx.value >= bars.value.length) {
    hoverIdx.value = null
  }

  mouseY.value = ((e.clientY - rect.top) / rect.height) * 500

  if (isPanning.value) {
    const dx = e.clientX - panStartX
    const dy = e.clientY - panStartY

    const n = bars.value.length
    const shift = (dx / chartAreaWidth) * n_visible
    const range = panStartRange[1] - panStartRange[0]

    let nextStart = panStartRange[0] - shift
    if (nextStart < 0) nextStart = 0
    if (nextStart + range > n - 1) nextStart = n - 1 - range

    viewStart.value = nextStart
    viewEnd.value = nextStart + range

    // Vertical pan
    const rangeY = panStartPriceRange[1] - panStartPriceRange[0]
    // SVG height is 500, but plot area is roughly y=40 to y=400 (360px height)
    const shiftY = (dy / rect.height) * (rangeY * (500 / 360))
    vStart.value = panStartPriceRange[0] + shiftY
    vEnd.value = panStartPriceRange[1] + shiftY
  }
}

function onMouseDown(e) {
  if (e.detail === 2) { resetZoom(); return }
  isPanning.value = true
  panStartX = e.clientX
  panStartY = e.clientY
  panStartRange = [viewStart.value, viewEnd.value]

  const eb = effectiveBounds.value
  panStartPriceRange = [eb.lo, eb.hi]

  window.addEventListener('mouseup', onMouseUp)
}

function onMouseUp() {
  isPanning.value = false
  window.removeEventListener('mouseup', onMouseUp)
}

function onWheel(e) {
  e.preventDefault()
  const factor = e.deltaY > 0 ? 1.15 : 0.85

  // Horizontal Zoom
  const n = bars.value.length
  const currentN = viewEnd.value - viewStart.value
  let newN = currentN * factor

  if (newN < 10) newN = 10
  if (newN > n - 1) newN = n - 1

  const center = hoverIdx.value !== null ? hoverIdx.value : (viewStart.value + viewEnd.value) / 2
  const ratio = (center - viewStart.value) / Math.max(currentN, 1)

  let nextStart = center - newN * ratio
  if (nextStart < 0) nextStart = 0
  if (nextStart + newN > n - 1) nextStart = n - 1 - newN

  viewStart.value = nextStart
  viewEnd.value = nextStart + newN

  // Vertical Zoom
  const eb = effectiveBounds.value
  const currentRangeY = eb.hi - eb.lo
  const newRangeY = currentRangeY * factor

  // Plot area is y=40 to y=400 (height 360). MouseY is relative to SVG 0-500.
  const mouseFracY = Math.max(0, Math.min(1, (mouseY.value - 40) / 360))
  const priceAtMouse = eb.hi - mouseFracY * currentRangeY

  vStart.value = priceAtMouse - newRangeY * (1 - mouseFracY)
  vEnd.value = priceAtMouse + newRangeY * mouseFracY
}

function onMouseLeave() {
  hoverIdx.value = null
}

function resetZoom() {
  viewStart.value = 0
  viewEnd.value = bars.value.length - 1
  vStart.value = null
  vEnd.value = null
}

// ── Sigma band display config ─────────────────────────────────────────────────
const SIGMA_CONFIGS = [
  { key: 'u2',   color: '#ef535077', dash: '5 3', label: '+2σ' },
  { key: 'u1',   color: '#ffa72677', dash: '5 3', label: '+1σ' },
  { key: 'vwap', color: '#9e9e9e99', dash: '',    label: 'μ'   },
  { key: 'l1',   color: '#42a5f577', dash: '5 3', label: '-1σ' },
  { key: 'l2',   color: '#26a69a77', dash: '5 3', label: '-2σ' },
]
const CVD_H = 90

const candleW = computed(() => {
  const n = Math.max(viewEnd.value - viewStart.value, 1)
  return Math.min(60, Math.max(1, (890 / n) * 0.8))
})
const cvdBarW = computed(() => {
  const n = Math.max(viewEnd.value - viewStart.value, 1)
  return (890 / n)
})

const stopPrice = computed(() => props.snapshot.stop_price ?? trade.value.entry_stop ?? trade.value.stop ?? null)
const tpPrice = computed(() => props.snapshot.tp_price ?? null)
const pocPrice = computed(() => {
  const v = props.snapshot.entry_signal?.meta?.poc
  return v != null ? v : null
})
const exitColor = computed(() => {
  if (trade.value.exit_label === 'TS') return '#ff9800'
  if (trade.value.exit_label === 'TD') return '#ce93d8'
  return trade.value.net_pnl >= 0 ? '#26a69a' : '#ef5350'
})
const entryMarkerPath = computed(() => markerPath(snapshotIndexX(props.snapshot.entry_index), y(trade.value.entry), 'up'))
const exitMarkerPath = computed(() => markerPath(snapshotIndexX(props.snapshot.exit_index), y(trade.value.exit), 'down'))

const badgeText = computed(() => {
  const meta = props.snapshot.entry_signal?.meta || {}
  return [regimeLabel.value, meta.session, meta.market_vol_regime, meta.vwap_dev_zone, meta.vwap_z_score != null ? `z=${Number(meta.vwap_z_score).toFixed(2)}` : '']
    .filter(val => val && val !== '—').join(' | ')
})

// ── Price bounds (include sigma bands so they're always visible) ──────────────
const naturalBounds = computed(() => {
  const prices = []
  for (const k of bars.value) prices.push(k.high, k.low)
  if (trade.value.entry) prices.push(trade.value.entry)
  if (trade.value.exit) prices.push(trade.value.exit)
  if (stopPrice.value != null) prices.push(stopPrice.value)
  if (tpPrice.value != null) prices.push(tpPrice.value)
  if (pocPrice.value != null) prices.push(pocPrice.value)

  const t = trade.value
  const m = props.snapshot.entry_signal?.meta || {}
  const wt = t.wick_type || m.wick_type || ''
  const hideLower = wt === 'long_breakout' || wt === 'short_reclaim'
  const hideUpper = wt === 'short_breakout' || wt === 'long_reclaim'

  const activeKeys = ['vwap']
  if (!hideUpper) activeKeys.push('u1', 'u2')
  if (!hideLower) activeKeys.push('l1', 'l2')

  for (const f of windowFeatures.value) {
    if (!f) continue
    for (const k of activeKeys) {
      if (f[k] != null) prices.push(f[k])
    }
  }
  const lo = prices.length ? Math.min(...prices) : 0
  const hi = prices.length ? Math.max(...prices) : 1
  const pad = Math.max((hi - lo) * 0.10, 0.1)
  return { lo: lo - pad, hi: hi + pad }
})

const effectiveBounds = computed(() => ({
  lo: vStart.value !== null ? vStart.value : naturalBounds.value.lo,
  hi: vEnd.value !== null ? vEnd.value : naturalBounds.value.hi
}))

// ── Sigma polylines ───────────────────────────────────────────────────────────
const sigmaLines = computed(() => {
  if (!hasSigma.value) return []
  const feats = windowFeatures.value

  const t = trade.value
  const m = props.snapshot.entry_signal?.meta || {}
  const wt = t.wick_type || m.wick_type || ''
  const hideLower = wt === 'long_breakout' || wt === 'short_reclaim'
  const hideUpper = wt === 'short_breakout' || wt === 'long_reclaim'

  const activeConfigs = SIGMA_CONFIGS.filter(cfg => {
    if (hideLower && (cfg.key === 'l1' || cfg.key === 'l2')) return false
    if (hideUpper && (cfg.key === 'u1' || cfg.key === 'u2')) return false
    return true
  })

  return activeConfigs.map(cfg => {
    let d = '', prevNull = true
    feats.forEach((f, i) => {
      const v = f?.[cfg.key]
      if (v == null) { prevNull = true; return }
      const px = x(i).toFixed(1), py = y(v).toFixed(1)
      d += prevNull ? `M${px} ${py}` : ` L${px} ${py}`
      prevNull = false
    })
    let labelY = null
    for (let i = feats.length - 1; i >= 0; i--) {
      if (feats[i]?.[cfg.key] != null) { labelY = y(feats[i][cfg.key]); break }
    }
    return { ...cfg, d, labelY }
  })
})

// ── CVD panel ─────────────────────────────────────────────────────────────────
const lastCvdVal = computed(() => {
  const vals = cvdVals.value
  for (let i = vals.length - 1; i >= 0; i--) {
    if (vals[i] != null) return vals[i]
  }
  return null
})

const cvdRange = computed(() => {
  const vals = cvdVals.value.filter(v => v != null)
  if (!vals.length) return { lo: -1, hi: 1 }
  const lo = Math.min(...vals, 0)
  const hi = Math.max(...vals, 0)
  const pad = Math.max((hi - lo) * 0.15, 0.01)
  return { lo: lo - pad, hi: hi + pad }
})

const cvdZeroY = computed(() => cvdY(0))

function cvdY(v) {
  const { lo, hi } = cvdRange.value
  // CVD area: padding 12px top/bottom
  return 12 + (1 - (v - lo) / Math.max(hi - lo, 1e-9)) * (CVD_H - 24)
}

// CVD line path
const cvdLinePath = computed(() => {
  const vals = cvdVals.value
  let d = '', prevNull = true
  vals.forEach((v, i) => {
    if (v == null) { prevNull = true; return }
    const px = x(i).toFixed(1), py = cvdY(v).toFixed(1)
    d += prevNull ? `M${px} ${py}` : ` L${px} ${py}`
    prevNull = false
  })
  return d
})

// Fill above/below zero
const cvdAreaAbove = computed(() => buildCvdArea(true))
const cvdAreaBelow = computed(() => buildCvdArea(false))

function buildCvdArea(above) {
  const zy = cvdZeroY.value.toFixed(1)
  const vals = cvdVals.value
  let segments = [], seg = null

  vals.forEach((v, i) => {
    if (v == null) {
      if (seg) { segments.push(seg); seg = null }
      return
    }
    const isTarget = above ? v > 0 : v < 0
    if (!isTarget) {
      if (seg) { segments.push(seg); seg = null }
      return
    }

    const px = x(i), py = cvdY(v)
    if (!seg) {
      seg = { pts: [], startX: px }
    }
    seg.pts.push([px, py])
    seg.endX = px
  })

  if (seg) segments.push(seg)

  return segments.map(s => {
    const pts = s.pts.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(' L')
    return `M${s.startX.toFixed(1)},${zy} L${pts} L${s.endX.toFixed(1)},${zy} Z`
  }).join(' ')
}

// Divergence markers (circle on CVD line)
const cvdDivMarkers = computed(() => {
  const out = []
  const feats = windowFeatures.value
  const vals = cvdVals.value
  feats.forEach((f, i) => {
    if (!f) return
    const v = vals[i]
    if (v == null) return
    if (f.bd) out.push({ idx: i, val: v, bull: true })
    if (f.berd) out.push({ idx: i, val: v, bull: false })
  })
  return out
})

// Acceleration markers
const cvdAccMarkers = computed(() => {
  const out = []
  const feats = windowFeatures.value
  feats.forEach((f, i) => {
    if (!f) return
    if (f.ba && !f.bd) out.push({ idx: i, bull: true })
    if (f.bera && !f.berd) out.push({ idx: i, bull: false })
  })
  return out
})

function accTriangle(m) {
  const cx = x(m.idx)
  if (m.bull) {
    const cy = CVD_H - 6
    return `${cx},${cy - 6} ${cx - 5},${cy + 2} ${cx + 5},${cy + 2}`
  } else {
    const cy = 6
    return `${cx},${cy + 6} ${cx - 5},${cy - 2} ${cx + 5},${cy - 2}`
  }
}

// ── Chart helpers ─────────────────────────────────────────────────────────────
const maxVolume = computed(() => Math.max(1, ...bars.value.map(k => Number(k.volume || 0))))
const gridY = [80, 140, 200, 260, 320, 380]
const gridX = [46, 230, 410, 590, 770, 958] // Sync with clip-path (46 to 958)
const sampledTicks = computed(() => props.snapshot.ticks || [])

function x(i) {
  const s = viewStart.value
  const e = viewEnd.value
  const n = Math.max(e - s, 1)
  // Sync with clip-path: start 46, width 912
  return 46 + ((i - s) / n) * 912
}
function snapshotIndexX(i) {
  return x(Number.isFinite(Number(i)) ? Number(i) : 0)
}
function y(price) {
  const { lo, hi } = effectiveBounds.value
  return 400 - ((Number(price) - lo) / Math.max(hi - lo, 1e-9)) * 360
}
function volY(volume) {
  return 480 - (Number(volume || 0) / maxVolume.value) * 70
}
function markerPath(cx, cy, direction) {
  if (!Number.isFinite(cx) || !Number.isFinite(cy)) return ''
  if (direction === 'up') return `M${cx} ${cy - 10} L${cx - 8} ${cy + 7} L${cx + 8} ${cy + 7} Z`
  return `M${cx} ${cy + 10} L${cx - 8} ${cy - 7} L${cx + 8} ${cy - 7} Z`
}
function tickOffset(i) { return ((i % 31) - 15) * 0.9 }


function fmt(v) {
  return v != null && v !== '' ? Number(v).toLocaleString('en-US', { maximumFractionDigits: 4 }) : '-'
}
function fmtPnl(v) { return v != null ? `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}` : '-' }
function fmtTime(ms) { return ms ? new Date(ms).toISOString().slice(0, 16).replace('T', ' ') : '-' }
function fmtCvd(v) {
  if (v == null) return ''
  const abs = Math.abs(v)
  if (abs >= 1000) return `${(v / 1000).toFixed(1)}K`
  return v.toFixed(2)
}
</script>

<style scoped>
.snap-backdrop {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(5, 8, 12, 0.72);
  display: grid;
  place-items: center;
  padding: 24px;
}
.snap-window {
  width: min(1400px, 98vw);
  max-height: 96vh;
  overflow: auto;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 12px;
  box-shadow: 0 32px 64px rgba(0,0,0,0.6);
  padding: 16px;
}
.chart-sync-container {
  position: relative;
  cursor: crosshair;
  user-select: none;
}
.snap-titlebar {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 12px;
}
.snap-title-center {
  flex: 1;
  min-width: 0;
}
.nav-btn {
  font-size: 28px;
  line-height: 1;
  padding: 4px 16px;
  flex-shrink: 0;
  border-radius: 8px;
}
.nav-btn:disabled {
  opacity: 0.2;
  cursor: default;
}
.badge-row {
  color: #58a6ff;
  font-size: 13px;
  font-weight: 500;
  text-align: center;
  padding: 4px 0 12px;
  letter-spacing: 0.5px;
}
.snap-chart {
  width: 100%;
  height: 520px;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 8px;
  margin-bottom: 8px;
  display: block;
}
.cvd-chart {
  width: 100%;
  height: 96px;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 8px;
  margin-bottom: 8px;
  display: block;
}
.sigma-legend {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  padding: 8px 12px 16px;
  font-size: 11px;
  color: #8b949e;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 8px;
}
.legend-swatch {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 3px;
}
.snap-stat {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 12px 16px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  transition: all 0.2s;
}
.snap-stat:hover {
  background: #1c2128;
  border-color: #444c56;
}
.snap-stat span {
  color: #8b949e;
  font-size: 10px;
  text-transform: uppercase;
  font-weight: 600;
  letter-spacing: 1px;
}
.snap-stat b {
  font-family: 'JetBrains Mono', monospace;
  font-size: 14px;
  color: #f0f6fc;
}
</style>
