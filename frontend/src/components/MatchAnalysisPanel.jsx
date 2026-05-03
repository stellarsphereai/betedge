import { useEffect, useState } from 'react'
import { api } from '../api'

// Section keys match the headings emitted by the system prompt (without
// the leading emoji). Order here is the display order. MY RECOMMENDATION
// is rendered separately with bigger / bolder treatment because it's the
// actionable bottom line.
const SECTIONS = [
  { key: 'QUICK SUMMARY',          label: '🔍 Quick summary' },
  { key: 'TEAM FORM',              label: '⚽ Team form' },
  { key: 'XG CHECK',               label: '🔢 xG check' },
  { key: 'BET BY BET ANALYSIS',    label: '✅ Bet by bet analysis' },
  { key: 'ANOMALY FLAGS',          label: '🚨 Anomaly flags' },
]
const RECOMMENDATION_KEY = 'MY RECOMMENDATION'

// Line-based parser. A header line is one that contains a section key and is
// short enough to be a heading (not a paragraph that happens to mention the
// phrase). Tolerates emoji prefixes, markdown, and trailing parentheticals.
function parseSections(text) {
  if (!text) return {}
  const allKeys = [...SECTIONS.map(s => s.key), RECOMMENDATION_KEY]
  const lines = text.split('\n')
  const result = {}
  let currentKey = null
  let buffer = []
  const flush = () => {
    if (currentKey && !result[currentKey]) {
      result[currentKey] = buffer.join('\n').trim()
    }
  }
  for (const line of lines) {
    const upper = line.toUpperCase()
    // Strip leading non-word chars (emojis, ═, *, #, whitespace) before matching.
    const stripped = upper.replace(/^[^A-Z0-9]+/, '')
    let matched = null
    for (const k of allKeys) {
      if (stripped.startsWith(k)) {
        const rest = line.slice(line.toUpperCase().indexOf(k) + k.length).trim()
        if (rest.length <= 60) {
          matched = k
          break
        }
      }
    }
    if (matched) {
      flush()
      currentKey = matched
      buffer = []
    } else if (currentKey) {
      // Skip the equals-line dividers around MY RECOMMENDATION
      if (/^[═=]+$/.test(line.trim())) continue
      buffer.push(line)
    }
  }
  flush()
  return result
}

function CopyToClaudeCodeBox({ problemText, matchLabel, onSkip }) {
  const [copied, setCopied] = useState(false)
  const [hidden, setHidden] = useState(false)
  const instruction = `BetEdge model issue flagged for "${matchLabel}":\n\n${problemText}\n\nInvestigate the cause and fix it.`
  async function copy() {
    try {
      await navigator.clipboard.writeText(instruction)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* noop */ }
  }
  function skip() {
    setHidden(true)
    onSkip?.()
  }
  if (hidden) return null
  return (
    <div className="mt-3 rounded-md border border-bad-soft bg-bad-soft/40 p-3">
      <div className="text-xs font-semibold text-bad mb-2">🚨 Issue flagged — Fix this?</div>
      <div className="text-[11px] text-slate-300 mb-2">
        Copy the instruction and paste it into Claude Code to investigate and fix.
      </div>
      <pre className="text-[11px] bg-ink-950 text-slate-200 p-2 rounded max-h-48 overflow-auto whitespace-pre-wrap">{instruction}</pre>
      <div className="flex gap-2 mt-2">
        <button
          onClick={copy}
          className="px-3 py-1 rounded text-xs font-medium bg-bad text-white hover:opacity-90"
        >
          {copied ? '✓ Copied — paste into Claude Code' : 'Yes — fix it'}
        </button>
        <button
          onClick={skip}
          className="px-3 py-1 rounded text-xs font-medium bg-ink-800 text-slate-300 hover:bg-ink-700 border border-ink-700"
        >
          Skip
        </button>
      </div>
    </div>
  )
}

