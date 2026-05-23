<template>
  <canvas ref="canvas" class="w-full h-full block" />
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  snapshots:  { type: Array, default: () => [] },  // [{price: qty,...}]
  lastPrice:  { type: Number, default: 0 },
  priceRange: { type: Number, default: 0.015 },    // ±1.5%
  buckets:    { type: Number, default: 150 },
})

const canvas = ref(null)
let ro = null

// viridis-like LUT (0→1 → RGB)
function viridis(t) {
  const r = Math.round(Math.min(255, Math.max(0, 68 + t * (59 + t * (-6 + t * 134)))))
  const g = Math.round(Math.min(255, Math.max(0, 1  + t * (82 + t * (121 + t * -71)))))
  const b = Math.round(Math.min(255, Math.max(0, 84 + t * (64 + t * (14  + t * 32)))))
  return `rgb(${r},${g},${b})`
}

function draw() {
  const c = canvas.value
  if (!c || !props.lastPrice) return
  const ctx = c.getContext('2d')
  const W = c.width, H = c.height

  ctx.fillStyle = '#131722'
  ctx.fillRect(0, 0, W, H)

  const snaps = props.snapshots
  if (!snaps.length) return

  const N = snaps.length
  const P = props.buckets
  const pMin = props.lastPrice * (1 - props.priceRange)
  const pMax = props.lastPrice * (1 + props.priceRange)
  const tickW = W / N
  const tickH = H / P

  // Find global max qty for normalisation
  let globalMax = 1
  for (const snap of snaps)
    for (const qty of Object.values(snap))
      if (qty > globalMax) globalMax = qty

  for (let xi = 0; xi < N; xi++) {
    const snap = snaps[xi]
    for (const [priceStr, qty] of Object.entries(snap)) {
      const price = parseFloat(priceStr)
      if (price < pMin || price > pMax) continue
      const yi = Math.floor((price - pMin) / (pMax - pMin) * P)
      if (yi < 0 || yi >= P) continue
      const logVal = Math.log1p(qty) / Math.log1p(globalMax)
      ctx.fillStyle = viridis(logVal)
      ctx.fillRect(xi * tickW, H - (yi + 1) * tickH, Math.ceil(tickW), Math.ceil(tickH))
    }
  }

  // Current price line
  const lineY = H - ((props.lastPrice - pMin) / (pMax - pMin)) * H
  ctx.strokeStyle = '#ffffff55'
  ctx.lineWidth = 1
  ctx.setLineDash([3, 3])
  ctx.beginPath(); ctx.moveTo(0, lineY); ctx.lineTo(W, lineY); ctx.stroke()
  ctx.setLineDash([])
}

function resize() {
  const c = canvas.value
  if (!c) return
  c.width  = c.clientWidth  * devicePixelRatio
  c.height = c.clientHeight * devicePixelRatio
  const ctx = c.getContext('2d')
  ctx.scale(devicePixelRatio, devicePixelRatio)
  draw()
}

watch(() => [props.snapshots.length, props.lastPrice], draw)

onMounted(() => {
  resize()
  ro = new ResizeObserver(resize)
  ro.observe(canvas.value)
})
onUnmounted(() => ro?.disconnect())
</script>
