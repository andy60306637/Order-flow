import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 60_000,
})

// ── Backtest ──────────────────────────────────────────────────────────────────
export const backtestApi = {
  strategies:    ()        => api.get('/api/backtest/strategies'),
  symbols:       ()        => api.get('/api/backtest/symbols'),
  availableData: ()        => api.get('/api/backtest/available-data'),
  importTicks:   (payload) => api.post('/api/backtest/tick-cache/import', payload),
  clearTicks:    (symbol)  => api.delete(`/api/backtest/tick-cache/${symbol}`),
  run:           (payload) => api.post('/api/backtest/run', payload),
  getJob:        (jobId)   => api.get(`/api/backtest/jobs/${jobId}`, { timeout: 0 }),
  exportUrl:     (jobId)   => `/api/backtest/jobs/${jobId}/export.xlsx`,
  snapshot:      (jobId, tradeIdx) => api.get(`/api/backtest/jobs/${jobId}/snapshots/${tradeIdx}`),
  listJobs:      ()        => api.get('/api/backtest/jobs'),
}

// ── Research ──────────────────────────────────────────────────────────────────
export const researchApi = {
  factors:    (tick=true)  => api.get('/api/research/factors', { params: { include_tick: tick } }),
  regimeOptions: ()        => api.get('/api/research/regime-options'),
  run:        (payload)    => api.post('/api/research/run', payload),
  getJob:     (jobId)      => api.get(`/api/research/jobs/${jobId}`, { timeout: 0 }),
  signals:    (payload)    => api.post('/api/research/signals', payload, { timeout: 0 }),
}

// ── Settings ──────────────────────────────────────────────────────────────────
export const settingsApi = {
  get:        ()           => api.get('/api/settings'),
  update:     (data)       => api.put('/api/settings', { data }),
  dataRoot:   ()           => api.get('/api/settings/data-root'),
  setDataRoot:(path)       => api.put('/api/settings/data-root', { path }),
}

// ── Market ───────────────────────────────────────────────────────────────────
export const marketApi = {
  symbols:    ()           => api.get('/api/market/symbols'),
}

// ── Pipeline Studio ──────────────────────────────────────────────────────────
export const pipelineApi = {
  strategies: ()        => api.get('/api/pipeline/strategies'),
  get:        (name)    => api.get(`/api/pipeline/strategies/${encodeURIComponent(name)}`),
}

export default api
