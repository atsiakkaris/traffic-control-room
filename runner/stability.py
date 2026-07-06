"""
stability.py — Shared stability-tier definitions and constants.

Single source of truth for the 6-tier health badge system used by both the
dashboard report (report.py) and the weekly email digest (digest.py).
If a threshold or colour changes, change it here only.
"""
from collections import namedtuple
from zoneinfo import ZoneInfo

CYPRUS_TZ = ZoneInfo("Asia/Nicosia")

# Sensor statuses that count as "good" when computing health percentages
GOOD_STATUSES = {"working", "ok"}

Tier = namedtuple("Tier", ["key", "label", "min_pct", "bg", "fg", "tooltip"])

# Ordered highest tier first; tier_for() returns the first tier whose
# min_pct the (integer) percentage meets. 100 and 0 are exact by construction.
STABILITY_TIERS = [
    Tier("always_on",    "Always on",    99, "#e1f5ee", "#085041", "99% of runs good"),
    Tier("healthy",      "Healthy",       90, "#c0dd97", "#27500a", "90–98% of runs good"),
    Tier("intermittent", "Intermittent",  70, "#faeeda", "#633806", "70–89% of runs good"),
    Tier("unstable",     "Unstable",      40, "#fac775", "#412402", "40–69% of runs good"),
    Tier("critical",     "Critical",       1, "#f09595", "#501313", "1–39% of runs good"),
    Tier("offline",      "Always off",     0, "#e24b4a", "#ffffff", "0% of runs good"),
]


def tier_for(pct):
    """Return the Tier for an integer health percentage (0–100)."""
    for tier in STABILITY_TIERS:
        if pct >= tier.min_pct:
            return tier
    return STABILITY_TIERS[-1]


def health_pct(good, total):
    """Integer health percentage, or None when there is no data."""
    return round(good / total * 100) if total else None
