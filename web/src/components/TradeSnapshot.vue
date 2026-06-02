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
        <button class="btn-ghost ml-auto" @click="$emit('close')">關閉</button>
      </div>
      <div v-if="badgeText" class="badge-row">{{ badgeText }}</div>

      <!-- ── Main candlestick chart ── -->
      <svg class="snap-chart" viewBox="0 0 980 430" preserveAspectRatio="none">
        <line v-for="g in gridY" :key="'gy' + g" x1="46" :y1="g" x2="958" :y2="g" stroke="#2a2e3966" stroke-width="1" />
        <line v-for="g in gridX" :key="'gx' + g" :x1="g" y1="24" :x2="g" y2="382" stroke="#2a2e3955" stroke-width="1" />
        <rect v-if="snapshot.k0_index != null" :x="x(snapshot.k0_index) - candleW * 0.78" y="24"
              :width="candleW * 1.56" height="328" fill="#ff980024" />
        <text v-if="snapshot.k0_index != null" :x="x(snapshot.k0_index)" y="42" text-anchor="middle" fill="#ff9800" font-size="11">k0</text>

        <!-- Sigma band polylines (drawn before candles so they appear behind) -->
        <g v-if="hasSigma">
          <path v-for="band in sigmaLines" :key="band.key"
            :d="band.d"
            :stroke="band.color"
            :stroke-width="band.key === 'vwap' ? 1.3 : 0.9"
            :stroke-dasharray="band.dash || undefined"
            fill="none"
            stroke-linejoin="round"
          />
          <!-- Right-edge labels -->
          <template v-for="band in sigmaLines" :key="'lb' + band.key">
            <text v-if="band.labelY != null"
              x="958" :y="band.labelY - 2"
              text-anchor="end"
              :fill="band.color"
              font-size="9"
              font-weight="600"
            >{{ band.label }}</text>
          </template>
        </g>

        <g v-for="(k, i) in bars" :key="k.time_ms">
          <line
            :x1="x(i)" :x2="x(i)"
            :y1="y(k.high)" :y2="y(k.low)"
            :stroke="k.close >= k.open ? '#26a69a' : '#ef5350'"
            stroke-width="1.1"
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
            :height="382 - volY(k.volume)"
            :fill="k.close >= k.open ? '#26a69a44' : '#ef535044'"
          />
        </g>

        <line v-if="stopPrice != null" x1="46" :y1="y(stopPrice)" x2="958" :y2="y(stopPrice)" stroke="#ef5350" stroke-dasharray="5 5" />
        <text v-if="stopPrice != null" x="952" :y="y(stopPrice) - 4" text-anchor="end" fill="#ef5350" font-size="11">SL {{ fmt(stopPrice) }}</text>
        <line v-if="tpPrice != null" x1="46" :y1="y(tpPrice)" x2="958" :y2="y(tpPrice)" stroke="#26a69a" stroke-dasharray="5 5" />
        <text v-if="tpPrice != null" x="952" :y="y(tpPrice) + 14" text-anchor="end" fill="#26a69a" font-size="11">{{ pocPrice != null ? 'TP/POC' : 'TP' }} {{ fmt(tpPrice) }}</text>

        <path :d="entryMarkerPath" fill="#2196f3" stroke="#90caf9" stroke-width="1" />
        <text :x="x(snapshot.entry_index) + 10" :y="y(trade.entry) - 8" fill="#90caf9" font-size="11">Entry {{ fmt(trade.entry) }}</text>
        <path v-if="snapshot.exit_index != null" :d="exitMarkerPath" :fill="exitColor" :stroke="exitColor" stroke-width="1" />
        <text v-if="snapshot.exit_index != null" :x="x(snapshot.exit_index) + 10" :y="y(trade.exit) + 14" :fill="exitColor" font-size="11">
          {{ trade.exit_label || 'Exit' }} {{ fmt(trade.exit) }}
        </text>

        <circle
          v-for="(t, i) in sampledTicks"
          :key="i"
          :cx="x(snapshot.entry_index) + tickOffset(i)"
          :cy="y(t.price)"
          r="1.8"
          :fill="t.is_sell ? '#ef535088' : '#26a69a88'"
        />
      </svg>

      <!-- ── CVD₁₅ₘ mini panel ── -->
      <svg v-if="hasCvd" class="cvd-chart" :viewBox="`0 0 980 ${CVD_H}`" preserveAspectRatio="none">
        <!-- Zero reference -->
        <line x1="46" :y1="cvdZeroY" x2="958" :y2="cvdZeroY" stroke="#3a4052" stroke-width="1" stroke-dasharray="3 2" />
        <!-- Warmup region tint -->
        <template v-for="(f, i) in windowFeatures" :key="'wu' + i">
          <rect v-if="f?.wu"
            :x="x(i) - cvdBarW / 2" y="0"
            :width="cvdBarW" :height="CVD_H"
            fill="#ffa72608"
          />
        </template>
        <!-- CVD area fill (above zero: blue-tint, below: red-tint) -->
        <path v-if="cvdAreaAbove" :d="cvdAreaAbove" fill="#42a5f518" />
        <path v-if="cvdAreaBelow" :d="cvdAreaBelow" fill="#ef535018" />
        <!-- CVD line -->
        <path :d="cvdLinePath" fill="none" stroke="#64b5f6" stroke-width="1.4" stroke-linejoin="round" />
        <!-- Divergence markers (circle on the CVD line) -->
        <circle v-for="(m, i) in cvdDivMarkers" :key="'div' + i"
          :cx="x(m.idx)" :cy="cvdY(m.val)"
          r="4" :fill="m.bull ? '#26a69a' : '#ef5350'" opacity="0.9"
        />
        <!-- Acceleration markers (small triangle at panel edge) -->
        <polygon v-for="(m, i) in cvdAccMarkers" :key="'acc' + i"
          :points="accTriangle(m)"
          :fill="m.bull ? '#26a69acc' : '#ef5350cc'"
        />
        <!-- Panel label -->
        <text x="52" y="11" fill="#546e7a" font-size="9" font-weight="600">CVD₁₅ₘ</text>
        <!-- Current CVD value -->
        <text v-if="lastCvdVal != null" x="958" y="11" text-anchor="end" fill="#64b5f6" font-size="9">
          {{ fmtCvd(lastCvdVal) }}
        </text>
      </svg>

      <!-- ── Sigma legend (only when sigma data present) ── -->
      <div v-if="hasSigma" class="sigma-legend">
        <span v-for="band in sigmaLines" :key="'leg' + band.key" class="legend-item">
          <span class="legend-swatch" :style="{ background: band.color }"></span>
          {{ band.label }}
        </span>
        <span class="legend-item" style="margin-left:auto;color:#546e7a;font-size:10px">
          Weekly VWAP σ bands
        </span>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-7 gap-2 text-xs">
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
import { computed } from 'vue'

