"""Tests for bt_data_quality.py - persistent no-data / unrealistic-data detection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import bt_data_quality as dq


def hist(*statuses):
    return [{"run_at": "2026-10-%02dT00:00:00+00:00" % (i + 1), "status": s}
            for i, s in enumerate(statuses)]


def snaps(*pairs):
    return [{"run_at": "2026-10-%02dT00:00:00+00:00" % (i + 1), "speed_kmh": s, "travel_time_s": t}
            for i, (s, t) in enumerate(pairs)]


def test_limits():
    assert dq.implausible_reason(60, 100) is None
    assert dq.implausible_reason(4.9, 100) is not None     # below 5
    assert dq.implausible_reason(5, 100) is None
    assert dq.implausible_reason(140, 100) is None
    assert dq.implausible_reason(141, 100) is not None     # above 140
    assert dq.implausible_reason(-1, 100) is None          # -1 is "no data", not implausible
    assert dq.implausible_reason(0.2, 134910, 8577) is not None


def test_implied_speed_from_length():
    # reported speed looks fine but the travel time over 8.6 km implies ~0.2 km/h
    assert dq.implausible_reason(60, 134910, 8577) is not None
    assert dq.implausible_reason(60, 600, 8577) is None    # 51 km/h


def test_no_data_when_failing_80_percent():
    r = dq.classify_path(hist(*["failing"] * 16 + ["ok"] * 4), [])
    assert r["category"] == "no_data" and r["bad_pct"] == 0.8
    r = dq.classify_path(hist(*["failing"] * 15 + ["ok"] * 5), [])
    assert r["category"] == "intermittent"


def test_missing_counts_as_no_data_and_bad_since_is_streak_start():
    h = hist("ok", "ok", "failing", "failing", "failing", "failing", "failing", "failing", "missing", "failing")
    r = dq.classify_path(h, [])
    assert r["category"] == "no_data"
    assert r["bad_since"] == h[2]["run_at"]


def test_implausible_from_snapshots_even_if_status_ok():
    r = dq.classify_path(hist(*["ok"] * 10), snaps(*[(0.2, 134910)] * 5), length_m=8577)
    assert r["category"] == "implausible" and r["bad_since"].startswith("2026-10-01")


def test_implausible_needs_enough_snapshots():
    r = dq.classify_path(hist(*["ok"] * 10), snaps((0.2, 134910), (0.2, 134910)), length_m=8577)
    assert r["category"] == "good"


def test_one_bad_snapshot_is_not_consistent():
    r = dq.classify_path(hist(*["ok"] * 10), snaps(*[(50, 500)] * 4 + [(0.2, 134910)]), length_m=8577)
    assert r["category"] == "good"


def test_stale_good_and_unknown():
    assert dq.classify_path(hist(*["stale"] * 10), [])["category"] == "stale"
    assert dq.classify_path(hist(*["ok"] * 10), [])["category"] == "good"
    assert dq.classify_path(hist("failing", "failing"), [])["category"] == "unknown"
    assert dq.classify_path([], [])["category"] == "unknown"


def test_only_recent_window_counts():
    # long-ago outage, healthy for the last 20 runs
    r = dq.classify_path(hist(*["failing"] * 30 + ["ok"] * 20), [])
    assert r["category"] == "good"
