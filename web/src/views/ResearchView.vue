<template>
  <div class="p-4 space-y-4">
    <h1 class="text-base font-semibold">Research Lab — 因子 IC 分析</h1>

    <!-- Config -->
    <div class="card grid grid-cols-2 md:grid-cols-4 gap-4">
      <div>
        <label class="block text-xs text-dim mb-1">交易對</label>
        <select v-model="form.symbol" class="select-field">
          <option v-for="s in ['BTCUSDT','ETHUSDT','SOLUSDT']" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">週期</label>
        <select v-model="form.interval" class="select-field">
          <option v-for="i in ['1m','3m','5m','15m','30m','1h']" :key="i" :value="i">{{ i }}</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">預測週期 (逗號分隔)</label>
        <input v-model="horizonsInput" type="text" class="input-field" placeholder="3,6,12,24" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">分位數</label>
        <input v-model.number="form.quantiles" type="number" class="input-field" min="2" max="10" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">進場延遲 (bars)</label>
        <input v-model.number="form.entry_lag" type="number" class="input-field" min="0" max="5" />
      </div>
      <div>
        <label class="block text-xs text-dim mb-1">樣本內比例</label>
        <input v-model.number="form.train_ratio" type="number" class="input-field" step="0.1" min="0.1" max="0.9" />
      </div>
      <div class="flex items-end">
        <label class="text-xs text-dim flex items-center gap-2 cursor-pointer">
          <input v-model="form.use_tick_features" type="checkbox" />
          Tick 特徵
        </label>
      </div>
      <div class="flex items-end">
        <button class="btn-primary w-full" :disabled="running" @click="runResearch">
          {{ running ? '分析中…' : '執行分析' }}
        </button>
      </div>
    </div>

    <!-- Month picker -->
    <div class="card">
      <div class="text-xs text-dim mb-2">選擇月份（最多可多選）</div>
      <div class="flex flex-wrap gap-2 max-h-40 overflow-y-auto">
        <button
          v-for="m in availableMonths" :key="m"
          class="px-2 py-0.5 text-xs rounded border transition-colors"
          :class="form.selected_months.includes(m)
            ? 'bg-accent border-accent text-white'
            : 'border-border text-dim hover:border-text'"
          @click="toggleMonth(m)"
        >{{ m }}</button>
      </div>
    </div>

    <!-- Factor picker -->
    <div class="card">
      <div class="flex items-center gap-2 mb-2">
        <span class="text-xs text-dim">選擇因子</span>
        <button class="text-xs text-accent" @click="selectAllFactors">全選</button>
        <button class="text-xs text-dim"    @click="form.factor_names = []">清除</button>
      </div>
      <div class="flex flex-wrap gap-2 max-h-48 overflow-y-auto">
        <button
          v-for="f in factors" :key="f.name"
          class="px-2 py-0.5 text-xs rounded border transition-colors"
          :class="form.factor_names.includes(f.name)
            ? 'bg-accent/20 border-accent text-text'
            : 'border-border text-dim hover:border-text'"
          :title="f.description"
          @click="toggleFactor(f.name)"
        >{{ f.name }}</button>
      </div>
    </div>

    <!-- Progress / Error -->
    <div v-if="progress" class="text-xs text-dim px-1">{{ progress }}</div>
    <div v-if="error"    class="text-xs text-down px-1">錯誤：{{ error }}</div>

    <!-- Results -->
    <div v-if="result">
      <div v-for="(res, regime) in result" :key="regime" class="space-y-3 mb-6">
        <h2 class="text-sm font-medium text-dim">Regime: {{ regime }}</h2>

        <!-- Summary table -->
        <div class="card overflow-auto">
          <table class="w-full text-xs">
            <thead>
              <tr class="text-dim border-b border-border">
                <th class="text-left py-1 px-2">因子</th>
                <th v-for="h in resultHorizons(res)" :key="h"
                    class="text-right py-1 px-2" colspan="2">h={{ h }}</th>
              </tr>
              <tr class="text-dim border-b border-border/50 text-[10px]">
                <th></th>
                <template v-for="h in resultHorizons(res)" :key="h">
                  <th class="text-right py-0.5 px-2">IC</th>
                  <th class="text-right py-0.5 px-2">IR</th>
                </template>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in res.summary" :key="row.factor"
                  class="border-b border-border/30 hover:bg-border/30">
                <td class="py-1 px-2 font-mono">{{ row.factor }}</td>
                <template v-for="h in resultHorizons(res)" :key="h">
                  <td class="text-right py-1 px-2"
                      :class="icColor(row[`IC_h${h}`])">
                    {{ fmtIC(row[`IC_h${h}`]) }}
                  </td>
                  <td class="text-right py-1 px-2"
                      :class="icColor(row[`IR_h${h}`])">
                    {{ fmtIC(row[`IR_h${h}`]) }}
                  </td>
                </template>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { researchApi } from '@/api/client.js'

