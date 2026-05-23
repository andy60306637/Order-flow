<template>
  <v-chart ref="chartRef" :option="option" autoresize class="w-full h-full"
           @datazoom="_onDataZoom" />
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import VChart from 'vue-echarts'
import { use, connect, disconnect } from 'echarts/core'
import { CandlestickChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent, AxisPointerComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([CandlestickChart, BarChart, GridComponent, TooltipComponent, DataZoomComponent, AxisPointerComponent, CanvasRenderer])

const props = defineProps({
  klines:   { type: Array, default: () => [] },
  markers:  { type: Array, default: () => [] },  // [{time_ms, side, label}]
  groupId:  { type: String, default: 'market' }, // ECharts connect group
})

const emit = defineEmits(['scroll-left'])

const chartRef = ref(null)

// Join ECharts crosshair group on mount
onMounted(() => {
  if (chartRef.value?.chart) connect(props.groupId)
})
onUnmounted(() => {
  if (chartRef.value?.chart) disconnect(props.groupId)
})

// Detect scroll to the left edge → request more history
function _onDataZoom(params) {
  const start = params?.batch?.[0]?.start ?? params?.start ?? null
  if (start !== null && start <= 1) {
    emit('scroll-left')
  }
}

const option = computed(() => {
  const times  = props.klines.map(k => new Date(k.time_ms).toISOString().slice(0, 16).replace('T', '\n'))
  const candles = props.klines.map(k => [k.open, k.close, k.low, k.high])
  const volumes = props.klines.map(k => ({
    value: k.volume,
    itemStyle: { color: k.close >= k.open ? '#26a69a55' : '#ef535055' },
  }))

  return {
    backgroundColor: '#131722',
    animation: false,
    group: props.groupId,
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#1e222d',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 11 },
      formatter(params) {
        const c = params.find(p => p.seriesType === 'candlestick')
        if (!c) return ''
        const [o, cl, l, h] = c.value
        const upDown = cl >= o ? '#26a69a' : '#ef5350'
        return `<span style="color:#787b86">${c.name}</span><br/>
          O <b style="color:${upDown}">${o}</b>&nbsp;
          H <b style="color:${upDown}">${h}</b>&nbsp;
          L <b style="color:${upDown}">${l}</b>&nbsp;
          C <b style="color:${upDown}">${cl}</b>`
      },
    },
    grid: [
      { top: 8, left: 56, right: 8, bottom: 80 },
      { top: '78%', left: 56, right: 8, bottom: 28 },
    ],
    xAxis: [
      {
        type: 'category', data: times, gridIndex: 0,
        axisLine: { lineStyle: { color: '#2a2e39' } },
        axisLabel: { color: '#787b86', fontSize: 10 },
        splitLine: { lineStyle: { color: '#1e222d' } },
        axisTick: { show: false },
      },
      {
        type: 'category', data: times, gridIndex: 1,
        axisLabel: { color: '#787b86', fontSize: 10 },
        axisLine: { lineStyle: { color: '#2a2e39' } },
        splitLine: { show: false },
        axisTick: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true, gridIndex: 0,
        axisLabel: { color: '#787b86', fontSize: 10 },
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#1e222d' } },
        splitNumber: 6,
      },
      {
        scale: true, gridIndex: 1,
        axisLabel: { show: false },
        axisLine: { show: false },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - 6000 / Math.max(props.klines.length, 1) * 100), end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 20, borderColor: '#2a2e39', fillerColor: '#2a2e3966', handleStyle: { color: '#787b86' }, textStyle: { color: '#787b86', fontSize: 10 } },
    ],
    series: [
      {
        name: 'K線', type: 'candlestick',
        xAxisIndex: 0, yAxisIndex: 0,
        data: candles,
        itemStyle: {
          color:        '#26a69a',
          color0:       '#ef5350',
          borderColor:  '#26a69a',
          borderColor0: '#ef5350',
        },
        markPoint: {
          symbolSize: 14,
          data: props.markers.map(m => ({
            coord: [
              props.klines.findIndex(k => k.time_ms === m.time_ms),
              m.price,
            ],
            itemStyle: { color: m.side === 'long' ? '#26a69a' : '#ef5350' },
            label: { show: false },
          })),
        },
      },
      {
        name: 'Volume', type: 'bar',
        xAxisIndex: 1, yAxisIndex: 1,
        data: volumes,
        barMaxWidth: 8,
      },
    ],
  }
})
</script>
