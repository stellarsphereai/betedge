"""Per-book bankroll tracking.

Each NY-licensed sportsbook account is tracked separately so the EV
recommender can cap its suggested stake by the actual money sitting on
that book. Settlement updates a book's balance via `apply_settled_bet`:
    won  → balance += profit  (where profit = stake * (odds - 1))
    lost → balance += profit  (where profit is negative -stake)
either way it's `balance += profit`.

Initial balances come from BALANCE_<BOOK> env vars and are seeded on
startup with `INSERT OR IGNORE`, so once a row exists subsequent
restarts don't reset it. To zero out and re-seed, delete the rows first.
"""
from __future__ import annotations

import logging
import os

from database import db

log = logging.getLogger("arb.book_balance")

# Tracked books — order is the display order in the UI/digest.
BOOKS: list[tuple[str, str, str]] = [
    # (book_key, display_name, env_var_suffix)
    ("fanduel",    "FanDuel",    "FANDUEL"),
    ("draftkings", "DraftKings", "DRAFTKINGS"),
    ("espnbet",    "ESPN Bet",   "ESPNBET"),
    ("fanatics",   "Fanatics",   "FANATICS"),
    ("ballybet",   "Bally Bet",  "BALLYBET"),
    ("betrivers",  "BetRivers",  "BETRIVERS"),
    ("caesars",    "Caesars",    "CAESARS"),
]

# Display-name → book_key reverse map (for normalising the `book` field
# stored on bets_placed back to the canonical key).
_DISPLAY_TO_KEY = {dn.lower(): k for k, dn, _ in BOOKS}
_DISPLAY_TO_KEY.update({k: k for k, _, _ in BOOKS})  # also accept the key itself

LOW_AMBER_USD = 50.0
LOW_RED_USD = 20.0


def normalize_book(name: str | None) -> str | None:
    """Map a stored book title (e.g. 'FanDuel', 'ESPN Bet', 'Bally Bet') to
    our internal book_key (e.g. 'fanduel'). Returns None for untracked books."""
    if not name:
        return None
    s = name.strip().lower().replace(" ", "")
    return _DISPLAY_TO_KEY.get(s)


def seed_from_env() -> int:
    """Insert any missing book rows from BALANCE_<BOOK> env vars. Returns the
    number of rows actually inserted (zero on subsequent restarts since the
    rows already exist). Logs a warning for any tracked book without an env
    var so it's obvious in the logs that defaults of 0 were used."""
    inserted = 0
    with db() as conn:
        for key, display, env_suffix in BOOKS:
            raw = os.getenv(f"BALANCE_{env_suffix}")
            if raw is None:
                # No env var — seed with 0 but log so the operator notices
                amount = 0.0
                log.warning("book_balance: no BALANCE_%s in env, seeding %s with 0", env_suffix, display)
            else:
                try:
                    amount = float(raw)
                except ValueError:
                    log.warning("book_balance: BALANCE_%s=%r isn't a number, skipping", env_suffix, raw)
                    continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO book_balance
                  (book_key, display_name, balance_usd, initial_balance_usd)
                VALUES (?, ?, ?, ?)
                """,
                (key, display, amount, amount),
            )
            if cur.rowcount:
                inserted += 1
    return inserted


def get_all() -> list[dict]:
    """Returns one dict per tracked book in the order from BOOKS, with
    balance + low-warning level. Books not yet seeded show balance=0."""
    with db() as conn:
        rows = conn.execute(
            "SELECT book_key, display_name, balance_usd, initial_balance_usd, updated_at FROM book_balance"
        ).fetchall()
    by_key = {r["book_key"]: dict(r) for r in rows}
    out = []
    for key, display, _ in BOOKS:
        row = by_key.get(key, {
            "book_key": key, "display_name": display,
            "balance_usd": 0.0, "initial_balance_usd": 0.0, "updated_at": None,
        })
        bal = float(row["balance_usd"] or 0.0)
        warn = "red" if bal < LOW_RED_USD else "amber" if bal < LOW_AMBER_USD else "ok"
        row["warning_level"] = warn
        out.append(row)
    return out


def total_balance() -> float:
    return sum(float(b["balance_usd"] or 0.0) for b in get_all())


def get_balance(book_key: str | None) -> float | None:
    """Current balance for one book. None if the book isn't tracked."""
    if not book_key:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT balance_usd FROM book_balance WHERE book_key = ?", (book_key,)
        ).fetchone()
    return float(row["balance_usd"]) if row else None


def set_balance(book_key: str, amount: float) -> dict:
    """Operator override — set a tracked book's balance to an exact value
    (and seed initial_balance_usd if the row is missing). Used by the UI
    inline editor so the dashboard can fund books without a redeploy."""
    if book_key not in {k for k, _, _ in BOOKS}:
        raise ValueError(f"untracked book_key: {book_key!r}")
    if amount < 0:
        raise ValueError("balance cannot be negative")
    display = next(dn for k, dn, _ in BOOKS if k == book_key)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO book_balance (book_key, display_name, balance_usd, initial_balance_usd, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(book_key) DO UPDATE SET
                balance_usd = excluded.balance_usd,
                updated_at  = datetime('now')
            """,
            (book_key, display, amount, amount),
        )
        row = conn.execute(
            "SELECT book_key, display_name, balance_usd, initial_balance_usd, updated_at "
            "FROM book_balance WHERE book_key = ?",
            (book_key,),
        ).fetchone()
    return dict(row)


def apply_settled_bet(book_name: str | None, profit: float, *, is_paper: bool) -> dict | None:
    """Apply a settled bet's signed profit to its book's balance.

    Skips paper bets (those simulate; real-money balances shouldn't move).
    Skips untracked books. Returns the post-update row dict, or None when
    skipped.
    """
    if is_paper:
        return None
    key = normalize_book(book_name)
    if not key:
        return None
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE book_balance
            SET balance_usd = balance_usd + ?,
                updated_at = datetime('now')
            WHERE book_key = ?
            """,
            (float(profit or 0.0), key),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM book_balance WHERE book_key = ?", (key,)
        ).fetchone()
    return dict(row)
