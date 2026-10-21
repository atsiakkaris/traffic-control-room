"""Tests for bt_data_quality.py - persistent no-data / unrealistic-data detection."""

import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import bt_data_quality as dq


def hist(*statuses, unrealistic=()):
    """History oldest first; `unrealistic` = indexes of runs that carry the marker."""
    return [{"run_at": "2026-10-%02dT00:00:00+00:00" % (i + 1), "status": st,
             "unrealistic": i in unrealistic} for i, st in enumerate(statuses)]


def test_limits():
    assert dq.unrealistic_speed(60) is False
    assert dq.unrealistic_speed(4.9) is True        # below 5
    assert dq.unrealistic_speed(5) is False
    assert dq.unrealistic_speed(140) is False
    assert dq.unrealistic_speed(141) is True        # above 140
    assert dq.unrealistic_speed(-1) is False        # -1 is "no data", not unrealistic
    assert dq.unrealistic_speed(0) is False
    assert dq.unrealistic_speed(None) is False


def test_marker_only_for_unrealistic_speed():
    assert dq.unrealistic_marker(0.24) == {"unrealistic_speed_kmh": 0.2}
    assert dq.unrealistic_marker(200) == {"unrealistic_speed_kmh": 200}
    assert dq.unrealistic_marker(45.5) is None
    assert dq.unrealistic_marker(-1) is None
    assert dq.unrealistic_marker(None) is None


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


def test_implausible_when_marker_on_80_percent_of_runs_even_though_status_ok():
    r = dq.classify_path(hist(*["ok"] * 10, unrealistic=range(2, 10)))   # 8 of 10
    assert r["category"] == "implausible" and r["bad_pct"] == 0.8
    assert r["bad_since"].startswith("2026-10-03")
    assert "under 5 or over 140" in r["reason"]


def test_implausible_needs_consistency_and_enough_runs():
    # 7 of 10 marked: not consistent
    assert dq.classify_path(hist(*["ok"] * 10, unrealistic=range(7)))["category"] == "good"
    # all marked but only 4 runs: too little history
    assert dq.classify_path(hist(*["ok"] * 4, unrealistic=range(4)))["category"] == "unknown"


def test_no_data_wins_over_implausible():
    r = dq.classify_path(hist(*["failing"] * 10, unrealistic=range(10)))
    assert r["category"] == "no_data"


def test_loader_reads_markers_and_live_values(tmp_path):
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE sensor_results (id INTEGER PRIMARY KEY, run_id TEXT, run_at TEXT, "
               "group_name TEXT, sensor_id TEXT, status TEXT, data TEXT)")
    rows = [("r1", "2026-10-01", "ok", None),
            ("r2", "2026-10-02", "ok", '{"unrealistic_speed_kmh": 0.2}'),                 # CI marker
            ("r3", "2026-10-03", "ok", '{"speed_kmh": 300.0, "travel_time_s": 9.0}'),      # LIVE_MODE, too fast
            ("r4", "2026-10-04", "ok", '{"speed_kmh": 50.0, "travel_time_s": 600.0}'),     # LIVE_MODE, fine
            ("r5", "2026-10-05", "failing", '{"speed_kmh": -1.0, "travel_time_s": -1.0}')]  # no data, not unrealistic
    for rid, at, st, data in rows:
        db.execute("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) "
                   "VALUES (?,?,?,?,?,?)", (rid, at, "Bluetooth Paths", "7", st, data))
    hist_by_id, snaps_by_id = dq.load_histories(db)
    assert [h["unrealistic"] for h in hist_by_id["7"]] == [False, True, True, False, False]
    # only LIVE_MODE rows carry full values; the marker-only row is not a snapshot
    assert [x["speed_kmh"] for x in snaps_by_id["7"]] == [300.0, 50.0, -1.0]


def test_stale_good_and_unknown():
    assert dq.classify_path(hist(*["stale"] * 10), [])["category"] == "stale"
    assert dq.classify_path(hist(*["ok"] * 10), [])["category"] == "good"
    assert dq.classify_path(hist("failing", "failing"), [])["category"] == "unknown"
    assert dq.classify_path([], [])["category"] == "unknown"


def test_only_recent_window_counts():
    # long-ago outage, healthy for the last 20 runs
    r = dq.classify_path(hist(*["failing"] * 30 + ["ok"] * 20), [])
    assert r["category"] == "good"


