import { AlertTriangle, AlertOctagon, Info } from 'lucide-react'

/**
 * Lists today's anomaly_log rows. Color-coded:
 *   - red (border-bad)  : PHANTOM_EDGE — bet excluded entirely
 *   - orange (warn)     : EDGE_HIGH, SHARP_DIVERGE — confidence downgraded / warning
 *   - slate (neutral)   : PENALTY_STACK, FORM_DIVERGE — informational
 *
 * Each row shows the anomaly_type, the match, model vs book probabilities
 * (when present), and the full description so the user can review and
 * decide whether to override the exclusion.
 */
export default function AnomaliesPanel({ anomalies }) {
  const rows = anomalies?.anomalies ?? []

  if (rows.length === 0) {
    return (
      <div className="bg-ink-900 border border-ink-700 rounded-xl p-5">
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <Info size={16} className="text-good" />
          No anomalies detected today. The model and books are in alignment.
        </div>
      </div>
    )
  }

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4">
      <div className="text-xs uppercase tracking-wider text-slate-400 mb-3">
        Anomalies — {rows.length} flag{rows.length !== 1 ? 's' : ''} today
      </div>
      <div className="space-y-2">
        {rows.map(a => {
          const severity = severityFor(a.anomaly_type)
          const Icon = severity.icon
          return (
            <div key={a.id} className={`border rounded-lg px-3 py-2.5 ${severity.classes}`}>
              <div className="flex items-start gap-2">
                <Icon size={14} className={`mt-0.5 ${severity.iconClass}`} />
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
                    <span className={`font-semibold ${severity.labelClass}`}>{a.anomaly_type}</span>
                    {a.home_team && a.away_team && (
                      <span className="text-slate-300">{a.home_team} vs {a.away_team}</span>
                    )}
                    <span className="text-[10px] text-slate-500 ml-auto tabular-nums">
                      {fmtTime(a.created_at)}
                    </span>
                  </div>
                  <div className="text-[11px] text-slate-300 mt-1 leading-snug">
                    {a.description}
                  </div>
                  {(a.model_prob != null || a.book_implied != null || a.edge_shown != null) && (
                    <div className="text-[10px] text-slate-400 mt-1 flex flex-wrap gap-x-3 tabular-nums">
                      {a.model_prob != null && <span>Model: {(a.model_prob * 100).toFixed(0)}%</span>}
                      {a.book_implied != null && <span>Book implies: {(a.book_implied * 100).toFixed(0)}%</span>}
                      {a.edge_shown != null && <span>Edge shown: {(a.edge_shown * 100).toFixed(1)}%</span>}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function severityFor(type) {
  switch (type) {
    case 'PHANTOM_EDGE':
      return {
        icon: AlertOctagon,
        classes: 'bg-bad/10 border-bad/40',
        iconClass: 'text-bad',
        labelClass: 'text-bad',
      }
    case 'EDGE_HIGH':
    case 'SHARP_DIVERGE':
      return {
        icon: AlertTriangle,
        classes: 'bg-warn/10 border-warn/40',
        iconClass: 'text-warn',
        labelClass: 'text-warn',
      }
    default:
      return {
        icon: Info,
        classes: 'bg-ink-800 border-ink-700',
        iconClass: 'text-slate-400',
        labelClass: 'text-slate-200',
      }
  }
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso.replace(' ', 'T') + 'Z')
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
