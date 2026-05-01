import { BarChart, Bar, ResponsiveContainer, XAxis, YAxis, Tooltip, ReferenceLine, Cell } from 'recharts'

function fmtCLV(n) {
  if (n == null || Number.isNaN(n)) return '—'
  const v = Math.round(Number(n) * 100) / 100
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

export default function CLVChart({ data }) {
  const rows = (data || []).map((d, i) => ({
    idx: i + 1,
    label: d.label || `Bet #${i + 1}`,
    clv: Math.round((d.clv ?? 0) * 100) / 100,
    won: d.won,
  }))

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 h-full">
      <div className="flex justify-between items-center mb-2">
        <div className="text-sm font-medium">CLV per bet</div>
        <div className="text-xs text-slate-500">{rows.length} settled w/ closing line</div>
      </div>
      <div className="h-44">
        {rows.length === 0 ? (
          <div className="h-full flex items-center justify-center text-xs text-slate-500 text-center px-4">
            No CLV samples yet. Closing lines populate after kickoff —
            POST <code className="bg-ink-800 px-1 rounded">/bets/capture-closing-sweep</code> or enable the scheduler.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows}>
              <XAxis dataKey="idx" tick={{ fill: '#8a93b8', fontSize: 10 }} />
              <YAxis tick={{ fill: '#8a93b8', fontSize: 10 }} />
              <Tooltip
                contentStyle={{ background: '#141a31', border: '1px solid #232a4a' }}
                labelStyle={{ color: '#8a93b8' }}
                formatter={(value) => [fmtCLV(value), 'CLV']}
                labelFormatter={(_idx, payload) => payload?.[0]?.payload?.label ?? ''}
              />
              <ReferenceLine y={0} stroke="#8a93b8" />
              <Bar dataKey="clv">
                {rows.map((r, i) => (
                  <Cell key={i} fill={r.clv >= 0 ? '#25c26a' : '#ff6b6b'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
