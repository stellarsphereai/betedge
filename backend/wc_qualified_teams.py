"""WC 2026 qualified-team list — preliminary calibration corpus seed.

Drives the qualifier-only grid search. Teams listed here are the pool of
"WC-relevant" sides whose qualifying campaigns we treat as the calibration
corpus. Any fixture where AT LEAST ONE team is in this list is in scope.

Status as of editing time:
  - Hosts (3) — confirmed since 2018: USA, Canada, Mexico
  - UEFA (16) — pool drawn from current standings + recent FIFA rankings
  - CONMEBOL (6) — top six of ten in current qualifying table
  - CAF, AFC, CONCACAF, OFC — leading qualifiers per current standings
  - Inter-confederation playoffs (2) — tentative; flag and edit as needed

Adjust this list before running the grid search. Wrong-team-in-pool just
adds noise their team-quality already accounts for; missing-team would
miss matches but the grid is robust to a few hundred extra/missing
fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QualifiedTeam:
    name: str
    confederation: str
    api_football_id: int | None = None  # populated lazily on first lookup
    note: str = ""


WC_2026_QUALIFIED: list[QualifiedTeam] = [
    # --- Hosts (3, locked since 2018) ---
    QualifiedTeam("USA",       "CONCACAF", note="host"),
    QualifiedTeam("Canada",    "CONCACAF", note="host"),
    QualifiedTeam("Mexico",    "CONCACAF", note="host"),

    # --- UEFA (16) ---
    QualifiedTeam("Spain",       "UEFA"),
    QualifiedTeam("France",      "UEFA"),
    QualifiedTeam("England",     "UEFA"),
    QualifiedTeam("Germany",     "UEFA"),
    QualifiedTeam("Italy",       "UEFA"),
    QualifiedTeam("Portugal",    "UEFA"),
    QualifiedTeam("Netherlands", "UEFA"),
    QualifiedTeam("Belgium",     "UEFA"),
    QualifiedTeam("Croatia",     "UEFA"),
    QualifiedTeam("Poland",      "UEFA"),
    QualifiedTeam("Switzerland", "UEFA"),
    QualifiedTeam("Austria",     "UEFA"),
    QualifiedTeam("Denmark",     "UEFA"),
    QualifiedTeam("Ukraine",     "UEFA"),
    QualifiedTeam("Czech Republic", "UEFA"),
    QualifiedTeam("Turkey",      "UEFA"),

    # --- CONMEBOL (6, top six of ten) ---
    QualifiedTeam("Argentina",   "CONMEBOL"),
    QualifiedTeam("Brazil",      "CONMEBOL"),
    QualifiedTeam("Uruguay",     "CONMEBOL"),
    QualifiedTeam("Colombia",    "CONMEBOL"),
    QualifiedTeam("Ecuador",     "CONMEBOL"),
    QualifiedTeam("Paraguay",    "CONMEBOL"),

    # --- CAF (9) ---
    QualifiedTeam("Morocco",     "CAF"),
    QualifiedTeam("Egypt",       "CAF"),
    QualifiedTeam("Algeria",     "CAF"),
    QualifiedTeam("Senegal",     "CAF"),
    QualifiedTeam("Cameroon",    "CAF"),
    QualifiedTeam("Nigeria",     "CAF"),
    QualifiedTeam("Ivory Coast", "CAF"),
    QualifiedTeam("Ghana",       "CAF"),
    QualifiedTeam("Tunisia",     "CAF"),

    # --- AFC (8) ---
    QualifiedTeam("Japan",        "AFC"),
    QualifiedTeam("South Korea",  "AFC"),
    QualifiedTeam("Australia",    "AFC"),
    QualifiedTeam("Iran",         "AFC"),
    QualifiedTeam("Saudi Arabia", "AFC"),
    QualifiedTeam("Qatar",        "AFC"),
    QualifiedTeam("Iraq",         "AFC"),
    QualifiedTeam("UAE",          "AFC"),

    # --- CONCACAF (3 non-host slots) ---
    QualifiedTeam("Costa Rica",  "CONCACAF"),
    QualifiedTeam("Panama",      "CONCACAF"),
    QualifiedTeam("Jamaica",     "CONCACAF"),

    # --- OFC (1) ---
    QualifiedTeam("New Zealand", "OFC"),

    # --- Inter-confederation playoffs (2) — TENTATIVE ---
    QualifiedTeam("Bolivia",  "CONMEBOL", note="inter-conf playoff (tentative)"),
    QualifiedTeam("Bahrain",  "AFC",       note="inter-conf playoff (tentative)"),
]


def by_confederation() -> dict[str, list[QualifiedTeam]]:
    out: dict[str, list[QualifiedTeam]] = {}
    for t in WC_2026_QUALIFIED:
        out.setdefault(t.confederation, []).append(t)
    return out


def all_names() -> list[str]:
    return [t.name for t in WC_2026_QUALIFIED]
