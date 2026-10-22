"""
bt_sensor_suspects.py - which Bluetooth SENSORS are probably faulty, judged from how
their PATHS behave ("triangulation"). Read-only.

A path A->B only returns data when BOTH end sensors detect the same device. So:
  * a path that works proves both of its sensors are alive ("exoneration"), and
  * a sensor that has no working path while its paths to proven-alive sensors fail
    is the likely culprit.
Example: A->B and B->C fail, D->A and J->C work. A and C are proven alive, so the
common end B is the suspect.

Honest limits, built into the rules below:
  * Whole-feed outages (every path stale/failing) say nothing about single sensors, so
    runs where fewer than OUTAGE_OK_SHARE of the paths were ok are ignored.
  * Path names are not trusted blindly: the sensor named in the path name must really be
    near the path's end (NAME_RADIUS_M). A path whose name points somewhere else is set
    aside as `mismatch` instead of being guessed at.
  * A path can fail for path-specific reasons (config, direction, distance). A sensor
    that has BOTH working and failing paths is therefore called a path problem, not
    a faulty sensor.
  * A path with unrealistic speeds still proves its sensors detect devices.

Verdicts per sensor (see VERDICTS):
  faulty_high  no working path, and >= 2 failing paths that lead to proven-alive sensors
  faulty_likely no working path and failing path(s), but fewer/weaker proof
  path_problem has working AND failing paths: the sensor works, look at the paths
  explained    its failing paths all end at a sensor judged faulty_high: probably fine
  working      has working paths, none failing
  no_evidence  no usable path, or only unclear ones
"""

import json
import math
import re
from collections import defaultdict

import bt_data_quality as dq

# Tunable in one place.
LOOKBACK_RUNS = 150        # newest monitor runs read from the database
WINDOW_RUNS = 20           # newest HEALTHY runs used per path
MIN_RUNS = 5               # fewer healthy runs than this -> not enough history
OUTAGE_OK_SHARE = 0.5      # a run with a smaller share of ok paths is a feed outage, ignored
NAME_RADIUS_M = 250        # the sensor named in the path name must be within this of the path end
ENDPOINT_RADIUS_M = 100    # without a usable name: a path end belongs to a sensor at most this far away ...
ENDPOINT_MARGIN_M = 30     # ... and the runner-up must be at least this much farther
BAD_RATIO = 0.8            # share of runs that must be bad / good to call a path so
HIGH_CONFIDENCE_MIN_BAD = 2

GROUP = "Bluetooth Paths"

VERDICTS = {
    "faulty_high":   "Faulty (high confidence)",
    "faulty_likely": "Faulty (likely)",
    "path_problem":  "Works, but some paths fail",
    "explained":     "Probably fine (its failing paths end at a faulty sensor)",
    "working":       "Working",
    "no_evidence":   "Not enough evidence",
}
SUSPECT_VERDICTS = ("faulty_high", "faulty_likely", "path_problem")


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ── 1. which sensor is at each end of a path ─────────────────────────────────

def _nearest(point, sensors, only_code=None):
    return sorted((_haversine_m(point[0], point[1], s["lat"], s["lon"]), sid)
                  for sid, s in sensors.items()
                  if s.get("lat") is not None
                  and (only_code is None or str(s.get("site_code")) == only_code))


def resolve_endpoint(point, sensors, code=None):
    """(sensor_id, how) for one end of a path; how = 'name' | 'geometry' | 'mismatch' | None.

    With a site code from the path name: the sensor with that code must really be near
    the end (NAME_RADIUS_M) - then the name is confirmed by geometry. If it is not near
    but another sensor clearly is, the name is wrong -> 'mismatch'. Without a usable
    code the nearest clear sensor within ENDPOINT_RADIUS_M is used.
    """
    if code is not None:
        named = [x for x in _nearest(point, sensors, only_code=code) if x[0] <= NAME_RADIUS_M]
        if named:
            return named[0][1], "name"
    ranked = _nearest(point, sensors)
    clear = (bool(ranked) and ranked[0][0] <= ENDPOINT_RADIUS_M
             and (len(ranked) < 2 or ranked[1][0] - ranked[0][0] >= ENDPOINT_MARGIN_M))
    if not clear:
        return None, None
    return ranked[0][1], ("mismatch" if code is not None else "geometry")


