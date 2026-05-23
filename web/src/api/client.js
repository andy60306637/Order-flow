import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 60_000,
})

// ── Backtest ──────────────────────────────────────────────────────────────────
export const backtestApi = {
  strategies: ()           => api.get('/api/backtest/strategies'),
  symbols:    ()           => api.get('/api/backtest/symbols'),
  run:        (payload)    => api.post('/api/backtest/run', payload),
  getJob:     (jobId)      => api.get(`/api/backtest/jobs/${jobId}`),
  listJobs:   ()           => api.get('/api/backtest/jobs'),
}

// ── Research ──────────────────────────────────────────────────────────────────
export const researchApi = {
  factors:    (tick=true)  => api.get('/api/research/factors', { params: { include_tick: tick } }),
  run:        (payload)    => api.post('/api/research/run', payload),
  getJob:     (jobId)      => api.get(`/api/research/jobs/${jobId}`),
}

// ── Settings ──────────────────────────────────────────────────────────────────
export const settingsApi = {
  get:        ()           => api.get('/api/settings'),
  update:     (data)       => api.put('/api/settings', { data }),
}

// ── Market ───────────────────────────────────────────────────────────────────
export const marketApi = {
  symbols:    ()           => api.get('/api/market/symbols'),
}

export default api
