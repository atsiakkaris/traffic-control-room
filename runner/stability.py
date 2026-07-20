"""
stability.py — Shared stability-tier definitions and constants.

Single source of truth for the 6-tier health badge system used by both the
dashboard report (report.py) and the weekly email digest (digest.py).
If a threshold or colour changes, change it here only.
"""
import csv as _csv
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CYPRUS_TZ = ZoneInfo("Asia/Nicosia")

PROJECTS_CSV = Path(__file__).parent.parent / "config" / "projects.csv"

# Sensor statuses that count as "good" when computing health percentages
GOOD_STATUSES = {"working", "ok"}

# Commissioning states meaning "not expected to be working". These sensors are
# left out of every health statistic — dashboard and digest alike. An unpowered
# VMS is published by the API and reports not_working forever; counting it as a
# fault would bury the real ones.
EXCLUDED_COMMISSIONING = {"not_electrified", "decommissioned"}

Tier = namedtuple("Tier", ["key", "label", "min_pct", "bg", "fg", "tooltip", "range_label"])

# Ordered highest tier first. `range_label` is the short range shown in legends —
# a real field, so callers never have to reverse-engineer it out of `tooltip`.
STABILITY_TIERS = [
    Tier("always_on",    "Always on",    99, "#e1f5ee", "#085041", "99–100% of runs good",            "99–100%"),
    Tier("healthy",      "Healthy",       90, "#c0dd97", "#27500a", "90–98% of runs good",             "90–98%"),
    Tier("intermittent", "Intermittent",  70, "#faeeda", "#633806", "70–89% of runs good",             "70–89%"),
    Tier("unstable",     "Unstable",      40, "#fac775", "#412402", "40–69% of runs good",             "40–69%"),
    Tier("critical",     "Critical",       1, "#f09595", "#501313", "Under 40% of runs good, but has worked at least once", "<40%"),
    Tier("offline",      "Always off",     0, "#e24b4a", "#ffffff", "Never reported a single good run", "never"),
]

_OFFLINE_TIER  = STABILITY_TIERS[-1]
_CRITICAL_TIER = STABILITY_TIERS[-2]


def tier_for(pct):
    """Return the Tier for a health percentage (0–100).

    Percentage-based, so it cannot distinguish "never worked" from "worked once
    long ago" — both round toward 0. Prefer tier_for_counts() when you have the
    raw counts. Kept for callers that only ever hold a percentage.
    """
    for tier in STABILITY_TIERS:
        if pct >= tier.min_pct:
            return tier
    return _OFFLINE_TIER


def tier_for_counts(good, total):
    """Return the Tier from raw good/total counts, or None when there is no data.

    Two things this gets right that a percentage cannot:

    * "Always off" means *literally zero good runs* — never once reported. That
      is a defensible claim to put in front of a contractor ("this has never
      worked"), unlike a percentage that merely rounds to zero.
    * The ratio is compared unrounded. Rounding first created an arbitrary cliff
      at 0.5%: 2 good runs in 500 (0.40%) landed in "Always off" while 3 in 500
      (0.60%) landed in "Critical". Now anything above zero is at least Critical.
    """
    if not total:
        return None
    if good == 0:
        return _OFFLINE_TIER
    pct = good / total * 100          # raw — deliberately not rounded
    for tier in STABILITY_TIERS[:-1]:  # every tier except offline
        if pct >= tier.min_pct:
            return tier
    return _CRITICAL_TIER              # >0 but under 1%: worked once, still Critical


def health_pct(good, total):
    """Integer health percentage, or None when there is no data.
    For display only — tiering uses the raw ratio (see tier_for_counts)."""
    return round(good / total * 100) if total else None


# ── Confidence floor ──────────────────────────────────────────────────────────
# The stability tier is a *lifetime* rating: it answers "can I trust this sensor?"
# and is deliberately insensitive to recent noise. What it must NOT do is state a
# confident tier from a handful of runs, so below this many recorded runs the
# dashboard shows a neutral "Collecting data" badge instead.
#
# Current operational state ("is it down right now, and for how long?") is a
# separate question, answered by the Current state column and the fault age —
# never by this tier.
TIER_MIN_RUNS = 5


