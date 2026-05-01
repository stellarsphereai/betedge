import { useEffect, useState } from 'react'
import { api } from '../api'

// Section names emitted by the system prompt — order is significant.
const SECTIONS = [
  { key: 'TEAM FORM',     label: 'Team form' },
  { key: 'XG ANALYSIS',   label: 'xG analysis' },
  { key: 'MODEL INPUTS',  label: 'Model inputs' },
  { key: 'BET VERDICTS',  label: 'Bet verdicts' },
  { key: 'ANOMALY FLAGS', label: 'Anomaly flags' },
  { key: 'FINAL VERDICT', label: 'Final verdict' },
]

function parseSections(text) {
  const map = {}
  if (!text) return map
  const positions = []
  for (const s of SECTIONS) {
    const re = new RegExp(`(^|\\n)\\s*${s.key.replace(/ /g, '[ /]')}\\s*:?`, 'i')
    const match = re.exec(text)
    if (match) positions.push({ key: s.key, start: match.index + match[0].length })
  }
  positions.sort((a, b) => a.start - b.start)
  for (let i = 0; i < positions.length; i++) {
    const start = positions[i].start
    const end = i + 1 < positions.length ? positions[i + 1].start - SECTIONS.find(s => s.key === positions[i + 1].key).key.length - 2 : text.length
    map[positions[i].key] = text.slice(start, Math.max(start, end)).trim()
  }
  return map
}

function flagTone(line) {
  const upper = line.toUpperCase()
  if (upper.includes('CRITICAL')) return { tone: 'bad', icon: '🚨' }
  if (upper.includes('WARNING')) return { tone: 'warn', icon: '⚠️' }
  if (upper.includes('INFO')) return { tone: 'accent', icon: 'ℹ️' }
  return { tone: 'neutral', icon: '•' }
}

function AnomalyFlagsBlock({ text }) {
  if (!text) return <div className="text-slate-500 text-xs">None.</div>
  const lines = text.split('\n').map(s => s.trim()).filter(Boolean)
  return (
    <div className="space-y-1.5">
      {lines.map((line, i) => {
        const { tone, icon } = flagTone(line)
        const cls = {
          bad: 'bg-bad-soft text-bad border-bad-soft',
          warn: 'bg-warn-soft text-warn border-warn-soft',
          accent: 'bg-accent-soft text-accent border-accent-soft',
          neutral: 'border-ink-700 text-slate-300',
        }[tone]
        return (
          <div key={i} className={`flex gap-2 text-xs px-2 py-1.5 rounded border ${cls}`}>
            <span>{icon}</span>
            <span className="flex-1">{line.replace(/^[-•]\s*/, '')}</span>
          </div>
        )
      })}
    </div>
  )
}

function CopyToClaudeCodeBox({ text, matchLabel }) {
  const [copied, setCopied] = useState(false)
  const instruction = `Claude flagged a critical issue in the BetEdge model for "${matchLabel}". Investigate and fix.\n\nClaude's analysis:\n${text}`
  async function copy() {
    try {
      await navigator.clipboard.writeText(instruction)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* noop */ }
  }
  return (
    <div className="mt-3 rounded-md border border-bad-soft bg-bad-soft/40 p-3">
      <div className="text-xs font-semibold text-bad mb-2">🚨 Critical issue flagged by Claude</div>
      <div className="text-[11px] text-slate-300 mb-2">
        Copy the instruction below and paste into Claude Code to investigate / fix.
      </div>
      <pre className="text-[11px] bg-ink-950 text-slate-200 p-2 rounded max-h-48 overflow-auto whitespace-pre-wrap">{instruction}</pre>
      <div className="flex gap-2 mt-2">
        <button
          onClick={copy}
          className="px-3 py-1 rounded text-xs font-medium bg-bad text-white hover:opacity-90"
        >
          {copied ? '✓ Copied' : 'Copy for Claude Code'}
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
          {data.critical_flags && (
            <CopyToClaudeCodeBox text={data.analysis_text} matchLabel={matchLabel} />
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
            {SECTIONS.map(s => {
              const body = sections[s.key]
              if (!body) return null
              const isAnomaly = s.key === 'ANOMALY FLAGS'
              return (
                <div
                  key={s.key}
                  className={`rounded-md border border-ink-700 bg-ink-950/60 p-3 ${s.key === 'FINAL VERDICT' ? 'md:col-span-2' : ''}`}
                >
                  <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">{s.label}</div>
                  {isAnomaly
                    ? <AnomalyFlagsBlock text={body} />
                    : <div className="text-xs text-slate-200 whitespace-pre-wrap leading-relaxed">{body}</div>}
                </div>
              )
            })}
          </div>

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
