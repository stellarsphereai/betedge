import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import Header from './components/Header'
import BookBalanceStrip from './components/BookBalanceStrip'
import BestBetsGrid from './components/BestBetsGrid'
import StatsRow from './components/StatsRow'
import FilterTabs from './components/FilterTabs'
import MatchCard from './components/MatchCard'
import BankrollChart from './components/BankrollChart'
import CLVChart from './components/CLVChart'
import ModelAccuracyPanel from './components/ModelAccuracyPanel'
import PaperTradeLog from './components/PaperTradeLog'
import AnomaliesPanel from './components/AnomaliesPanel'
import ModelHealthPanel from './components/ModelHealthPanel'
import PortfolioView from './components/PortfolioView'

export default function App() {
  const [league, setLeague] = useState('epl')
  const [tab, setTab] = useState('all')
  // Theme: 'dark' (default) | 'light'. Stored on <html data-theme="…"> so a
  // single CSS layer in index.css can override the dark Tailwind utilities
  // without rewriting every component class.
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem('betedge_theme') || 'dark' } catch { return 'dark' }
  })
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { localStorage.setItem('betedge_theme', theme) } catch {}
  }, [theme])
  const toggleTheme = () => setTheme(t => t === 'light' ? 'dark' : 'light')
  const [windowHours, setWindowHours] = useState(72)
  const [flashedMatchId, setFlashedMatchId] = useState(null)
  const [stats, setStats] = useState(null)
  const [ev, setEv] = useState(null)
  const [predictions, setPredictions] = useState([])
  const [bets, setBets] = useState([])
  const [anomalies, setAnomalies] = useState(null)
  const [modelHealth, setModelHealth] = useState(null)
  const [timeseries, setTimeseries] = useState(null)
  const [backtest, setBacktest] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [lastFetched, setLastFetched] = useState(null)
  const leagueRef = useRef(league)
  leagueRef.current = league

  async function loadAll({ force = false } = {}) {
    setLoading(true)
    setError(null)
    try {
      const [s, e, p, b, ts, bt, an, mh] = await Promise.all([
        api.stats(),
        api.evBets(1000, 0.03, leagueRef.current, { force }),
        api.predictions(50),
        api.bets(200),
        api.timeseries(),
        api.backtestResult().catch(() => null),
        api.anomalies(200).catch(() => ({ count_today: 0, anomalies: [] })),
        api.modelHealth(leagueRef.current).catch(() => null),
      ])
      setStats(s); setEv(e); setPredictions(p.predictions || []); setBets(b.bets || [])
      setTimeseries(ts); setBacktest(bt); setAnomalies(an); setModelHealth(mh)
      setLastFetched(new Date())
    } catch (err) {
      setError(err.message || String(err))
    } finally {
      setLoading(false)
    }
  }

  // Initial fetch on mount + on league switch. No background polling — the
  // user clicks "Refresh odds" when they want fresh data. The Odds API meters
  // by markets×regions, so a 30s polling loop burns ~3,000 credits/hour.
  useEffect(() => {
    const id = setTimeout(loadAll, 200)
    return () => clearTimeout(id)
  }, [league])

  // Show matches kicking off in [-3h, +windowHours]. The future window is
  // user-controlled via the header dropdown.
  const KICKOFF_WINDOW_PAST_MS = 3 * 3600 * 1000

  const matchView = useMemo(() => {
    const now = Date.now()
    const futureMs = windowHours * 3600 * 1000
    const inWindow = (iso) => {
      if (!iso) return false
      const t = new Date(iso).getTime()
      if (Number.isNaN(t)) return false
      return t >= now - KICKOFF_WINDOW_PAST_MS && t <= now + futureMs
    }

    // Set of (match_id, market, line, outcome) for OPEN bets already logged
    // — paper or cash, doesn't matter. Once a bet has been recorded in the
    // trade log, it shouldn't keep nagging the user from the main +EV grid.
    // Settled bets (won/lost/void) are not included so a future bet on the
    // same matchup-after-results would still be actionable.
    const placedKeys = new Set()
    for (const b of bets || []) {
      if (b.status !== 'open') continue
      const k = `${b.match_id}|${b.market || 'h2h'}|${b.market_line ?? ''}|${b.bet_type}`
      placedKeys.add(k)
    }

    const consensus = ev?.match_consensus || {}
    const modelView = ev?.match_model_view || {}
    const inPlaySet = new Set(ev?.in_play_match_ids || [])
    const byMatch = new Map()
    for (const p of predictions) {
      if (p.league && p.league !== league) continue
      if (!inWindow(p.kickoff_time)) continue
      byMatch.set(p.match_id, {
        prediction: p, bets: [],
        consensus: consensus[p.match_id], modelView: modelView[p.match_id],
        inPlay: inPlaySet.has(p.match_id),
      })
    }
    for (const b of (ev?.bets || [])) {
      if (!inWindow(b.commence_time)) continue
      const k = `${b.match_id}|${b.market || 'h2h'}|${b.market_line ?? ''}|${b.outcome}`
      if (placedKeys.has(k)) continue
      // Only attach bets to predictions we already accepted into byMatch via
      // the league filter above. The previous fallback synthesized a fake
      // prediction with `league: <currently selected>` whenever a bet's
      // match_id wasn't in byMatch — but during a league switch, `ev` is
      // briefly stale (still holds the previous league's bets) while
      // predictions have already been filtered to the new league. The
      // fallback was resurrecting stale cross-league bets into the grid,
      // which read as 'league filter doesn't work'.
      const entry = byMatch.get(b.match_id)
      if (!entry) continue
      entry.bets.push(b)
    }
    return [...byMatch.values()]
  }, [predictions, ev, league, bets, windowHours])

  // Best edge across what's actually visible (current league + window + after
  // dedupe of placed bets). Drives the Best edge stat card and the click-to-
  // scroll handoff.
  const bestEdgeBet = useMemo(() => {
    let best = null
    for (const m of matchView) {
      for (const b of m.bets) {
        if (!best || b.edge > best.edge) {
          best = { ...b, _match_id: m.prediction.match_id }
        }
      }
    }
    return best
  }, [matchView])

  function scrollToMatch(matchId) {
    if (!matchId) return
    if (tab !== 'all' && tab !== 'ev' && tab !== 'high') setTab('all')
    setTimeout(() => {
      const el = document.getElementById(`match-${matchId}`)
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setFlashedMatchId(matchId)
      setTimeout(() => setFlashedMatchId(null), 1800)
    }, 50)
  }

  const filtered = useMemo(() => {
    if (tab === 'all') return matchView
    if (tab === 'ev') return matchView.filter(m => m.bets.length > 0)
    if (tab === 'high') return matchView.filter(m => m.prediction?.confidence === 'HIGH')
    return []
  }, [tab, matchView])

  const counts = {
    all: matchView.length,
    ev: matchView.filter(m => m.bets.length > 0).length,
    high: matchView.filter(m => m.prediction?.confidence === 'HIGH').length,
    log: bets.filter(b => b.is_paper).length,
    anomalies: anomalies?.count_today ?? 0,
    anomalies_excluding: (anomalies?.anomalies ?? []).filter(
      a => a.anomaly_type === 'PHANTOM_EDGE'
    ).length,
  }

  async function markBetResult(bet, payload) {
    try {
      await api.markResult(bet.id, payload)
      loadAll()
    } catch (e) {
      setError(e.message)
      throw e
    }
  }

  function onDeleteBet(deletedId) {
    // Drop from local state immediately so the +EV grid stops hiding the row
    // and the log row disappears. Portfolio re-queries on tab change so it'll
    // reflect the deletion next time it's opened.
    setBets(prev => prev.filter(b => b.id !== deletedId))
  }

  function onModeChangeBet(betId, isPaper) {
    setBets(prev => prev.map(b => b.id === betId ? { ...b, is_paper: isPaper ? 1 : 0 } : b))
  }

  async function _logBet(prediction, bet, isPaper) {
    try {
      await api.logBet({
        match_id: prediction.match_id,
        home_team: prediction.home_team,
        away_team: prediction.away_team,
        bet_type: bet.outcome,
        book: bet.best_book ?? bet.book,
        odds_at_placement: bet.best_odds ?? bet.decimal_odds,
        stake: bet.stake,
        edge_at_placement: bet.edge,
        is_paper: isPaper,
        market: bet.market || 'h2h',
        market_line: bet.market_line ?? null,
      })
      loadAll()
    } catch (e) {
      setError(e.message)
      throw e
    }
  }

  const logPaperBet = (prediction, bet) => _logBet(prediction, bet, true)
  const logRealBet = (prediction, bet) => _logBet(prediction, bet, false)

  return (
    <div className="max-w-7xl mx-auto px-5 py-6">
      <Header
        league={league}
        onLeagueChange={setLeague}
        windowHours={windowHours}
        onWindowChange={setWindowHours}
        lastFetched={lastFetched}
        loading={loading}
        error={error}
        ageS={ev?.age_s}
        cacheAgeS={ev?.cache?.hit ? ev.cache.age_s : null}
        onRefresh={() => loadAll({ force: true })}
        sportsbookCount={(stats?.actionable_books?.length) ?? 7}
        anomalyCount={anomalies?.count_today ?? 0}
        anomalyExcluding={counts.anomalies_excluding}
        onJumpToAnomalies={() => setTab('anomalies')}
        theme={theme}
        onToggleTheme={toggleTheme}
      />

      <BookBalanceStrip refreshKey={lastFetched?.getTime?.()} />

      <BestBetsGrid
        refreshKey={lastFetched?.getTime?.()}
        onJumpToMatch={scrollToMatch}
        headerLeague={league}
      />

      <StatsRow
        ev={ev}
        stats={stats}
        bestEdgeBet={bestEdgeBet}
        onJumpToBest={() => scrollToMatch(bestEdgeBet?._match_id)}
      />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 mb-3">
        <BankrollChart data={timeseries?.bankroll} startingBankroll={timeseries?.starting_bankroll} />
        <CLVChart data={timeseries?.clv} />
        <ModelAccuracyPanel accuracy={stats?.accuracy} backtest={backtest} />
      </div>
      <div className="mb-6">
        <ModelHealthPanel health={modelHealth} />
      </div>

      <FilterTabs active={tab} onChange={setTab} counts={counts} />

      {tab === 'log' ? (
        <PaperTradeLog
          bets={bets}
          onMarkResult={markBetResult}
          onDeleteBet={onDeleteBet}
          onModeChangeBet={onModeChangeBet}
        />
      ) : tab === 'portfolio' ? (
        <PortfolioView />
      ) : tab === 'anomalies' ? (
        <AnomaliesPanel anomalies={anomalies} />
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {filtered.length === 0 && (
            <div className="col-span-full bg-ink-900 border border-dashed border-ink-700 rounded-xl p-8 text-center text-slate-400 text-sm">
              {predictions.length === 0
                ? 'No predictions stored. Run the model: POST /run-model with team xG, or run python3 backtest.py for historical analysis.'
                : 'No matches in this filter. Switch tabs or change league.'}
            </div>
          )}
          {filtered.map(m => (
            <MatchCard
              key={m.prediction.match_id}
              prediction={m.prediction}
              bets={m.bets}
              consensus={m.consensus}
              modelView={m.modelView}
              league={league}
              flashed={flashedMatchId === m.prediction.match_id}
              onLogPaper={logPaperBet}
              onLogReal={logRealBet}
              inPlay={m.inPlay}
            />
          ))}
        </div>
      )}
    </div>
  )
}
