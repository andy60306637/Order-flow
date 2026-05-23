<template>
  <div class="min-h-screen bg-bg flex flex-col">
    <!-- Navbar -->
    <nav class="flex items-center h-11 px-4 bg-panel border-b border-border gap-6 shrink-0">
      <span class="text-accent font-semibold tracking-wide text-sm">OrderFlow</span>
      <router-link
        v-for="item in nav"
        :key="item.path"
        :to="item.path"
        class="text-dim text-sm hover:text-text transition-colors"
        active-class="text-text border-b-2 border-accent pb-0.5"
      >
        {{ item.label }}
      </router-link>
      <span class="ml-auto text-xs text-dim">{{ currentTime }}</span>
    </nav>

    <!-- Page content -->
    <main class="flex-1 overflow-auto">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const nav = [
  { path: '/market',   label: '即時行情' },
  { path: '/backtest', label: '回測儀表板' },
  { path: '/research', label: 'Research Lab' },
  { path: '/pipeline', label: 'Pipeline 設計室' },
  { path: '/settings', label: '設定' },
]

const currentTime = ref('')
let timer

function tick() {
  currentTime.value = new Date().toLocaleTimeString('zh-TW', { hour12: false })
}

onMounted(() => { tick(); timer = setInterval(tick, 1000) })
onUnmounted(() => clearInterval(timer))
</script>
