import { useState } from 'react'
import { api } from '../api'

function fmtMoney(n) { return n == null ? '—' : `$${Number(n).toFixed(2)}` }
function fmtTime(iso) { try { return new Date(iso).toLocaleString() } catch { return iso } }
function fmtPct(n) { return (n == null || Number.isNaN(n)) ? '—' : `${(Number(n) * 100).toFixed(1)}%` }
function decimalToAmerican(d) {
  if (d == null || Number.isNaN(Number(d))) return '—'
  const x = Number(d)
  if (x <= 1) return '—'
  if (x >= 2) return `+${Math.round((x - 1) * 100)}`
  return `${Math.round(-100 / (x - 1))}`
}

function betLabel(b) {
  const t = b.bet_type
  const market = b.market || 'h2h'
  if (market === 'h2h') {
    if (t === 'home') return `${b.home_team} (home)`
    if (t === 'away') return `${b.away_team} (away)`
    return 'Draw'
  }
  if (market === 'btts') return `BTTS ${t.charAt(0).toUpperCase() + t.slice(1)}`
  if (market === 'totals') return `${t.charAt(0).toUpperCase() + t.slice(1)} ${b.market_line}`
  return t
}

function fixtureOutcome(b) {
  const result = b.fixture_result
  const hg = b.fixture_home_goals
  const ag = b.fixture_away_goals
  if (!result) return null
  const score = (hg != null && ag != null) ? `${hg}-${ag}` : null
  const label = result === 'home' ? `${b.home_team}` : result === 'away' ? `${b.away_team}` : 'Draw'
  return score ? `${label}  ${score}` : label
}

const STATUS_STYLES = {
  open: 'text-slate-300',
  won:  'text-good',
  lost: 'text-bad',
}

function MarkResultControl({ bet, onMark }) {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [home, setHome] = useState('')
  const [away, setAway] = useState('')

  if (bet.status !== 'open') {
    return (
      <span className={`uppercase text-[10px] font-semibold ${STATUS_STYLES[bet.status] || ''}`}>
        {bet.status}
      </span>
    )
  }

  if (!expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        className="text-[10px] uppercase font-semibold tracking-wider bg-ink-800 border border-ink-700 hover:border-accent text-slate-200 px-2 py-1 rounded"
      >
        Mark result
      </button>
    )
  }

  const homeGoals = home === '' ? null : Number(home)
  const awayGoals = away === '' ? null : Number(away)
  const validScore =
    homeGoals !== null && awayGoals !== null &&
    Number.isInteger(homeGoals) && Number.isInteger(awayGoals) &&
    homeGoals >= 0 && awayGoals >= 0

  async function submit() {
    if (busy || !validScore) return
    setBusy(true)
    try {
      await onMark(bet, { home_goals: homeGoals, away_goals: awayGoals })
    } finally {
      setBusy(false); setExpanded(false); setHome(''); setAway('')
    }
  }

  return (
    <div className="inline-flex items-center gap-1">
      <input
        type="number" min="0" inputMode="numeric"
        placeholder={bet.home_team?.[0] ?? 'H'}
        value={home}
        onChange={e => setHome(e.target.value)}
        onKeyDown={e => e.key === 'Enter' && submit()}
        disabled={busy}
        className="w-9 h-6 text-center text-[11px] tabular-nums bg-ink-800 border border-ink-700 rounded text-slate-200 focus:border-accent focus:outline-none"
      />
      <span className="text-slate-500 text-[10px]">–</span>
      <input
        type="number" min="0" inputMode="numeric"
        placeholder={bet.away_team?.[0] ?? 'A'}
        value={away}
        onChange={e => setAway(e.target.value)}
        onKeyDown={e => e.key === 'Enter' && submit()}
        disabled={busy}
        className="w-9 h-6 text-center text-[11px] tabular-nums bg-ink-800 border border-ink-700 rounded text-slate-200 focus:border-accent focus:outline-none"
      />
      <button
        onClick={submit}
        disabled={busy || !validScore}
        title={validScore ? 'Settle all open bets on this fixture' : 'Enter both scores'}
        className="text-[10px] font-bold uppercase w-7 h-6 rounded bg-good text-ink-950 hover:opacity-90 disabled:opacity-30"
      >
        {busy ? '…' : 'OK'}
      </button>
      <button
        onClick={() => setExpanded(false)}
        disabled={busy}
        className="text-[10px] text-slate-500 hover:text-slate-300"
      >
        ×
      </button>
    </div>
  )
}

