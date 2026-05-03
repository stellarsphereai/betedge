import { useEffect, useState } from 'react'
import { api } from '../api'

const LEAGUE_FILTERS = [
  { id: 'all',       label: 'All leagues' },
  { id: 'epl',       label: 'EPL' },
  { id: 'ucl',       label: 'UCL' },
  { id: 'uel',       label: 'EL' },
  { id: 'world_cup', label: 'World Cup' },
]

// Tailwind's default palette is available because tailwind.config.js uses
// `extend`. Soft + text + border tones for each league badge.
const LEAGUE_BADGE = {
  epl:        { label: 'EPL',       cls: 'bg-purple-500/20 text-purple-300 border-purple-500/40' },
  ucl:        { label: 'UCL',       cls: 'bg-blue-500/20 text-blue-300 border-blue-500/40' },
  uel:        { label: 'EL',        cls: 'bg-orange-500/20 text-orange-300 border-orange-500/40' },
  world_cup:  { label: 'World Cup', cls: 'bg-good-soft text-good border-good-soft' },
  default:    { label: '—',         cls: 'bg-ink-800 text-slate-400 border-ink-700' },
}

const PODIUM = [
  { medal: '🥇', label: 'BEST BET',    accent: 'border-yellow-500/40 bg-yellow-500/5' },
  { medal: '🥈', label: 'SECOND BEST', accent: 'border-slate-400/30 bg-slate-400/5' },
  { medal: '🥉', label: 'THIRD BEST',  accent: 'border-amber-700/30 bg-amber-700/5' },
]

function fmtKickoff(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const now = new Date()
    const sameDay = d.toDateString() === now.toDateString()
    const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1)
    const isTomorrow = d.toDateString() === tomorrow.toDateString()
    const date = sameDay ? 'Today'
                : isTomorrow ? 'Tomorrow'
                : d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
    const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
    return `${date}, ${time}`
  } catch { return iso }
}

function fmtPct(n, { signed = false } = {}) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  const x = Number(n) * 100
  const sign = signed && x > 0 ? '+' : ''
  return `${sign}${x.toFixed(1)}%`
}
function fmtMoney(n) {
  if (n == null) return '—'
  return `$${Number(n).toFixed(0)}`
}
function decimalToAmerican(d) {
  const x = Number(d)
  if (!Number.isFinite(x) || x <= 1) return '—'
  if (x >= 2) return `+${Math.round((x - 1) * 100)}`
  return `${Math.round(-100 / (x - 1))}`
}
function betLabel(b) {
  const t = b.outcome
  const market = b.market || 'h2h'
  if (market === 'h2h') {
    if (t === 'home') return `${b.home_team} (home win)`
    if (t === 'away') return `${b.away_team} (away win)`
    return 'Draw'
  }
  if (market === 'btts') return `BTTS ${t?.charAt(0).toUpperCase() + t?.slice(1)}`
  if (market === 'totals') return `${t?.charAt(0).toUpperCase() + t?.slice(1)} ${b.market_line}`
  return t
}

function CoverageBar({ covered, required, total = 7 }) {
  const filled = Math.min(covered, total)
  const empty = total - filled
  return (
    <span className="font-mono text-[10px] tabular-nums">
      {covered}/{total} books{' '}
      <span className={covered >= required ? 'text-good' : 'text-warn'}>
        {'█'.repeat(filled)}
      </span>
      <span className="text-slate-700">{'░'.repeat(empty)}</span>
      <span className="text-slate-500">  (need {required})</span>
    </span>
  )
}

function MonitoringRow({ bet }) {
  const lg = LEAGUE_BADGE[bet.league] || LEAGUE_BADGE.default
  const odds = bet.best_odds || bet.decimal_odds
  const bookmaker = bet.best_book || bet.book
  return (
    <div className="grid grid-cols-12 gap-2 items-center bg-ink-900 border border-ink-700 rounded-lg px-3 py-2 text-xs">
      <span className={`col-span-1 text-[10px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded border text-center ${lg.cls}`}>
        {lg.label}
      </span>
      <div className="col-span-4">
        <div className="text-slate-200">{bet.home_team} <span className="text-slate-500">vs</span> {bet.away_team}</div>
        <div className="text-[10px] text-slate-500">
          {bet.market || 'h2h'} {bet.outcome}{bet.market_line ? ` ${bet.market_line}` : ''} · {bookmaker} {Number(odds).toFixed(2)}
        </div>
      </div>
      <div className="col-span-2 text-right tabular-nums text-good">
        {fmtPct(bet.edge, { signed: true })} edge
      </div>
      <div className="col-span-5">
        <CoverageBar
          covered={bet.book_coverage ?? 0}
          required={bet.min_book_coverage ?? 4}
        />
      </div>
    </div>
  )
}

