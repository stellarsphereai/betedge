"""Morning digest email — plain-text, mobile-friendly.

Renders the same shape as the spec:
    === TODAY'S BETS ===
    [league] [home] vs [away] — [time]
    Bet: [outcome] at [odds] on [book]
    ...
    === BANKROLL STATUS ===
    Bankroll / ROI / CLV / accuracy / mode

Sending uses STARTTLS on port 587 with a Gmail app password.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger("arb.digest")


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%a %b %d, %H:%M UTC")
    except ValueError:
        return iso


_MEDALS = ["🥇 BEST BET", "🥈 SECOND BEST", "🥉 THIRD BEST"]
_LEAGUE_LABEL = {"epl": "EPL", "ucl": "UCL", "uel": "EL", "world_cup": "World Cup"}


def _filter_unflagged(bets_all: list[dict], excluded_ids: set[str]) -> list[dict]:
    def _is_flagged(b: dict) -> bool:
        if b.get("match_id") in excluded_ids:
            return True
        for f in b.get("anomaly_flags") or []:
            if f.get("excludes_bet") or f.get("downgrades_to_low"):
                return True
        return False
    return [b for b in bets_all if not _is_flagged(b)]


def _render_bet_block(bets: list[dict], max_bets: int, league_mode: str) -> list[str]:
    """Render the medal/team/bet block for one section. Empty list → '(none)'."""
    lines: list[str] = []
    if not bets:
        lines.append("(no +EV bets at the current edge threshold)")
        return lines
    for i, b in enumerate(bets[:max_bets]):
        bookmaker = b.get("best_book") or b.get("book")
        odds = b.get("best_odds") or b.get("decimal_odds")
        edge_pct = (b.get("edge") or 0) * 100
        timing = b.get("timing", "GREEN")
        stake = b.get("stake", 0)
        outcome_label = {"home": b.get("home_team"), "away": b.get("away_team"), "draw": "Draw"}.get(
            b["outcome"], b["outcome"]
        )
        medal = _MEDALS[i] if i < len(_MEDALS) else f"#{i + 1}"
        lg_key = (b.get("league") or league_mode).lower()
        lg_label = _LEAGUE_LABEL.get(lg_key, lg_key.upper())
        lines.append(f"\n{medal} — {lg_label}")
        lines.append(
            f"{b.get('home_team')} vs {b.get('away_team')} — {_fmt_time(b.get('commence_time'))}"
        )
        lines.append(f"Bet: {outcome_label} at {odds} on {bookmaker}")
        lines.append(f"Stake: ${stake:.0f} | Edge: {edge_pct:.2f}% | Timing: {timing}")
        lines.append(f"Model: {b.get('model_prob', 0)*100:.1f}% (own outcome)")
        lines.append(f"Confidence: {b.get('confidence', 'MEDIUM')}")
        lines.append("---")
    return lines


def render(
    ev_payload: dict,
    stats_payload: dict,
    max_bets: int = 3,
    week_payload: dict | None = None,
) -> tuple[str, str]:
    """Return (subject, body).

    `ev_payload['bets']` is the imminent-window picks ('today's matches').
    `week_payload['bets']` is the look-ahead picks (matches kicking off
    after the today window — typically next 24h to 7 days). Optional
    for backwards compatibility.
    """
    import anomaly  # local import keeps this module standalone-importable
    excluded_ids = anomaly.excluded_match_ids_today()
    excluded_today = anomaly.recent(limit=200)
    today_bets = _filter_unflagged((ev_payload or {}).get("bets", []) or [], excluded_ids)[:max_bets]
    week_bets = _filter_unflagged((week_payload or {}).get("bets", []) or [], excluded_ids)[:max_bets]
    # Don't show the same match twice across sections — the today section wins.
    today_match_ids = {b.get("match_id") for b in today_bets}
    week_bets = [b for b in week_bets if b.get("match_id") not in today_match_ids]

    league_mode = (stats_payload or {}).get("league_mode", "epl")
    bankroll = (stats_payload or {}).get("bankroll", 0)
    weekly = (stats_payload or {}).get("weekly", {}) or {}
    accuracy_data = (stats_payload or {}).get("accuracy", {}) or {}

    now = datetime.now(timezone.utc)
    today_short = f"{now.strftime('%b')} {now.day}"

    # Subject reflects today's section primarily; fall back to look-ahead if today is empty.
    leagues_in_top: list[str] = []
    seen: set[str] = set()
    for b in today_bets or week_bets:
        lg = (b.get("league") or "").lower()
        if lg and lg not in seen:
            seen.add(lg)
            leagues_in_top.append(_LEAGUE_LABEL.get(lg, lg.upper()))
    n_today = len(today_bets)
    if n_today:
        across = " + ".join(leagues_in_top) if leagues_in_top else ""
        subject = f"BetEdge — {today_short} — {n_today} bet{'s' if n_today != 1 else ''} ready across {across}".rstrip(" across ")
    elif week_bets:
        subject = f"BetEdge — {today_short} — no bets today; {len(week_bets)} look-ahead pick{'s' if len(week_bets) != 1 else ''}"
    else:
        subject = f"BetEdge — {today_short} — no +EV bets in window"

    lines: list[str] = ["=== BEST BETS — TODAY'S MATCHES ==="]
    lines.extend(_render_bet_block(today_bets, max_bets, league_mode))

    lines.append("")
    lines.append("=== BEST BETS — LOOKING AHEAD (next 7 days) ===")
    if week_payload is None:
        lines.append("(week-ahead view not provided)")
    else:
        lines.extend(_render_bet_block(week_bets, max_bets, league_mode))

    # Monitoring — bets with a real edge but not enough books pricing yet.
    monitoring = []
    for src in (ev_payload, week_payload):
        for b in (src or {}).get("monitoring", []) or []:
            monitoring.append(b)
    if monitoring:
        lines.append("")
        lines.append("=== MONITORING — waiting for more books ===")
        lines.append(f"({len(monitoring)} bet{'s' if len(monitoring) != 1 else ''} below coverage minimum — auto-promotes when more books price the line)")
        for b in monitoring[:5]:
            lg_key = (b.get("league") or league_mode).lower()
            lg_label = _LEAGUE_LABEL.get(lg_key, lg_key.upper())
            covered = b.get("book_coverage") or 0
            required = b.get("min_book_coverage") or 4
            edge_pct = (b.get("edge") or 0) * 100
            line_str = f" {b['market_line']}" if b.get("market_line") is not None else ""
            lines.append(
                f"  • [{lg_label}] {b.get('home_team')} vs {b.get('away_team')} — "
                f"{(b.get('market') or 'h2h')} {b.get('outcome')}{line_str} "
                f"@ {b.get('best_book') or b.get('book')} {b.get('best_odds') or b.get('decimal_odds')}  "
                f"({edge_pct:.1f}% edge, {covered}/7 books, need {required})"
            )

    lines.append("")
    lines.append("=== BANKROLL STATUS ===")
    lines.append(f"Bankroll: ${bankroll:.2f}")
    lines.append(f"Week ROI: {(weekly.get('roi') or 0) * 100:.2f}%  ({weekly.get('total_bets', 0)} bets)")
    avg_clv = weekly.get("avg_clv")
    lines.append(f"CLV Average: {avg_clv if avg_clv is not None else 'n/a'}")
    win_rate = accuracy_data.get("win_rate")
    n_pred = accuracy_data.get("n_predictions", 0)
    if win_rate is not None and n_pred > 0:
        lines.append(f"Model accuracy: {win_rate * 100:.1f}% (last {n_pred} predictions)")
    else:
        lines.append("Model accuracy: not enough settled predictions yet")
    mode_label = "WORLD CUP LIVE" if league_mode == "world_cup" else "EPL PAPER TRADE"
    lines.append(f"Mode: {mode_label}")

    # Cash restriction state — render BEFORE the rollup so the user sees
    # the rules first, then the numbers. Always rendered (even pre-cash)
    # because the restrictions exist regardless of whether bets are placed.
    try:
        import cash_restrictions
        rs = cash_restrictions.restriction_status()
        gm = rs["goal_market_progress"]
        lines.append("")
        lines.append("=== REAL MONEY RESTRICTIONS ===")
        lines.append("Active restrictions:")
        lines.append("  - Goal markets (BTTS / Totals): paper only")
        lines.append("  - H2H Draw: paper only")
        lines.append(f"  - Min cash edge: {rs['min_cash_edge']*100:.0f}%")
        lines.append(f"  - Daily cash loss cap: ${rs['daily_loss_cap_usd']:.0f}")
        lines.append(f"  - Paper trade required first on every cash bet")
        if rs["daily_cap_hit"]:
            lines.append("")
            lines.append(f"  ⚠️ DAILY CAP HIT today (${rs['todays_cash_pnl']:+.0f}) — cash betting locked")
        lines.append("")
        lines.append("Goal markets unlock when paper trades show:")
        wr = gm["win_rate"]
        wr_str = f"{wr*100:.0f}%" if wr is not None else "n/a"
        clv_str = f"{gm['avg_clv']:+.3f}" if gm["avg_clv"] is not None else "n/a"
        lines.append(f"  - {gm['settled']}/{gm['target_settled']} settled  (need ≥{gm['target_settled']})")
        lines.append(f"  - Win rate {wr_str}  (need ≥{int(gm['target_win_rate']*100)}%)")
        lines.append(f"  - Avg CLV {clv_str}  (need ≥0)")
        unlock = "🔓 UNLOCKED" if gm["unlocked"] else "🔒 Still locked"
        lines.append(f"  Status: {unlock}")
    except Exception:
        pass

    # Real money rollup — only render when at least one cash bet has been
    # logged. During the paper-only phase this section is silent so the
    # digest doesn't pad with empty zeroes.
    rm = (stats_payload or {}).get("real_money") or {}
    if (rm.get("settled") or 0) > 0 or (rm.get("open") or 0) > 0:
        lines.append("")
        lines.append("=== REAL MONEY STATUS ===")
        lines.append(f"Total deployed: ${rm.get('deployed') or 0:.2f}")
        pnl = rm.get("realized_pnl") or 0
        pct = rm.get("realized_pct")
        pct_str = f" ({pct*100:+.1f}% of bankroll)" if pct is not None else ""
        lines.append(f"Total profit:   {pnl:+.2f}{pct_str}")
        settled = rm.get("settled") or 0
        won = rm.get("won") or 0
        if settled > 0:
            lines.append(f"Win rate:       {won/settled*100:.1f}%  ({won}–{rm.get('lost') or 0}, {settled} settled)")
        if rm.get("open"):
            lines.append(f"Open bets:      {rm['open']}")
        clv_r = rm.get("avg_clv")
        if clv_r is not None:
            lines.append(f"CLV average:    {clv_r:+.3f}")

    # Per-book account balances + low-balance warnings.
    try:
        import book_balance
        balances = book_balance.get_all()
    except Exception:
        balances = []
    if balances:
        lines.append("")
        lines.append("=== ACCOUNT BALANCES ===")
        col_width = max(len(b["display_name"]) for b in balances) + 1
        for b in balances:
            lines.append(f"  {b['display_name']:<{col_width}} ${b['balance_usd']:.2f}")
        total = sum(float(b["balance_usd"] or 0.0) for b in balances)
        lines.append(f"  {'Total':<{col_width}} ${total:.2f}")

        low = [b for b in balances if b["warning_level"] != "ok"]
        if low:
            lines.append("")
            lines.append("Low balance warnings:")
            for b in low:
                tag = "CRITICAL" if b["warning_level"] == "red" else "low"
                lines.append(f"  - {b['display_name']}: ${b['balance_usd']:.2f} ({tag} — top up)")

    if excluded_today:
        lines.append("")
        lines.append("=== ANOMALIES DETECTED ===")
        lines.append(
            f"({len(excluded_today)} flag{'s' if len(excluded_today) != 1 else ''} today — "
            "predictions/bets these touched were excluded from recommendations)"
        )
        # One line per anomaly, grouped by match for readability.
        seen_keys: set[tuple[str, str]] = set()
        for a in excluded_today:
            key = (a.get("match_id") or "", a.get("anomaly_type") or "")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            home = a.get("home_team") or "?"
            away = a.get("away_team") or "?"
            atype = a.get("anomaly_type") or "?"
            desc = a.get("description") or ""
            lines.append(f"- {home} vs {away}: {atype} — {desc}")

    # Self-eval health summary — pulled from prediction_results + bias_log
    # which the 23:55 cron writes nightly. Falls back to "warming up" when no
    # results have settled yet.
    try:
        import self_eval
        health = self_eval.health_summary(league=None)
    except Exception:
        health = None
    if health:
        last10 = health["rolling"].get("last_10")
        last20 = health["rolling"].get("last_20")
        baseline = health.get("baseline") or {}
        status = health.get("status")
        delta_brier = health.get("delta_brier")
        status_label = {
            "on_track": "On track",
            "monitor":  "Monitor",
            "review":   "Review needed",
            "no_data":  "Warming up — no settled predictions yet",
        }.get(status, status or "")
        lines.append("")
        lines.append("=== MODEL PERFORMANCE ===")
        if last10:
            lines.append(
                f"Last 10 matches: {last10['correct']}/{last10['n']} correct "
                f"({last10['winner_accuracy']*100:.1f}%)"
            )
        if last20:
            lines.append(
                f"Last 20 matches: {last20['correct']}/{last20['n']} correct "
                f"({last20['winner_accuracy']*100:.1f}%)"
            )
        baseline_brier = baseline.get("avg_brier")
        if last20 and baseline_brier is not None:
            lines.append(
                f"Avg Brier: {last20['avg_brier']:.4f} (baseline: {baseline_brier:.4f}"
                f"{f', Δ {delta_brier:+.4f}' if delta_brier is not None else ''})"
            )
        lines.append(f"Status: {status_label}")

        lines.append("")
        lines.append("=== BIAS ALERTS ===")
        alerts = health.get("alerts") or []
        if not alerts:
            lines.append("No bias detected ✅")
        else:
            for a in alerts:
                check = a.get("check_name") or "?"
                desc = a.get("description") or ""
                fix = a.get("suggested_adjustment")
                lines.append(f"- [{check}] {desc}")
                if fix:
                    lines.append(f"    suggested: {fix}")

    return subject, "\n".join(lines)


def render_weekly_accuracy(snapshot_payload: dict) -> tuple[str, str]:
    """`snapshot_payload` is the dict returned by calibrate.write_weekly_snapshot()."""
    date = snapshot_payload.get("date", "")
    subject = f"BetEdge NY · weekly accuracy snapshot — {date}"
    lines = [f"Weekly accuracy snapshot · {date}", "=" * 50, ""]
    for snap in snapshot_payload.get("snapshots", []):
        league = snap["league"].upper()
        n = snap["n_settled"]
        if n == 0:
            lines.append(f"[{league}]  no settled predictions yet")
        else:
            lines.append(
                f"[{league}]  n={n:<3}  Brier={snap['avg_brier']:.4f}  "
                f"win_rate={snap['win_rate']*100:.1f}%"
            )
        if snap.get("n_clv_samples"):
            avg_clv = snap.get("avg_clv")
            avg_str = f"{avg_clv:+.2f}" if avg_clv is not None else "—"
            lines.append(f"           CLV n={snap['n_clv_samples']}  avg={avg_str}")
        lines.append("")
    lines.append("Trend: query accuracy_snapshots table or hit /admin/accuracy-history.")
    return subject, "\n".join(lines)


def render_wc_phase_report(check_payload: dict) -> tuple[str, str]:
    """`check_payload` is the dict returned by wc_calibrate.run_post_phase_check()."""
    phase = check_payload.get("phase", "?")
    n = check_payload.get("n_settled", 0)
    n_min = check_payload.get("n_min_for_grid_search", 30)
    snap = check_payload.get("snapshot", {})
    subject = f"BetEdge NY · World Cup phase report — {phase}"
    lines = [
        f"World Cup phase report · phase: {phase}",
        "=" * 50,
        f"Settled WC predictions: {n}",
        f"Eligibility threshold (WC-specific): {n_min}",
        "",
    ]
    if snap.get("avg_brier") is not None:
        lines.append(f"Avg Brier   : {snap['avg_brier']:.4f}")
        lines.append(f"Win rate    : {snap['win_rate']*100:.1f}%")
    else:
        lines.append("No settled fixtures yet — accuracy can't be scored.")
    if snap.get("n_clv_samples"):
        avg_clv = snap.get("avg_clv")
        avg_str = f"{avg_clv:+.2f}" if avg_clv is not None else "—"
        lines.append(f"CLV samples : {snap['n_clv_samples']}  avg={avg_str}")
    lines.append("")
    if check_payload.get("eligible"):
        grid = check_payload.get("grid_search") or {}
        if grid.get("implemented"):
            lines.append("ELIGIBLE for grid search — recommendation in attached payload.")
        else:
            lines.append(f"ELIGIBLE for grid search — engine not yet wired ({grid.get('note','')})")
    else:
        lines.append("Below grid-search threshold — gathering data only.")
    lines.append("")
    lines.append("WC params live in model_params_wc.json (when present). Never auto-applied.")
    return subject, "\n".join(lines)


def render_monthly_calibration(check_payload: dict) -> tuple[str, str]:
    """`check_payload` is the dict returned by calibrate.run_monthly_calibration_check()."""
    n_min = check_payload.get("n_min_for_grid_search")
    subject = "BetEdge NY · monthly calibration check"
    lines = [
        "Monthly calibration check",
        "=" * 50,
        f"Threshold for grid search: n ≥ {n_min} settled predictions per league.",
        "",
    ]
    for r in check_payload.get("results", []):
        league = r["league"].upper()
        n = r["n_settled"]
        if r["eligible_for_grid_search"]:
            grid = r.get("grid_search") or {}
            if grid.get("implemented"):
                lines.append(f"[{league}]  ELIGIBLE  n={n} → recommendation: see grid_search payload")
            else:
                note = grid.get("note", "")
                lines.append(f"[{league}]  ELIGIBLE  n={n} → grid search engine not yet wired ({note})")
        else:
            lines.append(f"[{league}]  GATHERING  n={n}/{n_min}  (need {r['shortfall']} more)")
    lines.append("")
    lines.append("Recommendations are NEVER auto-applied. Review and edit model.py manually.")
    return subject, "\n".join(lines)


def send(subject: str, body: str, to_addr: str | None = None) -> dict:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = (os.getenv("SMTP_PASS", "") or "").replace(" ", "")
    to_addr = to_addr or os.getenv("DIGEST_EMAIL", "") or user

    if not (user and password and to_addr):
        return {"sent": False, "reason": "SMTP_USER / SMTP_PASS / DIGEST_EMAIL not all set"}

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        return {"sent": False, "reason": f"SMTP auth failed: {e.smtp_code} {e.smtp_error.decode(errors='ignore')}"}
    except Exception as e:
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}

    return {"sent": True, "to": to_addr, "subject": subject}
