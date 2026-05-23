<template>
  <canvas ref="canvas" class="w-full h-full block" />
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  snapshots:  { type: Array, default: () => [] },  // [{price: qty,...}]
  trades:     { type: Array, default: () => [] },
  lastPrice:  { type: Number, default: 0 },
  priceRange: { type: Number, default: 0.015 },    // ±1.5%
  buckets:    { type: Number, default: 150 },
})

const canvas = ref(null)
let ro = null
let raf = 0

// viridis-like LUT (0→1 → RGB)
function viridis(t) {
  const stops = [
    [20, 20, 30],
    [30, 50, 100],
    [10, 100, 120],
    [30, 170, 100],
    [200, 180, 30],
    [255, 220, 0],
  ]
  const x = Math.max(0, Math.min(1, t)) * (stops.length - 1)
  const lo = Math.floor(x)
  const hi = Math.min(stops.length - 1, lo + 1)
  const f = x - lo
  const r = Math.round(stops[lo][0] * (1 - f) + stops[hi][0] * f)
  const g = Math.round(stops[lo][1] * (1 - f) + stops[hi][1] * f)
  const b = Math.round(stops[lo][2] * (1 - f) + stops[hi][2] * f)
  return `rgb(${r},${g},${b})`
}

function scheduleDraw() {
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    draw()
  })
}

function draw() {
  const c = canvas.value
  if (!c || !props.lastPrice) return
  const ctx = c.getContext('2d')
  const W = c.clientWidth
  const H = c.clientHeight
  if (W <= 0 || H <= 0) return
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, c.width, c.height)
  const dpr = window.devicePixelRatio || 1
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

  ctx.fillStyle = '#131722'
  ctx.fillRect(0, 0, W, H)

  const snaps = props.snapshots
  if (!snaps.length) return

  const N = snaps.length
  const P = props.buckets
  const pMin = props.lastPrice * (1 - props.priceRange)
  const pMax = props.lastPrice * (1 + props.priceRange)
  const tickW = Math.max(1, W / N)
  const tickH = H / P

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

  const visibleTrades = props.trades.slice(-300)
  for (const t of visibleTrades) {
    const price = Number(t.price)
    if (!price || price < pMin || price > pMax) continue
    const y = H - ((price - pMin) / (pMax - pMin)) * H
    const x = W - Math.max(2, (Date.now() - Number(t.ts || Date.now())) / 1000) * 4
    if (x < 0 || x > W) continue
    const size = Math.max(3, Math.min(9, Math.log1p(Number(t.qty || 0)) * 3))
    ctx.fillStyle = t.isSell ? '#ef5350cc' : '#26a69acc'
    ctx.beginPath()
    ctx.moveTo(x, y + (t.isSell ? size : -size))
    ctx.lineTo(x - size, y + (t.isSell ? -size : size))
    ctx.lineTo(x + size, y + (t.isSell ? -size : size))
    ctx.closePath()
    ctx.fill()
  }

  const lineY = H - ((props.lastPrice - pMin) / (pMax - pMin)) * H
  ctx.strokeStyle = '#ffffff55'
  ctx.lineWidth = 1
  ctx.setLineDash([3, 3])
  ctx.beginPath(); ctx.moveTo(0, lineY); ctx.lineTo(W, lineY); ctx.stroke()
  ctx.setLineDash([])

  ctx.fillStyle = '#8f96a8'
  ctx.font = '10px sans-serif'
  ctx.textAlign = 'right'
  ctx.fillText(pMax.toFixed(1), W - 4, 11)
  ctx.fillText(props.lastPrice.toFixed(1), W - 4, Math.max(22, Math.min(H - 6, lineY - 4)))
  ctx.fillText(pMin.toFixed(1), W - 4, H - 5)
}

function resize() {
  const c = canvas.value
  if (!c) return
  const dpr = window.devicePixelRatio || 1
  c.width  = Math.max(1, Math.floor(c.clientWidth  * dpr))
  c.height = Math.max(1, Math.floor(c.clientHeight * dpr))
  scheduleDraw()
}

watch(() => [props.snapshots.length, props.trades.length, props.lastPrice], scheduleDraw, { flush: 'post' })

onMounted(() => {
  resize()
  ro = new ResizeObserver(resize)
  ro.observe(canvas.value)
})
onUnmounted(() => {
  ro?.disconnect()
  if (raf) cancelAnimationFrame(raf)
})
</script>
