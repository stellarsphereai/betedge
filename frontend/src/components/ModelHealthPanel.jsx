import { TrendingUp, TrendingDown, Activity, AlertTriangle, CheckCircle } from 'lucide-react'

/**
 * Reads /model-health and shows:
 *   - Rolling accuracy (last 10 / 20 / 50)
 *   - Avg Brier with delta vs the 0.4024 backtest baseline
 *   - Status: green (on track) / amber (monitor) / red (review needed)
 *   - Active bias alerts with one-line descriptions + suggested adjustments
 *   - Last self-evaluation timestamp + next-evaluation hint
 *
 * Brier is "lower is better", so a positive delta = regression. Color tone
 * comes from the backend.
 */
export default function ModelHealthPanel({ health }) {
  if (!health) return null

  const { rolling, baseline, delta_brier, status, color, alerts, last_eval, next_eval } = health
  const last10 = rolling?.last_10
  const last20 = rolling?.last_20
  const last50 = rolling?.last_50

  const statusInfo = STATUS_MAP[status] ?? STATUS_MAP.no_data
  const Icon = statusInfo.icon
  const tone = TONE_BY_COLOR[color] ?? TONE_BY_COLOR.neutral

  return (
    <div className={`rounded-xl border p-4 ${tone.surface}`}>
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-slate-400 mb-1">Model Health</div>
          <div className={`flex items-center gap-1.5 text-sm font-semibold ${tone.text}`}>
            <Icon size={14} /> {statusInfo.label}
          </div>
        </div>
        {next_eval && (
          <div className="text-[10px] text-slate-500 text-right">
            Next eval<br />
            <span className="text-slate-400 tabular-nums">{fmtTs(next_eval)}</span>
          </div>
        )}
      </div>

      {!last10 && (
        <div className="text-[11px] text-slate-400 italic">
          No settled predictions yet. The 23:55 NY self-eval will populate this once matches start finishing.
        </div>
      )}

      {last10 && (
        <div className="grid grid-cols-3 gap-2 text-[11px] mb-3">
          <Card label="Last 10" stat={last10} />
          <Card label="Last 20" stat={last20} />
          <Card label="Last 50" stat={last50} />
        </div>
      )}

      {last20 && baseline?.avg_brier != null && (
        <div className="text-[11px] text-slate-400 mb-3 flex items-center gap-2">
          <span>Avg Brier (last 20):</span>
          <span className={`font-semibold tabular-nums ${tone.text}`}>{last20.avg_brier.toFixed(4)}</span>
          <span className="text-slate-500">vs baseline {baseline.avg_brier.toFixed(4)}</span>
          {delta_brier != null && (
            <span className={`tabular-nums flex items-center gap-0.5 ${delta_brier > 0 ? 'text-bad' : 'text-good'}`}>
              {delta_brier > 0 ? <TrendingUp size={10} /> : <TrendingDown size={10} />}
              {delta_brier > 0 ? '+' : ''}{delta_brier.toFixed(4)}
            </span>
          )}
        </div>
      )}

      <div className="border-t border-ink-700/60 pt-2.5">
        <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1.5">
          Bias Alerts {alerts?.length > 0 && <span className="ml-1 text-warn">({alerts.length})</span>}
        </div>
        {(!alerts || alerts.length === 0) ? (
          <div className="flex items-center gap-1.5 text-[11px] text-good">
            <CheckCircle size={12} /> No bias detected
          </div>
        ) : (
          <div className="space-y-1.5">
            {alerts.map(a => (
              <div key={a.id} className="text-[11px] border border-warn/30 bg-warn/10 rounded px-2 py-1.5">
                <div className="flex items-center gap-1.5 text-warn font-medium">
                  <AlertTriangle size={11} /> {humanizeCheck(a.check_name)}
                  {a.league_key && <span className="text-slate-500 font-normal">· {a.league_key.toUpperCase()}</span>}
                </div>
                <div className="text-slate-300 mt-0.5">{a.description}</div>
                {a.suggested_adjustment && (
                  <div className="text-[10px] text-slate-500 italic mt-0.5">→ {a.suggested_adjustment}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {last_eval && (
        <div className="text-[10px] text-slate-500 mt-3">
          Last eval: <span className="tabular-nums">{fmtTs(last_eval)}</span>
        </div>
      )}
    </div>
  )
}

function Card({ label, stat }) {
  if (!stat) {
    return (
      <div className="bg-ink-800/60 border border-ink-700 rounded px-2 py-1.5">
        <div className="text-slate-500">{label}</div>
        <div className="text-slate-600 tabular-nums">—</div>
      </div>
    )
  }
  return (
    <div className="bg-ink-800/60 border border-ink-700 rounded px-2 py-1.5">
      <div className="text-slate-500">{label}</div>
      <div className="text-slate-200 tabular-nums">
        {stat.correct}/{stat.n} <span className="text-slate-500">({(stat.winner_accuracy * 100).toFixed(0)}%)</span>
      </div>
    </div>
  )
}

function fmtTs(iso) {
  if (!iso) return '—'
  const d = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z')
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function humanizeCheck(name) {
  const map = {
    home_bias: 'Home bias',
    favorite_overconfidence: 'Favorite overconfidence',
    form_recency_bias: 'Form recency bias',
    edge_materialization: 'Edges not materializing',
    xg_accuracy: 'xG accuracy',
  }
  return map[name] || name
}

const STATUS_MAP = {
  on_track: { label: 'On track', icon: CheckCircle },
  monitor:  { label: 'Monitor — slight regression', icon: Activity },
  review:   { label: 'Review needed', icon: AlertTriangle },
  no_data:  { label: 'Warming up', icon: Activity },
}

const TONE_BY_COLOR = {
  green:   { surface: 'bg-good/5 border-good/40', text: 'text-good' },
  amber:   { surface: 'bg-warn/5 border-warn/40', text: 'text-warn' },
  red:     { surface: 'bg-bad/5 border-bad/40',   text: 'text-bad' },
  neutral: { surface: 'bg-ink-900 border-ink-700', text: 'text-slate-300' },
}
