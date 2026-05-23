import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/',          redirect: '/market' },
  { path: '/market',    component: () => import('@/views/MarketView.vue'),   meta: { title: '即時行情' } },
  { path: '/backtest',  component: () => import('@/views/BacktestView.vue'), meta: { title: '回測儀表板' } },
  { path: '/research',  component: () => import('@/views/ResearchView.vue'), meta: { title: 'Research Lab' } },
  { path: '/pipeline',  component: () => import('@/views/PipelineView.vue'), meta: { title: 'Pipeline 設計室' } },
  { path: '/settings',  component: () => import('@/views/SettingsView.vue'), meta: { title: '設定' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = `${to.meta.title || ''} — OrderFlow`
})

export default router
