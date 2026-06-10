import { useEffect, useState } from 'react'
import { Wallet } from 'lucide-react'
import { api } from '../api'

function fmtMoney(n) {
  if (n == null) return '—'
  return `$${Number(n).toFixed(0)}`
}

const TONE_CLS = {
  ok:    'text-slate-300',
  amber: 'text-warn',
  red:   'text-bad',
}

function BalanceCell({ book, onSaved }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(String(Math.round(book.balance_usd ?? 0)))
  const [saving, setSaving] = useState(false)
  const tone = TONE_CLS[book.warning_level] || 'text-slate-300'

  async function commit() {
    const n = Number(value)
    if (!Number.isFinite(n) || n < 0) {
      setEditing(false)
      setValue(String(Math.round(book.balance_usd ?? 0)))
      return
    }
    if (n === Math.round(book.balance_usd ?? 0)) {
      setEditing(false)
      return
    }
    setSaving(true)
    try {
      await api.setBookBalance(book.book_key, n)
      onSaved?.()
    } catch (e) {
      console.error('setBookBalance failed', e)
    } finally {
      setSaving(false)
      setEditing(false)
    }
  }

  return (
    <span className="flex items-center gap-1">
      <span className="text-slate-500">{book.display_name}</span>
      {editing ? (
        <input
          autoFocus
          type="number"
          min="0"
          step="1"
          value={value}
          disabled={saving}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit()
            else if (e.key === 'Escape') {
              setValue(String(Math.round(book.balance_usd ?? 0)))
              setEditing(false)
            }
          }}
          className="w-16 px-1 py-0 bg-ink-800 border border-accent rounded text-slate-100 text-xs tabular-nums focus:outline-none"
        />
      ) : (
        <button
          onClick={() => { setValue(String(Math.round(book.balance_usd ?? 0))); setEditing(true) }}
          title="Click to edit"
          className={`tabular-nums font-medium ${tone} hover:underline cursor-pointer`}
        >
          {fmtMoney(book.balance_usd)}
        </button>
      )}
    </span>
  )
}

export default function BookBalanceStrip({ refreshKey }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    let active = true
    fetch('/book-balances')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { if (active) setData(d) })
      .catch(e => { if (active) setError(e.message) })
    return () => { active = false }
  }, [refreshKey, reloadTick])

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
      {data.books.map(b => (
        <BalanceCell key={b.book_key} book={b} onSaved={() => setReloadTick(t => t + 1)} />
      ))}
      <span className="ml-auto text-slate-500">
        Total: <span className="text-slate-200 tabular-nums font-semibold">{fmtMoney(data.total)}</span>
      </span>
    </div>
  )
}
