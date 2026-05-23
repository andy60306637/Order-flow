<template>
  <v-chart :option="option" autoresize class="w-full h-full" />
</template>

<script setup>
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, AxisPointerComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, GridComponent, TooltipComponent, AxisPointerComponent, CanvasRenderer])

const props = defineProps({
  cvd: { type: Array, default: () => [] },  // [{time_ms, value}]
})

const option = computed(() => {
  const times  = props.cvd.map(d => new Date(d.time_ms).toISOString().slice(11, 16))
  const values = props.cvd.map(d => d.value)

  return {
    backgroundColor: '#131722',
    animation: false,
    grid: { top: 8, left: 56, right: 8, bottom: 20 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1e222d',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 10 },
    },
    xAxis: {
      type: 'category',
      data: times,
      axisLabel: { color: '#787b86', fontSize: 9 },
      axisLine: { lineStyle: { color: '#2a2e39' } },
      splitLine: { show: false },
      axisTick: { show: false },
    },
    yAxis: {
      scale: true,
      axisLabel: { color: '#787b86', fontSize: 9 },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: '#1e222d' } },
      splitNumber: 3,
    },
    series: [{
      type: 'line',
      data: values,
      smooth: false,
      symbol: 'none',
      lineStyle: { color: '#7a8aa0', width: 1.2 },
      areaStyle: {
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(38,166,154,0.25)' },
            { offset: 0.5, color: 'rgba(38,166,154,0.05)' },
            { offset: 0.5, color: 'rgba(239,83,80,0.05)' },
            { offset: 1, color: 'rgba(239,83,80,0.25)' },
          ],
        },
      },
      markLine: {
        silent: true,
        symbol: 'none',
        data: [{ yAxis: 0 }],
        lineStyle: { color: '#252d3e', type: 'dashed', width: 1 },
        label: { show: false },
      },
    }],
  }
})
</script>
