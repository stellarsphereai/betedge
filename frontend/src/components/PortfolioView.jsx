import { useEffect, useMemo, useState } from 'react'
import {
  BarChart, Bar, LineChart, Line, ResponsiveContainer,
  XAxis, YAxis, Tooltip, ReferenceLine, Legend, CartesianGrid,
} from 'recharts'
import { api } from '../api'

// ---------- formatters ------------------------------------------------------

function fmtMoney(n, { signed = false } = {}) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  const x = Number(n)
  const sign = signed && x > 0 ? '+' : ''
  return `${sign}$${x.toFixed(2)}`
}
function fmtPct(n, { signed = false, digits = 1 } = {}) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  const x = Number(n) * 100
  const sign = signed && x > 0 ? '+' : ''
  return `${sign}${x.toFixed(digits)}%`
}
function fmtDate(iso) {
  try { return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) }
  catch { return iso }
}
// Date + time in the BROWSER'S local timezone (toLocale* uses the host TZ
// automatically). Used for kickoff so a NY user sees a Saturday 2pm Eastern
// kickoff as "May 3, 2:00 PM" instead of the underlying UTC string.
function fmtDateTimeLocal(iso) {
  try {
    const d = new Date(iso)
    const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
    return `${date}, ${time}`
  } catch { return iso }
}
function decimalToAmerican(d) {
  const x = Number(d)
  if (!Number.isFinite(x) || x <= 1) return '—'
  if (x >= 2) return `+${Math.round((x - 1) * 100)}`
  return `${Math.round(-100 / (x - 1))}`
}
function betLabel(b) {
  const t = b.bet_type
  const market = b.market || 'h2h'
  if (market === 'h2h') {
    if (t === 'home') return `${b.home_team}`
    if (t === 'away') return `${b.away_team}`
    return 'Draw'
  }
  if (market === 'btts') return `BTTS ${t?.charAt(0).toUpperCase() + t?.slice(1)}`
  if (market === 'totals') return `${t?.charAt(0).toUpperCase() + t?.slice(1)} ${b.market_line}`
  return t
}
function impliedFromOdds(odds) {
  const x = Number(odds)
  if (!Number.isFinite(x) || x <= 1) return null
  return 1 / x
}

// ---------- summary cards ---------------------------------------------------

