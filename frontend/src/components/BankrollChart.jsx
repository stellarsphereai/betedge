import { useState } from 'react'
import { LineChart, Line, ResponsiveContainer, XAxis, YAxis, Tooltip, ReferenceLine } from 'recharts'

const RANGES = [
  { id: 'all', label: 'All time', days: null },
  { id: 'month', label: 'Month', days: 30 },
  { id: 'week', label: 'Week', days: 7 },
]

export default function BankrollChart({ data, startingBankroll }) {
  const [range, setRange] = useState('all')
  const cfg = RANGES.find(r => r.id === range)
  const cutoff = cfg.days ? Date.now() - cfg.days * 86400_000 : 0
  const filtered = (data || []).filter(d => d.t === 'start' || new Date(d.t).getTime() >= cutoff)
  const showStart = data?.length || 1

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 h-full">
      <div className="flex justify-between items-center mb-2">
        <div>
          <div className="text-sm font-medium">Bankroll</div>
          <div className="text-xs text-slate-500">{showStart - 1} settled bets</div>
        </div>
        <div className="bg-ink-800 border border-ink-700 rounded-full p-0.5 flex">
          {RANGES.map(r => (
            <button
              key={r.id}
              onClick={() => setRange(r.id)}
              className={`px-2.5 py-0.5 rounded-full text-[10px] font-medium ${
                range === r.id ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      <div className="h-44">
        {filtered.length < 2 ? (
          <div className="h-full flex items-center justify-center text-xs text-slate-500">
            No settled bets yet — chart populates as bets are placed and resolved.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={filtered}>
              <XAxis dataKey="t" tick={{ fill: '#8a93b8', fontSize: 10 }} hide />
              <YAxis tick={{ fill: '#8a93b8', fontSize: 10 }} domain={['auto', 'auto']} />
              <Tooltip contentStyle={{ background: '#141a31', border: '1px solid #232a4a' }} labelStyle={{ color: '#8a93b8' }} />
              <ReferenceLine y={startingBankroll} stroke="#8a93b8" strokeDasharray="3 3" />
              <Line type="monotone" dataKey="bankroll" stroke="#5b8cff" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