def resolve_paths(paths, sensors):
    """Per path: which sensors it joins, or why it can't be used.

    Returns {path_id: {name, a, b, problem}} with problem in
    None | 'unresolved' (an end has no clear sensor) | 'mismatch' (the name's site
    code is not where the path really ends) | 'loop' (both ends are the same sensor).
    """
    out = {}
    for pid, p in paths.items():
        coords = p.get("coords") or []
        info = {"name": p.get("name") or pid, "a": None, "b": None, "problem": "unresolved"}
        out[pid] = info
        if len(coords) < 2:
            continue
        m = re.match(r"^\s*(\w+)\s*->\s*(\w+)\s*$", p.get("name") or "")
        codes = (m.group(1), m.group(2)) if m else (None, None)
        a, how_a = resolve_endpoint(coords[0], sensors, codes[0])
        b, how_b = resolve_endpoint(coords[-1], sensors, codes[1])
        info["a"], info["b"] = a, b
        if a is None or b is None:
            continue
        if "mismatch" in (how_a, how_b):
            info["problem"] = "mismatch"
        elif a == b:
            info["problem"] = "loop"
        else:
            info["problem"] = None
    return out


# ── 2. is a path working, failing or unclear (outage runs ignored) ────────────

def healthy_runs(run_statuses):
    """Newest WINDOW_RUNS runs in which the feed worked. run_statuses: {run_id: [status, ...]}
    in newest-first order (run ids are random UUIDs, so the dict order is the only clock;
    plain ints in tests sort the same way). Returns (kept run ids oldest first, skipped outage count)."""
    kept, skipped = [], 0
    for run_id in run_statuses:
        statuses = run_statuses[run_id]
        ok = sum(1 for s in statuses if s in dq.GOOD_STATUSES)
        if statuses and ok / len(statuses) >= OUTAGE_OK_SHARE:
            if len(kept) < WINDOW_RUNS:
                kept.append(run_id)
        else:
            skipped += 1
    return kept[::-1], skipped


def path_state(history):
    """'good' | 'bad' | 'odd' | 'mixed' | 'unknown' from [{run_at, status, unrealistic}] (healthy runs).

    odd = reports positive but unrealistic speeds: its sensors do detect devices.
    """
    if len(history) < MIN_RUNS:
        return "unknown"
    c = dq.classify_path(history)["category"]
    if c in ("no_data", "stale"):
        return "bad"
    if c == "implausible":
        return "odd"
    if c == "good":
        return "good"
    if c == "intermittent":
        return "mixed"
    return "unknown"


# ── 3. the verdict for each sensor ───────────────────────────────────────────

def judge_sensors(sensors, resolved, states):
    """sensors: {sid: {name, site_code,...}}; resolved: resolve_paths(); states: {path_id: state}.

    Returns {sid: {verdict, reason, paths:[{id,name,state,other}], good, bad, mixed}}.
    """
    usable = {pid: r for pid, r in resolved.items()
              if r["problem"] is None and states.get(pid, "unknown") != "unknown"}
    by_sensor = defaultdict(list)
    for pid, r in usable.items():
        st = states[pid]
        by_sensor[r["a"]].append({"id": pid, "name": r["name"], "state": st, "other": r["b"]})
        by_sensor[r["b"]].append({"id": pid, "name": r["name"], "state": st, "other": r["a"]})

    def label(sid):
        s = sensors.get(sid) or {}
        return s.get("name") or s.get("site_code") or str(sid)

    alive = {sid for sid, ps in by_sensor.items() if any(p["state"] in ("good", "odd") for p in ps)}
    result = {}
    for sid, s in sensors.items():
        ps = by_sensor.get(sid, [])
        good = [p for p in ps if p["state"] in ("good", "odd")]
        bad = [p for p in ps if p["state"] == "bad"]
        mixed = [p for p in ps if p["state"] == "mixed"]
        result[sid] = {"verdict": "no_evidence", "reason": "", "paths": ps,
                       "good": len(good), "bad": len(bad), "mixed": len(mixed)}
        r = result[sid]
        if not ps:
            r["reason"] = "no usable path ends here"
        elif good and bad:
            r["verdict"] = "path_problem"
            r["reason"] = ("%d path(s) work, so the sensor is alive; %d fail (%s) - check those paths"
                           % (len(good), len(bad), ", ".join(p["name"] for p in bad)))
        elif good:
            r["verdict"] = "working"
            r["reason"] = "%d path(s) work" % len(good)
        elif bad:
            proven = [p for p in bad if p["other"] in alive]
            if len(proven) >= HIGH_CONFIDENCE_MIN_BAD:
                r["verdict"] = "faulty_high"
                r["reason"] = ("no path through it works, and %d of its failing paths (%s) lead to sensors that "
                               "work on other paths: %s"
                               % (len(proven), ", ".join(p["name"] for p in proven),
                                  ", ".join(sorted({label(p["other"]) for p in proven}))))
            else:
                r["verdict"] = "faulty_likely"
                r["reason"] = ("all its paths fail (%s)" % ", ".join(p["name"] for p in bad)
                               + ("" if proven else "; the sensors at the other ends are unproven, so blame is shared")
                               + ("; only one path to a working sensor, so it could be the path" if len(proven) == 1 else ""))
        else:
            r["reason"] = "only unclear (intermittent) paths"

    # Second pass: a "likely" sensor whose failing paths all end at a high-confidence
    # faulty sensor is explained by that sensor.
    high = {sid for sid, r in result.items() if r["verdict"] == "faulty_high"}
    for sid, r in result.items():
        if r["verdict"] == "faulty_likely":
            bad = [p for p in r["paths"] if p["state"] == "bad"]
            if bad and all(p["other"] in high for p in bad):
                r["verdict"] = "explained"
                r["reason"] = ("its failing paths all end at %s, which looks faulty"
                               % ", ".join(sorted({label(p["other"]) for p in bad})))
    return result


