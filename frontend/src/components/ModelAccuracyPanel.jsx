import { ShieldCheck, ShieldAlert, BarChart3 } from 'lucide-react'

function readinessTone(readiness) {
  if (!readiness) return 'default'
  if (readiness.startsWith('READY')) return 'good'
  if (readiness.startsWith('ADJUST') || readiness.startsWith('NEGATIVE')) return 'bad'
  return 'warn'
}

export default function ModelAccuracyPanel({ accuracy, backtest }) {
  const live = accuracy || {}
  const bt = backtest || {}
  const tone = readinessTone(live.readiness)
  const toneCls = {
    good: 'text-good bg-good-soft border-good/30',
    warn: 'text-warn bg-warn-soft border-warn/30',
    bad:  'text-bad bg-bad-soft border-bad/30',
    default: 'text-slate-300 bg-ink-800 border-ink-700',
  }[tone]

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 h-full">
      <div className="flex items-center gap-2 mb-3">
        <BarChart3 size={16} className="text-accent" />
        <div className="text-sm font-medium">Model accuracy</div>
      </div>

      <div className={`rounded-lg border px-3 py-2 mb-3 text-xs ${toneCls}`}>
        <div className="flex items-center gap-1.5 font-semibold">
          {tone === 'good' ? <ShieldCheck size={14} /> : <ShieldAlert size={14} />}
          {live.readiness || 'No data yet'}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-y-2 text-xs">
        <dt className="text-slate-400">Settled predictions</dt>
        <dd className="text-right tabular-nums">{live.n_predictions ?? 0}</dd>

        <dt className="text-slate-400">Live win rate</dt>
        <dd className="text-right tabular-nums">{live.win_rate != null ? `${(live.win_rate * 100).toFixed(1)}%` : '—'}</dd>

        <dt className="text-slate-400">Live Brier</dt>
        <dd className="text-right tabular-nums">{live.avg_brier ?? '—'}</dd>

        <dt className="text-slate-400">CLV samples / avg</dt>
        <dd className="text-right tabular-nums">
          {live.n_clv_samples ?? 0}
          {live.avg_clv != null && (
            <> · {live.avg_clv >= 0 ? '+' : ''}{Number(live.avg_clv).toFixed(2)}</>
          )}
        </dd>
      </dl>

      {bt.available !== false && bt.n > 0 && (
        <div className="mt-4 pt-3 border-t border-ink-700/60">
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1.5">Backtest (EPL 2023-24, R37-38)</div>
          <dl className="grid grid-cols-2 gap-y-1 text-xs">
            <dt className="text-slate-400">n</dt>
            <dd className="text-right tabular-nums">{bt.n}</dd>
            <dt className="text-slate-400">Winner accuracy</dt>
            <dd className="text-right tabular-nums">{(bt.winner_accuracy * 100).toFixed(2)}%</dd>
            <dt className="text-slate-400">Avg Brier</dt>
            <dd className="text-right tabular-nums">{bt.avg_brier}</dd>
          </dl>
        </div>
      )}
    </div>
  )
}
