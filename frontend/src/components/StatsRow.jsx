import { TrendingUp, TrendingDown, Target, DollarSign, Activity, Wallet } from 'lucide-react'

function fmtMoney(n) {
  if (n == null || Number.isNaN(n)) return '—'
  return `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtPct(n, digits = 1) {
  if (n == null || Number.isNaN(n)) return '—'
  return `${(n * 100).toFixed(digits)}%`
}

function Card({ icon: Icon, label, value, sub, tone = 'default', onClick }) {
  const subTone = {
    good: 'text-good',
    bad: 'text-bad',
    warn: 'text-warn',
    default: 'text-slate-400',
  }[tone]
  const interactive = typeof onClick === 'function'
  const Tag = interactive ? 'button' : 'div'
  return (
    <Tag
      onClick={onClick}
      className={`bg-ink-900 border border-ink-700 rounded-xl p-4 text-left w-full ${
        interactive ? 'hover:border-accent transition-colors cursor-pointer' : ''
      }`}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs uppercase tracking-wider text-slate-400">{label}</span>
        <Icon size={16} className="text-slate-500" />
      </div>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      {sub && <div className={`text-xs mt-1 ${subTone}`}>{sub}</div>}
    </Tag>
  )
}

function describeBet(b) {
  if (!b) return null
  const m = (b.market || 'h2h').toLowerCase()
  const t = (b.outcome || '').toLowerCase()
  if (m === 'h2h') {
    const team = t === 'home' ? b.home_team : t === 'away' ? b.away_team : 'Draw'
    return `${team} @ ${b.best_book ?? b.book}`
  }
  if (m === 'btts') return `BTTS ${t.charAt(0).toUpperCase() + t.slice(1)} @ ${b.best_book ?? b.book}`
  if (m === 'totals') return `${t.charAt(0).toUpperCase() + t.slice(1)} ${b.market_line} @ ${b.best_book ?? b.book}`
  return `${t} @ ${b.best_book ?? b.book}`
}

export default function StatsRow({ ev, stats, bestEdgeBet, onJumpToBest }) {
  // Count unique opportunities visible in the current window (one row per
  // match/market/outcome). Falls back to raw count if not provided.
  const todays = bestEdgeBet ? undefined : (ev?.bets?.length ?? 0)
  const bestEdge = bestEdgeBet?.edge ?? 0
  const bankroll = stats?.bankroll ?? 0
  const weekly = stats?.weekly ?? {}
  const accuracy = stats?.accuracy ?? {}
  const clv = weekly.avg_clv
  const roi = weekly.roi

  const bankrollSub = roi != null
    ? `Week ${roi >= 0 ? '+' : ''}${(roi * 100).toFixed(2)}% (${weekly.total_bets ?? 0} bets)`
    : 'no settled bets this week'

  const clvIcon = clv == null ? Activity : clv >= 0 ? TrendingUp : TrendingDown
  const clvTone = clv == null ? 'default' : clv >= 0 ? 'good' : 'bad'

  // Real-money rollup card — only renders when there's at least one settled
  // cash bet, so the dashboard isn't padded with a "—" card during the
  // paper-only phase. Once cash bets exist it bumps the grid to 5 columns.
  const rm = stats?.real_money
  const hasReal = rm && (rm.settled > 0 || rm.open > 0)
  const realPnl = rm?.realized_pnl ?? 0
  const realPct = rm?.realized_pct
  const realTone = !hasReal ? 'default' : realPnl > 0 ? 'good' : realPnl < 0 ? 'bad' : 'default'
  const realValue = hasReal
    ? `${realPnl >= 0 ? '+' : ''}${fmtMoney(realPnl)}${realPct != null ? ` (${realPct >= 0 ? '+' : ''}${(realPct*100).toFixed(1)}%)` : ''}`
    : '—'
  const realSub = hasReal
    ? `${rm.settled} settled · ${rm.won}–${rm.lost}${rm.open ? ` · ${rm.open} open` : ''} · books $${(rm.bankroll_total||0).toFixed(0)}`
    : 'no cash bets yet'

  const cols = hasReal ? 'lg:grid-cols-5' : 'lg:grid-cols-4'
  return (
    <div className={`grid grid-cols-2 ${cols} gap-3 mb-6`}>
      <Card icon={Target} label="Today's bets"
            value={ev?.bets?.length ?? 0}
            sub={ev?.match_count != null ? `${ev.match_count} matches scanned` : null} />
      <Card icon={TrendingUp} label="Best edge"
            value={fmtPct(bestEdge, 2)}
            tone={bestEdge >= 0.03 ? 'good' : bestEdge > 0 ? 'warn' : 'default'}
            sub={describeBet(bestEdgeBet) ?? 'no +EV in current window'}
            onClick={bestEdgeBet ? onJumpToBest : undefined} />
      <Card icon={DollarSign} label="Bankroll" value={fmtMoney(bankroll)} sub={bankrollSub}
            tone={roi != null && roi >= 0 ? 'good' : roi != null ? 'bad' : 'default'} />
      <Card icon={clvIcon} label="CLV avg"
            value={clv == null ? '—' : (clv >= 0 ? '+' : '') + Number(clv).toFixed(2)}
            tone={clvTone}
            sub={accuracy.n_predictions > 0 ? `${(accuracy.win_rate * 100).toFixed(1)}% acc · ${accuracy.n_predictions} preds` : 'awaiting predictions'} />
      {hasReal && (
        <Card icon={Wallet} label="Real money P&L"
              value={realValue}
              tone={realTone}
              sub={realSub} />
      )}
    </div>
  )
}
