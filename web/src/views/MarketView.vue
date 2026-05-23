<template>
  <!--
    布局對應 GUI main_window.py _build_ui：
    ┌─── inner ctrl bar ──────────────────────────────────────────────────┐
    │ [Symbol▼] [Interval▼] | FP[▼] | [連線▶]  $price  ● status        │
    ├──────────────┬──────────────────────────────────────────────────────┤
    │              │  [K線] [Footprint] [容量分析]                         │
    │  Order Book  │  ───────────────── KlineChart ─────────────────────  │
    │  (Level 2)   │                                                       │
    │  ──────────  ├──────────────────── CVD Chart ─────────────────────  │
    │  OB Heatmap  │  ─────────────── Stats Panel ──────────────────────  │
    └──────────────┴──────────────────────────────────────────────────────┘
  -->
  <div class="flex flex-col bg-bg" style="height: calc(100vh - 44px)">

    <!-- ── Inner Control Bar (height 36px, 對應 _live_ctrl_bar) ───────── -->
    <div class="flex items-center gap-2 px-3 shrink-0 border-b border-border"
         style="height:36px; background:#1a1f2e">

      <select v-model="store.symbol" class="select-field w-28 h-6 py-0 text-xs"
              @change="reconnect">
        <option v-for="s in symbols" :key="s" :value="s">{{ s }}</option>
      </select>

      <select v-model="store.interval" class="select-field w-16 h-6 py-0 text-xs"
              @change="reconnect">
        <option v-for="i in intervals" :key="i" :value="i">{{ i }}</option>
      </select>

      <div class="h-4 w-px bg-border mx-1" />

      <!-- Chart tab selector (mirrors QTabWidget tabs) -->
      <div class="flex gap-1">
        <button v-for="tab in chartTabs" :key="tab"
                class="px-3 py-0.5 text-xs rounded-sm border transition-colors"
                :class="activeTab === tab
                  ? 'border-up text-text bg-up/10'
                  : 'border-border text-dim hover:text-text'"
                @click="activeTab = tab">
          {{ tab }}
        </button>
      </div>

      <div class="h-4 w-px bg-border mx-1" />

      <!-- Connect / Disconnect -->
      <button class="px-3 py-0.5 text-xs rounded border transition-colors"
              :class="store.status === 'connected'
                ? 'border-up/50 text-up hover:bg-up/10'
                : 'border-accent text-accent hover:bg-accent/10'"
              @click="store.status === 'connected' ? store.disconnect() : store.connect()">
        {{ store.status === 'connected' ? '斷線' : '連線' }}
      </button>

      <!-- Price -->
      <span class="ml-2 text-lg font-semibold font-mono"
            :class="priceColor">
        {{ store.lastPrice
            ? store.lastPrice.toLocaleString('en-US', {minimumFractionDigits: 1})
            : '—' }}
      </span>

      <!-- Status dot + engine message -->
      <div class="ml-auto flex items-center gap-1.5 text-xs">
        <span v-if="store.statusMsg" class="text-dim truncate max-w-xs">
          {{ store.statusMsg }}
        </span>
        <span class="inline-block w-1.5 h-1.5 rounded-full ml-1"
              :class="{
                'bg-up animate-pulse': store.status === 'connected',
                'bg-down':             store.status === 'error',
                'bg-dim':              store.status === 'disconnected',
              }" />
        <span class="text-dim">{{ store.status }}</span>
      </div>
    </div>

    <!-- ── Main body: left panel + right panel ─────────────────────────── -->
    <div class="flex flex-1 overflow-hidden gap-px bg-border">

      <!-- ── Left column: OrderBook (3/5) + OB Heatmap (2/5) ─────────── -->
      <!-- 對應 GUI left_widget setMinimumWidth(200) setMaximumWidth(280) -->
      <div class="flex flex-col shrink-0 bg-bg overflow-hidden"
           style="width:230px">

        <!-- Order Book label -->
        <div class="text-[10px] text-dim px-1 py-0.5 shrink-0">Order Book</div>

        <!-- OrderBook: flex 3 (佔 3/5) -->
        <div class="overflow-hidden" style="flex:3">
          <OrderBookComp
            :bids="store.bids"
            :asks="store.asks"
            :last-price="store.lastPrice"
            :spread="store.spread"
          />
        </div>

        <!-- Heatmap label -->
        <div class="text-[10px] text-dim px-1 py-0.5 shrink-0 border-t border-border">
          OB Heatmap
        </div>

        <!-- OB Heatmap: flex 2 (佔 2/5) -->
        <div class="overflow-hidden" style="flex:2">
          <ObHeatmap
            :snapshots="store.hmSnapshots"
            :last-price="store.lastPrice ?? 0"
          />
        </div>
      </div>

      <!-- ── Right column: chart tabs + CVD + Stats ─────────────────────── -->
      <!-- 對應 GUI right_splitter (Vertical) -->
      <div class="flex flex-col flex-1 overflow-hidden bg-bg">

        <!-- Chart tab content (flex 600 / (600+160+82)) -->
        <div class="overflow-hidden" style="flex:600">
          <KeepAlive>
            <KlineChart
              v-if="activeTab === 'K 線'"
              :klines="store.klines"
              class="w-full h-full"
            />
            <div v-else-if="activeTab === 'Footprint'"
                 class="w-full h-full flex items-center justify-center text-dim text-sm">
              Footprint — 需要 Tick 資料串流（開發中）
            </div>
            <div v-else
                 class="w-full h-full flex items-center justify-center text-dim text-sm">
              容量分析 — 需執行回測後查看
            </div>
          </KeepAlive>
        </div>

        <!-- CVD Chart (flex 160) -->
        <div class="overflow-hidden border-t border-border" style="flex:160">
          <div class="text-[10px] text-dim px-1 pt-0.5 shrink-0">CVD</div>
          <CvdChart :cvd="store.cvd" class="w-full" style="height: calc(100% - 16px)" />
        </div>

        <!-- Stats Panel (fixed 64px 對應 GUI setMaximumHeight(82)) -->
        <div class="border-t border-border shrink-0" style="height:64px">
          <StatsPanel
            :bar="store.lastBar"
            :delta="barDelta"
            :cvd="store.lastCvd"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useMarketStore } from '@/stores/market.js'
