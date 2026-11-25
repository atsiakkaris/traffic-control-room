"""Triangulation: which BT sensor is faulty, judged from how its paths behave."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import bt_sensor_suspects as bs  # noqa: E402


def S(name, lat, lon, code):
    return {"name": name, "lat": lat, "lon": lon, "site_code": code}


# Five sensors in a row, ~1 km apart (0.01 deg latitude ~ 1.1 km).
SENSORS = {
    "A": S("A", 35.00, 33.0, "1001"),
    "B": S("B", 35.01, 33.0, "1002"),
    "C": S("C", 35.02, 33.0, "1003"),
    "D": S("D", 35.03, 33.0, "1004"),
    "J": S("J", 35.04, 33.0, "1005"),
}


def path(name, a, b):
    return {"name": name, "coords": [[SENSORS[a]["lat"], SENSORS[a]["lon"]], [SENSORS[b]["lat"], SENSORS[b]["lon"]]]}


def run(paths, states):
    resolved = bs.resolve_paths(paths, SENSORS)
    return bs.judge_sensors(SENSORS, resolved, states)


# ── the user's example ───────────────────────────────────────────────────────

def test_users_example_points_at_b():
    """A->B and B->C fail, D->A and J->C work  =>  B is the suspect."""
    paths = {"1": path("1001->1002", "A", "B"), "2": path("1002->1003", "B", "C"),
             "3": path("1004->1001", "D", "A"), "4": path("1005->1003", "J", "C")}
    states = {"1": "bad", "2": "bad", "3": "good", "4": "good"}
    v = run(paths, states)
    assert v["B"]["verdict"] == "faulty_high"
    assert v["A"]["verdict"] == "path_problem" or v["A"]["verdict"] == "working"
    assert v["D"]["verdict"] == "working" and v["J"]["verdict"] == "working"


def test_sensor_with_working_and_failing_paths_is_a_path_problem_not_a_faulty_sensor():
    paths = {"1": path("1001->1002", "A", "B"), "2": path("1002->1003", "B", "C"),
             "3": path("1004->1001", "D", "A")}
    v = run(paths, {"1": "bad", "2": "good", "3": "good"})
    assert v["B"]["verdict"] == "path_problem"


def test_single_failing_path_is_only_likely_not_high_confidence():
    paths = {"1": path("1001->1002", "A", "B"), "3": path("1004->1001", "D", "A")}
    v = run(paths, {"1": "bad", "3": "good"})
    assert v["B"]["verdict"] == "faulty_likely" and "only one path" in v["B"]["reason"]


def test_two_unproven_sensors_sharing_a_failing_path_share_the_blame():
    paths = {"1": path("1001->1002", "A", "B")}
    v = run(paths, {"1": "bad"})
    assert v["A"]["verdict"] == "faulty_likely" and v["B"]["verdict"] == "faulty_likely"
    assert "shared" in v["A"]["reason"]


def test_a_sensor_whose_only_failing_paths_end_at_a_faulty_sensor_is_explained():
    # B is faulty (two bad paths to proven-alive A and C). E only talks to B.
    sensors = dict(SENSORS, E=S("E", 35.05, 33.0, "1006"))
    paths = {"1": path("1001->1002", "A", "B"), "2": path("1002->1003", "B", "C"),
             "3": path("1004->1001", "D", "A"), "4": path("1005->1003", "J", "C"),
             "5": {"name": "1006->1002", "coords": [[35.05, 33.0], [35.01, 33.0]]}}
    resolved = bs.resolve_paths(paths, sensors)
    v = bs.judge_sensors(sensors, resolved, {"1": "bad", "2": "bad", "3": "good", "4": "good", "5": "bad"})
    assert v["B"]["verdict"] == "faulty_high"
    assert v["E"]["verdict"] == "explained"


def test_unrealistic_speed_path_still_proves_its_sensors_alive():
    paths = {"1": path("1001->1002", "A", "B"), "2": path("1002->1003", "B", "C"),
             "3": path("1001->1004", "A", "D")}
    v = run(paths, {"1": "odd", "2": "bad", "3": "bad"})
    assert v["A"]["verdict"] == "path_problem"          # A has an 'odd' (alive) path and a bad one
    assert v["B"]["verdict"] == "path_problem"


def test_unknown_paths_give_no_evidence():
    v = run({"1": path("1001->1002", "A", "B")}, {"1": "unknown"})
    assert v["A"]["verdict"] == "no_evidence" and v["B"]["verdict"] == "no_evidence"


# ── which sensor is at each end ──────────────────────────────────────────────

def test_name_must_be_confirmed_by_geometry_otherwise_mismatch():
    ok = bs.resolve_paths({"1": path("1001->1002", "A", "B")}, SENSORS)["1"]
    assert ok["problem"] is None and (ok["a"], ok["b"]) == ("A", "B")
    wrong = bs.resolve_paths({"1": path("1003->1002", "A", "B")}, SENSORS)["1"]   # A's end is named 1003 (= C)
    assert wrong["problem"] == "mismatch"


def test_end_far_from_every_sensor_is_unresolved():
    far = {"name": "1001->1002", "coords": [[36.0, 34.0], [35.01, 33.0]]}
    assert bs.resolve_paths({"1": far}, SENSORS)["1"]["problem"] == "unresolved"


def test_unparseable_name_falls_back_to_clear_geometry():
    p = path("Kofinou-96789", "A", "B")
    r = bs.resolve_paths({"1": p}, SENSORS)["1"]
    assert r["problem"] is None and (r["a"], r["b"]) == ("A", "B")


def test_two_sensors_close_together_are_ambiguous_without_a_name():
    sensors = {"X": S("X", 35.0000, 33.0, "1"), "Y": S("Y", 35.0001, 33.0, "2")}
    assert bs.resolve_endpoint([35.00005, 33.0], sensors) == (None, None)


# ── outage runs and path state ───────────────────────────────────────────────

def test_outage_runs_are_ignored_and_newest_healthy_runs_kept():
    runs = {i: ["stale"] * 10 for i in range(40, 30, -1)}             # outage, newest first
    runs.update({i: ["ok"] * 9 + ["failing"] for i in range(30, 0, -1)})   # healthy
    kept, skipped = bs.healthy_runs(runs)
    assert skipped == 10
    assert kept == list(range(11, 31)) and len(kept) == bs.WINDOW_RUNS


def hist(statuses, unrealistic=False):
    return [{"run_at": str(i), "status": s, "unrealistic": unrealistic} for i, s in enumerate(statuses)]


def test_path_state_from_healthy_history():
    assert bs.path_state(hist(["failing"] * 20)) == "bad"
    assert bs.path_state(hist(["ok"] * 20)) == "good"
    assert bs.path_state(hist(["ok", "failing"] * 10)) == "mixed"
    assert bs.path_state(hist(["ok"] * 3)) == "unknown"
    assert bs.path_state(hist(["ok"] * 20, unrealistic=True)) == "odd"


def test_csv_lists_suspects_first_and_skips_working_sensors():
    paths = {"1": path("1001->1002", "A", "B"), "2": path("1002->1003", "B", "C"),
             "3": path("1004->1001", "D", "A"), "4": path("1005->1003", "J", "C")}
    resolved = bs.resolve_paths(paths, SENSORS)
    verdicts = bs.judge_sensors(SENSORS, resolved, {"1": "bad", "2": "bad", "3": "good", "4": "good"})
    for sid, v in verdicts.items():
        v.update(name=sid, site_code=SENSORS[sid]["site_code"], lat=SENSORS[sid]["lat"], lon=SENSORS[sid]["lon"])
    rows = bs.csv_rows({"sensors": verdicts})
    assert rows[0][0] == "Sensor ID" and rows[1][0] == "B" and rows[1][5] == bs.VERDICTS["faulty_high"]
    head = rows[0]
    assert head[3:5] == ["Latitude", "Longitude"] and head[-1] == "Total paths counted"
    assert rows[1][head.index("Total paths counted")] == 2 and rows[1][head.index("Failing paths")] == 2
    assert all(r[0] not in ("D", "J") for r in rows[1:])


# ── the run-history read is remembered until the database file changes ───────

def _runs_db(tmp_path, rows):
    import sqlite3
    f = tmp_path / "h.db"
    c = sqlite3.connect(f)
    c.execute("CREATE TABLE sensor_results (id INTEGER PRIMARY KEY, run_id TEXT, run_at TEXT, group_name TEXT, "
              "sensor_id TEXT, status TEXT, data TEXT)")
    c.executemany("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    return f, c


def test_run_statuses_are_read_once_per_database_version_and_callers_get_their_own_copy(tmp_path, monkeypatch):
    rows = [("r1", "2026-01-01T00:00:00", bs.GROUP, "10", "ok", None), ("r2", "2026-01-02T00:00:00", bs.GROUP, "10", "fail", None)]
    f, c = _runs_db(tmp_path, rows)
    calls = []
    real = bs._load_run_statuses
    monkeypatch.setattr(bs, "_load_run_statuses", lambda conn, group=bs.GROUP: calls.append(1) or real(conn, group))
    bs._RUNS_MEMO.clear()
    first = bs.load_run_statuses(c)
    second = bs.load_run_statuses(c)
    assert len(calls) == 1 and first[0] == second[0] and first[1] == second[1]
    del first[0]["r2"]                                                             # the analysis does this for skip_runs
    first[0]["r1"].append("junk")
    first[1]["10"]["r1"] = {"x": 1}
    third = bs.load_run_statuses(c)
    assert len(calls) == 1 and list(third[0]) == ["r2", "r1"] and third[0]["r1"] == ["ok"] and third[1]["10"]["r1"]["status"] == "ok"
    c.execute("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)",
              ("r3", "2026-01-03T00:00:00", bs.GROUP, "10", "ok", None))
    c.commit()                                                                      # the file changed: read again
    fresh = bs.load_run_statuses(c)
    assert len(calls) == 2 and "r3" in fresh[0]
    import sqlite3
    mem = sqlite3.connect(":memory:")                                               # no file, nothing to key on: never remembered
    mem.execute("CREATE TABLE sensor_results (id INTEGER PRIMARY KEY, run_id TEXT, run_at TEXT, group_name TEXT, sensor_id TEXT, status TEXT, data TEXT)")
    bs.load_run_statuses(mem)
    bs.load_run_statuses(mem)
    assert len(calls) == 4


def _history(tmp_path, n_runs, lacking):
    """n_runs runs, one hour apart; runs whose number `lacking` accepts have no rows for the Bluetooth group (the other group has some)."""
    import sqlite3
    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, run_at TEXT)")
    c.execute("CREATE TABLE sensor_results (id INTEGER PRIMARY KEY, run_id TEXT, run_at TEXT, group_name TEXT, sensor_id TEXT, status TEXT, data TEXT)")
    c.execute("CREATE INDEX idx_sensor_results_sensor ON sensor_results (group_name, sensor_id, run_at)")
    for i in range(n_runs):
        at = "2026-%02d-%02dT%02d:00:00+00:00" % (1 + i // 720, 1 + (i % 720) // 24, i % 24)
        c.execute("INSERT INTO runs VALUES (?,?)", ("run%03d" % i, at))
        for sid in ("1", "2", "3"):
            if not lacking(i):
                c.execute("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)",
                          ("run%03d" % i, at, bs.GROUP, sid, "ok" if (i + int(sid)) % 3 else "fail", None))
            c.execute("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)",
                      ("run%03d" % i, at, "VMS", sid, "ok", None))
    c.commit()
    return c


def test_the_fast_read_of_the_newest_runs_gives_exactly_what_the_plain_scan_gives(tmp_path):
    for n_runs, lacking in ((400, lambda i: False), (400, lambda i: i % 7 == 0), (400, lambda i: i % 5 != 0),   # most runs without rows: the margin must widen
                            (120, lambda i: False), (300, lambda i: i < 250), (5, lambda i: False)):
        c = _history(tmp_path, n_runs, lacking)
        fast = bs._recent_rows(c, bs.GROUP)
        plain = bs._scan_rows(c, bs.GROUP)
        assert fast[0] == plain[0] and sorted(fast[1]) == sorted(plain[1]), (n_runs,)
        c.close()
        (tmp_path / "x.db").unlink()
    import sqlite3
    bare = sqlite3.connect(":memory:")                                               # no runs table: the caller falls back to the plain scan
    bare.execute("CREATE TABLE sensor_results (id INTEGER PRIMARY KEY, run_id TEXT, run_at TEXT, group_name TEXT, sensor_id TEXT, status TEXT, data TEXT)")
    assert bs._recent_rows(bare, bs.GROUP) is None
