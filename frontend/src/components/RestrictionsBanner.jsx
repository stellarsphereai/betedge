import { Lock, Unlock, AlertTriangle } from 'lucide-react'

// Specs A-D — single banner showing the four cash-money guardrails:
//   • restricted markets (BTTS / Totals / H2H Draw)
//   • min cash edge (6%)
//   • daily loss circuit-breaker (-$50)
//   • paper-first requirement
// Plus an unlock-progress strip for goal-market paper trades that lifts
// the BTTS/Totals restriction once the model proves itself on paper.
export default function RestrictionsBanner({ restrictions }) {
  if (!restrictions) return null

  const r = restrictions
  const gm = r.goal_market_progress || {}
  const wr  = gm.win_rate
  const wrPct = wr != null ? `${(wr * 100).toFixed(0)}%` : '—'
  const target = gm.target_settled || 20
  const progress = Math.min(100, ((gm.settled || 0) / target) * 100)
  const capHit = !!r.daily_cap_hit

  return (
    <div className={`mb-4 rounded-lg border p-3 ${
      capHit
        ? 'border-bad/50 bg-bad-soft'
        : 'border-warn/40 bg-warn-soft'
    }`}>
      {capHit && (
        <div className="flex items-center gap-2 mb-2 text-bad font-bold tracking-wide uppercase text-xs">
          <AlertTriangle size={14} />
          Daily loss limit reached — ${(r.daily_loss_cap_usd ?? 50).toFixed(0)}.
          Cash betting locked until tomorrow. Paper still available.
          <span className="text-bad/70 normal-case ml-2 font-normal">
            (today: ${r.todays_cash_pnl?.toFixed(2)})
          </span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
        <div className="flex items-center gap-1.5 text-warn font-semibold">
          <Lock size={12} /> Cash restrictions active
        </div>
        <div>
          <span className="text-slate-500 mr-1.5">Goal markets:</span>
          <span className="font-mono text-bad">🔒 paper only</span>
        </div>
        <div>
          <span className="text-slate-500 mr-1.5">H2H Draw:</span>
          <span className="font-mono text-bad">🔒 paper only</span>
        </div>
        <div>
          <span className="text-slate-500 mr-1.5">H2H home/away:</span>
          <span className="font-mono text-good">✅ cash allowed</span>
        </div>
        <div>
          <span className="text-slate-500 mr-1.5">Min cash edge:</span>
          <span className="font-mono">{((r.min_cash_edge ?? 0.06) * 100).toFixed(0)}%</span>
        </div>
        <div>
          <span className="text-slate-500 mr-1.5">Daily loss cap:</span>
          <span className="font-mono">${(r.daily_loss_cap_usd ?? 50).toFixed(0)}</span>
        </div>
        <div>
          <span className="text-slate-500 mr-1.5">Paper-first required:</span>
          <span className="font-mono text-good">on every cash bet</span>
        </div>
      </div>

      {/* Unlock progress for the goal-markets gate */}
      <div className="mt-3 pt-3 border-t border-warn/30">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
            Goal-market unlock progress
          </span>
          <span className="text-[10px] text-slate-500">
            unlocks at {Math.round((gm.target_win_rate || 0.5) * 100)}% win rate
            over {target}+ paper bets, with positive avg CLV
          </span>
        </div>
        <div className="h-2 bg-ink-800 rounded-full overflow-hidden border border-ink-700/60">
          <div
            className={`h-full rounded-full transition-all ${
              gm.unlocked ? 'bg-good' : 'bg-warn'
            }`}
            style={{ width: `${progress}%` }}
          />
        </div>
        <div className="flex flex-wrap gap-x-4 mt-1.5 text-[11px] text-slate-300">
          <span>
            Sample: <span className="font-mono">{gm.settled || 0}/{target}</span>
            <span className="text-slate-500 ml-1">{gm.sample_ok ? '✓' : ''}</span>
          </span>
          <span>
            Win rate: <span className="font-mono">{wrPct}</span>
            <span className="text-slate-500 ml-1">{gm.win_rate_ok ? '✓' : ''}</span>
          </span>
          <span>
            Avg CLV: <span className="font-mono">{gm.avg_clv != null ? (gm.avg_clv >= 0 ? '+' : '') + gm.avg_clv.toFixed(3) : '—'}</span>
            <span className="text-slate-500 ml-1">{gm.clv_ok ? '✓' : ''}</span>
          </span>
          <span className="ml-auto">
            {gm.unlocked
              ? <span className="text-good font-semibold inline-flex items-center gap-1"><Unlock size={11} /> UNLOCKED</span>
              : <span className="text-warn font-semibold inline-flex items-center gap-1"><Lock size={11} /> still locked</span>
            }
          </span>
        </div>
      </div>
    </div>
  )
}
