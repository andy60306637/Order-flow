<template>
  <div class="flex flex-col h-full bg-bg font-mono text-xs overflow-hidden">
    <!-- Asks (sell side) — top, price descending, red bars aligned right -->
    <div class="flex-1 flex flex-col justify-end overflow-hidden">
      <div
        v-for="row in displayAsks"
        :key="row.price"
        class="relative flex items-center h-[18px] shrink-0"
      >
        <div
          class="absolute right-0 top-0 h-full bg-down/20"
          :style="{ width: pct(row.qty) + '%' }"
        />
        <span class="relative z-10 flex-1 text-right pr-2 text-down">
          {{ fmtPrice(row.price) }}
        </span>
        <span class="relative z-10 w-20 text-right pr-1 text-dim">
          {{ fmtQty(row.qty) }}
        </span>
      </div>
    </div>

    <!-- Spread row -->
    <div class="flex items-center justify-between px-2 py-0.5 bg-border/40 text-dim text-[10px] shrink-0">
      <span class="text-text font-semibold">{{ fmtPrice(lastPrice) }}</span>
      <span>差價 {{ spread ?? '—' }}</span>
    </div>

    <!-- Bids (buy side) — bottom, price descending, green bars aligned left -->
    <div class="flex-1 flex flex-col overflow-hidden">
      <div
        v-for="row in displayBids"
        :key="row.price"
        class="relative flex items-center h-[18px] shrink-0"
      >
        <div
          class="absolute left-0 top-0 h-full bg-up/20"
          :style="{ width: pct(row.qty) + '%' }"
        />
        <span class="relative z-10 flex-1 text-right pr-2 text-up">
          {{ fmtPrice(row.price) }}
        </span>
        <span class="relative z-10 w-20 text-right pr-1 text-dim">
          {{ fmtQty(row.qty) }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  bids:      { type: Array, default: () => [] },
  asks:      { type: Array, default: () => [] },
  lastPrice: { type: Number, default: null },
  spread:    { type: [String, Number], default: null },
  levels:    { type: Number, default: 20 },
})

const displayAsks = computed(() => {
  const rows = props.asks.slice(0, props.levels).map(([price, qty]) => ({
    price: parseFloat(price), qty: parseFloat(qty),
  }))
  return [...rows].reverse()  // show lowest ask at bottom (nearest spread)
})

const displayBids = computed(() =>
  props.bids.slice(0, props.levels).map(([price, qty]) => ({
    price: parseFloat(price), qty: parseFloat(qty),
  }))
)

const maxQty = computed(() => {
  const all = [...displayAsks.value, ...displayBids.value].map(r => r.qty)
  return Math.max(...all, 1)
})

function pct(qty) { return (qty / maxQty.value * 100).toFixed(1) }
function fmtPrice(p) {
  if (p == null) return '—'
  return Number(p).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 })
}
function fmtQty(q) {
  if (q == null) return '—'
  return q >= 1000 ? (q / 1000).toFixed(2) + 'K' : q.toFixed(3)
}
</script>