const props = defineProps({
  snapshot: { type: Object, required: true },
  totalTrades: { type: Number, default: 0 },
})
defineEmits(['close', 'prev', 'next'])

// ── Sigma band display config ─────────────────────────────────────────────────
const SIGMA_CONFIGS = [
  { key: 'u2',   color: '#ef535077', dash: '5 3', label: '+2σ' },
  { key: 'u1',   color: '#ffa72677', dash: '5 3', label: '+1σ' },
  { key: 'vwap', color: '#9e9e9e99', dash: '',    label: 'μ'   },
  { key: 'l1',   color: '#42a5f577', dash: '5 3', label: '-1σ' },
  { key: 'l2',   color: '#26a69a77', dash: '5 3', label: '-2σ' },
]
const CVD_H = 74

// ── Base data ─────────────────────────────────────────────────────────────────
const trade = computed(() => props.snapshot.trade || {})
const bars = computed(() => props.snapshot.window || [])
const windowFeatures = computed(() => props.snapshot.window_features || [])
const hasSigma = computed(() => windowFeatures.value.some(f => f?.vwap != null))
const hasCvd = computed(() => windowFeatures.value.some(f => f?.cvd15 != null))
const candleW = computed(() => Math.max(5, 760 / Math.max(bars.value.length, 1) * 0.55))
const cvdBarW = computed(() => Math.max(3, 890 / Math.max(bars.value.length, 1)))
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
  return [meta.session, meta.market_vol_regime, meta.vwap_dev_zone, meta.vwap_z_score != null ? `z=${Number(meta.vwap_z_score).toFixed(2)}` : '']
    .filter(Boolean).join(' | ')
})