import { marketApi } from '@/api/client.js'

import KlineChart    from '@/components/KlineChart.vue'
import CvdChart      from '@/components/CvdChart.vue'
import OrderBookComp from '@/components/OrderBookComp.vue'
import ObHeatmap     from '@/components/ObHeatmap.vue'
import StatsPanel    from '@/components/StatsPanel.vue'

const store     = useMarketStore()
const symbols   = ref(['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT'])
const intervals = ref(['1m', '3m', '5m', '15m', '30m', '1h', '4h'])
const chartTabs = ['K 線', 'Footprint', '容量分析']
const activeTab = ref('K 線')

// ── Computed ─────────────────────────────────────────────────────────────────
let _prevClose = 0
const priceColor = computed(() => {
  const p = store.lastPrice
  if (!p) return 'text-text'
  const color = p >= _prevClose ? 'text-up' : 'text-down'
  _prevClose = p
  return color
})

const barDelta = computed(() => {
  // Approximate bar delta from recent trades (since last kline reset)
  const bar = store.lastBar
  if (!bar) return 0
  let buy = 0, sell = 0
  for (const t of store.trades) {
    if (!t.ts || t.ts < bar.time_ms) continue
    if (t.isSell) sell += t.qty
    else          buy  += t.qty
  }
  return buy - sell
})

// ── Actions ───────────────────────────────────────────────────────────────────
async function reconnect() {
  store.disconnect()
  await new Promise(r => setTimeout(r, 150))
  store.connect()
}

onMounted(async () => {
  try {
    const { data } = await marketApi.symbols()
    symbols.value  = data.symbols
    intervals.value = data.intervals
  } catch { /* ignore */ }
  store.connect()
})

onUnmounted(() => store.disconnect())
</script>
