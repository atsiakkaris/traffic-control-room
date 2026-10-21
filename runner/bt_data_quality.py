"""
bt_data_quality.py - which Bluetooth paths CONSISTENTLY produce no or bad data.

Pure classification (classify_path) plus a loader for results/history.db.
Used by bt_paths_map.py for its "Data quality" layer; read-only.

A path's category over its last WINDOW_RUNS runs:
  no_data      - failing/missing (speed or travel time <= 0, e.g. -1, or absent) in
                 BAD_RATIO or more of the runs
  implausible  - reports a positive but unrealistic speed (under MIN_SPEED_KMH or
                 over MAX_SPEED_KMH) in BAD_RATIO or more of the runs
  stale        - measurement too old in BAD_RATIO or more of the runs
  intermittent - bad often enough to notice, but not consistently
  good         - reporting normally
  unknown      - fewer than MIN_RUNS runs recorded, nothing to judge

Separately, `recovering` is True when the category still looks bad (no_data, stale
or intermittent, because the window remembers the outage) but the newest
RECOVERING_RUNS runs were all good. It is a note on the path, not a category, so
the category counts stay as they are.

How an unrealistic speed is recorded: the monitor (tests.py / run_tests.py) keeps a
path's status as `ok` but stores a tiny marker, {"unrealistic_speed_kmh": 0.2}, in
that row's `data` for any run whose speed is out of range. Normal rows stay empty.
Runs from LIVE_MODE (manual, local) store the full speed/travel time instead, and
those are judged by the same limits.
"""

import json

WINDOW_RUNS   = 20     # most recent runs considered
MIN_RUNS      = 5      # fewer than this -> too little history to call it
BAD_RATIO     = 0.80   # share of the window that must be bad to call it "consistently"
RECOVERING_RUNS = 4    # newest runs that must ALL be good to call a bad-looking path "recovering"
                       # (4 runs = a full day on the 6-hourly schedule)
MIN_SPEED_KMH = 5      # below this is unrealistic
MAX_SPEED_KMH = 140    # above this is unrealistic

GOOD_STATUSES = {"working", "ok"}
NO_DATA_STATUSES = {"failing", "missing"}
MARKER_KEY = "unrealistic_speed_kmh"

CATEGORIES = ("no_data", "implausible", "stale", "intermittent", "good", "unknown")


def unrealistic_speed(speed_kmh):
    """True for a positive speed outside MIN_SPEED_KMH..MAX_SPEED_KMH.

    Absent or <= 0 values (e.g. -1, the API's "malfunctioning" value) are NO data,
    not unrealistic, so they return False.
    """
    if speed_kmh is None or speed_kmh <= 0:
        return False
    return speed_kmh < MIN_SPEED_KMH or speed_kmh > MAX_SPEED_KMH


def unrealistic_marker(speed_kmh):
    """The `data` to store for a run, or None when the speed is fine."""
    if unrealistic_speed(speed_kmh):
        return {MARKER_KEY: round(speed_kmh, 1)}
    return None


def data_to_store(measurements, live_mode):
    """What run_tests.py writes to sensor_results.data for one path/sensor.

    LIVE_MODE (manual, local) keeps the full measurements. Every other run keeps
    only the tiny unrealistic-speed marker when there is one, otherwise nothing.
    """
    if live_mode:
        return measurements
    if measurements and MARKER_KEY in measurements:
        return {MARKER_KEY: measurements[MARKER_KEY]}
    return None


def _data_is_unrealistic(data):
    """Does a stored `data` dict record an unrealistic speed (marker or full values)?"""
    return MARKER_KEY in data or unrealistic_speed(data.get("speed_kmh"))


def _share(items, pred):
    return sum(1 for i in items if pred(i)) / len(items) if items else 0.0


def _streak_start(items, pred):
    """run_at of the earliest item in the current unbroken run of pred-true items."""
    if not items or not pred(items[-1]):
        return None
    start = items[-1]["run_at"]
    for it in reversed(items):
        if not pred(it):
            break
        start = it["run_at"]
    return start


