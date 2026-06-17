import { useEffect, useState } from 'react'
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
  const [autoBusy, setAutoBusy] = useState(false)
  const [autoMessage, setAutoMessage] = useState(null)  // { tone: 'info' | 'bad', text }
  const [home, setHome] = useState('')
  const [away, setAway] = useState('')

  // Auto-clear info messages after 6s; keep error messages until next attempt.
  useEffect(() => {
    if (!autoMessage || autoMessage.tone === 'bad') return
    const t = setTimeout(() => setAutoMessage(null), 6000)
    return () => clearTimeout(t)
  }, [autoMessage])

  if (bet.status !== 'open') {
    return (
      <span className={`uppercase text-[10px] font-semibold ${STATUS_STYLES[bet.status] || ''}`}>
        {bet.status}
      </span>
    )
  }

  async function fetchResult() {
    if (autoBusy || busy) return
    setAutoBusy(true); setAutoMessage(null)
    try {
      const r = await api.autoMarkResult(bet.id)
      // The endpoint settles every open bet on this match. The list will
      // refresh via onMark's loadAll, so just show a brief confirmation.
      setAutoMessage({
        tone: 'info',
        text: `Settled ${r.home_goals}–${r.away_goals}`,
      })
      // Trigger the parent's reload by reusing onMark with the fetched score.
      // The endpoint already wrote the settlement; this call hits mark-result
      // again with the same goals, which is a no-op for non-open bets but
      // refreshes the local bets list. Cleaner alternative: expose a separate
      // refresh callback. For now this is the smallest change.
      try { await onMark(bet, { home_goals: r.home_goals, away_goals: r.away_goals }) } catch {}
    } catch (e) {
      // 409 = match still going on — that's the explicit message we want.
      // Other errors fall through with whatever the server said.
      setAutoMessage({ tone: 'bad', text: e.message || String(e) })
    } finally {
      setAutoBusy(false)
    }
  }

  if (!expanded) {
    return (
      <div className="inline-flex flex-col items-stretch gap-1">
        <div className="inline-flex gap-1">
          <button
            onClick={fetchResult}
            disabled={autoBusy}
            title="Fetch the final score from API-Football and settle all open bets on this match"
            className="text-[10px] uppercase font-semibold tracking-wider bg-accent-soft border border-accent/40 text-accent hover:bg-accent hover:text-white px-2 py-1 rounded disabled:opacity-50"
          >
            {autoBusy ? '…' : 'Fetch result'}
          </button>
          <button
            onClick={() => setExpanded(true)}
            disabled={autoBusy}
            title="Enter the score manually"
            className="text-[10px] uppercase font-semibold tracking-wider bg-ink-800 border border-ink-700 hover:border-slate-500 text-slate-300 px-2 py-1 rounded"
          >
            Manual
          </button>
        </div>
        {autoMessage && (
          <div
            className={`text-[10px] px-1 ${
              autoMessage.tone === 'bad' ? 'text-warn' : 'text-good'
            }`}
            title={autoMessage.text}
          >
            {autoMessage.text}
          </div>
        )}
      </div>
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

function EditableStake({ bet, onUpdated }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)

  function startEdit() {
    setValue(bet.stake?.toFixed(2) || '')
    setEditing(true)
  }

  async function save() {
    const num = parseFloat(value)
    if (!num || num <= 0 || busy) return
    setBusy(true)
    try {
      const r = await api.updateBetStake(bet.id, num)
      onUpdated?.(bet.id, r)
      setEditing(false)
    } catch { /* noop */ }
    finally { setBusy(false) }
  }

  if (!editing) {
    return (
      <span
        onClick={startEdit}
        className="cursor-pointer hover:text-accent border-b border-dashed border-transparent hover:border-accent"
        title="Click to edit stake"
      >
        {fmtMoney(bet.stake)}
      </span>
    )
  }

  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="text-slate-500">$</span>
      <input
        type="number" min="1" step="0.01"
        value={value}
        onChange={e => setValue(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEditing(false) }}
        autoFocus
        disabled={busy}
        className="w-14 h-5 text-right text-[11px] tabular-nums bg-ink-800 border border-accent/40 rounded px-1 text-slate-200 focus:border-accent focus:outline-none"
      />
      <button onClick={save} disabled={busy} className="text-[10px] font-bold text-good hover:opacity-80">✓</button>
      <button onClick={() => setEditing(false)} className="text-[10px] text-slate-500 hover:text-slate-300">✕</button>
    </span>
  )
}

