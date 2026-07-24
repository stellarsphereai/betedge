import { useEffect, useState } from 'react'
import { BarChart, Bar, ResponsiveContainer, XAxis, YAxis, Tooltip, ReferenceLine, CartesianGrid } from 'recharts'

function fmtMoney(n) {
  if (n == null) return '—'
  return `${n >= 0 ? '+' : ''}$${Math.abs(n).toFixed(2)}`
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
  if (!data || !data.monthly?.length) return <div className="text-slate-500 text-sm p-4">No performance data yet — bets will appear once the season starts.</div>

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

      {/* Win tally — only show when there are bets this month with open or recent activity */}
      {current && current.bets >= 5 && (current.open > 0 || current.won + current.lost >= 5) && (
        <div className="bg-ink-800 border border-ink-700 rounded p-3 mb-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-300 font-medium">
              {current.month} Win Tally
            </span>
            <span className="text-xs text-slate-400">
              Target: {fmtMoney(current.target_pnl)} profit
            </span>
          </div>
          <div className="flex items-center gap-4">
            <div className="flex-1">
              <div className="flex justify-between text-xs text-slate-400 mb-1">
                <span>{current.won}W – {current.lost}L ({current.bets} bets)</span>
                <span>
                  {current.win_rate != null ? `${(current.win_rate * 100).toFixed(0)}% win rate` : '—'}
                </span>
              </div>
              {/* Progress bar */}
              <div className="w-full bg-ink-700 rounded-full h-3 overflow-hidden">
                <div
                  className="h-3 rounded-full transition-all"
                  style={{
                    width: `${Math.min(100, Math.max(0, (current.pnl / current.target_pnl) * 100))}%`,
                    backgroundColor: BENCHMARK_COLORS[current.benchmark],
                  }}
                />
              </div>
              <div className="flex justify-between text-[11px] mt-1">
                <span className={current.pnl >= 0 ? 'text-good' : 'text-red-400'}>
                  {fmtMoney(current.pnl)}
                </span>
                <span className="text-slate-500">
                  {current.wins_to_target > 0
                    ? `${current.wins_to_target} more win${current.wins_to_target > 1 ? 's' : ''} needed`
                    : 'Target reached!'
                  }
                </span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Monthly chart */}
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData} margin={{ top: 5, right: 10, bottom: 5, left: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
            <XAxis dataKey="month" tick={{ fill: '#94a3b8', fontSize: 11 }} />
            <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} tickFormatter={v => `$${v}`} />
            <Tooltip
              contentStyle={{ background: '#1e293b', border: '1px solid #475569', borderRadius: 8 }}
              labelStyle={{ color: '#e2e8f0' }}
              formatter={(v, name) => [name === 'target' ? `$${v.toFixed(0)}` : `$${v.toFixed(2)}`, name === 'target' ? '5% Target' : 'Actual P&L']}
            />
            <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
            <Bar dataKey="target" fill="#3b82f640" radius={[2, 2, 0, 0]} name="target" />
            <Bar dataKey="pnl" radius={[2, 2, 0, 0]} name="pnl">
              {chartData.map((entry, i) => (
                <rect key={i} fill={entry.fill} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Monthly table */}
      <table className="w-full text-xs mt-3">
        <thead>
          <tr className="text-slate-400 border-b border-ink-700">
            <th className="text-left py-1">Month</th>
            <th className="text-right">W-L</th>
            <th className="text-right">Win%</th>
            <th className="text-right">P&L</th>
            <th className="text-right">Return</th>
            <th className="text-right">Target</th>
            <th className="text-right">To Target</th>
            <th className="text-left pl-3">Status</th>
          </tr>
        </thead>
        <tbody>
          {data.monthly.map(m => (
            <tr key={m.month} className="border-b border-ink-800 hover:bg-ink-800/50">
              <td className="py-1.5 text-slate-300">{m.month}</td>
              <td className="text-right text-slate-300">{m.won}–{m.lost}</td>
              <td className="text-right text-slate-300">{m.win_rate != null ? `${(m.win_rate * 100).toFixed(0)}%` : '—'}</td>
              <td className={`text-right font-mono ${m.pnl >= 0 ? 'text-good' : 'text-red-400'}`}>{fmtMoney(m.pnl)}</td>
              <td className={`text-right ${m.monthly_return_pct >= 0.05 ? 'text-good' : m.monthly_return_pct >= 0 ? 'text-amber-400' : 'text-red-400'}`}>
                {(m.monthly_return_pct * 100).toFixed(1)}%
              </td>
              <td className="text-right text-slate-500">{fmtMoney(m.target_pnl)}</td>
              <td className="text-right text-slate-400">
                {m.wins_to_target > 0 ? `${m.wins_to_target} wins` : '—'}
              </td>
              <td className="pl-3">
                <span className="px-1.5 py-0.5 rounded text-[10px] font-medium"
                      style={{ color: BENCHMARK_COLORS[m.benchmark], backgroundColor: BENCHMARK_COLORS[m.benchmark] + '20' }}>
                  {BENCHMARK_LABELS[m.benchmark]}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