# ── loading + the one-call entry point ───────────────────────────────────────

def load_run_statuses(conn, group=GROUP):
    """({run_id: [status]}, {path_id: {run_id: {run_at, status, unrealistic}}}) for the newest runs."""
    run_ids = [r[0] for r in conn.execute(
        "SELECT run_id FROM sensor_results WHERE group_name=? GROUP BY run_id ORDER BY MIN(run_at) DESC LIMIT ?",
        (group, LOOKBACK_RUNS))]
    per_run = {r: [] for r in run_ids}      # newest first: run ids are random UUIDs, not a clock
    per_path = defaultdict(dict)
    if not run_ids:
        return per_run, per_path
    marks = ",".join("?" * len(run_ids))
    for run_id, run_at, sid, status, data in conn.execute(
            f"SELECT run_id, run_at, sensor_id, status, data FROM sensor_results "
            f"WHERE group_name=? AND run_id IN ({marks})", (group, *run_ids)):
        unrealistic = False
        if data:
            try:
                d = json.loads(data) if isinstance(data, str) else data
                unrealistic = bool(isinstance(d, dict) and dq._data_is_unrealistic(d))
            except (ValueError, TypeError):
                pass
        per_run[run_id].append(status)
        per_path[str(sid)][run_id] = {"run_at": run_at, "status": status, "unrealistic": unrealistic}
    return per_run, per_path


def analyse(conn=None, paths=None, sensors=None):
    """Everything in one go from the database (read-only).

    Returns {sensors: {sid: {...verdict..., name, site_code, lat, lon}}, paths: {pid: {...}},
             meta: {runs_used, runs_ignored_as_outage, counts}}.
    """
    import db
    own = conn is None
    conn = conn or db.get_connection()
    try:
        paths = paths if paths is not None else db.fetch_bt_path_coords()
        sensors = sensors if sensors is not None else db.fetch_sensor_coords().get("Bluetooth", {})
        per_run, per_path = load_run_statuses(conn)
    finally:
        if own:
            conn.close()
    kept, skipped = healthy_runs(per_run)
    states = {}
    for pid in paths:
        mine = per_path.get(str(pid), {})
        hist = sorted((mine[r] for r in kept if r in mine), key=lambda h: h["run_at"])
        states[pid] = path_state(hist)
    resolved = resolve_paths(paths, sensors)
    verdicts = judge_sensors(sensors, resolved, states)
    for sid, v in verdicts.items():
        s = sensors[sid]
        v.update(name=s.get("name") or sid, site_code=s.get("site_code"), lat=s.get("lat"), lon=s.get("lon"))
    counts = defaultdict(int)
    for v in verdicts.values():
        counts[v["verdict"]] += 1
    path_info = {pid: dict(resolved[pid], state=states[pid]) for pid in paths}
    return {"sensors": verdicts, "paths": path_info,
            "meta": {"runs_used": len(kept), "runs_ignored_as_outage": skipped,
                     "enough_history": len(kept) >= MIN_RUNS,
                     "unusable_paths": sum(1 for r in resolved.values() if r["problem"]),
                     "counts": dict(counts)}}


def csv_rows(result):
    """Rows for an export: suspects first (most confident first)."""
    order = {v: i for i, v in enumerate(("faulty_high", "faulty_likely", "path_problem",
                                         "explained", "no_evidence", "working"))}
    rows = [["Sensor ID", "Name", "Site code", "Latitude", "Longitude", "Verdict", "Reason",
             "Working paths", "Failing paths", "Unclear paths", "Total paths counted"]]
    for sid, v in sorted(result["sensors"].items(), key=lambda kv: (order[kv[1]["verdict"]], kv[1]["name"])):
        if v["verdict"] in ("working", "no_evidence"):
            continue
        rows.append([sid, v["name"], v["site_code"], v.get("lat"), v.get("lon"), VERDICTS[v["verdict"]], v["reason"],
                     v["good"], v["bad"], v["mixed"], v["good"] + v["bad"] + v["mixed"]])
    return rows