function ActionsCell({ bet, onDeleted, onModeChanged }) {
  const [stage, setStage] = useState('idle')   // 'idle' | 'confirm-cancel' | 'busy'
  const [err, setErr] = useState(null)

  const isOpen = bet.status === 'open'
  const targetIsPaper = !bet.is_paper
  const targetLabel = targetIsPaper ? 'Paper' : 'Cash'

  async function doCancel() {
    setStage('busy'); setErr(null)
    try {
      await api.deleteBet(bet.id)
      onDeleted?.(bet.id)
    } catch (e) {
      setErr(e.message || String(e))
      setStage('idle')
    }
  }

  async function doSwitch() {
    setStage('busy'); setErr(null)
    try {
      const r = await api.setBetPaper(bet.id, targetIsPaper)
      onModeChanged?.(bet.id, !!r.is_paper)
      setStage('idle')
    } catch (e) {
      setErr(e.message || String(e))
      setStage('idle')
    }
  }

  if (err) {
    return <span className="text-bad text-[10px]" title={err}>error</span>
  }

  if (stage === 'busy') {
    return <span className="text-[10px] text-slate-500">…</span>
  }

  if (stage === 'confirm-cancel') {
    return (
      <div className="inline-flex gap-1">
        <button
          onClick={doCancel}
          className="px-2 py-0.5 text-[10px] font-medium rounded bg-bad text-white hover:opacity-90"
        >
          Cancel bet
        </button>
        <button
          onClick={() => setStage('idle')}
          className="px-2 py-0.5 text-[10px] rounded bg-ink-800 text-slate-300 hover:bg-ink-700 border border-ink-700"
        >
          Keep
        </button>
      </div>
    )
  }

  // Switch button color matches its DESTINATION mode (where the bet would
  // move) so the visual identity is consistent with the rest of the UI:
  // → Cash uses warn/amber, → Paper uses accent/blue.
  const switchClass = targetIsPaper
    ? 'bg-accent-soft border-accent/40 text-accent hover:bg-accent hover:text-white'
    : 'bg-warn-soft border-warn/40 text-warn hover:bg-warn hover:text-ink-950'

  return (
    <div className="inline-flex gap-1 whitespace-nowrap">
      {isOpen && (
        <button
          onClick={doSwitch}
          title={`Move this bet to ${targetLabel} trade`}
          className={`px-2 py-1 text-[10px] font-semibold rounded border ${switchClass}`}
        >
          → {targetLabel}
        </button>
      )}
      <button
        onClick={() => setStage('confirm-cancel')}
        title={isOpen
          ? 'Cancel this bet — removes it from the log and reopens it on the +EV grid'
          : 'Cancel this bet — removes it from the log and from portfolio totals'}
        className="px-2 py-1 text-[10px] font-semibold rounded border bg-bad-soft border-bad/40 text-bad hover:bg-bad hover:text-white"
      >
        Cancel
      </button>
    </div>
  )
}

export default function PaperTradeLog({ bets, onMarkResult, onDeleteBet, onModeChangeBet, onStakeUpdated }) {
  const [mode, setMode] = useState('cash')  // 'paper' | 'cash'
  const [leagueFilter, setLeagueFilter] = useState('')  // '' = all
  const all = bets || []
  const paperRows = all.filter(b => b.is_paper)
  const cashRows = all.filter(b => !b.is_paper)
  const modeRows = mode === 'paper' ? paperRows : cashRows
  const rows = leagueFilter
    ? modeRows.filter(b => (b.league || b.match_league || '') === leagueFilter)
    : modeRows
  // Collect available leagues from current mode's bets for the dropdown
  const availableLeagues = [...new Set(modeRows.map(b => b.league || b.match_league).filter(Boolean))].sort()

  const ModeTabs = (
    <div className="flex justify-between items-center mb-3">
      <div className="bg-ink-800 border border-ink-700 rounded-full p-0.5 flex">
        <button
          onClick={() => setMode('paper')}
          className={`px-3 py-1 rounded-full text-xs font-medium ${
            mode === 'paper' ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Paper trade <span className="ml-1 opacity-60">({leagueFilter ? paperRows.filter(b => (b.league || b.match_league) === leagueFilter).length : paperRows.length})</span>
        </button>
        <button
          onClick={() => setMode('cash')}
          className={`px-3 py-1 rounded-full text-xs font-medium ${
            mode === 'cash' ? 'bg-warn text-ink-950' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Cash trade <span className="ml-1 opacity-60">({leagueFilter ? cashRows.filter(b => (b.league || b.match_league) === leagueFilter).length : cashRows.length})</span>
        </button>
      </div>
      {availableLeagues.length > 1 && (
        <select
          value={leagueFilter}
          onChange={e => setLeagueFilter(e.target.value)}
          className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-xs text-slate-200"
        >
          <option value="">All tournaments</option>
          {availableLeagues.map(lg => (
            <option key={lg} value={lg}>
              {lg === 'epl' ? 'EPL' : lg === 'ucl' ? 'UCL' : lg === 'uel' ? 'Europa League' : lg === 'world_cup' ? 'World Cup' : lg}
            </option>
          ))}
        </select>
      )}
      <div className="text-[11px] text-slate-500 ml-auto">
        {mode === 'cash'
          ? 'Real-money bets — settling these moves your book balances.'
          : 'Simulated bets — book balances are not affected.'}
      </div>
    </div>
  )

  if (!rows.length) {
    return (
      <div>
        {ModeTabs}
        <div className="bg-ink-900 border border-dashed border-ink-700 rounded-xl p-8 text-center text-slate-400 text-sm">
          {leagueFilter
            ? `No ${mode === 'paper' ? 'paper' : 'cash'} bets for this tournament.`
            : mode === 'paper'
            ? 'No paper bets logged yet. Click the + on a +EV match card to add one.'
            : 'No cash trades logged yet. Click the $ on a +EV match card and confirm to add one.'}
        </div>
      </div>
    )
  }

  return (
    <div>
      {ModeTabs}
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
            <th className="text-center pr-3">Actions</th>
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
                <td className="text-right tabular-nums"><EditableStake bet={b} onUpdated={onStakeUpdated} /></td>
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
                <td className="text-center px-2 whitespace-nowrap">
                  <ActionsCell bet={b} onDeleted={onDeleteBet} onModeChanged={onModeChangeBet} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
    </div>
  )
}