export default function MatchAnalysisPanel({ matchId, matchLabel, onClose }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function load(force = false) {
    setLoading(true); setError(null)
    try {
      const r = await fetch(`/match-analysis/${encodeURIComponent(matchId)}${force ? '?force=true' : ''}`)
      const body = await r.json().catch(() => ({}))
      if (!r.ok) {
        throw new Error(body?.detail || `HTTP ${r.status}`)
      }
      setData(body)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(false) }, [matchId])

  const sections = parseSections(data?.analysis_text || '')

  return (
    <div className="mt-3 border border-accent/40 rounded-xl bg-ink-900/80 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-accent">AI Analysis</span>
          {data?.cached && <span className="text-[10px] text-slate-500 px-1.5 py-0.5 rounded bg-ink-800">cached</span>}
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => window.print()}
            className="text-[11px] text-slate-400 hover:text-slate-200"
          >Print</button>
          <button
            onClick={() => load(true)}
            disabled={loading}
            className="text-[11px] text-slate-400 hover:text-slate-200 disabled:opacity-50"
          >Re-run</button>
          <button
            onClick={onClose}
            className="text-[11px] text-slate-400 hover:text-slate-200"
          >Hide ▲</button>
        </div>
      </div>

      {loading && (
        <div className="text-sm text-slate-400">🤔 Analyzing {matchLabel}…</div>
      )}

      {error && (
        <div className="text-sm text-bad">
          Analysis unavailable — {error}
        </div>
      )}

      {data && !loading && (
        <>
          {data.critical_flags && sections['ANOMALY FLAGS'] && (
            <CopyToClaudeCodeBox
              problemText={sections['ANOMALY FLAGS'].replace(/Fix this issue\?[^\n]*/i, '').trim()}
              matchLabel={matchLabel}
            />
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
            {SECTIONS.map(s => {
              const body = sections[s.key]
              if (!body) return null
              const isAnomaly = s.key === 'ANOMALY FLAGS'
              const isWide = isAnomaly || s.key === 'QUICK SUMMARY' || s.key === 'BET BY BET ANALYSIS'
              let cleanedBody = isAnomaly
                ? body.replace(/Fix this issue\?[^\n]*/i, '').trim()
                : body
              // Anomaly section status parsing: hide if STATUS: OK so the
              // panel doesn't show a near-empty box on the happy path.
              let anomalyStatus = null
              if (isAnomaly) {
                const m = cleanedBody.match(/STATUS:\s*(OK|WARNING|CRITICAL)\b/i)
                anomalyStatus = m ? m[1].toUpperCase() : null
                if (anomalyStatus === 'OK') return null
                // For WARNING / CRITICAL, drop the bare token line
                cleanedBody = cleanedBody.replace(/^.*STATUS:\s*\w+.*$/im, '').trim()
              }
              const tone = isAnomaly && anomalyStatus === 'CRITICAL'
                ? 'border-bad-soft bg-bad-soft/20'
                : isAnomaly && anomalyStatus === 'WARNING'
                  ? 'border-warn-soft bg-warn-soft/20'
                  : 'border-ink-700 bg-ink-950/60'
              return (
                <div
                  key={s.key}
                  className={`rounded-md border p-3 ${tone} ${isWide ? 'md:col-span-2' : ''}`}
                >
                  <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">{s.label}</div>
                  <div className="text-xs text-slate-200 whitespace-pre-wrap leading-relaxed">{cleanedBody}</div>
                </div>
              )
            })}
          </div>

          {sections[RECOMMENDATION_KEY] && (
            <div className="mt-4 rounded-xl border-2 border-accent bg-gradient-to-br from-accent-soft to-ink-900/60 p-4">
              <div className="flex items-center gap-2 mb-2 pb-2 border-b border-accent/40">
                <span className="text-lg">🎯</span>
                <span className="text-sm font-bold tracking-wider text-accent uppercase">My Recommendation</span>
              </div>
              <pre className="text-sm text-slate-100 whitespace-pre-wrap leading-relaxed font-sans">
                {sections[RECOMMENDATION_KEY]}
              </pre>
            </div>
          )}

          <div className="mt-3 text-[10px] text-slate-500 flex flex-wrap gap-x-3 gap-y-0.5">
            <span>Model: {data.claude_model_used}</span>
            <span>Tokens: {data.tokens_used} ({data.input_tokens} in / {data.output_tokens} out)</span>
            <span>Cost: ${(data.cost_usd ?? 0).toFixed(4)}</span>
            {data.cache_expires_at && <span>Cached until {new Date(data.cache_expires_at + 'Z').toLocaleTimeString()}</span>}
          </div>
        </>
      )}
    </div>
  )
}
