async function get(path) {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`HTTP ${r.status} on ${path}`)
  return r.json()
}

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: body ? { 'content-type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(`HTTP ${r.status} on ${path}`)
  return r.json()
}

export const api = {
  stats: () => get('/stats'),
  evBets: (bankroll, minEdge, league, { force = false } = {}) =>
    get(`/ev-bets?bankroll=${bankroll}&min_edge=${minEdge}${league ? `&league=${league}` : ''}${force ? '&force=true' : ''}`),
  predictions: (limit = 50) => get(`/predictions?limit=${limit}`),
  bets: (limit = 200) => get(`/bets?limit=${limit}`),
  timeseries: () => get('/stats/timeseries'),
  backtestResult: () => get('/backtest-result'),
  syncStatus: () => get('/sync-data/status'),
  anomalies: (limit = 200) => get(`/anomalies?limit=${limit}`),
  modelHealth: (league = null) => get(`/model-health${league ? `?league=${league}` : ''}`),
  logBet: (b) => post('/bets', b),
  markResult: (betId, payload) => post(`/bets/${betId}/mark-result`, payload),
  sendDigest: () => post('/send-digest'),
  digestPreview: () => get('/digest-preview'),
  portfolioSummary: ({ league, isPaper } = {}) => {
    const params = new URLSearchParams()
    if (league) params.set('league', league)
    if (isPaper != null) params.set('is_paper', isPaper ? 'true' : 'false')
    const qs = params.toString()
    return get(`/portfolio/summary${qs ? `?${qs}` : ''}`)
  },
  portfolioProjection: ({ matches, stake, edge, betsPerMatch, avgDecimalOdds, startingBankroll }) => {
    const params = new URLSearchParams()
    if (matches != null) params.set('matches', matches)
    if (stake != null) params.set('stake', stake)
    if (edge != null) params.set('edge', edge)
    if (betsPerMatch != null) params.set('bets_per_match', betsPerMatch)
    if (avgDecimalOdds != null) params.set('avg_decimal_odds', avgDecimalOdds)
    if (startingBankroll != null) params.set('starting_bankroll', startingBankroll)
    return get(`/portfolio/projection?${params}`)
  },
}