def test_live_geometry_refuses_publish(monkeypatch):
    """--live-geometry is a local preview: it must never be combined with --publish."""
    import bt_paths_map as bpm
    monkeypatch.setattr(sys, "argv", ["bt_paths_map.py", "--live-geometry", "--publish"])
    with pytest.raises(SystemExit) as exc:
        bpm.main()
    assert exc.value.code == 2


def test_recovering_needs_four_good_runs_after_a_bad_stretch():
    # 16 bad then 4 good: still "no data" by the 80% rule, but recovering
    r = dq.classify_path(hist(*["failing"] * 16 + ["ok"] * 4), [])
    assert r["category"] == "no_data" and r["recovering"] is True
    assert r["good_since"].startswith("2026-10-17")
    # only 3 good in a row: not yet
    r = dq.classify_path(hist(*["failing"] * 17 + ["ok"] * 3), [])
    assert r["category"] == "no_data" and r["recovering"] is False and r["good_since"] is None


def test_recovering_flap_does_not_count():
    # the real 643 history: good, bad, ..., good, good
    h = hist(*list("fffffffoffffffffoo".replace("f", "F").replace("o", "O")))
    h = [dict(x, status="failing" if x["status"] == "F" else "ok") for x in h]
    r = dq.classify_path(h, [])
    assert r["recovering"] is False


def test_recovering_applies_to_intermittent_but_not_to_good_or_unknown():
    r = dq.classify_path(hist(*["failing"] * 10 + ["ok"] * 10), [])
    assert r["category"] == "intermittent" and r["recovering"] is True
    r = dq.classify_path(hist(*["ok"] * 20), [])
    assert r["category"] == "good" and r["recovering"] is False
    assert dq.classify_path(hist("ok", "ok", "ok", "ok"), [])["recovering"] is False


def test_data_to_store_keeps_values_only_in_live_mode():
    full = {"speed_kmh": 0.2, "travel_time_s": 1000.0, "age_hours": 0.1, "unrealistic_speed_kmh": 0.2}
    assert dq.data_to_store(full, live_mode=True) == full
    assert dq.data_to_store(full, live_mode=False) == {"unrealistic_speed_kmh": 0.2}
    normal = {"speed_kmh": 45.5, "travel_time_s": 1054.0, "age_hours": 0.0}
    assert dq.data_to_store(normal, live_mode=False) is None
    assert dq.data_to_store(None, live_mode=False) is None


def test_bt_check_marks_unrealistic_speeds_only():
    """tests.bt_paths_speed_and_traveltime puts the marker on positive out-of-range speeds."""
    from datetime import datetime, timezone
    import tests as checks   # runner/tests.py (the monitor's checks)
    now = datetime.now(timezone.utc).isoformat()

    def path(pid, speed, tt):
        return ("<predefinedLocationReference id='%s'><_predefinedLocationExtension>"
                "<obs_speed>%s</obs_speed><obs_t_time>%s</obs_t_time>"
                "<measurement_timestamp>%s</measurement_timestamp>"
                "</_predefinedLocationExtension></predefinedLocationReference>" % (pid, speed, tt, now))

    xml = "<root>" + path("1", 0.2, 1000) + path("2", 50, 600) + path("3", -1, -1) + path("4", 200, 30) + "</root>"
    res = checks.bt_paths_speed_and_traveltime(xml)
    m = res["measurements"]
    assert m["1"]["unrealistic_speed_kmh"] == 0.2 and m["4"]["unrealistic_speed_kmh"] == 200
    assert "unrealistic_speed_kmh" not in m["2"] and "unrealistic_speed_kmh" not in m["3"]
    # the marker never changes the path's status
    assert res["sensors"]["1"] == "ok" and res["sensors"]["3"] == "failing"


def test_published_data_quality_keeps_category_and_reason_but_not_values():
    import bt_paths_map as bpm
    by_id = {"7": {"category": "implausible", "reason": "speed under 5 or over 140 km/h in 85% of the last 20 runs",
                   "last": {"speed_kmh": 0.2, "travel_time_s": 9.0, "at": "21/10/26 12:00"}}}
    pub = bpm.public_data_quality(by_id)
    assert pub["7"]["category"] == "implausible"
    assert pub["7"]["reason"].startswith("speed under 5")
    assert pub["7"]["last"] is None
