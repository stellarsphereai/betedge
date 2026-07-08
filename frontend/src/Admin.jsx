import { useEffect, useState } from 'react'

/**
 * Password-protected emergency-only admin panel.
 * Routed via path `/admin` (set by App.jsx). Credentials are kept only in
 * sessionStorage so closing the tab clears them.
 */
export default function Admin() {
  const [auth, setAuth] = useState(() => sessionStorage.getItem('betedge_admin_auth') || '')
  const [user, setUser] = useState('admin')
  const [pass, setPass] = useState('')
  const [authError, setAuthError] = useState(null)
  const [health, setHealth] = useState(null)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState(null)

  async function tryAuth(e) {
    e?.preventDefault()
    setAuthError(null)
    const token = btoa(`${user}:${pass}`)
    try {
      const r = await fetch('/admin/health', { headers: { Authorization: `Basic ${token}` } })
      if (!r.ok) {
        setAuthError(r.status === 401 ? 'wrong username or password' : `HTTP ${r.status}`)
        return
      }
      sessionStorage.setItem('betedge_admin_auth', token)
      setAuth(token)
      setHealth(await r.json())
    } catch (err) {
      setAuthError(err.message)
    }
  }

  function logout() {
    sessionStorage.removeItem('betedge_admin_auth')
    setAuth('')
    setHealth(null)
  }

  async function adminFetch(path, options = {}) {
    const r = await fetch(path, {
      method: 'POST',
      ...options,
      headers: { Authorization: `Basic ${auth}`, ...(options.headers || {}) },
    })
    if (r.status === 401) {
      logout()
      throw new Error('session expired')
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`)
    return r.json()
  }

  async function loadHealth() {
    try {
      const r = await fetch('/admin/health', { headers: { Authorization: `Basic ${auth}` } })
      if (!r.ok) {
        if (r.status === 401) logout()
        return
      }
      setHealth(await r.json())
    } catch {}
  }

  useEffect(() => {
    if (!auth) return
    loadHealth()
    const id = setInterval(loadHealth, 15_000)
    return () => clearInterval(id)
  }, [auth])

  async function manualSync(league, force = true) {
    setBusy(true); setFeedback(null)
    try {
      const params = new URLSearchParams({ league, force: String(force) })
      const res = await adminFetch(`/admin/sync?${params}`)
      setFeedback({ kind: 'good', text: `${league}: ${JSON.stringify(res)}` })
      loadHealth()
    } catch (e) {
      setFeedback({ kind: 'bad', text: e.message })
    } finally {
      setBusy(false)
    }
  }

  async function schedulerControl(action) {
    setBusy(true); setFeedback(null)
    try {
      const res = await adminFetch(`/admin/scheduler/${action}`)
      setFeedback({ kind: 'good', text: `scheduler ${action}: ${JSON.stringify(res)}` })
      loadHealth()
    } catch (e) {
      setFeedback({ kind: 'bad', text: e.message })
    } finally {
      setBusy(false)
    }
  }

  async function runCalibrationAction(label, path) {
    setBusy(true); setFeedback(null)
    try {
      const res = await adminFetch(path)
      const text = JSON.stringify(res).slice(0, 240)
      setFeedback({ kind: 'good', text: `${label}: ${text}${text.length >= 240 ? '…' : ''}` })
      loadHealth()
    } catch (e) {
      setFeedback({ kind: 'bad', text: e.message })
    } finally {
      setBusy(false)
    }
  }

  async function captureClosingSweep() {
    if (!window.confirm('Sweep closing lines for every open bet whose match has kicked off? Hits the paid historical Odds API (~1 call per eligible bet).')) return
    setBusy(true); setFeedback(null)
    try {
      const res = await adminFetch('/bets/capture-closing-sweep')
      setFeedback({ kind: 'good', text: `closing-line sweep: ${JSON.stringify(res).slice(0, 240)}` })
    } catch (e) {
      setFeedback({ kind: 'bad', text: e.message })
    } finally {
      setBusy(false)
    }
  }

  if (!auth) {
    return (
      <div className="max-w-md mx-auto px-5 py-20">
        <div className="bg-ink-900 border border-ink-700 rounded-xl p-6">
          <h1 className="text-lg font-semibold mb-1">BetEdge NY · Admin</h1>
          <p className="text-xs text-slate-400 mb-4">Emergency manual controls. Don't share these credentials.</p>
          <form onSubmit={tryAuth} className="space-y-3">
            <input
              type="text"
              autoComplete="username"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              placeholder="username"
              className="w-full bg-ink-800 border border-ink-700 rounded-md px-3 py-2 text-sm text-slate-200 focus:border-accent focus:outline-none"
            />
            <input
              type="password"
              autoComplete="current-password"
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              placeholder="password"
              className="w-full bg-ink-800 border border-ink-700 rounded-md px-3 py-2 text-sm text-slate-200 focus:border-accent focus:outline-none"
            />
            <button type="submit" className="w-full bg-accent text-white rounded-md px-3 py-2 text-sm font-medium hover:opacity-90">
              Sign in
            </button>
          </form>
          {authError && <div className="text-xs text-bad mt-3">{authError}</div>}
          <div className="text-[11px] text-slate-500 mt-4">
            Goes back to the dashboard at <a href="/" className="text-accent hover:underline">/</a>.
          </div>
        </div>
      </div>
    )
  }

  const sched = health?.scheduler
  const quota = health?.quota
  const quotaPct = quota ? Math.round((quota.calls / quota.limit) * 100) : 0
  const quotaTone = !quota
    ? 'bg-slate-700/60 text-slate-300'
    : quota.exceeded
    ? 'bg-bad-soft text-bad'
    : quotaPct >= 80
    ? 'bg-warn-soft text-warn'
    : 'bg-good-soft text-good'

  return (
    <div className="max-w-3xl mx-auto px-5 py-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold">BetEdge NY · Admin</h1>
          <p className="text-xs text-slate-400">Emergency overrides only. Daily syncs run automatically — don't trigger manually unless something is broken.</p>
        </div>
        <button onClick={logout} className="text-xs text-slate-400 hover:text-slate-200">Log out</button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-6">
        <div className="bg-ink-900 border border-ink-700 rounded-xl p-4">
          <div className="text-xs uppercase tracking-wider text-slate-400 mb-2">Scheduler</div>
          <div className="text-sm">
            Status: <span className={sched?.running ? 'text-good font-semibold' : 'text-warn font-semibold'}>
              {sched?.running ? 'running' : 'stopped'}
            </span>
            {sched?.timezone && <span className="text-slate-500"> · tz {sched.timezone}</span>}
          </div>
          {sched?.jobs?.length > 0 && (
            <div className="mt-2 text-[11px] text-slate-400 space-y-0.5">
              {sched.jobs.map(j => (
                <div key={j.id} className="flex justify-between gap-2">
                  <span>{j.id}</span>
                  <span className="tabular-nums">{j.next_run ? new Date(j.next_run).toLocaleString() : '—'}</span>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-2 mt-3">
            <button onClick={() => schedulerControl('start')} disabled={busy} className="text-xs bg-good-soft text-good border border-good/40 rounded px-2.5 py-1 hover:opacity-90 disabled:opacity-50">Start</button>
            <button onClick={() => schedulerControl('stop')} disabled={busy} className="text-xs bg-bad-soft text-bad border border-bad/40 rounded px-2.5 py-1 hover:opacity-90 disabled:opacity-50">Stop</button>
          </div>
        </div>

        <div className="bg-ink-900 border border-ink-700 rounded-xl p-4">
          <div className="text-xs uppercase tracking-wider text-slate-400 mb-2">API quota (today)</div>
          <div className={`inline-block text-xs font-semibold px-2 py-0.5 rounded ${quotaTone}`}>
            {quota ? `${quota.calls} / ${quota.limit} (${quotaPct}%)` : '—'}
          </div>
          {quota && (
            <div className="mt-2 text-[11px] text-slate-400">
              <div>warn at: {quota.warn_threshold}</div>
              <div>warning sent: {quota.warning_sent ? 'yes' : 'no'}</div>
              <div>remaining: {quota.remaining}</div>
              <div>blocking syncs: {quota.exceeded ? 'YES' : 'no'}</div>
            </div>
          )}
        </div>
      </div>

      <details open className="bg-ink-900 border border-ink-700 rounded-xl p-4 mb-4 group">
        <summary className="text-xs uppercase tracking-wider text-slate-400 cursor-pointer select-none flex items-center justify-between">
          <span>Playbook · what each button does &amp; when to run it</span>
          <span className="text-[10px] text-slate-500 group-open:hidden">click to expand</span>
          <span className="text-[10px] text-slate-500 hidden group-open:inline">click to collapse</span>
        </summary>
        <p className="text-[11px] text-slate-400 mt-3 mb-3">
          Almost everything is automated. The cron table in the Scheduler card shows what runs and when (UTC). Manual buttons are for emergencies, ad-hoc checks, and the one-off WC pre-tournament calibration.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] text-slate-300 border-collapse">
            <thead className="text-slate-500 text-left">
              <tr>
                <th className="py-1.5 pr-3 font-medium border-b border-ink-700">Button</th>
                <th className="py-1.5 pr-3 font-medium border-b border-ink-700">What it does</th>
                <th className="py-1.5 font-medium border-b border-ink-700">When to run</th>
              </tr>
            </thead>
            <tbody className="align-top">
              <tr>
                <td className="py-1.5 pr-3 text-good border-b border-ink-700/50">Scheduler · Start / Stop</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">Toggles the in-process APScheduler. All auto cron jobs (syncs, digest, P&amp;L, WC nightly) freeze when stopped.</td>
                <td className="py-1.5 border-b border-ink-700/50">Almost never. The scheduler starts itself when the server boots. Only stop it if you're about to take the server down on purpose — and start it again afterward.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-accent border-b border-ink-700/50">Force-sync EPL / UCL / UEL / world_cup</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">Pulls fresh fixtures + xG + injuries + top-scorers from API-Football, ignoring the 6h cache. Re-runs the model and upserts predictions. Costs ~30-50 quota calls per league.</td>
                <td className="py-1.5 border-b border-ink-700/50">Almost never. Each league pulls fresh data overnight on its own (between midnight and 3am NY). Use this only if the dashboard looks wrong or a fixture you expect is missing. Check the API-quota card first — each force-sync eats 30-50 calls.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-accent border-b border-ink-700/50">Capture closing lines now</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">For every open bet whose match has already kicked off, fetches the historical Odds API snapshot at kickoff time and writes <code className="text-slate-500">closing_odds</code> + <code className="text-slate-500">clv</code>. Idempotent. Costs ~1 paid Odds API call per eligible bet.</td>
                <td className="py-1.5 border-b border-ink-700/50">Every night at 11:55pm NY on its own. Click only if you want CLV right after a match kicks off — otherwise just wait for the nightly sweep.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-accent border-b border-ink-700/50">Run accuracy snapshot</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">For each regular league (EPL/UCL/UEL): count settled predictions, compute Brier + win rate, write a row to <code className="text-slate-500">accuracy_snapshots</code>, email a weekly digest.</td>
                <td className="py-1.5 border-b border-ink-700/50">Sunday mornings on its own. Click only if you want to peek at the numbers mid-week instead of waiting.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-accent border-b border-ink-700/50">Run monthly calibration check</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">Per regular league: counts settled predictions; if n ≥ 100, runs the grid search and emails the recommendation (never auto-applies). Surfaces the eligibility gate when n is below threshold.</td>
                <td className="py-1.5 border-b border-ink-700/50">First of the month, automatically. Click only if you want to see the recommendation early.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-warn border-b border-ink-700/50">Take WC snapshot</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">One-off WC-only accuracy snapshot + phase detection. Writes a row to <code className="text-slate-500">accuracy_snapshots</code>.</td>
                <td className="py-1.5 border-b border-ink-700/50">Every night at 4:30am NY on its own. Click only if you want today's WC numbers right now.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-warn border-b border-ink-700/50">UCL proxy · review grid</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">Grid-searches <code className="text-slate-500">RHO × KO_DAMPING</code> (30 combos) against cached UCL 2023-24 knockouts. Returns ranked top-5 + Brier improvement vs default. <em>Does not persist.</em></td>
                <td className="py-1.5 border-b border-ink-700/50">Once, a few weeks before the World Cup starts. You'll get an email reminder on May 25, 2026 telling you to do this.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-warn border-b border-ink-700/50">UCL proxy · apply best</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">Writes the lowest-Brier params from the grid to <code className="text-slate-500">model_params_wc.json</code>. Next <code className="text-slate-500">/admin/sync?league=world_cup</code> picks them up automatically.</td>
                <td className="py-1.5 border-b border-ink-700/50">Right after you click "review grid" and the result looks good — meaning the recommendation beats the defaults (positive improvement number) and the top 5 results are similar to each other, not wildly different.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-warn border-b border-ink-700/50">Group-stage report</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">WC snapshot + email tagged <code className="text-slate-500">phase=group_stage</code>. Includes eligibility flag (n ≥ 30 for grid search).</td>
                <td className="py-1.5 border-b border-ink-700/50">Automatically when the group stage ends. Click only if you didn't get that email.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-warn border-b border-ink-700/50">Knockouts report</td>
                <td className="py-1.5 pr-3 border-b border-ink-700/50">Same as group-stage but tagged <code className="text-slate-500">phase=knockouts</code>.</td>
                <td className="py-1.5 border-b border-ink-700/50">Automatically when the knockout rounds end. Click only if you didn't get that email.</td>
              </tr>
              <tr>
                <td className="py-1.5 pr-3 text-warn">Concluded · final review</td>
                <td className="py-1.5 pr-3">Final WC snapshot + wrap-up email. Last chance to sanity-check params before they sit dormant for 4 years.</td>
                <td className="py-1.5">Right after the World Cup final, once the result is in. This is the one button you're expected to click manually.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>

      <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 mb-4">
        <div className="text-xs uppercase tracking-wider text-slate-400 mb-2">Manual sync (force-refresh)</div>
        <p className="text-[11px] text-slate-400 mb-3">
          Pulls fresh data from API-Football for the chosen league, ignoring the cache TTL. Each
          manual sync uses ~30-50 calls of today's quota.
        </p>
        <div className="flex flex-wrap gap-2">
          {['epl', 'ucl', 'uel', 'world_cup', 'la_liga'].map(l => (
            <button
              key={l}
              onClick={() => manualSync(l, true)}
              disabled={busy || quota?.exceeded}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-accent text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Force-sync {l.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="mt-4 pt-3 border-t border-ink-800">
          <div className="text-[11px] text-slate-400 mb-2">
            Closing-line capture · sweeps every open bet whose match has kicked off and writes the closing odds + CLV. Auto-runs at 23:55 NY; click here to fire it now.
          </div>
          <button
            onClick={captureClosingSweep}
            disabled={busy}
            className="text-xs bg-ink-800 border border-ink-700 hover:border-accent text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
          >
            Capture closing lines now
          </button>
        </div>
        {feedback && (
          <div className={`mt-3 text-[11px] break-all ${feedback.kind === 'good' ? 'text-good' : 'text-bad'}`}>
            {feedback.text}
          </div>
        )}
      </div>

      <div className="bg-ink-900 border border-ink-700 rounded-xl p-4 mb-4">
        <div className="text-xs uppercase tracking-wider text-slate-400 mb-2">
          Calibration · manual triggers
        </div>
        <p className="text-[11px] text-slate-400 mb-4">
          The cron handles routine snapshots automatically. Use these only when you
          want to fire something on-demand. Buttons marked <span className="text-warn">stub</span> are
          wired but await the model parameter refactor.
        </p>

        <div className="border-l-2 border-accent/60 pl-3 mb-4">
          <div className="text-xs font-semibold text-slate-200 mb-0.5">
            Regular leagues <span className="text-slate-500 font-normal">— EPL / UCL / UEL</span>
          </div>
          <div className="text-[11px] text-slate-500 mb-2">
            Auto: weekly snapshot Sun 04:00 NY · monthly check 1st 04:00 NY
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => runCalibrationAction('accuracy snapshot', '/admin/accuracy-snapshot')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-accent text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Run accuracy snapshot
            </button>
            <button
              onClick={() => runCalibrationAction('monthly calibration check', '/admin/calibrate')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-accent text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Run monthly calibration check
            </button>
          </div>
        </div>

        <div className="border-l-2 border-warn/60 pl-3">
          <div className="text-xs font-semibold text-slate-200 mb-0.5">
            World Cup <span className="text-slate-500 font-normal">— separate cadence</span>
          </div>
          <div className="text-[11px] text-slate-500 mb-2">
            Auto: nightly snapshot 04:30 NY · phase-transition emails on group→KO and KO→concluded
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => runCalibrationAction('WC snapshot', '/admin/wc/snapshot')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-warn text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Take WC snapshot
            </button>
            <button
              onClick={() => runCalibrationAction('WC pre-tournament proxy (review)', '/admin/wc/calibrate-from-ucl-proxy?apply=false')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-warn text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              UCL proxy · review grid
            </button>
            <button
              onClick={() => {
                if (!window.confirm('Persist best-Brier params to model_params_wc.json? Next WC sync will use them.')) return
                runCalibrationAction('WC pre-tournament proxy (apply)', '/admin/wc/calibrate-from-ucl-proxy?apply=true')
              }}
              disabled={busy}
              className="text-xs bg-warn/20 border border-warn/60 hover:bg-warn/30 text-warn rounded px-3 py-1.5 disabled:opacity-50"
            >
              UCL proxy · apply best
            </button>
            <button
              onClick={() => runCalibrationAction('WC group-stage report', '/admin/wc/post-phase-check?phase=group_stage')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-warn text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Group-stage report
            </button>
            <button
              onClick={() => runCalibrationAction('WC knockouts report', '/admin/wc/post-phase-check?phase=knockouts')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-warn text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Knockouts report
            </button>
            <button
              onClick={() => runCalibrationAction('WC final review', '/admin/wc/post-phase-check?phase=concluded')}
              disabled={busy}
              className="text-xs bg-ink-800 border border-ink-700 hover:border-warn text-slate-200 rounded px-3 py-1.5 disabled:opacity-50"
            >
              Concluded · final review
            </button>
          </div>
        </div>
      </div>

      <div className="text-[11px] text-slate-500">
        Back to dashboard: <a href="/" className="text-accent hover:underline">/</a>
      </div>
    </div>
  )
}