function StatCard({ label, primary, secondary, tone = 'neutral' }) {
  const toneCls = {
    neutral: 'text-slate-200',
    good: 'text-good',
    warn: 'text-warn',
    bad: 'text-bad',
    accent: 'text-accent',
  }[tone] || 'text-slate-200'
  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4">
      <div className="text-xs text-slate-500 uppercase tracking-wide">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${toneCls}`}>{primary}</div>
      {secondary && <div className="text-xs text-slate-400 mt-1">{secondary}</div>}
    </div>
  )
}

function SummaryCards({ summary }) {
  if (!summary) return null
  const realizedTone = summary.realized_pnl > 0 ? 'good' : summary.realized_pnl < 0 ? 'bad' : 'neutral'
  const expectedTone = summary.expected_pnl > 0 ? 'accent' : 'neutral'
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
      <StatCard
        label="Total invested"
        primary={fmtMoney(summary.total_invested)}
        secondary={`${summary.open_bets_count + summary.settled_bets_count + summary.void_bets_count} bets total · ${summary.open_bets_count} open`}
      />
      <StatCard
        label="Portfolio value range"
        primary={`${fmtMoney(summary.current_value_worst)} – ${fmtMoney(summary.current_value_best)}`}
        secondary={`if all open ${summary.current_value_worst < summary.current_value_best ? 'lose / win' : 'settle'} · start ${fmtMoney(summary.starting_bankroll)}`}
      />
      <StatCard
        label="Realized P&L"
        primary={`${fmtMoney(summary.realized_pnl, { signed: true })} (${fmtPct(summary.realized_pct, { signed: true })})`}
        secondary={`on ${summary.settled_bets_count} settled bets · win rate ${fmtPct(summary.win_rate)}`}
        tone={realizedTone}
      />
      <StatCard
        label="Expected P&L"
        primary={fmtMoney(summary.expected_pnl, { signed: true })}
        secondary={`based on model edges · avg edge ${fmtPct(summary.avg_edge)}`}
        tone={expectedTone}
      />
    </div>
  )
}

// ---------- per-book breakdown ----------------------------------------------

// Order matches the BookBalanceStrip / digest row order so the eye learns it.
const BOOK_DISPLAY_ORDER = [
  'FanDuel', 'DraftKings', 'ESPN Bet', 'Fanatics', 'Bally Bet', 'BetRivers', 'Caesars',
]

function PortfolioByBook({ bets, bookBalances, mode }) {
  // Aggregate the (filtered) bets by book.
  const byBook = {}
  for (const b of bets) {
    const key = b.book || '(unknown)'
    if (!byBook[key]) {
      byBook[key] = { won: 0, lost: 0, open: 0, void: 0, staked: 0, profit: 0, expected: 0 }
    }
    const slot = byBook[key]
    slot.staked += b.stake || 0
    if (b.status === 'won') { slot.won += 1; slot.profit += b.profit || 0 }
    else if (b.status === 'lost') { slot.lost += 1; slot.profit += b.profit || 0 }
    else if (b.status === 'open') { slot.open += 1; slot.expected += (b.stake || 0) * (b.edge_at_placement || 0) }
    else if (b.status === 'void') { slot.void += 1 }
  }

  // Merge with book_balance rows so books with NO bets still show with current balance.
  // Only relevant for cash mode (paper bets don't move balances).
  const showBalances = mode === 'cash' && bookBalances && bookBalances.length > 0
  const balByName = showBalances
    ? Object.fromEntries(bookBalances.map(b => [b.display_name, b]))
    : {}
  const orderedBooks = BOOK_DISPLAY_ORDER.filter(n => byBook[n] || balByName[n])
  const otherBooks = Object.keys(byBook).filter(n => !BOOK_DISPLAY_ORDER.includes(n))
  const allBooks = [...orderedBooks, ...otherBooks]

  if (allBooks.length === 0) {
    return null
  }

  const totals = Object.values(byBook).reduce(
    (a, s) => ({
      bets: a.bets + s.won + s.lost + s.open + s.void,
      won: a.won + s.won,
      lost: a.lost + s.lost,
      open: a.open + s.open,
      staked: a.staked + s.staked,
      profit: a.profit + s.profit,
      expected: a.expected + s.expected,
    }),
    { bets: 0, won: 0, lost: 0, open: 0, staked: 0, profit: 0, expected: 0 }
  )

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl overflow-hidden mb-4">
      <div className="px-3 py-2 border-b border-ink-800 flex items-center gap-2 text-xs">
        <span className="text-slate-200 font-semibold">P&L by book</span>
        <span className="text-slate-500">— {mode === 'cash' ? 'Cash trade' : 'Paper trade'}</span>
      </div>
      <table className="w-full text-xs">
        <thead className="bg-ink-800 text-[10px] uppercase tracking-wider text-slate-400">
          <tr>
            <th className="text-left  px-3 py-1.5">Book</th>
            <th className="text-right">Bets</th>
            <th className="text-right">Record</th>
            <th className="text-right">Staked</th>
            <th className="text-right">Net P&L</th>
            <th className="text-right">Open EV</th>
            {showBalances && <th className="text-right pr-3">Balance</th>}
          </tr>
        </thead>
        <tbody>
          {allBooks.map(name => {
            const s = byBook[name] || { won: 0, lost: 0, open: 0, void: 0, staked: 0, profit: 0, expected: 0 }
            const bal = balByName[name]
            const settled = s.won + s.lost
            const total = settled + s.open + s.void
            const profitTone = s.profit > 0 ? 'text-good' : s.profit < 0 ? 'text-bad' : 'text-slate-500'
            const balTone = bal?.warning_level === 'red' ? 'text-bad'
                          : bal?.warning_level === 'amber' ? 'text-warn'
                          : 'text-slate-300'
            return (
              <tr key={name} className="border-t border-ink-800">
                <td className="px-3 py-1.5">{name}</td>
                <td className="text-right tabular-nums text-slate-400">{total || '—'}</td>
                <td className="text-right tabular-nums">
                  {settled
                    ? <span className={s.won > s.lost ? 'text-good' : s.won < s.lost ? 'text-bad' : 'text-slate-300'}>{s.won}–{s.lost}</span>
                    : <span className="text-slate-600">—</span>}
                </td>
                <td className="text-right tabular-nums">{s.staked ? fmtMoney(s.staked) : <span className="text-slate-600">—</span>}</td>
                <td className={`text-right tabular-nums font-semibold ${profitTone}`}>
                  {settled ? fmtMoney(s.profit, { signed: true }) : <span className="text-slate-600">—</span>}
                </td>
                <td className="text-right tabular-nums text-accent">
                  {s.open ? `${fmtMoney(s.expected, { signed: true })}` : <span className="text-slate-600">—</span>}
                </td>
                {showBalances && (
                  <td className={`text-right tabular-nums pr-3 ${balTone}`}>
                    {bal ? fmtMoney(bal.balance_usd) : <span className="text-slate-600">—</span>}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
        <tfoot className="bg-ink-800/60 font-semibold">
          <tr className="border-t border-ink-700">
            <td className="px-3 py-1.5">Total</td>
            <td className="text-right tabular-nums">{totals.bets}</td>
            <td className="text-right tabular-nums">{totals.won}–{totals.lost}{totals.open ? ` (·${totals.open}o)` : ''}</td>
            <td className="text-right tabular-nums">{fmtMoney(totals.staked)}</td>
            <td className={`text-right tabular-nums ${totals.profit > 0 ? 'text-good' : totals.profit < 0 ? 'text-bad' : ''}`}>
              {fmtMoney(totals.profit, { signed: true })}
            </td>
            <td className="text-right tabular-nums text-accent">{fmtMoney(totals.expected, { signed: true })}</td>
            {showBalances && (
              <td className="text-right tabular-nums pr-3 text-slate-200">
                {fmtMoney(bookBalances.reduce((s, b) => s + (b.balance_usd || 0), 0))}
              </td>
            )}
          </tr>
        </tfoot>
      </table>
    </div>
  )
}


// ---------- bet table -------------------------------------------------------

const STATUS_STYLES = {
  open: { label: 'OPEN',  cls: 'bg-accent-soft text-accent' },
  won:  { label: 'WON',   cls: 'bg-good-soft text-good' },
  lost: { label: 'LOST',  cls: 'bg-bad-soft text-bad' },
  void: { label: 'VOID',  cls: 'bg-ink-800 text-slate-400' },
}

function StatusPill({ status }) {
  const s = STATUS_STYLES[status] || { label: status?.toUpperCase() || '—', cls: 'text-slate-400' }
  return <span className={`uppercase text-[10px] font-semibold px-1.5 py-0.5 rounded ${s.cls}`}>{s.label}</span>
}

function BetTable({ bets, league, market, status, dateFrom, dateTo }) {
  const [sortBy, setSortBy] = useState('timestamp')
  const [sortDir, setSortDir] = useState('desc')

  const filtered = useMemo(() => {
    return bets.filter(b => {
      if (league && (b.league || b.match_league) !== league) return false
      if (market && (b.market || 'h2h') !== market) return false
      if (status && b.status !== status) return false
      if (dateFrom) {
        const t = new Date(b.timestamp).getTime()
        if (t < new Date(dateFrom).getTime()) return false
      }
      if (dateTo) {
        const t = new Date(b.timestamp).getTime()
        // Include the entire end date (up to 23:59:59)
        if (t > new Date(dateTo + 'T23:59:59').getTime()) return false
      }
      return true
    })
  }, [bets, league, market, status, dateFrom, dateTo])

  const sorted = useMemo(() => {
    const arr = [...filtered]
    arr.sort((a, b) => {
      const va = a[sortBy], vb = b[sortBy]
      if (va == null && vb == null) return 0
      if (va == null) return 1
      if (vb == null) return -1
      const cmp = (va < vb ? -1 : va > vb ? 1 : 0)
      return sortDir === 'asc' ? cmp : -cmp
    })
    return arr
  }, [filtered, sortBy, sortDir])

  function clickSort(col) {
    if (sortBy === col) setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    else { setSortBy(col); setSortDir('desc') }
  }

  if (sorted.length === 0) {
    return (
      <div className="bg-ink-900 border border-dashed border-ink-700 rounded-xl p-8 text-center text-slate-400 text-sm">
        No bets match the current filters.
      </div>
    )
  }

  const Th = ({ col, children, right }) => (
    <th
      onClick={() => clickSort(col)}
      className={`px-2 py-2 font-medium uppercase tracking-wide text-[10px] text-slate-400 cursor-pointer hover:text-slate-200 ${right ? 'text-right' : 'text-left'}`}
    >
      {children}{sortBy === col ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
    </th>
  )

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-ink-800">
            <tr>
              <Th col="timestamp">Placed</Th>
              <Th col="match_kickoff">Kickoff</Th>
              <Th col="home_team">Match</Th>
              <Th col="market">Market</Th>
              <Th col="bet_type">Outcome</Th>
              <Th col="book">Book</Th>
              <Th col="odds_at_placement" right>Odds</Th>
              <Th col="stake" right>Stake</Th>
              <Th col="edge_at_placement" right>Edge</Th>
              <Th col="ev" right>EV</Th>
              <Th col="status">Status</Th>
              <Th col="profit" right>P&L</Th>
              <Th col="clv" right>CLV</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.map(b => {
              const ev = (b.stake || 0) * (b.edge_at_placement || 0)
              const pnlCell = b.status === 'open'
                ? <span className="text-accent">{fmtMoney(ev, { signed: true })}</span>
                : b.status === 'won'
                  ? <span className="text-good">{fmtMoney(b.profit, { signed: true })}</span>
                  : b.status === 'lost'
                    ? <span className="text-bad">{fmtMoney(b.profit, { signed: true })}</span>
                    : <span className="text-slate-500">—</span>
              return (
                <tr key={b.id} className="border-t border-ink-800 hover:bg-ink-800/50">
                  <td className="px-2 py-1.5 text-slate-400 whitespace-nowrap">{fmtDate(b.timestamp)}</td>
                  <td
                    className="px-2 py-1.5 text-slate-400 whitespace-nowrap"
                    title={b.match_kickoff || ''}
                  >
                    {b.match_kickoff ? fmtDateTimeLocal(b.match_kickoff) : <span className="text-slate-600">—</span>}
                  </td>
                  <td className="px-2 py-1.5 whitespace-nowrap">
                    <div className="text-slate-200">{b.home_team}</div>
                    <div className="text-slate-500 text-[10px]">vs {b.away_team}</div>
                  </td>
                  <td className="px-2 py-1.5 uppercase text-[10px] text-slate-400">{b.market || 'h2h'}</td>
                  <td className="px-2 py-1.5 text-slate-200">{betLabel(b)}</td>
                  <td className="px-2 py-1.5 text-slate-400">{b.book}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">
                    <div>{Number(b.odds_at_placement).toFixed(2)}</div>
                    <div className="text-slate-500 text-[10px]">{decimalToAmerican(b.odds_at_placement)}</div>
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{fmtMoney(b.stake)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{fmtPct(b.edge_at_placement)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{fmtMoney(ev, { signed: true })}</td>
                  <td className="px-2 py-1.5"><StatusPill status={b.status} /></td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{pnlCell}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{b.clv != null ? fmtPct(b.clv, { signed: true }) : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="px-3 py-2 border-t border-ink-800 text-[10px] text-slate-500">
        {sorted.length} {sorted.length === 1 ? 'bet' : 'bets'} shown
      </div>
    </div>
  )
}

// ---------- charts ----------------------------------------------------------

function BankrollOverTimeChart({ bets, startingBankroll }) {
  // Build cumulative actual + expected lines from settled bets in chronological order.
  const settled = bets
    .filter(b => b.status === 'won' || b.status === 'lost')
    .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

  let actual = startingBankroll
  let expected = startingBankroll
  const points = [{ n: 0, actual, expected, label: 'start' }]
  settled.forEach((b, i) => {
    actual += (b.profit || 0)
    expected += (b.stake || 0) * (b.edge_at_placement || 0)
    points.push({ n: i + 1, actual: Math.round(actual * 100) / 100, expected: Math.round(expected * 100) / 100, label: fmtDate(b.timestamp) })
  })

  if (points.length < 2) {
    return <div className="h-64 flex items-center justify-center text-xs text-slate-500">No settled bets yet — chart populates as bets resolve.</div>
  }
  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
          <CartesianGrid stroke="#232a4a" strokeDasharray="3 3" />
          <XAxis dataKey="n" tick={{ fill: '#8a93b8', fontSize: 10 }} />
          <YAxis tick={{ fill: '#8a93b8', fontSize: 10 }} domain={['auto', 'auto']} />
          <Tooltip contentStyle={{ background: '#141a31', border: '1px solid #232a4a' }} labelStyle={{ color: '#8a93b8' }} />
          <ReferenceLine y={startingBankroll} stroke="#8a93b8" strokeDasharray="3 3" label={{ value: 'start', fill: '#8a93b8', fontSize: 10, position: 'right' }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Line type="monotone" name="Actual" dataKey="actual" stroke="#25c26a" strokeWidth={2} dot={false} />
          <Line type="monotone" name="Expected (EV)" dataKey="expected" stroke="#5b8cff" strokeWidth={2} strokeDasharray="5 5" dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function EdgeDistributionChart({ bets }) {
  const buckets = [
    { range: '<3%',     min: 0,    max: 0.03, count: 0, anomaly: false },
    { range: '3–5%',    min: 0.03, max: 0.05, count: 0, anomaly: false },
    { range: '5–8%',    min: 0.05, max: 0.08, count: 0, anomaly: false },
    { range: '8–12%',   min: 0.08, max: 0.12, count: 0, anomaly: false },
    { range: '12–15%',  min: 0.12, max: 0.15, count: 0, anomaly: false },
    { range: '15%+',    min: 0.15, max: Infinity, count: 0, anomaly: true },
  ]
  bets.forEach(b => {
    const e = b.edge_at_placement
    if (e == null) return
    const bucket = buckets.find(x => e >= x.min && e < x.max)
    if (bucket) bucket.count += 1
  })
  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={buckets} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
          <CartesianGrid stroke="#232a4a" strokeDasharray="3 3" />
          <XAxis dataKey="range" tick={{ fill: '#8a93b8', fontSize: 11 }} />
          <YAxis tick={{ fill: '#8a93b8', fontSize: 10 }} allowDecimals={false} />
          <Tooltip contentStyle={{ background: '#141a31', border: '1px solid #232a4a' }} labelStyle={{ color: '#8a93b8' }} />
          <Bar dataKey="count" fill="#5b8cff" radius={[3, 3, 0, 0]}
               // anomaly bucket gets a warning hue via shape
               shape={({ x, y, width, height, payload }) =>
                 <rect x={x} y={y} width={width} height={height} rx={3} ry={3}
                       fill={payload.anomaly ? '#ffb04a' : '#5b8cff'} />
               }
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function PnlByMarketChart({ bets }) {
  const buckets = { h2h: { name: '1X2', pnl: 0, n: 0 }, btts: { name: 'BTTS', pnl: 0, n: 0 }, totals: { name: 'O/U', pnl: 0, n: 0 } }
  bets.forEach(b => {
    const m = b.market || 'h2h'
    const slot = buckets[m]
    if (!slot) return
    if (b.status === 'won' || b.status === 'lost') {
      slot.pnl += (b.profit || 0)
      slot.n += 1
    } else if (b.status === 'open') {
      // include open bets' EV as a faint contribution? omit — chart shows realized
    }
  })
  const data = Object.values(buckets)
  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 8, left: 24, bottom: 8 }}>
          <CartesianGrid stroke="#232a4a" strokeDasharray="3 3" />
          <XAxis type="number" tick={{ fill: '#8a93b8', fontSize: 10 }} />
          <YAxis type="category" dataKey="name" tick={{ fill: '#8a93b8', fontSize: 11 }} width={50} />
          <Tooltip contentStyle={{ background: '#141a31', border: '1px solid #232a4a' }} formatter={(v, _, p) => [`${fmtMoney(v, { signed: true })} (${p.payload.n} bets)`, 'P&L']} />
          <ReferenceLine x={0} stroke="#8a93b8" />
          <Bar dataKey="pnl" radius={[0, 3, 3, 0]}
               shape={({ x, y, width, height, payload }) => {
                 const fill = payload.pnl > 0 ? '#25c26a' : payload.pnl < 0 ? '#ff6b6b' : '#5b8cff'
                 return <rect x={Math.min(x, x + width)} y={y} width={Math.abs(width)} height={height} rx={3} ry={3} fill={fill} />
               }}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function PortfolioCharts({ bets, startingBankroll, title = 'Portfolio charts', tone = 'neutral' }) {
  const [view, setView] = useState('bankroll')
  const tabs = [
    { id: 'bankroll', label: 'Bankroll over time' },
    { id: 'edges', label: 'Edge distribution' },
    { id: 'pnl', label: 'P&L by market' },
  ]
  // Tone gives each panel a colored top accent so paper vs cash is obvious
  // at a glance even when they're side-by-side.
  const toneCls = {
    paper:   'border-accent/40',
    cash:    'border-warn/40',
    neutral: 'border-ink-700',
  }[tone] || 'border-ink-700'
  return (
    <div className={`bg-ink-900 border ${toneCls} rounded-xl p-4`}>
      <div className="flex justify-between items-center mb-3">
        <div className="text-sm font-medium flex items-center gap-2">
          {tone === 'paper' && <span className="text-accent">📝</span>}
          {tone === 'cash' && <span className="text-warn">💵</span>}
          {title}
          <span className="text-[10px] text-slate-500 font-normal">
            ({bets.length} bet{bets.length === 1 ? '' : 's'})
          </span>
        </div>
        <div className="bg-ink-800 border border-ink-700 rounded-full p-0.5 flex">
          {tabs.map(t => (
            <button
              key={t.id}
              onClick={() => setView(t.id)}
              className={`px-2.5 py-0.5 rounded-full text-[10px] font-medium ${view === t.id ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'}`}
            >{t.label}</button>
          ))}
        </div>
      </div>
      {bets.length === 0 ? (
        <div className="h-64 flex items-center justify-center text-xs text-slate-500">
          No {tone === 'paper' ? 'paper' : tone === 'cash' ? 'cash' : ''} bets yet — chart populates as bets are placed.
        </div>
      ) : (
        <>
          {view === 'bankroll' && <BankrollOverTimeChart bets={bets} startingBankroll={startingBankroll} />}
          {view === 'edges' && <EdgeDistributionChart bets={bets} />}
          {view === 'pnl' && <PnlByMarketChart bets={bets} />}
        </>
      )}
    </div>
  )
}

