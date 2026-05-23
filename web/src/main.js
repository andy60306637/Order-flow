import { createApp } from 'vue'
import { createPinia } from 'pinia'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CandlestickChart, LineChart, BarChart, HeatmapChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  AxisPointerComponent,
  MarkLineComponent,
  MarkPointComponent,
} from 'echarts/components'

import App from './App.vue'
import router from './router/index.js'
import './assets/main.css'

use([
  CanvasRenderer,
  CandlestickChart, LineChart, BarChart, HeatmapChart,
  GridComponent, TooltipComponent, DataZoomComponent,
  AxisPointerComponent, MarkLineComponent, MarkPointComponent,
])

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.component('VChart', VChart)
app.mount('#app')
