"""Moving the history of a re-created path onto its new id."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import path_history_merge as phm  # noqa: E402
import merge_path_history as cli  # noqa: E402


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE sensor_results (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, run_at TEXT, group_name TEXT, sensor_id TEXT, status TEXT, data TEXT)")
    c.execute("CREATE TABLE bt_path_last_ts (path_id TEXT PRIMARY KEY, measurement_timestamp TEXT NOT NULL)")
    rows = [(f"r{i}", f"2026-10-0{i}", "Bluetooth Paths", "250", "ok", None) for i in range(1, 6)]
    rows += [(f"r{i}", f"2026-10-0{i}", "Bluetooth Paths", "661", "failing", None) for i in (8, 9)]
    rows += [(f"r{i}", f"2026-10-0{i}", "Bluetooth Paths", "400", "ok", None) for i in (1, 2)]
    rows += [("r1", "2026-10-01", "VMS", "250", "ok", None)]                                  # another group, same id
    c.executemany("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)", rows)
    c.executemany("INSERT INTO bt_path_last_ts VALUES (?,?)", [("250", "old"), ("661", "new"), ("400", "x")])
    c.commit()
    return c


def test_plan_reports_rows_and_refuses_unsafe_pairs(tmp_path):
    c = make_db(tmp_path / "h.db")
    steps = {s["old"]: s for s in phm.plan(c, [("250", "661"), ("400", "400"), ("999", "662"), ("x", "1")])}
    assert steps["250"]["old_rows"] == 5 and steps["250"]["new_rows"] == 2 and steps["250"]["problem"] is None
    assert "same" in steps["400"]["problem"] and "no history" in steps["999"]["problem"] and "numbers" in steps["x"]["problem"]
    c.execute("INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status) VALUES ('r1','x','Bluetooth Paths','661','ok')")
    assert "same run" in phm.plan(c, [("250", "661")])[0]["problem"]


def test_apply_moves_only_the_bluetooth_paths_history_and_keeps_the_newest_timestamp(tmp_path):
    c = make_db(tmp_path / "h.db")
    steps = phm.apply(c, [("250", "661"), ("400", "663")])
    assert [s["moved"] for s in steps] == [5, 2]
    ids = lambda sid, g="Bluetooth Paths": c.execute("SELECT COUNT(*) FROM sensor_results WHERE group_name=? AND sensor_id=?", (g, sid)).fetchone()[0]
    assert ids("250") == 0 and ids("661") == 7 and ids("663") == 2 and ids("250", "VMS") == 1
    ts = dict(c.execute("SELECT path_id, measurement_timestamp FROM bt_path_last_ts"))
    assert ts == {"661": "new", "663": "x"}                       # 661 kept its newer value, 400's row moved to 663
    assert phm.apply(c, [("250", "661")])[0]["problem"]            # a second run finds nothing left to move


def test_command_line_dry_run_changes_nothing_and_apply_writes_a_backup(tmp_path, capsys):
    db = tmp_path / "h.db"
    make_db(db).close()
    assert cli.main(["250=661", "--db", str(db)]) == 0
    assert "Dry run" in capsys.readouterr().out and sqlite3.connect(db).execute("SELECT COUNT(*) FROM sensor_results WHERE sensor_id='250'").fetchone()[0] == 6
    assert cli.main(["250=661", "--db", str(db), "--apply"]) == 0
    assert len(list(tmp_path.glob("h.db.bak-*"))) == 1
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM sensor_results WHERE sensor_id='661'").fetchone()[0] == 7
    assert cli.main(["nonsense", "--db", str(db)]) == 2
