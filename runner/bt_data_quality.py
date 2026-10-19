"""
bt_data_quality.py - which Bluetooth paths CONSISTENTLY produce no or bad data.

Pure classification (classify_path) plus a loader for results/history.db.
Used by bt_paths_map.py for its "Data quality" layer; read-only.

A path's category over its recent history:
  no_data      - failing/missing (speed or travel time <= 0, e.g. -1, or absent) in
                 BAD_RATIO or more of its last WINDOW_RUNS runs
  implausible  - reports positive but unrealistic values (speed outside
                 MIN_SPEED_KMH..MAX_SPEED_KMH, or a travel time that implies such
                 a speed over the path length) in BAD_RATIO or more of its last
                 WINDOW_RUNS stored value snapshots
  stale        - measurement too old in BAD_RATIO or more of its last WINDOW_RUNS runs
  intermittent - bad often enough to notice, but not consistently
  good         - reports normally
  unknown      - fewer than MIN_RUNS runs recorded, nothing to judge

Speed and travel time are only stored on LIVE_MODE runs (run manually from the
local machine), so "implausible" is judged on those snapshots, while "no_data"
and "stale" use the status recorded on every monitor run.
"""

import json

WINDOW_RUNS   = 20     # most recent runs / snapshots considered
MIN_RUNS      = 5      # fewer than this -> too little history to call it
MIN_SNAPSHOTS = 3      # fewer stored value snapshots than this -> cannot judge plausibility
BAD_RATIO     = 0.80   # share of the window that must be bad to call it "consistently"
MIN_SPEED_KMH = 5      # below this is unrealistic
MAX_SPEED_KMH = 140    # above this is unrealistic

GOOD_STATUSES = {"working", "ok"}
NO_DATA_STATUSES = {"failing", "missing"}

CATEGORIES = ("no_data", "implausible", "stale", "intermittent", "good", "unknown")


def implausible_reason(speed_kmh, travel_time_s, length_m=None):
    """Why a snapshot's values are unrealistic, or None.

    Values that are absent or <= 0 (e.g. -1) are NO data, not implausible, so they
    return None here.
    """
    if speed_kmh is None or travel_time_s is None or speed_kmh <= 0 or travel_time_s <= 0:
        return None
    if speed_kmh < MIN_SPEED_KMH:
        return "speed %.1f km/h is below %d" % (speed_kmh, MIN_SPEED_KMH)
    if speed_kmh > MAX_SPEED_KMH:
        return "speed %.1f km/h is above %d" % (speed_kmh, MAX_SPEED_KMH)
    if length_m:
        implied = (length_m / 1000.0) / (travel_time_s / 3600.0)
        if implied < MIN_SPEED_KMH or implied > MAX_SPEED_KMH:
            return "travel time %d s over %.1f km implies %.1f km/h" % (
                travel_time_s, length_m / 1000.0, implied)
    return None


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


def classify_path(history, snapshots, length_m=None):
    """history:   [{run_at, status}] oldest first (every monitor run)
    snapshots: [{run_at, speed_kmh, travel_time_s}] oldest first (LIVE_MODE runs only)

    Returns {category, bad_pct, runs, bad_since, reason, last} where bad_pct is the
    share of the window behind the category and last is the newest snapshot.
    """
    runs = history[-WINDOW_RUNS:]
    snaps = snapshots[-WINDOW_RUNS:]
    last = snaps[-1] if snaps else None
    out = {"category": "unknown", "bad_pct": None, "runs": len(runs),
           "bad_since": None, "reason": "", "last": last}

    def is_no_data(h):
        return h["status"] in NO_DATA_STATUSES

    def is_stale(h):
        return h["status"] == "stale"

    def snap_bad(s):
        return implausible_reason(s["speed_kmh"], s["travel_time_s"], length_m) is not None

    if len(runs) >= MIN_RUNS and _share(runs, is_no_data) >= BAD_RATIO:
        out.update(category="no_data", bad_pct=_share(runs, is_no_data),
                   bad_since=_streak_start(history, is_no_data),
                   reason="no usable speed/travel time (e.g. -1 or missing) in %d%% of the last %d runs"
                          % (round(100 * _share(runs, is_no_data)), len(runs)))
        return out

    if len(snaps) >= MIN_SNAPSHOTS and _share(snaps, snap_bad) >= BAD_RATIO:
        newest = snaps[-1]
        out.update(category="implausible", bad_pct=_share(snaps, snap_bad),
                   bad_since=_streak_start(snaps, snap_bad),
                   reason="%d%% of the last %d value snapshots are unrealistic (latest: %s)"
                          % (round(100 * _share(snaps, snap_bad)), len(snaps),
                             implausible_reason(newest["speed_kmh"], newest["travel_time_s"], length_m)
                             or "within limits"))
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
    """({path_id: [{run_at, status}]}, {path_id: [{run_at, speed_kmh, travel_time_s}]}),
    each oldest first. Snapshots exist only for runs where LIVE_MODE stored data."""
    histories, snapshots = {}, {}
    rows = conn.execute(
        "SELECT sensor_id, run_at, status, data FROM sensor_results "
        "WHERE group_name=? ORDER BY sensor_id, run_at", (group,)).fetchall()
    for r in rows:
        histories.setdefault(r["sensor_id"], []).append({"run_at": r["run_at"], "status": r["status"]})
        if r["data"]:
            try:
                d = json.loads(r["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            snapshots.setdefault(r["sensor_id"], []).append({
                "run_at": r["run_at"], "speed_kmh": d.get("speed_kmh"),
                "travel_time_s": d.get("travel_time_s")})
    return histories, snapshots