function BetCard({ rank, bet, onClick }) {
  const cfg = PODIUM[rank]
  const lg = LEAGUE_BADGE[bet.league] || LEAGUE_BADGE.default
  const odds = bet.best_odds || bet.decimal_odds
  const bookmaker = bet.best_book || bet.book
  const stake = bet.stake || 0
  const stakeReduced = bet.stake_reduced_low_balance
  return (
    <div
      onClick={onClick}
      className={`relative rounded-xl border-2 p-4 cursor-pointer transition hover:border-accent ${cfg.accent}`}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-2xl">{cfg.medal}</span>
          <span className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">{cfg.label}</span>
        </div>
        <span className={`text-[10px] uppercase tracking-wider font-semibold px-2 py-0.5 rounded border ${lg.cls}`}>
          {lg.label}
        </span>
      </div>

      <div className="mb-2">
        <div className="text-sm font-semibold text-slate-100 leading-tight">{bet.home_team}</div>
        <div className="text-[11px] text-slate-500">vs {bet.away_team}</div>
        {bet.commence_time && (
          <div className="text-[10px] text-slate-400 mt-1" title={bet.commence_time}>
            ⏱ {fmtKickoff(bet.commence_time)}
          </div>
        )}
      </div>

      <div className="border-t border-ink-800 pt-2 space-y-1">
        <div className="text-xs text-slate-200 font-medium">{betLabel(bet)}</div>
        <div className="flex items-baseline justify-between text-[11px]">
          <span className="text-slate-500">{bookmaker}</span>
          <span className="tabular-nums text-slate-200">{Number(odds).toFixed(2)} <span className="text-slate-500">({decimalToAmerican(odds)})</span></span>
        </div>
        <div className="grid grid-cols-3 gap-1 pt-1.5 text-[10px]">
          <div>
            <div className="text-slate-500 uppercase">Edge</div>
            <div className="text-good font-semibold tabular-nums">{fmtPct(bet.edge, { signed: true })}</div>
          </div>
          <div>
            <div className="text-slate-500 uppercase">Stake</div>
            <div className="text-slate-200 tabular-nums">
              {fmtMoney(stake)}
              {stakeReduced && (
                <span title={`Reduced — top up ${bet.top_up_book}`} className="ml-1 text-warn">⚠</span>
              )}
            </div>
          </div>
          <div>
            <div className="text-slate-500 uppercase">Conf</div>
            <div className="text-slate-200">{bet.confidence || '—'}</div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function BestBetsGrid({ refreshKey, onJumpToMatch, headerLeague }) {
  // Initial filter follows the header's league switcher, but the user can
  // override via the pills below to widen to 'all' or narrow to a different
  // league without touching the rest of the dashboard.
  const [filter, setFilter] = useState(headerLeague || 'all')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // When the header league changes, sync the grid filter so the hero
  // section reflects the league the rest of the page is showing.
  useEffect(() => {
    if (headerLeague) setFilter(headerLeague)
  }, [headerLeague])

  useEffect(() => {
    let active = true
    setLoading(true); setError(null)
    api.bestBets({ league: filter, limit: 3 })
      .then(r => { if (active) setData(r) })
      .catch(e => { if (active) setError(e.message || String(e)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [filter, refreshKey])

  const bets = data?.bets || []
  const inTop = data?.leagues_in_top || []
  const leagueLabels = inTop.map(k => LEAGUE_BADGE[k]?.label || k.toUpperCase())

  return (
    <div className="mb-6">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-200">Top 3 Best Bets</h2>
          <div className="text-[11px] text-slate-500">
            {loading ? 'Loading…' : data
              ? `${data.count_returned} of ${data.count_considered} qualifying bets${leagueLabels.length ? ` · ${leagueLabels.join(' + ')}` : ''}`
              : '—'}
          </div>
        </div>
        <div className="bg-ink-800 border border-ink-700 rounded-full p-0.5 flex gap-0.5">
          {LEAGUE_FILTERS.map(f => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`px-2.5 py-1 rounded-full text-[11px] font-medium ${
                filter === f.id ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'
              }`}
            >{f.label}</button>
          ))}
        </div>
      </div>

      {error && <div className="text-xs text-bad mb-2">Best bets unavailable — {error}</div>}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {[0, 1, 2].map(i => {
          const bet = bets[i]
          if (!bet) {
            return (
              <div key={i} className={`relative rounded-xl border border-dashed border-ink-700 p-4 bg-ink-900/40`}>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-2xl opacity-30">{PODIUM[i].medal}</span>
                  <span className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{PODIUM[i].label}</span>
                </div>
                <div className="text-xs text-slate-500 mt-2">No bet at this slot under current filter.</div>
              </div>
            )
          }
          return (
            <BetCard
              key={`${bet.match_id}-${bet.outcome}-${bet.market}-${bet.market_line}`}
              rank={i}
              bet={bet}
              onClick={() => onJumpToMatch?.(bet.match_id)}
            />
          )
        })}
      </div>

      {data?.monitoring?.length > 0 && (
        <div className="mt-4">
          <div className="flex items-center gap-2 mb-2">
            <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              👀 Monitoring — waiting for more books
            </h3>
            <span className="text-[10px] text-slate-500">
              {data.monitoring.length} bet{data.monitoring.length === 1 ? '' : 's'} below coverage min — promotes automatically as more books price the line
            </span>
          </div>
          <div className="space-y-1.5">
            {data.monitoring.map(b => (
              <MonitoringRow
                key={`${b.match_id}-${b.outcome}-${b.market}-${b.market_line}`}
                bet={b}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