# ── Simple green / amber / red health colouring ───────────────────────────────
# Single source of truth for the three-way health colour used on the dashboard
# group cards (report.py) and the QA report badges (qa.py). Keeping the
# thresholds here stops the two reports from silently disagreeing on the same
# percentage (e.g. one showing amber and the other red for 82%).
HEALTH_WARNING_PCT = 80   # ≥ this = amber, below = red; ≥ HEALTH_GOOD_PCT = green
HEALTH_GOOD_PCT    = 90

HEALTH_COLOR_GOOD  = "#1d9e75"
HEALTH_COLOR_WARN  = "#e58e0a"
HEALTH_COLOR_BAD   = "#e24b4a"
HEALTH_COLOR_NONE  = "#9ca3af"


def health_color(pct):
    """Green / amber / red for a 0–100 health percentage (grey when unknown)."""
    if pct is None:
        return HEALTH_COLOR_NONE
    if pct >= HEALTH_GOOD_PCT:
        return HEALTH_COLOR_GOOD
    if pct >= HEALTH_WARNING_PCT:
        return HEALTH_COLOR_WARN
    return HEALTH_COLOR_BAD


def to_cyprus(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp string to Cyprus time, formatted for display."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CYPRUS_TZ).strftime("%d/%m/%y %H:%M")


# Plain-English reason for each raw sensor status. Shared so the dashboard and
# the digest never describe the same fault differently.
STATUS_LABEL = {
    "working": "Working",
    "ok": "OK",
    "no_traffic": "No traffic",
    "no_measurement": "No measurement",
    "no_status": "No status reported",
    "not_working": "VMS not working",
    "malfunctioning": "Speed = -1 (sensor fault)",
    "failing": "No speed or travel time",
    "missing": "Not present in feed",
    "stale": "Feed data is stale",
}


def fault_streak_start(history):
    """The earliest run in the sensor's *current* unbroken run of bad statuses,
    or None if its latest run was good. That run is when the outage was first
    detected — the number a contractor can be held to."""
    if not history or history[-1]["status"] in GOOD_STATUSES:
        return None
    start = history[-1]
    for h in reversed(history):
        if h["status"] in GOOD_STATUSES:
            break
        start = h
    return start


def current_state(history):
    """(label, colour, tooltip, down_days) describing what the sensor is doing NOW.

    Deliberately separate from the lifetime stability tier: a sensor can be
    'Critical' on its record yet 'Working' today, and vice versa. This is what
    the control room acts on; the tier says whether the sensor can be trusted.
    Shared by report.py (dashboard) and digest.py (weekly email) so the two
    surfaces can never disagree about who is down right now.
    """
    if not history:
        return ("No data", "#9ca3af", "No runs recorded", None)

    last = history[-1]
    if last["status"] in GOOD_STATUSES:
        return ("Working", "#1d9e75",
                f"Reporting normally as of {to_cyprus(last['run_at'])}", 0)

    start = fault_streak_start(history)
    started_at = datetime.fromisoformat(start["run_at"].replace("Z", "+00:00"))
    down_days = (datetime.now(timezone.utc) - started_at).days
    reason = STATUS_LABEL.get(last["status"], last["status"])

    if not any(h["status"] in GOOD_STATUSES for h in history):
        # Never once produced a good reading since we started watching it.
        return (f"Never worked ({down_days}d)" if down_days else "Never worked",
                "#a32d2d",
                f"No good reading since first seen {to_cyprus(history[0]['run_at'])} — currently {reason}",
                down_days)

    label = "Down <1d" if down_days < 1 else f"Down {down_days}d"
    return (label, "#e24b4a",
            f"Failing since {to_cyprus(start['run_at'])} — currently {reason}",
            down_days)


def load_project_accountability():
    """Return {project_name: accountability} from config/projects.csv.

    Sensor-to-project assignment is computed elsewhere (qa.py, matched against
    the reference spreadsheet) and persisted to the sensor_projects DB table —
    callers only need the project's accountability, which is a small tracked
    file, not the (gitignored) reference spreadsheet itself.
    """
    status = {}
    if PROJECTS_CSV.exists():
        with open(PROJECTS_CSV, newline='', encoding='utf-8-sig') as f:
            for r in _csv.DictReader(f):
                proj = (r.get('project') or '').strip()
                acct = (r.get('accountability') or '').strip().lower()
                if proj:
                    status[proj] = acct or 'supported'
    return status