def classify_path(history, snapshots=None):
    """See _classify_path; adds the `recovering` note on top of its result:
    recovering (bool), good_since (run_at of the current run of good runs, or None).
    """
    out = _classify_path(history, snapshots or [])
    n = RECOVERING_RUNS
    is_good = lambda h: h["status"] in GOOD_STATUSES
    newest_good = len(history) >= n and all(is_good(h) for h in history[-n:])
    out["recovering"] = bool(newest_good and out["category"] in ("no_data", "stale", "intermittent"))
    out["good_since"] = _streak_start(history, is_good) if out["recovering"] else None
    return out


def _classify_path(history, snapshots):
    """history:   [{run_at, status, unrealistic?}] oldest first (every monitor run)
    snapshots: [{run_at, speed_kmh, travel_time_s}] oldest first (LIVE_MODE runs only;
               used just to show the latest stored values)

    Returns {category, bad_pct, runs, bad_since, reason, last} where bad_pct is the
    share of the window behind the category and last is the newest snapshot.
    """
    runs = history[-WINDOW_RUNS:]
    last = snapshots[-1] if snapshots else None
    out = {"category": "unknown", "bad_pct": None, "runs": len(runs),
           "bad_since": None, "reason": "", "last": last}

    def is_no_data(h):
        return h["status"] in NO_DATA_STATUSES

    def is_stale(h):
        return h["status"] == "stale"

    def is_unrealistic(h):
        return bool(h.get("unrealistic"))

    if len(runs) >= MIN_RUNS and _share(runs, is_no_data) >= BAD_RATIO:
        out.update(category="no_data", bad_pct=_share(runs, is_no_data),
                   bad_since=_streak_start(history, is_no_data),
                   reason="no usable speed/travel time (e.g. -1 or missing) in %d%% of the last %d runs"
                          % (round(100 * _share(runs, is_no_data)), len(runs)))
        return out

    if len(runs) >= MIN_RUNS and _share(runs, is_unrealistic) >= BAD_RATIO:
        out.update(category="implausible", bad_pct=_share(runs, is_unrealistic),
                   bad_since=_streak_start(history, is_unrealistic),
                   reason="speed under %d or over %d km/h in %d%% of the last %d runs"
                          % (MIN_SPEED_KMH, MAX_SPEED_KMH,
                             round(100 * _share(runs, is_unrealistic)), len(runs)))
        return out

    if len(runs) >= MIN_RUNS and _share(runs, is_stale) >= BAD_RATIO:
        out.update(category="stale", bad_pct=_share(runs, is_stale),
                   bad_since=_streak_start(history, is_stale),
                   reason="measurement timestamp too old in %d%% of the last %d runs"
                          % (round(100 * _share(runs, is_stale)), len(runs)))
        return out

    if len(runs) < MIN_RUNS:
        out["reason"] = "only %d run(s) recorded" % len(runs)
        return out

    bad_share = _share(runs, lambda h: h["status"] not in GOOD_STATUSES)
    if bad_share >= 1 - BAD_RATIO:
        out.update(category="intermittent", bad_pct=bad_share,
                   reason="bad in %d%% of the last %d runs, not consistently"
                          % (round(100 * bad_share), len(runs)))
        return out

    out.update(category="good", bad_pct=bad_share, reason="reporting normally")
    return out


def load_histories(conn, group="Bluetooth Paths"):
    """({path_id: [{run_at, status, unrealistic}]}, {path_id: [{run_at, speed_kmh, travel_time_s}]}),
    each oldest first. `unrealistic` comes from the marker CI stores (or full values
    from a LIVE_MODE run). Snapshots exist only for LIVE_MODE runs, which store the
    full speed and travel time."""
    histories, snapshots = {}, {}
    rows = conn.execute(
        "SELECT sensor_id, run_at, status, data FROM sensor_results "
        "WHERE group_name=? ORDER BY sensor_id, run_at", (group,)).fetchall()
    for r in rows:
        d = {}
        if r["data"]:
            try:
                d = json.loads(r["data"])
            except (json.JSONDecodeError, TypeError):
                d = {}
        histories.setdefault(r["sensor_id"], []).append({
            "run_at": r["run_at"], "status": r["status"],
            "unrealistic": _data_is_unrealistic(d) if d else False})
        if "speed_kmh" in d:
            snapshots.setdefault(r["sensor_id"], []).append({
                "run_at": r["run_at"], "speed_kmh": d.get("speed_kmh"),
                "travel_time_s": d.get("travel_time_s")})
    return histories, snapshots
