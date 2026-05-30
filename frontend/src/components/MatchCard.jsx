import { useEffect, useState } from 'react'
import { ExternalLink, Plus, Check, Loader2, ChevronDown, ChevronRight, Sparkles, DollarSign, X } from 'lucide-react'
import ProbabilityBar from './ProbabilityBar'
import MatchAnalysisPanel from './MatchAnalysisPanel'

// Per-match "I've placed my bets for this game" flag, persisted in
// localStorage. Keyed by match_id so it survives reloads but stays local
// to the browser — no server round-trip needed for a personal workflow toggle.
const DONE_LS_PREFIX = 'betedge_match_done_'

const TIMING_STYLES = {
  GREEN: 'bg-good-soft text-good',
  AMBER: 'bg-warn-soft text-warn',
  RED:   'bg-bad-soft text-bad',
}

const CONF_STYLES = {
  HIGH:   'bg-good-soft text-good',
  MEDIUM: 'bg-warn-soft text-warn',
  LOW:    'bg-slate-700/60 text-slate-300',
}

function fmtMoney(n) { return n == null ? '—' : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}` }
function fmtTime(iso) { try { return new Date(iso).toLocaleString() } catch { return iso || '' } }
function fmtPct(n) { return (n == null || Number.isNaN(n)) ? '—' : `${(Number(n) * 100).toFixed(1)}%` }
function decimalToAmerican(d) {
  if (d == null || Number.isNaN(Number(d))) return '—'
  const x = Number(d)
  if (x <= 1) return '—'
  if (x >= 2) return `+${Math.round((x - 1) * 100)}`
  return `${Math.round(-100 / (x - 1))}`
}

// Collapse per-(book, market, outcome) rows into one row per (market, line, outcome),
// keeping the highest-edge entry. "Best book/odds" already reflect the best price.
function bestPerOutcome(rows) {
  const byKey = new Map()
  for (const r of rows || []) {
    const k = `${r.market || 'h2h'}|${r.market_line ?? ''}|${r.outcome}`
    const cur = byKey.get(k)
    if (!cur || r.edge > cur.edge) byKey.set(k, r)
  }
  const marketOrder = { h2h: 0, btts: 1, totals: 2 }
  const outcomeOrder = { home: 0, draw: 1, away: 2, yes: 0, no: 1, over: 0, under: 1 }
  return [...byKey.values()].sort((a, b) => {
    const am = marketOrder[a.market || 'h2h'] ?? 9
    const bm = marketOrder[b.market || 'h2h'] ?? 9
    if (am !== bm) return am - bm
    if ((a.market_line ?? -1) !== (b.market_line ?? -1)) return (a.market_line ?? 0) - (b.market_line ?? 0)
    return (outcomeOrder[a.outcome] ?? 9) - (outcomeOrder[b.outcome] ?? 9)
  })
}

function displayMarket(bet) {
  if (!bet.market || bet.market === 'h2h') return '1X2'
  if (bet.market === 'btts') return 'BTTS'
  if (bet.market === 'totals') return `O/U ${bet.market_line}`
  return bet.market
}

function displayOutcome(bet, prediction) {
  if (!bet.market || bet.market === 'h2h') {
    if (bet.outcome === 'home') return prediction?.home_team || 'Home'
    if (bet.outcome === 'away') return prediction?.away_team || 'Away'
    return 'Draw'
  }
  return bet.outcome.charAt(0).toUpperCase() + bet.outcome.slice(1)
}

function rowKey(b) {
  return `${b.market || 'h2h'}|${b.market_line ?? ''}|${b.outcome}`
}

// Trend chip rendered inline next to each team name. attack > 0 = improving
// in attack; defense > 0 = conceding more (bad). Only render the strongest
// signal so the card stays scannable.
function TrendBadge({ attack, defense }) {
  if (attack == null && defense == null) return null
  const tickets = []
  if (attack != null && Math.abs(attack) > 0.30) {
    tickets.push({
      tone: attack > 0 ? 'good' : 'bad',
      icon: attack > 0 ? '📈' : '📉',
      label: `${attack >= 0 ? '+' : ''}${attack.toFixed(2)} attack`,
    })
  }
  if (defense != null && Math.abs(defense) > 0.30) {
    tickets.push({
      // For defense, NEGATIVE = improving (conceding less) = good
      tone: defense < 0 ? 'good' : 'bad',
      icon: defense < 0 ? '📈' : '📉',
      label: `${defense >= 0 ? '+' : ''}${defense.toFixed(2)} defense${defense < 0 ? ' (improving)' : ''}`,
    })
  }
  if (tickets.length === 0) return null
  return (
    <span className="inline-flex gap-1 ml-1.5 align-middle">
      {tickets.map((t, i) => (
        <span
          key={i}
          title={t.label}
          className={`inline-flex items-center gap-0.5 text-[10px] font-mono px-1.5 py-0.5 rounded ${
            t.tone === 'good' ? 'bg-good-soft text-good' : 'bg-bad-soft text-bad'
          }`}
        >
          {t.icon} {t.label}
        </span>
      ))}
    </span>
  )
}

function buildView(view, prediction) {
  if (!view) return []
  const items = []
  if (view.h2h) {
    items.push({ label: '1X2', parts: [
      { name: prediction?.home_team || 'Home', pct: view.h2h.home },
      { name: 'Draw', pct: view.h2h.draw },
      { name: prediction?.away_team || 'Away', pct: view.h2h.away },
    ]})
  }
  if (view.btts) {
    items.push({ label: 'BTTS', parts: [
      { name: 'Yes', pct: view.btts.yes },
      { name: 'No', pct: view.btts.no },
    ]})
  }
  if (view.totals) {
    for (const [line, vals] of Object.entries(view.totals)) {
      items.push({ label: `O/U ${line}`, parts: [
        { name: 'Over', pct: vals.over },
        { name: 'Under', pct: vals.under },
      ]})
    }
  }
  return items
}

function ProbabilityView({ title, view, prediction, valueClass = 'text-slate-300', defaultOpen = false }) {
  const items = buildView(view, prediction)
  const [open, setOpen] = useState(defaultOpen)
  if (items.length === 0) return null
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="mt-3 pt-3 border-t border-ink-700/60">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-slate-500 hover:text-slate-300 transition-colors"
      >
        <Chevron size={12} />
        {title}
        <span className="text-slate-600 normal-case tracking-normal ml-auto">
          {open ? '' : `${items.length} markets`}
        </span>
      </button>
      {open && (
        <div className="space-y-1.5 text-xs mt-2">
          {items.map((it, i) => (
            <div key={i} className="flex items-center gap-2">
              <span className="text-slate-400 text-[11px] w-14 shrink-0">{it.label}</span>
              <span className="flex flex-wrap gap-x-3 gap-y-0.5">
                {it.parts.map((p, j) => (
                  <span key={j} className={`tabular-nums ${valueClass}`}>
                    {p.name} <span className="text-slate-500">·</span> {p.pct == null ? '—' : `${(p.pct * 100).toFixed(1)}%`}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function MatchCard({ prediction, bets, consensus, modelView, league, flashed, onLogPaper, onLogReal, inPlay }) {
  // When the match has kicked off the backend stops emitting fresh
  // match_consensus / match_model_view (so the dashboard doesn't compare
  // pre-match model probs against live in-play prices), and we hide the
  // bet rows / Paper / Cash / AI buttons — placing a bet at this point is
  // either too late or trades against an unrelated in-play market.
  const sortedBets = inPlay ? [] : bestPerOutcome(bets)
  const hasArb = sortedBets.length > 0
  const topEdge = Math.max(0, ...sortedBets.map(b => b.edge))
  const topBet = sortedBets.slice().sort((a, b) => b.edge - a.edge)[0]
  // Per-row log state, keyed by (market, line, outcome). Lets multiple rows
  // be logged independently without sharing one button's spinner.
  const [logStates, setLogStates] = useState({}) // { rowKey: 'logging' | 'logged' }
  const [showAnalysis, setShowAnalysis] = useState(false)

  // Match-level "bets placed" flag, hydrated from localStorage. The toggle is
  // here for quickly visually marking a match "done" — no server state needed.
  const [betsPlaced, setBetsPlaced] = useState(false)
  useEffect(() => {
    try {
      setBetsPlaced(localStorage.getItem(DONE_LS_PREFIX + prediction.match_id) === '1')
    } catch {}
  }, [prediction.match_id])
  function toggleBetsPlaced() {
    const next = !betsPlaced
    setBetsPlaced(next)
    try {
      if (next) localStorage.setItem(DONE_LS_PREFIX + prediction.match_id, '1')
      else localStorage.removeItem(DONE_LS_PREFIX + prediction.match_id)
    } catch {}
  }

  // kind: 'paper' | 'real'. The real flow is gated by a confirm step
  // (see startConfirmReal) so a stray click can't push real money.
  async function handleLog(b, kind) {
    const k = rowKey(b)
    const cur = logStates[k]
    if (cur === 'logging' || cur === 'logging-real' || cur === 'logged') return
    const inFlight = kind === 'real' ? 'logging-real' : 'logging'
    setLogStates(s => ({ ...s, [k]: inFlight }))
    try {
      const handler = kind === 'real' ? onLogReal : onLogPaper
      await handler?.(prediction, b)
      setLogStates(s => ({ ...s, [k]: 'logged' }))
      setTimeout(() => setLogStates(s => {
        const { [k]: _drop, ...rest } = s
        return rest
      }), 2000)
    } catch {
      setLogStates(s => {
        const { [k]: _drop, ...rest } = s
        return rest
      })
    }
  }

  function startConfirmReal(b) {
    setLogStates(s => ({ ...s, [rowKey(b)]: 'confirming-real' }))
  }
  function cancelConfirm(b) {
    setLogStates(s => {
      const { [rowKey(b)]: _drop, ...rest } = s
      return rest
    })
  }

  return (
    <div
      id={`match-${prediction.match_id}`}
      className={`bg-ink-900 border rounded-xl p-5 transition-all duration-500 scroll-mt-6 ${
        hasArb ? 'border-good/40 shadow-[0_0_0_1px_rgba(37,194,106,0.15)]' : 'border-ink-700'
      } ${flashed ? 'ring-2 ring-accent shadow-[0_0_0_4px_rgba(91,140,255,0.25)]' : ''} ${
        betsPlaced ? 'opacity-60' : ''
      }`}
    >
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex items-start gap-3">
          <label
            className="flex items-center gap-1.5 cursor-pointer select-none mt-0.5"
            title="Mark this match as done — visually fades the card so you can tell at a glance it's already handled"
          >
            <input
              type="checkbox"
              checked={betsPlaced}
              onChange={toggleBetsPlaced}
              className="w-4 h-4 rounded accent-good"
            />
            {betsPlaced && <span className="text-[10px] font-bold text-good tracking-wider">DONE</span>}
          </label>
          <div>
            <div className={`font-semibold text-base tracking-tight ${betsPlaced ? 'line-through text-slate-400' : ''}`}>
              {prediction.home_team}
              <TrendBadge attack={prediction.home_attack_trend} defense={prediction.home_defense_trend} />
              <span className="text-slate-500"> vs </span>
              {prediction.away_team}
              <TrendBadge attack={prediction.away_attack_trend} defense={prediction.away_defense_trend} />
            </div>
            <div className="text-xs text-slate-400 mt-0.5">
              {prediction.league?.toUpperCase() || league?.toUpperCase()} · {fmtTime(prediction.kickoff_time)}
              {prediction.form_breakpoint_detected ? (
                <span className="ml-2 text-warn font-semibold">
                  ⚠ FORM BREAKPOINT — {prediction.form_breakpoint_team} ratio {prediction.breakpoint_ratio?.toFixed(2)} · blend {prediction.blend_used || '80/20'}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          {hasArb && (
            <span className="text-[10px] font-bold tracking-wider bg-good text-ink-950 px-2.5 py-1 rounded-md">
              +EV {(topEdge * 100).toFixed(2)}%
            </span>
          )}
          <span className={`text-[10px] font-bold tracking-wider px-2.5 py-1 rounded-md ${CONF_STYLES[prediction.confidence] || CONF_STYLES.LOW}`}>
            {prediction.confidence || 'LOW'}
          </span>
        </div>
      </div>

      {inPlay && (
        <div className="mt-2 mb-3 px-3 py-2 rounded-md bg-warn-soft border border-warn/40 text-warn text-xs flex items-center gap-2">
          <span className="text-base leading-none">⏱</span>
          <span className="font-semibold tracking-wide uppercase">In play</span>
          <span className="text-warn/80">— pre-match odds shown; market frozen at kickoff</span>
        </div>
      )}

      <ProbabilityBar
        home={prediction.home_win_pct} draw={prediction.draw_pct} away={prediction.away_win_pct}
        homeLabel={prediction.home_team} awayLabel={prediction.away_team}
      />

      {sortedBets.length > 0 && (
        <table className="w-full text-xs mt-3">
          <thead className="text-[10px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="text-left py-1.5">Market</th>
              <th className="text-left">Outcome</th>
              <th className="text-left">Best book</th>
              <th className="text-right">Odds</th>
              <th className="text-right" title="De-vigged market implied probability (book's 'fair' price)">Market %</th>
              <th className="text-right" title="Model's predicted probability for this outcome">Model %</th>
              <th className="text-right">Edge</th>
              <th className="text-right">Timing</th>
              <th className="text-right">Stake</th>
              {league !== 'world_cup' && <th className="text-center w-32">Log</th>}
            </tr>
          </thead>
          <tbody>
            {sortedBets.map((b, i) => {
              const state = logStates[rowKey(b)]
              // Tint the entire row amber when the bet was blocked (anomaly
              // excluded it, daily-loss cap hit, or Kelly produced $0).
              const blockedRow = !b.actionable || !b.stake || b.stake <= 0
              return (
                <tr
                  key={i}
                  className={`border-t border-ink-700/60 ${blockedRow ? 'bg-warn/10 text-warn' : ''}`}
                >
                  <td className="py-2 text-slate-400 text-[11px]">{displayMarket(b)}</td>
                  <td className="font-medium">{displayOutcome(b, prediction)}</td>
                  <td>{b.best_book ?? b.book}</td>
                  <td className="text-right tabular-nums" title={(b.best_odds ?? b.decimal_odds).toFixed(2)}>
                    {decimalToAmerican(b.best_odds ?? b.decimal_odds)}
                  </td>
                  <td className="text-right tabular-nums text-slate-300">{fmtPct(b.true_implied_prob)}</td>
                  <td className="text-right tabular-nums text-good">{fmtPct(b.model_prob)}</td>
                  <td className="text-right tabular-nums text-good font-semibold">{(b.edge * 100).toFixed(2)}%</td>
                  <td className="text-right">
                    <span className={`inline-block text-[10px] font-bold px-2 py-0.5 rounded ${TIMING_STYLES[b.timing] || TIMING_STYLES.GREEN}`}>
                      {b.timing || 'GREEN'}
                    </span>
                  </td>
                  <td className="text-right tabular-nums">{fmtMoney(b.stake)}</td>
                  {league !== 'world_cup' && (
                    <td className="text-center">
                      {(() => {
                        // Disable both buttons when stake resolved to $0 —
                        // usually a PHANTOM_EDGE exclusion or sub-$5 Kelly.
                        const blocked = !b.actionable || !b.stake || b.stake <= 0
                        const reason = b.lockout_reason
                          || (b.anomaly_flags?.find(f => f.excludes_bet)?.description)
                          || (b.stake <= 0 ? 'Stake is $0 — bet excluded from logging' : '')

                        if (state === 'logged') {
                          return (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-semibold bg-good-soft border-good text-good">
                              <Check size={10} /> Logged
                            </span>
                          )
                        }
                        if (state === 'logging' || state === 'logging-real') {
                          const isReal = state === 'logging-real'
                          return (
                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-medium bg-ink-800 ${isReal ? 'border-warn text-warn' : 'border-accent text-accent'}`}>
                              <Loader2 size={10} className="animate-spin" /> {isReal ? 'Cash…' : 'Paper…'}
                            </span>
                          )
                        }
                        if (state === 'confirming-real') {
                          return (
                            <span className="inline-flex gap-1" title={`Confirm CASH bet $${b.stake?.toFixed(0)} on ${b.best_book ?? b.book}`}>
                              <button
                                onClick={() => handleLog(b, 'real')}
                                title={`Yes — log REAL $${b.stake?.toFixed(0)} on ${b.best_book ?? b.book}`}
                                className="inline-flex items-center gap-1 px-2 py-0.5 rounded border bg-warn text-ink-950 border-warn font-semibold text-[10px] hover:opacity-90"
                              >
                                <Check size={10} /> Confirm Cash
                              </button>
                              <button
                                onClick={() => cancelConfirm(b)}
                                title="Cancel"
                                className="inline-flex items-center justify-center px-1.5 py-0.5 rounded border bg-ink-800 border-ink-700 text-slate-400 hover:border-slate-500"
                              >
                                <X size={10} />
                              </button>
                            </span>
                          )
                        }
                        return (
                          <span className="inline-flex gap-1">
                            <button
                              onClick={() => handleLog(b, 'paper')}
                              disabled={blocked}
                              title={blocked ? reason : `Log PAPER bet — $${b.stake?.toFixed(0)} on ${b.best_book ?? b.book}`}
                              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-medium transition-colors ${
                                blocked
                                  ? 'bg-ink-900 border-ink-800 text-slate-600 cursor-not-allowed'
                                  : 'bg-accent-soft border-accent/40 text-accent hover:bg-accent hover:text-white'
                              }`}
                            >
                              <Plus size={10} /> Paper
                            </button>
                            {(() => {
                              // Specs A-D — cash button is greyed when the
                              // backend's cash_eligible=false. The reason
                              // string lands in the tooltip so the user
                              // can see WHY (goal market locked, edge < 6%,
                              // no paper counterpart, daily cap hit).
                              const cashBlocked = blocked || b.cash_eligible === false
                              const cashReason = blocked
                                ? reason
                                : (b.cash_reason || `Log CASH (real-money) bet — $${b.stake?.toFixed(0)} on ${b.best_book ?? b.book}`)
                              return (
                                <button
                                  onClick={() => !cashBlocked && startConfirmReal(b)}
                                  disabled={cashBlocked}
                                  title={cashReason}
                                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-medium transition-colors ${
                                    cashBlocked
                                      ? 'bg-ink-900 border-ink-800 text-slate-600 cursor-not-allowed'
                                      : 'bg-warn-soft border-warn/40 text-warn hover:bg-warn hover:text-ink-950'
                                  }`}
                                >
                                  <DollarSign size={10} /> Cash
                                </button>
                              )
                            })()}
                          </span>
                        )
                      })()}
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {!inPlay && (
        <ProbabilityView title="What the market thinks" view={consensus} prediction={prediction} valueClass="text-slate-300" />
      )}
      <ProbabilityView title="What the model thinks" view={modelView} prediction={prediction} valueClass="text-good" />

      <div className="flex items-center justify-between mt-3 pt-3 border-t border-ink-700/60 text-xs text-slate-400">
        <div>
          xG: <span className="text-slate-200 tabular-nums">{prediction.home_xg?.toFixed(2)}</span>
          <span className="text-slate-500"> / </span>
          <span className="text-slate-200 tabular-nums">{prediction.away_xg?.toFixed(2)}</span>
        </div>
        <div className="flex gap-2">
          {!inPlay && (
            <button
              onClick={() => setShowAnalysis(s => !s)}
              className="flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-purple-600/20 text-purple-300 hover:bg-purple-600/30 border border-purple-600/40"
            >
              <Sparkles size={12} /> {showAnalysis ? 'Hide AI' : 'AI Analysis'}
            </button>
          )}
          {hasArb && league === 'world_cup' && (
            <button
              className="flex items-center gap-1 text-xs bg-accent text-white px-2.5 py-1 rounded-md hover:opacity-90"
            >
              <ExternalLink size={12} /> Open {topBet.best_book ?? topBet.book}
            </button>
          )}
        </div>
      </div>

      {showAnalysis && (
        <MatchAnalysisPanel
          matchId={prediction.match_id}
          matchLabel={`${prediction.home_team} vs ${prediction.away_team}`}
          onClose={() => setShowAnalysis(false)}
        />
      )}
    </div>
  )
}
