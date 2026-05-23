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
  const footprints = ref([]) // [{time_ms, open, high, low, close, levels:{price:{bid,ask}}}]

  const FOOTPRINT_MAX = 180
  const FOOTPRINT_BASE_TICKS = {
    BTCUSDT: 10.0,
    ETHUSDT: 1.0,
    BNBUSDT: 0.5,
    SOLUSDT: 0.1,
    XRPUSDT: 0.0001,
    DOGEUSDT: 0.00001,
    ADAUSDT: 0.0001,
    AVAXUSDT: 0.01,
  }
  const tickMultipliers = [1, 2, 5, 10, 20, 50]
  const tickMultiplier = ref(1)
  const footprintMode = ref('BidxAsk')

  // Order book (server always sends merged ob_snapshot format)
  const bids = ref([])      // [[price, qty], ...] sorted desc
  const asks = ref([])      // [[price, qty], ...] sorted asc

  // Exchange info (tick size per symbol for price formatting)
  const tickSizes = ref({}) // { BTCUSDT: 0.1, ... }
  const tickSize  = computed(() => tickSizes.value[symbol.value] ?? 0.1)
  const footprintTickSize = computed(() => {
    const base = FOOTPRINT_BASE_TICKS[symbol.value] ?? ((tickSizes.value[symbol.value] ?? 0.01) * 100)
    return base * tickMultiplier.value
  })

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
    trades.value = []; hmSnapshots.value = []; footprints.value = []
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
      case 'kline':         return _onKline(data)
      case 'trade':         return _onTrade(data)
      case 'ob_snapshot':   return _onOB(data)
      case 'history':       return _onHistory(data)
      case 'more_history':  return _onMoreHistory(data)
      case 'agg_history':   return _onAggHistory(data)
      case 'more_agg_history': return _onAggHistory(data)
      case 'exchange_info': return _onExchangeInfo(data)
      case 'status':        return _onStatus(data)
      // agg_history / more_agg_history — footprint (future)
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
      _upsertFootprintCandle(bar)
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
      _upsertFootprintCandle(bar)
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

    const ts = raw.T ?? raw.trade_time_ms ?? Date.now()
    _applyFootprintTrade({ price, qty, isSell, ts })
    trades.value.push({ price, qty, isSell, ts })
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
    footprints.value = klines.value.slice(-FOOTPRINT_MAX).map(k => ({
      ...k,
      closed: true,
      levels: {},
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

  // ── More history (scroll-left: prepend older klines) ─────────────────────
  function _onMoreHistory(list) {
    if (!Array.isArray(list) || !list.length) return
    const older = list.map(k => ({
      time_ms: Number(k[0]),
      open:    parseFloat(k[1]),
      high:    parseFloat(k[2]),
      low:     parseFloat(k[3]),
      close:   parseFloat(k[4]),
      volume:  parseFloat(k[5] ?? 0),
      _buyVol: parseFloat(k[9]  ?? 0),
    }))
    // Prepend without duplicating the boundary bar
    const firstExisting = klines.value[0]?.time_ms ?? Infinity
    const fresh = older.filter(k => k.time_ms < firstExisting)
    if (!fresh.length) return

    // Rebuild CVD prefix
    let running = 0
    const cvdPrefix = fresh.map(k => {
      const sellVol = k.volume - k._buyVol
      running += (k._buyVol - sellVol)
      return { time_ms: k.time_ms, value: running }
    })
    // Shift existing CVD baseline
    const offset = running
    const cvdShifted = cvd.value.map(c => ({ ...c, value: c.value + offset }))

    klines.value = [...fresh, ...klines.value]
    cvd.value    = [...cvdPrefix, ...cvdShifted]
    footprints.value = [
      ...fresh.map(k => ({ ...k, closed: true, levels: {} })),
      ...footprints.value,
    ]
    if (klines.value.length > 3000) {
      klines.value = klines.value.slice(-3000)
      cvd.value    = cvd.value.slice(-3000)
    }
    if (footprints.value.length > FOOTPRINT_MAX) {
      footprints.value = footprints.value.slice(-FOOTPRINT_MAX)
    }
  }

  // ── Footprint history / live aggregation ─────────────────────────────────
  function _onAggHistory(payloadList) {
    if (!Array.isArray(payloadList) || !payloadList.length) return
    for (const payload of payloadList) {
      const ranges = Array.isArray(payload?.klines)
        ? payload.klines.map(r => ({ open: Number(r[0]), close: Number(r[1]) }))
        : []
      for (const raw of payload?.trades || []) {
        const ts = Number(raw.T ?? raw.trade_time_ms ?? raw[0] ?? 0)
        const price = parseFloat(raw.p ?? raw.price ?? raw[1] ?? 0)
        const qty = parseFloat(raw.q ?? raw.qty ?? raw[2] ?? 0)
        const isSell = Boolean(raw.m ?? raw.is_buyer_maker ?? raw[3] ?? false)
        const openTime = _resolveOpenTime(ts, ranges)
        if (openTime) _applyFootprintTrade({ price, qty, isSell, ts }, openTime)
      }
    }
    footprints.value = [...footprints.value]
  }

  function _resolveOpenTime(ts, ranges = []) {
    if (!ts) return 0
    const hit = ranges.find(r => ts >= r.open && ts <= r.close)
    if (hit) return hit.open
    for (let i = klines.value.length - 1; i >= 0; i--) {
      if (klines.value[i].time_ms <= ts) return klines.value[i].time_ms
    }
    return 0
  }

  function _bucket(price) {
    const ts = footprintTickSize.value || 1
    return Math.floor(price / ts) * ts
  }

  function _findOrCreateFootprint(openTime) {
    let idx = footprints.value.findIndex(c => c.time_ms === openTime)
    if (idx >= 0) return footprints.value[idx]

    const k = klines.value.find(row => row.time_ms === openTime)
    const candle = {
      time_ms: openTime,
      open: k?.open ?? 0,
      high: k?.high ?? 0,
      low: k?.low ?? 0,
      close: k?.close ?? 0,
      volume: k?.volume ?? 0,
      closed: true,
      levels: {},
    }
    footprints.value.push(candle)
    footprints.value.sort((a, b) => a.time_ms - b.time_ms)
    if (footprints.value.length > FOOTPRINT_MAX) {
      footprints.value = footprints.value.slice(-FOOTPRINT_MAX)
    }
    return candle
  }

  function _upsertFootprintCandle(bar) {
    const candle = _findOrCreateFootprint(bar.time_ms)
    Object.assign(candle, {
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.volume,
    })
    footprints.value = [...footprints.value]
  }

  function _applyFootprintTrade(trade, openTime = 0) {
    const ot = openTime || _resolveOpenTime(trade.ts)
    if (!ot || !trade.price || !trade.qty) return
    const candle = _findOrCreateFootprint(ot)
    const bucket = _bucket(trade.price)
    const key = String(bucket)
    if (!candle.levels[key]) {
      candle.levels[key] = { price: bucket, bid: 0, ask: 0 }
    }
    if (trade.isSell) candle.levels[key].ask += trade.qty
    else candle.levels[key].bid += trade.qty
    footprints.value = [...footprints.value]
  }

  // ── Exchange info (tick sizes) ────────────────────────────────────────────
  // Payload: { BTCUSDT: 0.1, ETHUSDT: 0.01, ... }
  function _onExchangeInfo(data) {
    if (data && typeof data === 'object') {
      Object.assign(tickSizes.value, data)
    }
  }

  // ── Status message ────────────────────────────────────────────────────────
  function _onStatus(msg) {
    if (typeof msg === 'string') statusMsg.value = msg
  }

  // ── Request more history via WS ───────────────────────────────────────────
  function requestMoreHistory() {
    const oldest = klines.value[0]?.time_ms
    if (!oldest || !ws.value || ws.value.readyState !== 1) return
    ws.value.send(JSON.stringify({ type: 'more_history', end_time_ms: oldest }))
  }

  function setFootprintMode(mode) {
    footprintMode.value = mode
  }

  function setTickMultiplier(multiplier) {
    tickMultiplier.value = Number(multiplier) || 1
    footprints.value = footprints.value.map(c => ({ ...c, levels: {} }))
  }

  return {
    symbol, interval, status, statusMsg,
    klines, cvd, footprints, bids, asks, trades,
    hmSnapshots, lastPrice, spread, lastCvd, lastBar,
    tickSize, tickSizes, footprintTickSize,
    tickMultipliers, tickMultiplier, footprintMode,
    connect, disconnect, requestMoreHistory,
    setFootprintMode, setTickMultiplier,
  }
})
