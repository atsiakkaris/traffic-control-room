"""
Tests for stability.py — the shared health/tier maths used by both the
dashboard (report.py) and the weekly digest (digest.py).

These are pure functions with no DB or config dependency, so they are cheap to
test exhaustively. They guard the numbers a contractor gets held to, so a
silent change here would be expensive.

Design being pinned down:
  * The stability tier is a LIFETIME rating — "can I trust this sensor?"
  * "Always off" means literally zero good runs, never a percentage that
    happens to round to zero.
  * Tiering compares the raw ratio, so there is no rounding cliff at 0.5%.
  * Whether a sensor is down *right now* is a separate question, answered by
    report.py's Current state column — never by the tier.
"""

import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parent.parent / "runner"
sys.path.insert(0, str(RUNNER))

from stability import (  # noqa: E402
    GOOD_STATUSES,
    HEALTH_COLOR_BAD,
    HEALTH_COLOR_GOOD,
    HEALTH_COLOR_NONE,
    HEALTH_COLOR_WARN,
    HEALTH_GOOD_PCT,
    HEALTH_WARNING_PCT,
    STABILITY_TIERS,
    TIER_MIN_RUNS,
    health_color,
    health_pct,
    tier_for,
    tier_for_counts,
)


# ── health_pct ────────────────────────────────────────────────────────────────

def test_health_pct_basic():
    assert health_pct(1, 2) == 50
    assert health_pct(3, 4) == 75
    assert health_pct(0, 7) == 0
    assert health_pct(7, 7) == 100


def test_health_pct_no_data_is_none():
    """No runs must be None ("no data"), never 0% ("always off") — they are
    very different statements to put in front of a contractor."""
    assert health_pct(0, 0) is None


def test_health_pct_rounds_to_nearest_int():
    assert health_pct(2, 3) == 67    # 66.67 -> 67
    assert health_pct(1, 3) == 33    # 33.33 -> 33


# ── GOOD_STATUSES ─────────────────────────────────────────────────────────────

def test_only_working_and_ok_count_as_good():
    """Guards the semantics of every percentage in the system. Notably
    no_traffic (speed=0) is NOT good today — see the ITS review roadmap."""
    assert GOOD_STATUSES == {"working", "ok"}
    for bad in ("no_traffic", "no_measurement", "no_status",
                "not_working", "malfunctioning", "failing"):
        assert bad not in GOOD_STATUSES


# ── tier_for_counts: "Always off" means never worked ─────────────────────────

def test_no_runs_has_no_tier():
    assert tier_for_counts(0, 0) is None


def test_always_off_requires_zero_good_runs():
    """The whole point: 'Always off' is a claim that the sensor has *never*
    produced a good reading. It must not be reachable by rounding."""
    assert tier_for_counts(0, 1).key == "offline"
    assert tier_for_counts(0, 500).key == "offline"


def test_one_good_run_is_critical_not_always_off():
    """1 good run in 500 = 0.2%, which rounds to 0% — but the sensor HAS
    worked, so it cannot be labelled 'Always off'."""
    tier = tier_for_counts(1, 500)
    assert tier.key == "critical"
    assert tier.label == "Critical"


def test_no_rounding_cliff_at_half_a_percent():
    """Regression: tiering used to round first, so 2/500 (0.40% -> 0%) landed in
    'Always off' while 3/500 (0.60% -> 1%) landed in 'Critical'. Both have
    worked, so both must be Critical."""
    assert health_pct(2, 500) == 0      # the rounding that caused the old bug
    assert health_pct(3, 500) == 1
    assert tier_for_counts(2, 500).key == "critical"
    assert tier_for_counts(3, 500).key == "critical"


@pytest.mark.parametrize("good,total,expected", [
    (100, 100, "always_on"),
    (99,  100, "always_on"),
    (98,  100, "healthy"),
    (90,  100, "healthy"),
    (89,  100, "intermittent"),
    (70,  100, "intermittent"),
    (69,  100, "unstable"),
    (40,  100, "unstable"),
    (39,  100, "critical"),
    (1,   100, "critical"),
    (0,   100, "offline"),
])
def test_tier_for_counts_boundaries(good, total, expected):
    assert tier_for_counts(good, total).key == expected


def test_tier_for_counts_uses_raw_ratio_not_rounded():
    """98.6% must stay 'Healthy' — rounding it to 99 would promote it to
    'Always on', overstating the sensor's record."""
    assert health_pct(986, 1000) == 99          # rounds up
    assert tier_for_counts(986, 1000).key == "healthy"   # raw 98.6% does not


def test_lifetime_rating_is_insensitive_to_a_recent_repair():
    """Documents intended behaviour: the tier is a lifetime record, so a sensor
    repaired yesterday still reads badly. Current state (not the tier) is what
    tells the control room it is working again."""
    # 240 bad runs, then 4 good ones.
    assert tier_for_counts(4, 244).key == "critical"


# ── tier_for (percentage-based, used by the digest) ──────────────────────────

@pytest.mark.parametrize("pct,expected", [
    (100, "always_on"), (99, "always_on"),
    (98, "healthy"),    (90, "healthy"),
    (89, "intermittent"), (70, "intermittent"),
    (69, "unstable"),   (40, "unstable"),
    (39, "critical"),   (1, "critical"),
    (0, "offline"),
])
def test_tier_for_percentage_boundaries(pct, expected):
    assert tier_for(pct).key == expected


def test_tier_zero_is_always_off_not_critical():
    assert tier_for(0).label == "Always off"


# ── Tier metadata ────────────────────────────────────────────────────────────

def test_every_tier_has_a_range_label():
    """digest.py renders these directly. They used to be reverse-engineered out
    of the tooltip with a str.replace(), which silently broke when the tooltip
    wording changed."""
    for t in STABILITY_TIERS:
        assert t.range_label
        assert isinstance(t.range_label, str)


def test_tiers_are_ordered_high_to_low():
    mins = [t.min_pct for t in STABILITY_TIERS]
    assert mins == sorted(mins, reverse=True)


def test_tier_min_runs_is_a_sane_confidence_floor():
    assert TIER_MIN_RUNS > 1


# ── health_color: shared green/amber/red ─────────────────────────────────────

@pytest.mark.parametrize("pct,expected", [
    (100, HEALTH_COLOR_GOOD),
    (HEALTH_GOOD_PCT, HEALTH_COLOR_GOOD),
    (HEALTH_GOOD_PCT - 1, HEALTH_COLOR_WARN),
    (HEALTH_WARNING_PCT, HEALTH_COLOR_WARN),
    (HEALTH_WARNING_PCT - 1, HEALTH_COLOR_BAD),
    (0, HEALTH_COLOR_BAD),
])
def test_health_color_thresholds(pct, expected):
    assert health_color(pct) == expected


def test_health_color_none_is_grey():
    assert health_color(None) == HEALTH_COLOR_NONE


def test_health_color_thresholds_are_ordered():
    assert HEALTH_WARNING_PCT < HEALTH_GOOD_PCT
