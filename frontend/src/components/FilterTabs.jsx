const TABS = [
  { id: 'all', label: 'All matches' },
  { id: 'ev', label: '+EV only' },
  { id: 'high', label: 'High confidence' },
  { id: 'log', label: 'Trade log' },
  { id: 'portfolio', label: 'Portfolio' },
  { id: 'anomalies', label: 'Anomalies' },
]

export default function FilterTabs({ active, onChange, counts = {} }) {
  return (
    <div className="flex gap-1 mb-4 border-b border-ink-700">
      {TABS.map(t => {
        // Anomaly count badge: orange if any flags today, red if any are
        // bet-excluding (PHANTOM_EDGE-class). Other tabs use a quiet count.
        const isAnomalyTab = t.id === 'anomalies'
        const n = counts[t.id]
        const tone = isAnomalyTab && counts.anomalies_excluding > 0
          ? 'bg-bad-soft text-bad'
          : isAnomalyTab && n > 0
          ? 'bg-warn-soft text-warn'
          : 'text-slate-500'
        return (
          <button
            key={t.id}
            onClick={() => onChange(t.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              active === t.id
                ? 'border-accent text-white'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            {t.label}
            {n != null && (
              <span className={`ml-2 text-xs tabular-nums px-1.5 py-0.5 rounded ${tone}`}>{n}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}