// ---------- calculator + projection -----------------------------------------

function Slider({ label, value, onChange, min, max, step, format }) {
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-slate-400">{label}</span>
        <span className="text-slate-200 tabular-nums">{format ? format(value) : value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        className="w-full accent-accent"
      />
    </div>
  )
}

function ScenarioCard({ s }) {
  const toneCls = {
    good: 'border-good-soft bg-good-soft text-good',
    warn: 'border-warn-soft bg-warn-soft text-warn',
    bad: 'border-bad-soft bg-bad-soft text-bad',
  }[s.tone] || 'border-ink-700 bg-ink-900'
  return (
    <div className={`rounded-xl border p-3 ${toneCls}`}>
      <div className="text-xs uppercase tracking-wide opacity-70">{s.label}</div>
      <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 mt-2 text-[11px]">
        <div className="opacity-70">Win rate</div><div className="tabular-nums text-right">{fmtPct(s.win_rate)}</div>
        <div className="opacity-70">Avg edge</div><div className="tabular-nums text-right">{fmtPct(s.edge, { signed: true })}</div>
        <div className="opacity-70">ROI</div><div className="tabular-nums text-right">{fmtPct(s.roi, { signed: true })}</div>
        <div className="opacity-70">Profit</div><div className="tabular-nums text-right">{fmtMoney(s.expected_profit, { signed: true })}</div>
      </div>
      {s.note && <div className="mt-2 text-[10px] opacity-70">{s.note}</div>}
    </div>
  )
}

