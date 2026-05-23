<template>
  <div class="p-4 max-w-2xl space-y-4">
    <h1 class="text-base font-semibold">設定</h1>

    <div v-if="loading" class="text-dim text-sm">載入中…</div>

    <div v-else class="space-y-4">
      <!-- Backtest config -->
      <div class="card space-y-3">
        <h2 class="text-sm font-medium">回測預設值</h2>
        <div class="grid grid-cols-2 gap-3">
          <div v-for="field in btFields" :key="field.key">
            <label class="block text-xs text-dim mb-1">{{ field.label }}</label>
            <input
              v-model="bt[field.key]"
              :type="field.type || 'number'"
              :step="field.step"
              class="input-field"
            />
          </div>
        </div>
      </div>

      <!-- Data root -->
      <div class="card space-y-2">
        <h2 class="text-sm font-medium">資料根目錄</h2>
        <input v-model="dataRoot" type="text" class="input-field" placeholder="/path/to/data" />
        <p class="text-xs text-dim">留空則使用預設路徑 (project/data)</p>
      </div>

      <!-- Raw JSON editor -->
      <div class="card space-y-2">
        <h2 class="text-sm font-medium">進階 JSON 編輯</h2>
        <textarea v-model="rawJson" rows="12"
                  class="input-field font-mono text-xs resize-y"
                  spellcheck="false" />
        <div v-if="jsonError" class="text-xs text-down">{{ jsonError }}</div>
      </div>

      <div class="flex gap-3">
        <button class="btn-primary" @click="save">儲存設定</button>
        <button class="btn-ghost"   @click="load">重新載入</button>
      </div>
      <div v-if="saveMsg" class="text-xs" :class="saveOk ? 'text-up' : 'text-down'">{{ saveMsg }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { settingsApi } from '@/api/client.js'

const loading  = ref(true)
const rawJson  = ref('{}')
const jsonError = ref('')
const saveMsg  = ref('')
const saveOk   = ref(true)
const settings = ref({})

const btFields = [
  { key: 'initial_capital', label: '初始資金',   step: '100' },
  { key: 'leverage',        label: '槓桿',        step: '1' },
  { key: 'max_loss_pct',    label: '最大風險 %',  step: '0.1' },
  { key: 'custom_fee_rate', label: '手續費率',    step: '0.001' },
  { key: 'slippage_bps',    label: '滑價 bps',   step: '0.1' },
]

const bt = computed({
  get: () => settings.value.backtest_config || {},
  set: (v) => { settings.value.backtest_config = v },
})

const dataRoot = computed({
  get: () => settings.value.data_root || '',
  set: (v) => { settings.value.data_root = v },
})

watch(settings, (v) => {
  try {
    rawJson.value  = JSON.stringify(v, null, 2)
    jsonError.value = ''
  } catch { /* ignore */ }
}, { deep: true })

async function load() {
  loading.value = true
  try {
    const { data } = await settingsApi.get()
    settings.value = data
  } catch (e) {
    saveMsg.value = '載入失敗：' + e.message
    saveOk.value  = false
  } finally {
    loading.value = false
  }
}

async function save() {
  saveMsg.value = ''
  // Merge raw JSON edits back
  try {
    const parsed = JSON.parse(rawJson.value)
    settings.value = parsed
    jsonError.value = ''
  } catch (e) {
    jsonError.value = 'JSON 格式錯誤：' + e.message
    return
  }
  try {
    await settingsApi.update(settings.value)
    saveMsg.value = '✓ 已儲存'
    saveOk.value  = true
  } catch (e) {
    saveMsg.value = '儲存失敗：' + e.message
    saveOk.value  = false
  }
}

onMounted(load)
</script>
