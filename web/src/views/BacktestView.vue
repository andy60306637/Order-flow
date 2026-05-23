<template>
  <div class="p-4 space-y-4">
    <h1 class="text-base font-semibold">回測儀表板</h1>

    <!-- Config form -->
    <div class="card grid grid-cols-2 md:grid-cols-4 gap-4">
      <div>
        <label class="block text-xs text-dim mb-1">交易對</label>
        <select v-model="form.symbol" class="select-field">
          <option v-for="s in symbols" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">K 線週期</label>
        <select v-model="form.interval" class="select-field">
          <option v-for="i in intervals" :key="i" :value="i">{{ i }}</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">策略</label>
        <select v-model="form.strategy_name" class="select-field">
          <option v-for="s in strategies" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">槓桿</label>
        <input v-model.number="form.leverage" type="number" class="input-field" min="1" max="125" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">開始日期</label>
        <input v-model="form.start_date" type="date" class="input-field" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">結束日期</label>
        <input v-model="form.end_date" type="date" class="input-field" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">初始資金 (USDT)</label>
        <input v-model.number="form.initial_capital" type="number" class="input-field" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">最大風險 %</label>
        <input v-model.number="form.max_loss_pct" type="number" class="input-field" step="0.1" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">手續費率 %</label>
        <input v-model.number="form.custom_fee_rate_pct" type="number" class="input-field" step="0.001" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">滑價 (bps)</label>
        <input v-model.number="form.slippage_bps" type="number" class="input-field" step="0.1" />
      </div>
      <div class="flex items-end gap-2">
        <label class="text-xs text-dim flex items-center gap-2 cursor-pointer">
          <input v-model="form.use_tick_mode" type="checkbox" class="rounded" />
          Tick 模式
        </label>
        <label class="text-xs text-dim flex items-center gap-2 cursor-pointer">
          <input v-model="form.compound" type="checkbox" class="rounded" />
          複利
        </label>
      </div>
      <div class="flex items-end">
        <button class="btn-primary w-full" :disabled="running" @click="runBacktest">
          {{ running ? '執行中…' : '執行回測' }}
        </button>
      </div>
    </div>

    <!-- Progress / Error -->
    <div v-if="progress" class="text-xs text-dim px-1">{{ progress }}</div>
    <div v-if="error"    class="text-xs text-down px-1">錯誤：{{ error }}</div>

    <!-- Summary cards -->
    <div v-if="stats" class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
      <StatCard v-for="m in summaryMetrics" :key="m.label" v-bind="m" />
    </div>

    <!-- Trade list -->
    <div v-if="stats" class="card overflow-auto max-h-96">
      <table class="w-full text-xs">
        <thead>
          <tr class="text-dim border-b border-border">
            <th class="text-left py-1 px-2">方向</th>
            <th class="text-right py-1 px-2">進場</th>
            <th class="text-right py-1 px-2">出場</th>
            <th class="text-right py-1 px-2">淨損益</th>
            <th class="text-right py-1 px-2">R 值</th>
            <th class="text-left  py-1 px-2">進場時間</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in tradeList" :key="i"
              class="border-b border-border/30 hover:bg-border/30">
            <td class="py-1 px-2" :class="t.dir==='long' ? 'text-up' : 'text-down'">
              {{ t.dir === 'long' ? '多' : '空' }}
            </td>
            <td class="text-right py-1 px-2">{{ fmt(t.entry) }}</td>
            <td class="text-right py-1 px-2">{{ fmt(t.exit) }}</td>
            <td class="text-right py-1 px-2" :class="t.net_pnl>=0 ? 'text-up' : 'text-down'">
              {{ fmtPnl(t.net_pnl) }}
            </td>
            <td class="text-right py-1 px-2" :class="t.r_multiple>=0 ? 'text-up' : 'text-down'">
              {{ t.r_multiple != null ? t.r_multiple.toFixed(2)+'R' : '—' }}
            </td>
            <td class="py-1 px-2 text-dim">{{ fmtTime(t.entry_time) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { backtestApi } from '@/api/client.js'

// ── Inline StatCard ───────────────────────────────────────────────────────────
const StatCard = {
  props: { label: String, value: [String, Number], color: String },
  template: `
    <div class="card text-center">
      <div class="text-xs text-dim mb-1">{{ label }}</div>
      <div class="text-base font-semibold" :class="color || 'text-text'">{{ value }}</div>
    </div>
  `
}

// ── State ─────────────────────────────────────────────────────────────────────
const symbols    = ref([])
const intervals  = ref(['1m','3m','5m','15m','30m','1h','4h'])
const strategies = ref([])
const running    = ref(false)
const progress   = ref('')
const error      = ref('')
const stats      = ref(null)

const form = ref({
  symbol:            'BTCUSDT',
  interval:          '1m',
  strategy_name:     '',
  start_date:        '2024-01-01',
  end_date:          '2024-12-31',
  initial_capital:   10000,
  max_loss_pct:      2,
  leverage:          20,
  fee_mode:          '自訂',
  custom_fee_rate_pct: 0.032,
  slippage_bps:      0.2,
  compound:          false,
  use_tick_mode:     false,
})

// ── Computed ──────────────────────────────────────────────────────────────────
const tradeList = computed(() => stats.value?.trade_list || [])

const summaryMetrics = computed(() => {
  if (!stats.value) return []
  const s = stats.value
  const trades = s.trades ?? 0
  const wr = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—'
  const pf = s.profit_factor != null ? s.profit_factor.toFixed(2) : '—'
  const total = s.total_net_pnl != null ? fmtPnl(s.total_net_pnl) : '—'
  const dd = s.max_drawdown_pct != null ? (s.max_drawdown_pct * 100).toFixed(1) + '%' : '—'
  const sharpe = s.sharpe_ratio != null ? s.sharpe_ratio.toFixed(2) : '—'
  return [
    { label: '交易筆數', value: trades },
    { label: '勝率',     value: wr, color: parseFloat(wr) >= 50 ? 'text-up' : 'text-down' },
    { label: '盈虧比',   value: pf },
    { label: '淨損益',   value: total, color: s.total_net_pnl >= 0 ? 'text-up' : 'text-down' },
    { label: '最大回撤', value: dd, color: 'text-down' },
    { label: 'Sharpe',  value: sharpe },
  ]
})

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(v) { return v != null ? Number(v).toLocaleString('en-US', { minimumFractionDigits: 1 }) : '—' }
function fmtPnl(v) { return v != null ? (v >= 0 ? '+' : '') + v.toFixed(2) : '—' }
function fmtTime(ms) {
  if (!ms) return '—'
  return new Date(ms).toLocaleString('zh-TW', { hour12: false }).replace(/\//g, '-')
}

function dateToMs(dateStr) {
  return new Date(dateStr + 'T00:00:00Z').getTime()
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function runBacktest() {
  if (running.value) return
  running.value = true
  error.value   = ''
  progress.value = '提交回測任務…'
  stats.value   = null

  try {
    const payload = {
      ...form.value,
      start_ms:        dateToMs(form.value.start_date),
      end_ms:          dateToMs(form.value.end_date),
      custom_fee_rate: form.value.custom_fee_rate_pct / 100,
    }
    const { data } = await backtestApi.run(payload)
    await pollJob(data.job_id, backtestApi)
  } catch (e) {
    error.value = e.message
    running.value = false
  }
}

async function pollJob(jobId, api) {
  while (true) {
    await new Promise(r => setTimeout(r, 1500))
    const { data } = await api.getJob(jobId)
    progress.value = data.progress || ''
    if (data.status === 'done') {
      stats.value   = data.result
      running.value = false
      progress.value = ''
      return
    }
    if (data.status === 'error') {
      error.value   = data.error
      running.value = false
      progress.value = ''
      return
    }
  }
}

onMounted(async () => {
  try {
    const [sv, stv] = await Promise.all([backtestApi.symbols(), backtestApi.strategies()])
    symbols.value   = sv.data.symbols
    intervals.value = sv.data.intervals
    strategies.value = stv.data.strategies
    if (strategies.value.length) form.value.strategy_name = strategies.value[0]
  } catch { /* ignore */ }
})
</script>