function CalculatorPanel({ defaultEdge }) {
  const [matches, setMatches] = useState(64)
  const [stake, setStake] = useState(20)
  const [edge, setEdge] = useState(defaultEdge ?? 0.06)
  const [betsPerMatch, setBetsPerMatch] = useState(1.5)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  // Pull projection on input change (debounced).
  useEffect(() => {
    const id = setTimeout(async () => {
      try {
        setError(null)
        const r = await api.portfolioProjection({
          matches, stake, edge, betsPerMatch,
        })
        setData(r)
      } catch (e) { setError(e.message || String(e)) }
    }, 200)
    return () => clearTimeout(id)
  }, [matches, stake, edge, betsPerMatch])

  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 space-y-4">
      <div>
        <div className="text-sm font-medium">Expected return calculator</div>
        <div className="text-xs text-slate-500">Project remaining season under different assumptions.</div>
      </div>

      <div className="space-y-3">
        <Slider label="Remaining matches"   value={matches}      onChange={setMatches}      min={10}  max={200} step={1}     />
        <Slider label="Average stake"        value={stake}        onChange={setStake}        min={5}   max={100} step={1}     format={v => `$${v}`} />
        <Slider label="Average edge"         value={edge}         onChange={setEdge}         min={0.01} max={0.15} step={0.005} format={v => `${(v*100).toFixed(1)}%`} />
        <Slider label="Bets per match"       value={betsPerMatch} onChange={setBetsPerMatch} min={0.5} max={3}   step={0.1}   />
      </div>

      {error && <div className="text-xs text-bad">{error}</div>}

      {data?.summary && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs border-t border-ink-700 pt-3">
          <div className="text-slate-400">Total bets</div>             <div className="text-right tabular-nums">{data.summary.total_bets}</div>
          <div className="text-slate-400">Total staked</div>           <div className="text-right tabular-nums">{fmtMoney(data.summary.total_staked)}</div>
          <div className="text-slate-400">Expected profit</div>        <div className="text-right tabular-nums text-good">{fmtMoney(data.summary.expected_profit, { signed: true })}</div>
          <div className="text-slate-400">Expected ROI</div>           <div className="text-right tabular-nums">{fmtPct(data.summary.expected_roi, { signed: true })}</div>
          <div className="text-slate-400">Expected bankroll</div>      <div className="text-right tabular-nums">{fmtMoney(data.summary.expected_bankroll)}</div>
          <div className="col-span-2 border-t border-ink-800 mt-1 pt-1" />
          <div className="text-slate-400">Best case (edge +50%)</div>  <div className="text-right tabular-nums text-good">{fmtMoney(data.summary.best_case, { signed: true })}</div>
          <div className="text-slate-400">Worst case (edge −50%)</div> <div className="text-right tabular-nums text-warn">{fmtMoney(data.summary.worst_case, { signed: true })}</div>
          <div className="text-slate-400">With variance (60% loss)</div><div className="text-right tabular-nums text-bad">{fmtMoney(data.summary.variance_pnl, { signed: true })}</div>
        </div>
      )}
    </div>
  )
}

