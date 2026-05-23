<template>
  <div class="h-[calc(100vh-44px)] flex flex-col bg-bg">
    <div class="h-11 border-b border-border bg-panel flex items-center gap-3 px-4 shrink-0">
      <label class="text-xs text-dim">策略</label>
      <select v-model="selected" class="select-field w-80 h-7 py-0" @change="loadStrategy">
        <option v-for="s in strategies" :key="s" :value="s">{{ s }}</option>
      </select>
      <div v-if="detail" class="text-xs text-dim ml-auto">
        {{ detail.pipelines.length }} pipelines
      </div>
    </div>

    <div class="flex-1 min-h-0 flex">
      <aside class="w-[360px] border-r border-border bg-panel/60 p-4 overflow-auto">
        <div v-if="detail" class="space-y-4">
          <div>
            <h1 class="text-base font-semibold">{{ detail.name }}</h1>
            <p class="text-xs text-dim font-mono mt-1">{{ detail.class_name }}</p>
          </div>

          <div class="card space-y-2">
            <div class="text-xs text-dim">Pipeline 概覽</div>
            <div v-for="p in detail.pipelines" :key="p.name" class="flex justify-between text-xs">
              <span class="text-text">{{ p.name }}</span>
              <span class="text-dim">w={{ p.allocation_weight }}</span>
            </div>
          </div>

          <div class="card space-y-3">
            <div class="text-xs text-dim">Stage 詳細資訊</div>
            <template v-if="activeStage">
              <div>
                <div class="text-sm font-medium">{{ activeStage.name }}</div>
                <div class="text-xs text-dim font-mono">{{ activeStage.class_name }}</div>
              </div>
              <p v-if="activeStage.doc" class="text-xs text-dim whitespace-pre-wrap">{{ activeStage.doc }}</p>
              <div class="max-h-96 overflow-auto border border-border rounded">
                <table class="w-full text-xs">
                  <tbody>
                    <tr v-for="(v, k) in activeStage.params" :key="k" class="border-b border-border/40">
                      <td class="py-1 px-2 text-dim font-mono align-top">{{ k }}</td>
                      <td class="py-1 px-2 font-mono whitespace-pre-wrap break-all">{{ fmtParam(v) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
            <div v-else class="text-xs text-dim">點擊右側 Stage 卡片查看詳細參數</div>
          </div>
        </div>
      </aside>

      <main class="flex-1 overflow-auto p-6">
        <div v-if="loading" class="text-sm text-dim">載入中…</div>
        <div v-else-if="error" class="text-sm text-down">{{ error }}</div>
        <div v-else-if="detail" class="flex flex-col items-center gap-6">
          <section v-for="p in detail.pipelines" :key="p.name" class="w-full max-w-3xl">
            <div class="text-center text-xs text-dim mb-3 font-mono">
              {{ p.name }} · weight={{ p.allocation_weight }}
            </div>
            <div class="flex flex-col items-center">
              <template v-for="(stage, idx) in p.stages" :key="stage.index">
                <button
                  class="stage-card"
                  :class="{ selected: activeStage === stage }"
                  @click="activeStage = stage"
                >
                  <div class="stage-head">
                    <span>{{ stage.badge }}</span>
                    <strong>{{ stage.name }}</strong>
                  </div>
                  <div class="stage-body">
                    <div v-for="line in stage.summary" :key="line" class="truncate">{{ line }}</div>
                    <div v-if="!stage.summary.length" class="text-dim">(no scalar params)</div>
                  </div>
                </button>
                <div v-if="idx < p.stages.length - 1" class="text-border text-xl leading-8">↓</div>
              </template>
            </div>
          </section>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { pipelineApi } from '@/api/client.js'

const strategies = ref([])
const selected = ref('')
const detail = ref(null)
const activeStage = ref(null)
const loading = ref(false)
const error = ref('')

function fmtParam(v) {
  if (v == null) return 'null'
  if (typeof v === 'object') return JSON.stringify(v, null, 2)
  return String(v)
}

async function loadStrategy() {
  if (!selected.value) return
  loading.value = true
  error.value = ''
  activeStage.value = null
  try {
    const { data } = await pipelineApi.get(selected.value)
    detail.value = data
    activeStage.value = data.pipelines?.[0]?.stages?.[0] || null
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const { data } = await pipelineApi.strategies()
    strategies.value = data.strategies || []
    selected.value = strategies.value[0] || ''
    await loadStrategy()
  } catch (e) {
    error.value = e.message
  }
})
</script>

<style scoped>
.stage-card {
  width: min(420px, 100%);
  text-align: left;
  border: 1px solid #263245;
  background: #1e222d;
  border-radius: 8px;
  overflow: hidden;
  transition: border-color .15s, transform .15s;
}
.stage-card:hover,
.stage-card.selected {
  border-color: #64b5f6;
}
.stage-card:hover {
  transform: translateY(-1px);
}
.stage-head {
  min-height: 34px;
  background: #263245;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 12px;
}
.stage-head span {
  color: #8f96a8;
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.stage-head strong {
  color: #f2f5f9;
  font-size: 13px;
}
.stage-body {
  min-height: 72px;
  padding: 9px 12px;
  color: #d1d4dc;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