// ── Price bounds (include sigma bands so they're always visible) ──────────────
const bounds = computed(() => {
  const prices = []
  for (const k of bars.value) prices.push(k.high, k.low)
  if (trade.value.entry) prices.push(trade.value.entry)
  if (trade.value.exit) prices.push(trade.value.exit)
  if (stopPrice.value != null) prices.push(stopPrice.value)
  if (tpPrice.value != null) prices.push(tpPrice.value)
  if (pocPrice.value != null) prices.push(pocPrice.value)
  for (const f of windowFeatures.value) {
    if (!f) continue
    for (const k of ['u2', 'u1', 'vwap', 'l1', 'l2']) {
      if (f[k] != null) prices.push(f[k])
    }
  }
  const lo = prices.length ? Math.min(...prices) : 0
  const hi = prices.length ? Math.max(...prices) : 1
  const pad = Math.max((hi - lo) * 0.08, 1)
  return { lo: lo - pad, hi: hi + pad }
})

// ── Sigma polylines ───────────────────────────────────────────────────────────
const sigmaLines = computed(() => {
  if (!hasSigma.value) return []
  const feats = windowFeatures.value
  return SIGMA_CONFIGS.map(cfg => {
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
const cvdVals = computed(() => windowFeatures.value.map(f => f?.cvd15 ?? null))
const lastCvdVal = computed(() => {
  for (let i = cvdVals.value.length - 1; i >= 0; i--) {
    if (cvdVals.value[i] != null) return cvdVals.value[i]
  }
  return null
})
const cvdRange = computed(() => {
  const vals = cvdVals.value.filter(v => v != null)
  if (!vals.length) return { lo: -1, hi: 1 }
  const lo = Math.min(...vals, 0)
  const hi = Math.max(...vals, 0)
  const pad = Math.max((hi - lo) * 0.12, 0.01)
  return { lo: lo - pad, hi: hi + pad }
})
const cvdZeroY = computed(() => cvdY(0))

function cvdY(v) {
  const { lo, hi } = cvdRange.value
  return 8 + (1 - (v - lo) / Math.max(hi - lo, 1e-9)) * (CVD_H - 16)
}

// CVD line path
const cvdLinePath = computed(() => {
  let d = '', prevNull = true
  cvdVals.value.forEach((v, i) => {
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
  const zy = cvdZeroY.value
  const vals = cvdVals.value
  let segments = [], seg = null
  vals.forEach((v, i) => {
    if (v == null) { if (seg) { segments.push(seg); seg = null } return }
    const keep = above ? v > 0 : v < 0
    if (!keep) { if (seg) { segments.push(seg); seg = null } return }
    const px = x(i), py = cvdY(v)
    if (!seg) seg = { pts: [], startX: px }
    seg.pts.push([px, py])
    seg.endX = px
  })
  if (seg) segments.push(seg)
  return segments.map(s => {
    const pts = s.pts.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(' L')
    return `M${s.startX.toFixed(1)},${zy.toFixed(1)} L${pts} L${s.endX.toFixed(1)},${zy.toFixed(1)} Z`
  }).join(' ')
}

// Divergence markers (circle on CVD line)
const cvdDivMarkers = computed(() => {
  const out = []
  windowFeatures.value.forEach((f, i) => {
    if (!f) return
    const v = cvdVals.value[i]
    if (v == null) return
    if (f.bd) out.push({ idx: i, val: v, bull: true })
    if (f.berd) out.push({ idx: i, val: v, bull: false })
  })
  return out
})

// Acceleration markers (small triangle at bottom/top edge)
const cvdAccMarkers = computed(() => {
  const out = []
  windowFeatures.value.forEach((f, i) => {
    if (!f) return
    if (f.ba && !f.bd) out.push({ idx: i, bull: true })   // only if not already a div marker
    if (f.bera && !f.berd) out.push({ idx: i, bull: false })
  })
  return out
})

function accTriangle(m) {
  const cx = x(m.idx)
  if (m.bull) {
    const cy = CVD_H - 5
    return `${cx},${cy - 5} ${cx - 4},${cy + 2} ${cx + 4},${cy + 2}`
  } else {
    const cy = 5
    return `${cx},${cy + 5} ${cx - 4},${cy - 2} ${cx + 4},${cy - 2}`
  }
}

// ── Chart helpers ─────────────────────────────────────────────────────────────
const maxVolume = computed(() => Math.max(1, ...bars.value.map(k => Number(k.volume || 0))))
const gridY = [54, 110, 166, 222, 278, 334]
const gridX = [50, 230, 410, 590, 770, 950]
const sampledTicks = computed(() => props.snapshot.ticks || [])

function x(i) {
  const n = Math.max(bars.value.length - 1, 1)
  return 50 + (i / n) * 890
}
function snapshotIndexX(i) {
  return x(Number.isFinite(Number(i)) ? Number(i) : 0)
}
function y(price) {
  const { lo, hi } = bounds.value
  return 352 - ((Number(price) - lo) / Math.max(hi - lo, 1)) * 320
}
function volY(volume) {
  return 382 - (Number(volume || 0) / maxVolume.value) * 54
}
function markerPath(cx, cy, direction) {
  if (!Number.isFinite(cx) || !Number.isFinite(cy)) return ''
  if (direction === 'up') return `M${cx} ${cy - 9} L${cx - 7} ${cy + 6} L${cx + 7} ${cy + 6} Z`
  return `M${cx} ${cy + 9} L${cx - 7} ${cy - 6} L${cx + 7} ${cy - 6} Z`
}
function tickOffset(i) { return ((i % 31) - 15) * 0.8 }

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
  width: min(1180px, 96vw);
  max-height: 92vh;
  overflow: auto;
  background: #151c2a;
  border: 1px solid #334058;
  border-radius: 6px;
  padding: 10px;
}
.snap-titlebar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.snap-title-center {
  flex: 1;
  min-width: 0;
}
.nav-btn {
  font-size: 22px;
  line-height: 1;
  padding: 2px 10px;
  flex-shrink: 0;
}
.nav-btn:disabled {
  opacity: 0.3;
  cursor: default;
}
.badge-row {
  color: #80cbc4;
  font-size: 11px;
  text-align: center;
  padding: 3px 0 7px;
}
.snap-chart {
  width: 100%;
  height: 430px;
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 4px;
  margin-bottom: 4px;
}
.cvd-chart {
  width: 100%;
  height: 74px;
  background: #0f1620;
  border: 1px solid #1e2a38;
  border-radius: 4px;
  margin-bottom: 4px;
}
.sigma-legend {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 3px 4px 5px;
  font-size: 10px;
  color: #8f96a8;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}
.legend-swatch {
  display: inline-block;
  width: 18px;
  height: 2px;
  border-radius: 1px;
}
.snap-stat {
  background: #131722;
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 6px 8px;
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.snap-stat span {
  color: #787b86;
}
</style>