// ── State ─────────────────────────────────────────────────────────────────────
const factors        = ref([])
const running        = ref(false)
const progress       = ref('')
const error          = ref('')
const result         = ref(null)
const horizonsInput  = ref('3,6,12,24')

const form = ref({
  symbol:            'BTCUSDT',
  interval:          '1m',
  selected_months:   [],
  factor_names:      [],
  quantiles:         4,
  entry_lag:         1,
  train_ratio:       0.4,
  use_tick_features: true,
})

// ── Available months (2021-01 → current) ─────────────────────────────────────
const availableMonths = computed(() => {
  const months = []
  const start = new Date('2021-01-01')
  const now   = new Date()
  let   cur   = new Date(start)
  while (cur <= now) {
    const y = cur.getFullYear()
    const m = String(cur.getMonth() + 1).padStart(2, '0')
    months.push(`${y}${m}`)
    cur.setMonth(cur.getMonth() + 1)
  }
  return months
})

// ── Helpers ───────────────────────────────────────────────────────────────────
function toggleMonth(m) {
  const i = form.value.selected_months.indexOf(m)
  if (i >= 0) form.value.selected_months.splice(i, 1)
  else        form.value.selected_months.push(m)
}

function toggleFactor(name) {
  const i = form.value.factor_names.indexOf(name)
  if (i >= 0) form.value.factor_names.splice(i, 1)
  else        form.value.factor_names.push(name)
}

function selectAllFactors() {
  form.value.factor_names = factors.value.map(f => f.name)
}

function resultHorizons(res) {
  if (!res?.summary?.length) return []
  const keys = Object.keys(res.summary[0]).filter(k => k.startsWith('IC_h'))
  return keys.map(k => parseInt(k.replace('IC_h', '')))
}

function fmtIC(v) { return v != null ? v.toFixed(3) : '—' }
function icColor(v) {
  if (v == null) return 'text-dim'
  if (v >  0.05) return 'text-up'
  if (v < -0.05) return 'text-down'
  return 'text-text'
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function runResearch() {
  if (running.value) return
  if (!form.value.selected_months.length) { error.value = '請先選擇月份'; return }
  if (!form.value.factor_names.length)    { error.value = '請先選擇因子'; return }

  running.value = true
  error.value   = ''
  progress.value = '提交分析任務…'
  result.value   = null

  try {
    const horizons = horizonsInput.value.split(',').map(Number).filter(Boolean)
    const payload  = { ...form.value, horizons }
    const { data } = await researchApi.run(payload)
    await pollJob(data.job_id)
  } catch (e) {
    error.value   = e.message
    running.value = false
  }
}

async function pollJob(jobId) {
  while (true) {
    await new Promise(r => setTimeout(r, 2000))
    const { data } = await researchApi.getJob(jobId)
    progress.value = data.progress || ''
    if (data.status === 'done') {
      result.value   = data.result
      running.value  = false
      progress.value = ''
      return
    }
    if (data.status === 'error') {
      error.value    = data.error
      running.value  = false
      progress.value = ''
      return
    }
  }
}

onMounted(async () => {
  try {
    const { data } = await researchApi.factors()
    factors.value = data.factors
  } catch { /* ignore */ }
})
</script>
