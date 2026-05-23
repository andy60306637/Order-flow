<template>
  <div class="snap-backdrop" @click.self="$emit('close')">
    <div class="snap-window">
      <div class="snap-titlebar">
        <div>
          <div class="text-sm font-medium">Trade #{{ snapshot.trade_idx + 1 }} · {{ trade.dir?.toUpperCase() }}</div>
          <div class="text-xs text-dim">
            Entry {{ fmtTime(trade.entry_time) }} UTC · {{ trade.exit_label || 'Exit' }} ·
            <span :class="trade.net_pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtPnl(trade.net_pnl) }} USDT</span>
          </div>
        </div>
        <button class="btn-ghost ml-auto" @click="$emit('close')">關閉</button>
      </div>
      <div v-if="badgeText" class="badge-row">{{ badgeText }}</div>

      <svg class="snap-chart" viewBox="0 0 980 430" preserveAspectRatio="none">
        <line v-for="g in gridY" :key="'gy' + g" x1="46" :y1="g" x2="958" :y2="g" stroke="#2a2e3966" stroke-width="1" />
        <line v-for="g in gridX" :key="'gx' + g" :x1="g" y1="24" :x2="g" y2="382" stroke="#2a2e3955" stroke-width="1" />
        <rect v-if="snapshot.k0_index != null" :x="x(snapshot.k0_index) - candleW * 0.78" y="24"
              :width="candleW * 1.56" height="328" fill="#ff980024" />
        <text v-if="snapshot.k0_index != null" :x="x(snapshot.k0_index)" y="42" text-anchor="middle" fill="#ff9800" font-size="11">k0</text>

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
        <text v-if="tpPrice != null" x="952" :y="y(tpPrice) + 14" text-anchor="end" fill="#26a69a" font-size="11">TP {{ fmt(tpPrice) }}</text>

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

      <div class="grid grid-cols-2 md:grid-cols-6 gap-2 text-xs">
        <div class="snap-stat"><span>Entry</span><b>{{ fmt(trade.entry) }}</b></div>
        <div class="snap-stat"><span>Exit</span><b>{{ fmt(trade.exit) }}</b></div>
        <div class="snap-stat"><span>SL</span><b>{{ fmt(stopPrice) }}</b></div>
        <div class="snap-stat"><span>TP</span><b>{{ fmt(tpPrice) }}</b></div>
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
})
defineEmits(['close'])

const trade = computed(() => props.snapshot.trade || {})
const bars = computed(() => props.snapshot.window || [])
const candleW = computed(() => Math.max(5, 760 / Math.max(bars.value.length, 1) * 0.55))
const stopPrice = computed(() => props.snapshot.stop_price ?? trade.value.entry_stop ?? trade.value.stop ?? null)
const tpPrice = computed(() => props.snapshot.tp_price ?? null)
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

const bounds = computed(() => {
  const prices = []
  for (const k of bars.value) prices.push(k.high, k.low)
  if (trade.value.entry) prices.push(trade.value.entry)
  if (trade.value.exit) prices.push(trade.value.exit)
  if (stopPrice.value != null) prices.push(stopPrice.value)
  if (tpPrice.value != null) prices.push(tpPrice.value)
  const lo = prices.length ? Math.min(...prices) : 0
  const hi = prices.length ? Math.max(...prices) : 1
  const pad = Math.max((hi - lo) * 0.08, 1)
  return { lo: lo - pad, hi: hi + pad }
})

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

function tickOffset(i) {
  return ((i % 31) - 15) * 0.8
}

function fmt(v) {
  return v != null && v !== '' ? Number(v).toLocaleString('en-US', { maximumFractionDigits: 4 }) : '-'
}
function fmtPnl(v) {
  return v != null ? `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}` : '-'
}
function fmtTime(ms) {
  return ms ? new Date(ms).toISOString().slice(0, 16).replace('T', ' ') : '-'
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
  gap: 12px;
  margin-bottom: 6px;
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
  margin-bottom: 8px;
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