function KellyProjectionPanel({ projection, startingBankroll }) {
  if (!projection) return null
  const { kelly_growth_table, kelly_curves } = projection
  const points = (kelly_curves?.full_kelly || []).map((p, i) => ({
    n: p.n,
    full: p.bankroll,
    half: kelly_curves.half_kelly?.[i]?.bankroll ?? null,
  }))
  return (
    <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 mb-4">
      <div className="text-sm font-medium mb-3">Kelly growth projection</div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 uppercase text-[10px]">
                <th className="text-left  py-1">After</th>
                <th className="text-right py-1">Half Kelly</th>
                <th className="text-right py-1">Full Kelly</th>
              </tr>
            </thead>
            <tbody>
              {kelly_growth_table.map(row => (
                <tr key={row.n} className="border-t border-ink-800">
                  <td className="py-1.5">{row.n} bets</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {fmtMoney(row.half_kelly)}
                    <span className="text-slate-500 text-[10px] ml-1">({row.half_pct >= 0 ? '+' : ''}{row.half_pct}%)</span>
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {fmtMoney(row.full_kelly)}
                    <span className="text-slate-500 text-[10px] ml-1">({row.full_pct >= 0 ? '+' : ''}{row.full_pct}%)</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
              <CartesianGrid stroke="#232a4a" strokeDasharray="3 3" />
              <XAxis dataKey="n" tick={{ fill: '#8a93b8', fontSize: 10 }} />
              <YAxis tick={{ fill: '#8a93b8', fontSize: 10 }} />
              <Tooltip contentStyle={{ background: '#141a31', border: '1px solid #232a4a' }} labelStyle={{ color: '#8a93b8' }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <ReferenceLine y={startingBankroll} stroke="#8a93b8" strokeDasharray="3 3" />
              <Line type="monotone" name="Half Kelly" dataKey="half" stroke="#5b8cff" strokeWidth={2} dot={false} />
              <Line type="monotone" name="Full Kelly" dataKey="full" stroke="#25c26a" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  )
}

// ---------- CSV export ------------------------------------------------------

function exportCSV(bets) {
  const headers = [
    'Bet Placed', 'Match Kickoff', 'Match', 'League', 'Market', 'Outcome', 'Book',
    'Decimal Odds', 'American Odds', 'Stake', 'Edge', 'EV',
    'Model Probability', 'Book Implied', 'Status', 'Result',
    'P&L', 'CLV', 'Confidence', 'Anomaly Flagged',
  ]
  const escape = (v) => {
    if (v == null) return ''
    const s = String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const rows = bets.map(b => {
    const ev = (b.stake || 0) * (b.edge_at_placement || 0)
    const result = b.fixture_home_goals != null ? `${b.fixture_home_goals}-${b.fixture_away_goals}` : ''
    const implied = impliedFromOdds(b.odds_at_placement)
    return [
      b.timestamp,
      b.match_kickoff ? fmtDateTimeLocal(b.match_kickoff) : '',
      `${b.home_team} vs ${b.away_team}`,
      b.league || b.match_league || '',
      b.market || 'h2h',
      betLabel(b),
      b.book,
      Number(b.odds_at_placement).toFixed(2),
      decimalToAmerican(b.odds_at_placement),
      b.stake,
      b.edge_at_placement,
      ev.toFixed(2),
      b.model_prob ?? '',
      implied != null ? implied.toFixed(4) : '',
      b.status,
      result,
      b.profit ?? '',
      b.clv ?? '',
      b.confidence ?? '',
      b.anomaly_flagged ? 'yes' : '',
    ].map(escape).join(',')
  })
  const csv = [headers.join(','), ...rows].join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `betedge-portfolio-${new Date().toISOString().slice(0, 10)}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// ---------- top-level component ---------------------------------------------

export default function PortfolioView() {
  const [bets, setBets] = useState([])
  const [paperBets, setPaperBets] = useState([])
  const [cashBets, setCashBets] = useState([])
  const [bookBalances, setBookBalances] = useState([])
  const [summary, setSummary] = useState(null)
  const [projection, setProjection] = useState(null)
  // 'all' | 'paper' | 'cash'. Spec 1.5 — three tabs in portfolio so paper
  // and real-money performance can be inspected separately or together.
  const [mode, setMode] = useState('paper')
  const paperOnly = mode === 'paper'
  const [filters, setFilters] = useState({ league: '', market: '', status: '', dateFrom: '', dateTo: '' })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function load() {
    setLoading(true); setError(null)
    try {
      // Summary endpoint takes is_paper bool; for 'all' we sum both later.
      const summaryReq = mode === 'all'
        ? Promise.all([api.portfolioSummary({ isPaper: true }), api.portfolioSummary({ isPaper: false })])
            .then(([sp, sc]) => ({
              starting_bankroll: (sp.starting_bankroll || 0) + (sc.starting_bankroll || 0),
              realized_pnl:      (sp.realized_pnl || 0)     + (sc.realized_pnl || 0),
              realized_pct:      ((sp.realized_pnl || 0) + (sc.realized_pnl || 0)) /
                                 Math.max(1, (sp.starting_bankroll || 0) + (sc.starting_bankroll || 0)),
              expected_pnl:      (sp.expected_pnl || 0)     + (sc.expected_pnl || 0),
              avg_edge:          ((sp.avg_edge || 0) + (sc.avg_edge || 0)) / 2,
              current_value_best:  (sp.current_value_best  || 0) + (sc.current_value_best  || 0),
              current_value_worst: (sp.current_value_worst || 0) + (sc.current_value_worst || 0),
            }))
        : api.portfolioSummary({ isPaper: paperOnly })
      const [s, b, p, bb] = await Promise.all([
        summaryReq,
        api.bets(1000),
        api.portfolioProjection({ matches: 64, stake: 20, edge: 0.06, betsPerMatch: 1.5 }),
        fetch('/book-balances').then(r => r.ok ? r.json() : null).catch(() => null),
      ])
      setSummary(s)
      const allBets = b.bets || []
      const filtered = mode === 'all'
        ? allBets
        : allBets.filter(x => paperOnly ? x.is_paper === 1 : x.is_paper !== 1)
      setBets(filtered)
      // Charts always show both paper + cash, side-by-side, regardless of the
      // top toggle (the toggle only affects the summary cards + bet table).
      setPaperBets(allBets.filter(x => x.is_paper === 1))
      setCashBets(allBets.filter(x => x.is_paper !== 1))
      setProjection(p)
      setBookBalances(bb?.books || [])
    } catch (e) { setError(e.message || String(e)) }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [mode])

  const startingBankroll = summary?.starting_bankroll ?? 1000
  const defaultEdge = summary?.avg_edge || 0.06

  // Apply the same filters used by BetTable to compute a local summary
  // that updates the financial tiles when filters change.
  const hasFilters = !!(filters.league || filters.market || filters.status || filters.dateFrom || filters.dateTo)
  const filteredBets = useMemo(() => {
    if (!hasFilters) return bets
    return bets.filter(b => {
      if (filters.league && (b.league || b.match_league) !== filters.league) return false
      if (filters.market && (b.market || 'h2h') !== filters.market) return false
      if (filters.status && b.status !== filters.status) return false
      if (filters.dateFrom) {
        const t = new Date(b.timestamp).getTime()
        if (t < new Date(filters.dateFrom).getTime()) return false
      }
      if (filters.dateTo) {
        const t = new Date(b.timestamp).getTime()
        if (t > new Date(filters.dateTo + 'T23:59:59').getTime()) return false
      }
      return true
    })
  }, [bets, filters, hasFilters])

  const filteredSummary = useMemo(() => {
    if (!hasFilters) return summary
    const fb = filteredBets
    const open = fb.filter(b => b.status === 'open')
    const settled = fb.filter(b => b.status === 'won' || b.status === 'lost')
    const voidBets = fb.filter(b => b.status === 'void')
    const won = fb.filter(b => b.status === 'won')
    const totalInvested = fb.reduce((s, b) => s + (b.stake || 0), 0)
    const realizedPnl = settled.reduce((s, b) => s + (b.profit || 0), 0)
    const expectedPnl = open.reduce((s, b) => s + (b.stake || 0) * (b.edge_at_placement || 0), 0)
    const edges = fb.filter(b => b.edge_at_placement != null).map(b => b.edge_at_placement)
    const avgEdge = edges.length > 0 ? edges.reduce((a, e) => a + e, 0) / edges.length : 0
    const winRate = settled.length > 0 ? won.length / settled.length : 0
    const openStakes = open.reduce((s, b) => s + (b.stake || 0), 0)
    const openMaxPayout = open.reduce((s, b) => s + (b.stake || 0) * ((b.odds_at_placement || 1) - 1), 0)
    return {
      total_invested: totalInvested,
      open_bets_count: open.length,
      settled_bets_count: settled.length,
      void_bets_count: voidBets.length,
      realized_pnl: realizedPnl,
      realized_pct: totalInvested > 0 ? realizedPnl / totalInvested : 0,
      expected_pnl: expectedPnl,
      avg_edge: avgEdge,
      win_rate: winRate,
      starting_bankroll: startingBankroll,
      current_value_best: startingBankroll + realizedPnl + openMaxPayout,
      current_value_worst: startingBankroll + realizedPnl - openStakes,
    }
  }, [filteredBets, hasFilters, summary, startingBankroll])

  return (
    <div>
      {/* Header strip */}
      <div className="flex flex-wrap justify-between items-center gap-3 mb-3">
        <div>
          <h2 className="text-lg font-semibold">Portfolio</h2>
          <div className="text-xs text-slate-500">All bets, P&L, edges, and projections.</div>
        </div>
        <button
          onClick={() => exportCSV(bets)}
          className="px-3 py-1 rounded-md text-xs font-medium border border-ink-700 hover:border-slate-500 text-slate-200"
        >Export CSV</button>
      </div>

      {/* Mode toggle — three tabs: All / Paper / Cash (spec 1.5) */}
      <div className="flex flex-wrap items-center gap-3 mb-3 px-3 py-2 bg-ink-900 border border-ink-700 rounded-lg">
        <span className="text-xs text-slate-500 uppercase tracking-wider font-semibold">View:</span>
        <div className="bg-ink-800 border border-ink-700 rounded-full p-0.5 flex">
          <button
            onClick={() => setMode('all')}
            className={`px-4 py-1.5 rounded-full text-xs font-medium transition ${mode === 'all' ? 'bg-good text-ink-950 shadow' : 'text-slate-400 hover:text-slate-200'}`}
          >🧮 All bets</button>
          <button
            onClick={() => setMode('paper')}
            className={`px-4 py-1.5 rounded-full text-xs font-medium transition ${mode === 'paper' ? 'bg-accent text-white shadow' : 'text-slate-400 hover:text-slate-200'}`}
          >📝 Paper trade</button>
          <button
            onClick={() => setMode('cash')}
            className={`px-4 py-1.5 rounded-full text-xs font-medium transition ${mode === 'cash' ? 'bg-warn text-ink-950 shadow' : 'text-slate-400 hover:text-slate-200'}`}
          >💵 Cash trade</button>
        </div>
        <span className="text-[11px] text-slate-500 ml-auto">
          {mode === 'all'
            ? 'Showing every bet — paper and cash combined.'
            : mode === 'paper'
            ? 'Showing simulated bets — book balances unaffected.'
            : 'Showing real-money bets — these moved your book balances when settled.'}
        </span>
      </div>

      {/* Mode banner */}
      {mode === 'all' ? (
        <div className="mb-3 px-3 py-2 rounded-md text-xs bg-good-soft text-good border border-good-soft">
          Combined view — paper + cash bets together. Book balances reflect cash settlements only.
        </div>
      ) : mode === 'paper' ? (
        <div className="mb-3 px-3 py-2 rounded-md text-xs bg-warn-soft text-warn border border-warn-soft">
          PAPER TRADING — not real money. P&L is simulated against settled match outcomes.
        </div>
      ) : (
        <div className="mb-3 px-3 py-2 rounded-md text-xs bg-good-soft text-good border border-good-soft">
          Real money view — actual bankroll impact.
        </div>
      )}

      {error && <div className="mb-3 text-xs text-bad">{error}</div>}
      {loading && <div className="mb-3 text-xs text-slate-500">Loading portfolio…</div>}

      <SummaryCards summary={filteredSummary} />

      <PortfolioByBook
        bets={filteredBets}
        bookBalances={bookBalances}
        mode={mode === 'all' ? 'all' : mode === 'paper' ? 'paper' : 'cash'}
      />

      {/* Two-column layout: table+charts on left, calculator on right */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <div className="xl:col-span-2 space-y-4">
          {/* Filters */}
          <div className="flex flex-wrap gap-2 text-xs">
            <select
              value={filters.league}
              onChange={e => setFilters(f => ({ ...f, league: e.target.value }))}
              className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-slate-200"
            >
              <option value="">All leagues</option>
              <option value="epl">EPL</option>
              <option value="ucl">UCL</option>
              <option value="uel">EL</option>
              <option value="world_cup">World Cup</option>
            </select>
            <select
              value={filters.market}
              onChange={e => setFilters(f => ({ ...f, market: e.target.value }))}
              className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-slate-200"
            >
              <option value="">All markets</option>
              <option value="h2h">1X2</option>
              <option value="btts">BTTS</option>
              <option value="totals">Totals</option>
            </select>
            <select
              value={filters.status}
              onChange={e => setFilters(f => ({ ...f, status: e.target.value }))}
              className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-slate-200"
            >
              <option value="">All statuses</option>
              <option value="open">Open</option>
              <option value="won">Won</option>
              <option value="lost">Lost</option>
              <option value="void">Void</option>
            </select>
            <div className="flex items-center gap-1">
              <span className="text-slate-500">From</span>
              <input
                type="date"
                value={filters.dateFrom}
                onChange={e => setFilters(f => ({ ...f, dateFrom: e.target.value }))}
                className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-slate-200"
              />
              <span className="text-slate-500">to</span>
              <input
                type="date"
                value={filters.dateTo}
                onChange={e => setFilters(f => ({ ...f, dateTo: e.target.value }))}
                className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-slate-200"
              />
              {(filters.dateFrom || filters.dateTo) && (
                <button
                  onClick={() => setFilters(f => ({ ...f, dateFrom: '', dateTo: '' }))}
                  className="text-slate-500 hover:text-slate-300 px-1"
                  title="Clear date range"
                >×</button>
              )}
            </div>
          </div>

          <BetTable
            bets={bets}
            league={filters.league}
            market={filters.market}
            status={filters.status}
            dateFrom={filters.dateFrom}
            dateTo={filters.dateTo}
          />

        </div>

        <div className="space-y-4">
          <CalculatorPanel defaultEdge={defaultEdge} />
        </div>
      </div>

      {/* Charts always show both paper + cash side-by-side regardless of the
          top toggle — comparing the two views is the whole point. */}
      <div className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
        <PortfolioCharts
          title="Paper trade"
          tone="paper"
          bets={paperBets}
          startingBankroll={startingBankroll}
        />
        <PortfolioCharts
          title="Cash trade"
          tone="cash"
          bets={cashBets}
          startingBankroll={startingBankroll}
        />
      </div>

      <div className="mt-4">
        <KellyProjectionPanel projection={projection} startingBankroll={startingBankroll} />

        <div className="bg-ink-900 border border-ink-700 rounded-xl p-4">
          <div className="text-sm font-medium mb-3">Return scenarios (default inputs)</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {projection?.scenarios?.map(s => <ScenarioCard key={s.name} s={s} />)}
          </div>
        </div>
      </div>
    </div>
  )
}
