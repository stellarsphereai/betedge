import { Activity, AlertTriangle, RefreshCw, Sun, Moon } from 'lucide-react'

const WINDOW_OPTIONS = [
  { value: 24,    label: '24h' },
  { value: 72,    label: '3 days' },
  { value: 7*24,  label: '7 days' },
  { value: 14*24, label: '14 days' },
  { value: 30*24, label: '30 days' },
]

export default function Header({ league, onLeagueChange, windowHours, onWindowChange,
                                 lastFetched, loading, error, ageS, sportsbookCount,
                                 cacheAgeS, onRefresh,
                                 anomalyCount = 0, anomalyExcluding = 0, onJumpToAnomalies,
                                 theme, onToggleTheme }) {
  const status = loading ? 'Refreshing…' : error ? `Error: ${error}` : lastFetched
    ? `Updated ${lastFetched.toLocaleTimeString()}` : 'Idle'
  const cacheAgeMin = cacheAgeS != null ? Math.round(cacheAgeS / 60) : null

  const LEAGUES = [
    { id: 'epl', label: 'EPL' },
    { id: 'ucl', label: 'UCL' },
    { id: 'uel', label: 'UEL' },
    { id: 'world_cup', label: 'World Cup' },
    { id: 'la_liga', label: 'La Liga' },
  ]

  // Only the World Cup is the live-recommendation league per spec; everything
  // else is paper-trade until the model is validated against real outcomes.
  const isPaper = league !== 'world_cup'

  return (
    <header className="flex flex-wrap items-center justify-between gap-4 mb-6">
      <div className="flex items-center gap-3">
        <div className="bg-accent text-ink-950 font-bold rounded-lg px-3 py-1.5 text-lg tracking-tight">BetEdge NY</div>
        <div className="text-xs text-slate-400 hidden md:flex items-center gap-1.5">
          <Activity size={14} className={loading ? 'animate-pulse text-accent' : 'text-good'} />
          Scanning {sportsbookCount} books · {status}
          {ageS != null && <span className="text-slate-500">· data {ageS}s old</span>}
          {cacheAgeMin != null && cacheAgeMin > 0 && (
            <span className="text-slate-500">· odds cached {cacheAgeMin}m</span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 flex-wrap justify-end">
        {anomalyCount > 0 && (
          <button
            onClick={onJumpToAnomalies}
            title="Click to open the Anomalies tab"
            className={`flex items-center gap-1.5 text-xs font-medium rounded-md px-2.5 py-1 ${
              anomalyExcluding > 0
                ? 'bg-bad-soft text-bad border border-bad/40'
                : 'bg-warn-soft text-warn border border-warn/40'
            }`}
          >
            <AlertTriangle size={12} />
            {anomalyCount} anomal{anomalyCount === 1 ? 'y' : 'ies'} today
          </button>
        )}
        {isPaper && (
          <span className="text-[10px] font-bold tracking-widest bg-warn-soft text-warn px-2.5 py-1 rounded-md">
            PAPER TRADE
          </span>
        )}
        <button
          onClick={onRefresh}
          disabled={loading}
          title="Fetch fresh odds from the Odds API (bypasses the 30-minute cache). Use this when you're about to place a bet."
          className="flex items-center gap-1.5 bg-ink-900 border border-ink-700 hover:border-accent text-slate-200 rounded-full px-3 py-1.5 text-xs font-medium disabled:opacity-50"
        >
          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          Refresh odds
        </button>
        <button
          onClick={onToggleTheme}
          title={theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme'}
          aria-label="Toggle theme"
          className="flex items-center justify-center bg-ink-900 border border-ink-700 hover:border-accent text-slate-200 rounded-full w-8 h-8"
        >
          {theme === 'light' ? <Moon size={14} /> : <Sun size={14} />}
        </button>
        <select
          value={windowHours}
          onChange={(e) => onWindowChange?.(Number(e.target.value))}
          title="Lookahead window — only matches kicking off within this range are shown"
          className="bg-ink-900 border border-ink-700 text-slate-200 rounded-full px-3 py-1.5 text-xs hover:border-accent focus:border-accent focus:outline-none"
        >
          {WINDOW_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>Next {o.label}</option>
          ))}
        </select>
        <div className="bg-ink-900 border border-ink-700 rounded-full p-1 flex">
          {LEAGUES.map(t => (
            <button
              key={t.id}
              onClick={() => onLeagueChange(t.id)}
              className={`px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
                league === t.id ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
    </header>
  )
}