function RemoveControl({ bet, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  async function doDelete() {
    setBusy(true); setErr(null)
    try {
      await api.deleteBet(bet.id)
      onDeleted?.(bet.id)
    } catch (e) {
      setErr(e.message || String(e))
      setBusy(false)
    }
  }

  if (err) {
    return <span className="text-bad text-[10px]" title={err}>error</span>
  }
  if (!confirming) {
    return (
      <button
        onClick={() => setConfirming(true)}
        title="Remove this bet — moves it back to the +EV grid"
        className="px-1.5 py-0.5 text-[12px] leading-none text-slate-500 hover:text-bad rounded hover:bg-bad-soft"
      >
        ✕
      </button>
    )
  }
  return (
    <div className="inline-flex gap-1">
      <button
        onClick={doDelete}
        disabled={busy}
        className="px-2 py-0.5 text-[10px] font-medium rounded bg-bad text-white hover:opacity-90 disabled:opacity-50"
      >
        {busy ? '…' : 'Remove'}
      </button>
      <button
        onClick={() => setConfirming(false)}
        disabled={busy}
        className="px-2 py-0.5 text-[10px] rounded bg-ink-800 text-slate-300 hover:bg-ink-700 border border-ink-700"
      >
        Cancel
      </button>
    </div>
  )
}

export default function PaperTradeLog({ bets, onMarkResult, onDeleteBet }) {
  const rows = (bets || []).filter(b => b.is_paper)

  if (!rows.length) {
    return (
      <div className="bg-ink-900 border border-dashed border-ink-700 rounded-xl p-8 text-center text-slate-400 text-sm">
        No paper bets logged yet. Click "Log paper bet" on a +EV match card to add one.
      </div>
    )
  }

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl overflow-hidden">
      <table className="w-full text-xs">
        <thead className="bg-ink-800 text-[10px] uppercase tracking-wider text-slate-400">
          <tr>
            <th className="text-left py-2.5 px-3">Match</th>
            <th className="text-left">Bet</th>
            <th className="text-left">Book</th>
            <th className="text-right">Odds</th>
            <th className="text-right">Closing</th>
            <th className="text-right" title="Implied probability at placement (1/odds)">Place %</th>
            <th className="text-right" title="Implied probability at close (1/closing)">Close %</th>
            <th className="text-right" title="Model's predicted probability for this outcome">Model %</th>
            <th className="text-right">CLV</th>
            <th className="text-right">Stake</th>
            <th className="text-right">P/L</th>
            <th className="text-left">Match outcome</th>
            <th className="text-center">Status</th>
            <th className="text-left">When</th>
            <th className="text-center"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map(b => {
            const outcome = fixtureOutcome(b)
            const modelVsPlace = (b.model_prob != null && b.placement_implied_prob != null)
              ? b.model_prob - b.placement_implied_prob : null
            const placeAmer = decimalToAmerican(b.odds_at_placement)
            const closeAmer = b.closing_odds == null ? '—' : decimalToAmerican(b.closing_odds)
            return (
              <tr key={b.id} className="border-t border-ink-700/60">
                <td className="px-3 py-2 font-medium">{b.home_team} vs {b.away_team}</td>
                <td>{betLabel(b)}</td>
                <td>{b.book}</td>
                <td className="text-right tabular-nums" title={b.odds_at_placement?.toFixed(2)}>{placeAmer}</td>
                <td className="text-right tabular-nums" title={b.closing_odds?.toFixed(2)}>{closeAmer}</td>
                <td className="text-right tabular-nums">{fmtPct(b.placement_implied_prob)}</td>
                <td className="text-right tabular-nums">{fmtPct(b.closing_implied_prob)}</td>
                <td className={`text-right tabular-nums ${
                  modelVsPlace == null ? '' : modelVsPlace > 0 ? 'text-good' : 'text-slate-400'
                }`}>{fmtPct(b.model_prob)}</td>
                <td className={`text-right tabular-nums ${b.clv == null ? '' : b.clv >= 0 ? 'text-good' : 'text-bad'}`}>
                  {b.clv == null ? '—' : (b.clv >= 0 ? '+' : '') + Number(b.clv).toFixed(2)}
                </td>
                <td className="text-right tabular-nums">{fmtMoney(b.stake)}</td>
                <td className={`text-right tabular-nums ${b.profit == null ? '' : b.profit >= 0 ? 'text-good' : 'text-bad'}`}>
                  {b.profit == null ? '—' : fmtMoney(b.profit)}
                </td>
                <td className={outcome ? 'text-slate-200' : 'text-slate-500'}>
                  {outcome ?? '—'}
                </td>
                <td className="text-center">
                  <MarkResultControl bet={b} onMark={onMarkResult} />
                </td>
                <td className="text-slate-400">{fmtTime(b.timestamp)}</td>
                <td className="text-center px-2">
                  <RemoveControl bet={b} onDeleted={onDeleteBet} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
