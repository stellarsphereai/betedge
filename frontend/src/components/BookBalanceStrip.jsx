import { useEffect, useState } from 'react'
import { Wallet } from 'lucide-react'

function fmtMoney(n) {
  if (n == null) return '—'
  return `$${Number(n).toFixed(0)}`
}

const TONE_CLS = {
  ok:    'text-slate-300',
  amber: 'text-warn',
  red:   'text-bad',
}

export default function BookBalanceStrip({ refreshKey }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let active = true
    fetch('/book-balances')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { if (active) setData(d) })
      .catch(e => { if (active) setError(e.message) })
    return () => { active = false }
  }, [refreshKey])

  if (error) {
    return (
      <div className="text-xs text-bad mb-3">Balances unavailable — {error}</div>
    )
  }
  if (!data) return null

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs mb-4 px-3 py-2 bg-ink-900 border border-ink-700 rounded-lg">
      <Wallet size={14} className="text-slate-500" />
      <span className="text-slate-500">Balances:</span>
      {data.books.map(b => {
        const tone = TONE_CLS[b.warning_level] || 'text-slate-300'
        return (
          <span key={b.book_key} className="flex items-center gap-1">
            <span className="text-slate-500">{b.display_name}</span>
            <span className={`tabular-nums font-medium ${tone}`}>{fmtMoney(b.balance_usd)}</span>
          </span>
        )
      })}
      <span className="ml-auto text-slate-500">
        Total: <span className="text-slate-200 tabular-nums font-semibold">{fmtMoney(data.total)}</span>
      </span>
    </div>
  )
}
