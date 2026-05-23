import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useMarketStore = defineStore('market', () => {
  const symbol   = ref('BTCUSDT')
  const interval = ref('1m')
  const status   = ref('disconnected')
  const statusMsg = ref('')
  const ws       = ref(null)

  // Chart data
  const klines  = ref([])   // [{time_ms, open, high, low, close, volume}]
  const cvd     = ref([])   // [{time_ms, value}] running CVD per bar

  // Order book (server always sends merged ob_snapshot format)
  const bids = ref([])      // [[price, qty], ...] sorted desc
  const asks = ref([])      // [[price, qty], ...] sorted asc

  // Recent trades tape
  const trades = ref([])

  // Heatmap: ring buffer of OB snapshots (time → price→qty map)
  const hmSnapshots = ref([])
  const HM_SLOTS = 300

  // Per-bar buy/sell accumulators (reset on new bar)
  let _curBarTime = 0
  let _barBuyVol  = 0
  let _barSellVol = 0

  const lastPrice = computed(() => {
    if (!klines.value.length) return null
    return klines.value[klines.value.length - 1].close
  })
  const spread = computed(() => {
    const bestAsk = asks.value[0]?.[0]
    const bestBid = bids.value[0]?.[0]
    if (!bestAsk || !bestBid) return null
    return (bestAsk - bestBid).toFixed(1)
  })
  const lastCvd = computed(() => cvd.value[cvd.value.length - 1]?.value ?? 0)
  const lastBar = computed(() => klines.value[klines.value.length - 1])

  // ── WebSocket ────────────────────────────────────────────────────────────
  function connect() {
    disconnect()
    klines.value = []; cvd.value = []; bids.value = []; asks.value = []
    trades.value = []; hmSnapshots.value = []
    _curBarTime = 0; _barBuyVol = 0; _barSellVol = 0

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url   = `${proto}//${location.host}/ws/market/${symbol.value}/${interval.value}`
    const sock  = new WebSocket(url)

    let hbTimer
    sock.onopen  = () => {
      status.value = 'connected'
      hbTimer = setInterval(() => sock.readyState === 1 && sock.send('ping'), 25_000)
    }
    sock.onclose = () => { status.value = 'disconnected'; clearInterval(hbTimer) }
    sock.onerror = () => { status.value = 'error' }
    sock.onmessage = (ev) => {
      try { const m = JSON.parse(ev.data); handleEvent(m.type, m.data) }
      catch { /* ignore */ }
    }
    ws.value = sock
  }

  function disconnect() {
    if (ws.value) { ws.value.close(); ws.value = null }
    status.value = 'disconnected'
  }

  // ── Event dispatcher ──────────────────────────────────────────────────────
  function handleEvent(type, data) {
    if (!data && type !== 'status') return
    switch (type) {
      case 'kline':       return _onKline(data)
      case 'trade':       return _onTrade(data)
      case 'ob_snapshot': return _onOB(data)
      case 'history':     return _onHistory(data)
      case 'status':      return _onStatus(data)
      // agg_history / more_history / exchange_info — future use
    }
  }

  // ── Kline (live WS event) ─────────────────────────────────────────────────
  // Binance kline event shape: { k: { t, T, o, h, l, c, v, x } }
  function _onKline(raw) {
    const k = raw.k || raw
    const bar = {
      time_ms: k.t ?? k.open_time ?? 0,
      open:    parseFloat(k.o ?? k.open),
      high:    parseFloat(k.h ?? k.high),
      low:     parseFloat(k.l ?? k.low),
      close:   parseFloat(k.c ?? k.close),
      volume:  parseFloat(k.v ?? k.volume ?? 0),
    }
    const last = klines.value[klines.value.length - 1]
    if (last && last.time_ms === bar.time_ms) {
      klines.value[klines.value.length - 1] = bar
      // Update current CVD point
      if (cvd.value.length) {
        const prevSum = cvd.value.length > 1 ? cvd.value[cvd.value.length - 2].value : 0
        cvd.value[cvd.value.length - 1] = {
          time_ms: bar.time_ms,
          value: prevSum + (_barBuyVol - _barSellVol),
        }
      }
    } else {
      klines.value.push(bar)
      if (klines.value.length > 1500) klines.value.shift()
      // New bar: commit CVD with accumulated delta
      const prev = cvd.value[cvd.value.length - 1]?.value ?? 0
      cvd.value.push({ time_ms: bar.time_ms, value: prev + _barBuyVol - _barSellVol })
      if (cvd.value.length > 1500) cvd.value.shift()
      _curBarTime = bar.time_ms
      _barBuyVol = 0; _barSellVol = 0
    }
  }

  // ── Trade (aggTrade) ──────────────────────────────────────────────────────
  // Binance aggTrade: { p, q, m, T, s }  m=true → buyer is maker → sell
  function _onTrade(raw) {
    const price = parseFloat(raw.p ?? raw.price ?? 0)
    const qty   = parseFloat(raw.q ?? raw.qty   ?? 0)
    const isSell = raw.m ?? raw.is_buyer_maker ?? false

    if (isSell) _barSellVol += qty
    else        _barBuyVol  += qty

    trades.value.push({ price, qty, isSell, ts: raw.T ?? raw.trade_time_ms ?? Date.now() })
    if (trades.value.length > 200) trades.value.shift()
  }

  // ── Order book (server always sends merged ob_snapshot: bids/asks) ────────
  function _onOB(raw) {
    if (raw.bids) bids.value = raw.bids.slice(0, 20)
    if (raw.asks) asks.value = raw.asks.slice(0, 20)

    // Heatmap: price → qty snapshot
    const snap = {}
    for (const [p, q] of [...(raw.bids || []), ...(raw.asks || [])]) {
      snap[parseFloat(p)] = parseFloat(q)
    }
    hmSnapshots.value.push(snap)
    if (hmSnapshots.value.length > HM_SLOTS) hmSnapshots.value.shift()
  }

  // ── History (Binance REST kline list-of-arrays) ───────────────────────────
  // Each row: [open_time, open, high, low, close, volume, close_time,
  //            quote_vol, trades, taker_buy_base_vol, taker_buy_quote_vol, ignore]
  function _onHistory(list) {
    if (!Array.isArray(list) || !list.length) return

    klines.value = list.map(k => ({
      time_ms: Number(k[0]),
      open:    parseFloat(k[1]),
      high:    parseFloat(k[2]),
      low:     parseFloat(k[3]),
      close:   parseFloat(k[4]),
      volume:  parseFloat(k[5] ?? 0),
      // taker_buy_base_vol at index 9 — used for CVD seed
      _buyVol:  parseFloat(k[9]  ?? 0),
    }))

    // CVD backfill: Σ(buyVol - sellVol) from history
    let running = 0
    cvd.value = klines.value.map(k => {
      const sellVol = k.volume - k._buyVol
      running += (k._buyVol - sellVol)
      return { time_ms: k.time_ms, value: running }
    })

    // Reset live accumulators for the most recent bar
    const lastBar = klines.value[klines.value.length - 1]
    _curBarTime = lastBar?.time_ms ?? 0
    _barBuyVol = 0
    _barSellVol = 0
  }

  // ── Status message ────────────────────────────────────────────────────────
  function _onStatus(msg) {
    if (typeof msg === 'string') statusMsg.value = msg
  }

  return {
    symbol, interval, status, statusMsg,
    klines, cvd, bids, asks, trades,
    hmSnapshots, lastPrice, spread, lastCvd, lastBar,
    connect, disconnect,
  }
})
