import { useEffect, useState } from 'react'

function fmtMoney(n) {
  if (n == null) return '—'
  return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`
}

const BENCHMARK_COLORS = {
  OVER_PERFORMING: '#22c55e',
  AT_PAR: '#3b82f6',
  UNDER_PERFORMING: '#f59e0b',
  NEGATIVE: '#ef4444',
}

const BENCHMARK_LABELS = {
  OVER_PERFORMING: 'Over Performing',
  AT_PAR: 'At Par',
  UNDER_PERFORMING: 'Under Performing',
  NEGATIVE: 'Negative',
}

const LEAGUE_NAMES = {
  epl: 'EPL', la_liga: 'La Liga', ucl: 'UCL', uel: 'EL', world_cup: 'World Cup',
}

function MonthRow({ m }) {
  const [expanded, setExpanded] = useState(false)
  const leagues = m.by_league ? Object.keys(m.by_league).sort() : []
  const hasLeagues = leagues.length > 0

  return (
    <>
      <tr
        className={`border-b border-ink-800 hover:bg-ink-800/50 ${hasLeagues ? 'cursor-pointer' : ''}`}
        onClick={() => hasLeagues && setExpanded(e => !e)}
      >
        <td className="py-1.5 text-slate-300">
          {hasLeagues && <span className="text-slate-500 mr-1">{expanded ? '▾' : '▸'}</span>}
          {m.month}
        </td>
        <td className="text-right text-slate-300">{m.won}–{m.lost}</td>
        <td className="text-right text-slate-300">{m.win_rate != null ? `${(m.win_rate * 100).toFixed(0)}%` : '—'}</td>
        <td className={`text-right font-mono ${m.pnl >= 0 ? 'text-good' : 'text-red-400'}`}>{fmtMoney(m.pnl)}</td>
        <td className="text-right text-good font-mono">{m.avg_win_profit ? `+$${m.avg_win_profit.toFixed(0)}` : '—'}</td>
        <td className="text-right text-red-400 font-mono">{m.avg_loss ? `-$${Math.abs(m.avg_loss).toFixed(0)}` : '—'}</td>
        <td className={`text-right ${m.monthly_return_pct >= 0.05 ? 'text-good' : m.monthly_return_pct >= 0 ? 'text-amber-400' : 'text-red-400'}`}>
          {(m.monthly_return_pct * 100).toFixed(1)}%
        </td>
        <td className="text-right text-slate-500">{fmtMoney(m.target_pnl)}</td>
        <td className="text-right text-slate-400">
          {m.wins_to_target > 0 ? `${m.wins_to_target} wins × $${m.avg_win_profit?.toFixed(0) || '?'}` : '—'}
        </td>
        <td className="pl-3">
          <span className="px-1.5 py-0.5 rounded text-[10px] font-medium"
                style={{ color: BENCHMARK_COLORS[m.benchmark], backgroundColor: BENCHMARK_COLORS[m.benchmark] + '20' }}>
            {BENCHMARK_LABELS[m.benchmark]}
          </span>
        </td>
      </tr>
      {expanded && leagues.map(lg => {
        const d = m.by_league[lg]
        const totalBets = m.bets || 1
        const leagueShare = d.bets / totalBets
        const leagueTarget = (m.target_pnl || 0) * leagueShare
        const remaining = leagueTarget - d.pnl
        const winsNeeded = (remaining > 0 && d.avg_win_profit > 0)
          ? Math.ceil(remaining / d.avg_win_profit)
          : 0
        return (
          <tr key={`${m.month}-${lg}`} className="border-b border-ink-800/50 bg-ink-800/30">
            <td className="py-1 pl-6 text-slate-400 text-[11px]">{LEAGUE_NAMES[lg] || lg}</td>
            <td className="text-right text-slate-400 text-[11px]">{d.won}–{d.lost}</td>
            <td className="text-right text-slate-400 text-[11px]">{d.win_rate != null ? `${(d.win_rate * 100).toFixed(0)}%` : '—'}</td>
            <td className={`text-right font-mono text-[11px] ${d.pnl >= 0 ? 'text-good' : 'text-red-400'}`}>{fmtMoney(d.pnl)}</td>
            <td className="text-right text-good font-mono text-[11px]">{d.avg_win_profit ? `+$${d.avg_win_profit.toFixed(0)}` : '—'}</td>
            <td className="text-right text-red-400 font-mono text-[11px]">{d.avg_loss ? `-$${Math.abs(d.avg_loss).toFixed(0)}` : '—'}</td>
            <td className="text-right text-slate-500 text-[11px]"></td>
            <td className="text-right text-slate-500 text-[11px]">{fmtMoney(leagueTarget)}</td>
            <td className="text-right text-slate-400 text-[11px]">
              {winsNeeded > 0 ? `${winsNeeded} wins × $${d.avg_win_profit?.toFixed(0) || '?'}` : '—'}
            </td>
            <td className="text-[11px]"></td>
          </tr>
        )
      })}
    </>
  )
}

function ProjectionRow({ m }) {
  const [expanded, setExpanded] = useState(false)
  const leagues = m.by_league ? Object.keys(m.by_league).sort() : []

  return (
    <>
      <tr
        className="border-b border-ink-800 hover:bg-ink-800/50 cursor-pointer opacity-70"
        onClick={() => setExpanded(e => !e)}
      >
        <td className="py-1.5 text-slate-400">
          <span className="text-slate-500 mr-1">{expanded ? '▾' : '▸'}</span>
          {m.month}
        </td>
        <td className="text-right text-slate-500">~{m.bets} bets</td>
        <td className="text-right text-slate-500">—</td>
        <td className="text-right text-slate-500">—</td>
        <td className="text-right text-slate-500 font-mono">~${m.avg_win_profit?.toFixed(0)}</td>
        <td className="text-right text-slate-500 font-mono">~${Math.abs(m.avg_loss || 0).toFixed(0)}</td>
        <td className="text-right text-slate-500">5.0%</td>
        <td className="text-right text-blue-400 font-mono">{fmtMoney(m.target_pnl)}</td>
        <td className="text-right text-slate-400">
          {m.wins_to_target} wins × ${m.avg_win_profit?.toFixed(0)}
        </td>
        <td className="pl-3">
          <span className="px-1.5 py-0.5 rounded text-[10px] font-medium text-blue-400 bg-blue-400/10">
            Projected
          </span>
        </td>
      </tr>
      {expanded && leagues.map(lg => {
        const d = m.by_league[lg]
        return (
          <tr key={`${m.month}-${lg}`} className="border-b border-ink-800/50 bg-ink-800/20 opacity-70">
            <td className="py-1 pl-6 text-slate-500 text-[11px]">{LEAGUE_NAMES[lg] || lg}</td>
            <td className="text-right text-slate-500 text-[11px]">~{d.bets} bets</td>
            <td className="text-right text-slate-500 text-[11px]">—</td>
            <td className="text-right text-slate-500 text-[11px]">—</td>
            <td className="text-right text-slate-500 font-mono text-[11px]">~${d.avg_win_profit?.toFixed(0)}</td>
            <td className="text-right text-slate-500 font-mono text-[11px]">~${Math.abs(d.avg_loss || 0).toFixed(0)}</td>
            <td className="text-right text-slate-500 text-[11px]"></td>
            <td className="text-right text-blue-400/70 text-[11px]">{fmtMoney(d.target)}</td>
            <td className="text-right text-slate-500 text-[11px]">
              {d.wins_to_target > 0 ? `${d.wins_to_target} wins × $${d.avg_win_profit?.toFixed(0)}` : '—'}
            </td>
            <td className="text-[11px]"></td>
          </tr>
        )
      })}
    </>
  )
}

export default function PerformanceBenchmark({ league }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    const qs = league ? `?league=${league}` : ''
    fetch(`/performance${qs}`)
      .then(r => r.json())
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [league])

  if (loading) return <div className="text-slate-500 text-sm p-4">Loading performance data...</div>
  if (!data || (!data.monthly?.length && !data.projection?.length)) return <div className="text-slate-500 text-sm p-4">No performance data yet — bets will appear once the season starts.</div>

  const current = data.current_month
  const chartData = data.monthly.map(m => ({
    month: m.month.slice(2), // "26-08" → "26-08"
    pnl: m.pnl,
    target: m.target_pnl,
    return_pct: m.monthly_return_pct * 100,
    benchmark: m.benchmark,
    fill: BENCHMARK_COLORS[m.benchmark] || '#64748b',
  }))

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-lg p-4 mt-4">
      <h3 className="text-sm font-semibold text-slate-200 mb-3">
        Performance Benchmark {league ? `(${league.toUpperCase().replace('_', ' ')})` : '(All Leagues)'}
      </h3>

      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
        <div className="bg-ink-800 border border-ink-700 rounded p-3">
          <div className="text-[11px] text-slate-400 uppercase tracking-wider">Cumulative P&L</div>
          <div className={`text-lg font-bold ${data.cumulative_pnl >= 0 ? 'text-good' : 'text-red-400'}`}>
            {fmtMoney(data.cumulative_pnl)}
          </div>
          <div className="text-[11px] text-slate-500">
            {(data.cumulative_return_pct * 100).toFixed(1)}% return
          </div>
        </div>
        <div className="bg-ink-800 border border-ink-700 rounded p-3">
          <div className="text-[11px] text-slate-400 uppercase tracking-wider">Current Bankroll</div>
          <div className="text-lg font-bold text-slate-200">${data.current_bankroll?.toFixed(0)}</div>
          <div className="text-[11px] text-slate-500">from ${data.initial_bankroll?.toFixed(0)}</div>
        </div>
        <div className="bg-ink-800 border border-ink-700 rounded p-3">
          <div className="text-[11px] text-slate-400 uppercase tracking-wider">Target</div>
          <div className="text-lg font-bold text-blue-400">{(data.target_monthly_pct * 100).toFixed(0)}% / month</div>
          <div className="text-[11px] text-slate-500">compounded</div>
        </div>
        {current && (
          <div className="bg-ink-800 border border-ink-700 rounded p-3">
            <div className="text-[11px] text-slate-400 uppercase tracking-wider">This Month</div>
            <div className={`text-lg font-bold`} style={{ color: BENCHMARK_COLORS[current.benchmark] }}>
              {BENCHMARK_LABELS[current.benchmark]}
            </div>
            <div className="text-[11px] text-slate-500">
              {(current.monthly_return_pct * 100).toFixed(1)}% return
            </div>
          </div>
        )}
      </div>


      {/* Monthly table */}
      <table className="w-full text-xs mt-3">
        <thead>
          <tr className="text-slate-400 border-b border-ink-700">
            <th className="text-left py-1">Month</th>
            <th className="text-right">W-L</th>
            <th className="text-right">Win%</th>
            <th className="text-right">P&L</th>
            <th className="text-right">Avg Win</th>
            <th className="text-right">Avg Loss</th>
            <th className="text-right">Return</th>
            <th className="text-right">Target</th>
            <th className="text-right">To Target</th>
            <th className="text-left pl-3">Status</th>
          </tr>
        </thead>
        <tbody>
          {data.monthly.map(m => (
            <MonthRow key={m.month} m={m} />
          ))}
          {data.projection?.length > 0 && (
            <tr className="border-b border-ink-700">
              <td colSpan={10} className="py-2 text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
                Season Projection (5% compounded)
              </td>
            </tr>
          )}
          {data.projection?.map(m => (
            <ProjectionRow key={m.month} m={m} />
          ))}
        </tbody>
        {data.season_end_bankroll && (
          <tfoot className="bg-ink-800/60 font-semibold">
            <tr className="border-t border-ink-700">
              <td className="px-1 py-1.5 text-slate-200">Season End</td>
              <td colSpan={2}></td>
              <td className="text-right font-mono text-good">
                {fmtMoney(data.season_end_bankroll - data.initial_bankroll)}
              </td>
              <td colSpan={2}></td>
              <td className="text-right text-good">
                {(data.season_total_return * 100).toFixed(1)}%
              </td>
              <td className="text-right text-slate-300">${data.season_end_bankroll?.toFixed(0)}</td>
              <td colSpan={2}></td>
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  )
}
