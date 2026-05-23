<template>
  <div class="flex items-center gap-6 px-3 h-full bg-bg border-t border-border font-mono text-xs">
    <StatItem label="Volume" :value="fmtVol(bar?.volume)" />
    <StatItem label="Delta"  :value="fmtDelta(delta)" :color="delta >= 0 ? 'text-up' : 'text-down'" />
    <StatItem label="CVD"    :value="fmtDelta(cvd)"   :color="cvd   >= 0 ? 'text-up' : 'text-down'" />
    <StatItem v-if="bar" label="O" :value="fmtP(bar.open)"  />
    <StatItem v-if="bar" label="H" :value="fmtP(bar.high)"  color="text-up" />
    <StatItem v-if="bar" label="L" :value="fmtP(bar.low)"   color="text-down" />
    <StatItem v-if="bar" label="C" :value="fmtP(bar.close)" :color="bar.close >= bar.open ? 'text-up' : 'text-down'" />
  </div>
</template>

<script setup>
const props = defineProps({
  bar:   { type: Object, default: null },
  delta: { type: Number, default: 0 },
  cvd:   { type: Number, default: 0 },
})

const StatItem = {
  props: { label: String, value: String, color: { type: String, default: 'text-text' } },
  template: `<span class="flex gap-1"><span class="text-dim">{{label}}</span><span :class="color">{{value}}</span></span>`
}

const fmtP   = v => v != null ? Number(v).toLocaleString('en-US', { minimumFractionDigits: 1 }) : '—'
const fmtVol = v => v != null ? Number(v).toFixed(2) : '—'
const fmtDelta = v => {
  if (v == null) return '—'
  const s = v >= 0 ? '+' : ''
  return s + Number(v).toFixed(2)
}
</script>
