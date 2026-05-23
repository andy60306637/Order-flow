<template>
  <div class="h-full w-full bg-bg overflow-hidden">
    <div class="h-full overflow-auto">
      <div class="flex items-stretch gap-px min-h-full px-2 py-2">
        <div
          v-for="c in visibleCandles"
          :key="c.time_ms"
          class="fp-col"
        >
          <div class="fp-time">{{ fmtTime(c.time_ms) }}</div>
          <div class="fp-body">
            <div
              v-for="level in candleLevels(c)"
              :key="level.price"
              class="fp-cell"
              :class="cellClass(level, c)"
              :style="cellStyle(level, c)"
            >
              <template v-if="mode === 'Delta'">
                <span>{{ fmtDelta(level.bid - level.ask) }}</span>
              </template>
              <template v-else-if="mode === 'Volume'">
                <span>{{ fmtVol(level.bid + level.ask) }}</span>
              </template>
              <template v-else>
                <span class="text-down">{{ fmtVol(level.ask) }}</span>
                <span class="text-dim px-0.5">x</span>
                <span class="text-up">{{ fmtVol(level.bid) }}</span>
              </template>
              <i v-if="isImbalance(level)" class="fp-imb" />
            </div>
          </div>
          <div class="fp-price">{{ fmtPrice(c.close || c.open) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  candles: { type: Array, default: () => [] },
  mode: { type: String, default: 'BidxAsk' },
  tickSize: { type: Number, default: 1 },
  maxCandles: { type: Number, default: 48 },
})

const visibleCandles = computed(() =>
  props.candles
    .filter(c => c && c.time_ms)
    .slice(-props.maxCandles)
)

function candleLevels(candle) {
  const levels = Object.values(candle.levels || {})
    .filter(l => Number.isFinite(l.price))
    .sort((a, b) => b.price - a.price)
  if (levels.length) return levels

  const close = candle.close || candle.open || 0
  if (!close) return []
  return [{ price: close, bid: 0, ask: 0 }]
}

function maxLevelVol(candle) {
  return Math.max(
    ...Object.values(candle.levels || {}).map(l => (l.bid || 0) + (l.ask || 0)),
    1,
  )
}

function cellClass(level) {
  if (props.mode === 'Volume') return 'fp-vol'
  const delta = (level.bid || 0) - (level.ask || 0)
  if (delta > 0) return 'fp-bid'
  if (delta < 0) return 'fp-ask'
  return 'fp-neutral'
}

function cellStyle(level, candle) {
  const total = (level.bid || 0) + (level.ask || 0)
  const alpha = Math.min(0.9, Math.max(0.16, total / maxLevelVol(candle)))
  const isPoc = total >= maxLevelVol(candle) && total > 0
  return {
    opacity: alpha,
    outline: isPoc ? '1px solid rgba(255,215,0,.75)' : '0',
  }
}

function isImbalance(level) {
  if (props.mode !== 'Imbalance') return false
  const bid = level.bid || 0
  const ask = level.ask || 0
  if (!bid && !ask) return false
  return bid >= ask * 3 || ask >= bid * 3
}

function fmtTime(ms) {
  return new Date(ms).toISOString().slice(5, 16).replace('T', ' ')
}

function decimalsFromTick() {
  const s = String(props.tickSize || 1)
  if (!s.includes('.')) return 1
  return Math.min(6, s.split('.')[1].replace(/0+$/, '').length || 1)
}

function fmtPrice(v) {
  return Number(v || 0).toLocaleString('en-US', {
    minimumFractionDigits: decimalsFromTick(),
    maximumFractionDigits: decimalsFromTick(),
  })
}

function fmtVol(v) {
  const n = Number(v || 0)
  if (!n) return '-'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  if (n >= 10) return n.toFixed(0)
  if (n >= 1) return n.toFixed(1)
  return n.toFixed(3)
}

function fmtDelta(v) {
  const n = Number(v || 0)
  return n > 0 ? `+${fmtVol(n)}` : n < 0 ? `-${fmtVol(Math.abs(n))}` : '0'
}
</script>

<style scoped>
.fp-col {
  width: 92px;
  min-width: 92px;
  display: flex;
  flex-direction: column;
  border-left: 1px solid #202633;
}
.fp-time,
.fp-price {
  height: 18px;
  color: #787b86;
  font-size: 10px;
  line-height: 18px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.fp-price {
  color: #d1d4dc;
}
.fp-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  min-height: 0;
}
.fp-cell {
  position: relative;
  min-height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1px;
  border-top: 1px solid rgba(80, 86, 105, .25);
  font-size: 10px;
  line-height: 1;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  color: #d1d4dc;
}
.fp-bid { background: rgba(38, 166, 154, .55); }
.fp-ask { background: rgba(239, 83, 80, .55); }
.fp-neutral { background: rgba(42, 46, 57, .55); }
.fp-vol { background: rgba(220, 165, 30, .6); }
.fp-imb {
  position: absolute;
  right: 3px;
  top: 3px;
  width: 5px;
  height: 5px;
  background: #ffd54f;
}
</style>
