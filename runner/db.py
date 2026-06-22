"""
Database helpers — SQLite-backed test history.
"""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "results/history.db")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id      TEXT PRIMARY KEY,
            run_at      TEXT NOT NULL,
            total       INTEGER DEFAULT 0,
            passed      INTEGER DEFAULT 0,
            failed      INTEGER DEFAULT 0,
            errored     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS test_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT NOT NULL,
            group_name      TEXT NOT NULL,
            test_name       TEXT NOT NULL,
            endpoint        TEXT NOT NULL,
            method          TEXT NOT NULL DEFAULT 'GET',
            status          TEXT NOT NULL,
            status_code     INTEGER,
            expected_code   INTEGER DEFAULT 200,
            response_ms     REAL,
            failure_reason  TEXT,
            check_summary   TEXT,
            FOREIGN KEY (run_id) REFERENCES runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS sensor_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT NOT NULL,
            run_at      TEXT NOT NULL,
            group_name  TEXT NOT NULL,
            sensor_id   TEXT NOT NULL,
            status      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sensor_results_sensor
            ON sensor_results (group_name, sensor_id, run_at);

        CREATE TABLE IF NOT EXISTS sensor_coords (
            sensor_id   TEXT NOT NULL,
            group_name  TEXT NOT NULL,
            lat         REAL NOT NULL,
            lon         REAL NOT NULL,
            name        TEXT,
            site_code   TEXT,
            active      INTEGER NOT NULL DEFAULT 1,
            last_seen   TEXT,
            PRIMARY KEY (sensor_id, group_name)
        );

        CREATE TABLE IF NOT EXISTS bt_path_coords (
            path_id TEXT PRIMARY KEY,
            name    TEXT,
            coords  TEXT,
            active  INTEGER NOT NULL DEFAULT 1
        );
    """)
    conn.commit()

    # Migrate: add check_summary if the DB predates this column
    cols = [r[1] for r in conn.execute("PRAGMA table_info(test_results)").fetchall()]
    if "check_summary" not in cols:
        conn.execute("ALTER TABLE test_results ADD COLUMN check_summary TEXT")
        conn.commit()

    # Migrate: add data column to sensor_results if absent
    sr_cols = [r[1] for r in conn.execute("PRAGMA table_info(sensor_results)").fetchall()]
    if "data" not in sr_cols:
        conn.execute("ALTER TABLE sensor_results ADD COLUMN data TEXT")
        conn.commit()

    # Migrate: create coord tables if they predate this schema addition
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "sensor_coords" not in tables:
        conn.execute("""CREATE TABLE sensor_coords (
            sensor_id TEXT NOT NULL, group_name TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL, name TEXT, site_code TEXT,
            PRIMARY KEY (sensor_id, group_name))""")
        conn.commit()
    else:
        sc_cols = [r[1] for r in conn.execute("PRAGMA table_info(sensor_coords)").fetchall()]
        if "site_code" not in sc_cols:
            conn.execute("ALTER TABLE sensor_coords ADD COLUMN site_code TEXT")
            conn.commit()
    if "bt_path_coords" not in tables:
        conn.execute("""CREATE TABLE bt_path_coords (
            path_id TEXT PRIMARY KEY, name TEXT, coords TEXT,
            active INTEGER NOT NULL DEFAULT 1)""")
        conn.commit()

    # Migrate: add active and last_seen columns to sensor_coords
    sc_cols = [r[1] for r in conn.execute("PRAGMA table_info(sensor_coords)").fetchall()]
    if "active" not in sc_cols:
        conn.execute("ALTER TABLE sensor_coords ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    if "last_seen" not in sc_cols:
        conn.execute("ALTER TABLE sensor_coords ADD COLUMN last_seen TEXT")
        conn.commit()
    bt_cols = [r[1] for r in conn.execute("PRAGMA table_info(bt_path_coords)").fetchall()]
    if "active" not in bt_cols:
        conn.execute("ALTER TABLE bt_path_coords ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    conn.close()


def insert_run(run_id, run_at, totals):
    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (run_id, run_at, total, passed, failed, errored) VALUES (?,?,?,?,?,?)",
        (run_id, run_at, totals["total"], totals["passed"], totals["failed"], totals["errored"])
    )
    conn.commit()
    conn.close()


def insert_result(run_id, group_name, test_name, endpoint, method,
                  status, status_code, expected_code, response_ms, failure_reason,
                  check_summary=None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO test_results
           (run_id, group_name, test_name, endpoint, method, status,
            status_code, expected_code, response_ms, failure_reason, check_summary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, group_name, test_name, endpoint, method, status,
         status_code, expected_code, response_ms, failure_reason, check_summary)
    )
    conn.commit()
    conn.close()


def fetch_recent_runs(limit=30):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY run_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_results_for_run(run_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM test_results WHERE run_id = ? ORDER BY group_name, test_name",
        (run_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_history_for_test(test_name, limit=30):
    """Return recent pass/fail history for one test (for trend charts)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT tr.status, tr.response_ms, r.run_at
           FROM test_results tr
           JOIN runs r ON r.run_id = tr.run_id
           WHERE tr.test_name = ?
           ORDER BY r.run_at DESC LIMIT ?""",
        (test_name, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_sensor_statuses_for_run(run_id):
    """Return {group_name: {status: [sensor_id, ...]}} for a specific run."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT group_name, sensor_id, status FROM sensor_results WHERE run_id = ? ORDER BY group_name, status, sensor_id",
        (run_id,)
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        g, sid, s = row["group_name"], row["sensor_id"], row["status"]
        result.setdefault(g, {}).setdefault(s, []).append(sid)
    return result


def insert_sensor_result(run_id, run_at, group_name, sensor_id, status, data=None):
    import json as _json
    conn = get_connection()
    conn.execute(
        "INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)",
        (run_id, run_at, group_name, sensor_id, status, _json.dumps(data) if data else None)
    )
    conn.commit()
    conn.close()


def fetch_sensor_live_data_for_run(run_id):
    """Return {group_name: {sensor_id: {status, data}}} for a specific run."""
    import json as _json
    conn = get_connection()
    rows = conn.execute(
        "SELECT group_name, sensor_id, status, data FROM sensor_results WHERE run_id = ?",
        (run_id,)
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        g, sid = row["group_name"], row["sensor_id"]
        result.setdefault(g, {})[sid] = {
            "status": row["status"],
            "data": _json.loads(row["data"]) if row["data"] else {},
        }
    return result


def upsert_sensor_coords(group_name, coords_dict):
    """coords_dict: {sensor_id: {lat, lon, name, site_code?}}"""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = get_connection()
    for sid, c in coords_dict.items():
        conn.execute(
            """INSERT INTO sensor_coords (sensor_id, group_name, lat, lon, name, site_code, active, last_seen)
               VALUES (?,?,?,?,?,?,1,?)
               ON CONFLICT(sensor_id, group_name) DO UPDATE
               SET lat=excluded.lat, lon=excluded.lon, name=excluded.name,
                   site_code=excluded.site_code, active=1, last_seen=excluded.last_seen""",
            (sid, group_name, c["lat"], c["lon"], c.get("name", sid), c.get("site_code"), now)
        )
    conn.commit()
    conn.close()


def retire_missing_sensors(group_name, active_ids):
    """Mark sensors in group_name as inactive if their ID is not in active_ids."""
    conn = get_connection()
    if active_ids:
        placeholders = ",".join("?" * len(active_ids))
        conn.execute(
            f"UPDATE sensor_coords SET active=0 WHERE group_name=? AND sensor_id NOT IN ({placeholders})",
            [group_name] + list(active_ids)
        )
    else:
        conn.execute("UPDATE sensor_coords SET active=0 WHERE group_name=?", (group_name,))
    conn.commit()
    conn.close()


def upsert_bt_path_coords(paths_dict):
    """paths_dict: {path_id: {name, coords: [[lat,lon],...]}}"""
    import json
    conn = get_connection()
    for pid, p in paths_dict.items():
        conn.execute(
            """INSERT INTO bt_path_coords (path_id, name, coords, active)
               VALUES (?,?,?,1)
               ON CONFLICT(path_id) DO UPDATE
               SET name=excluded.name, coords=excluded.coords, active=1""",
            (pid, p["name"], json.dumps(p["coords"]))
        )
    conn.commit()
    conn.close()


def retire_missing_bt_paths(active_ids):
    """Mark BT paths as inactive if their ID is not in active_ids."""
    conn = get_connection()
    if active_ids:
        placeholders = ",".join("?" * len(active_ids))
        conn.execute(
            f"UPDATE bt_path_coords SET active=0 WHERE path_id NOT IN ({placeholders})",
            list(active_ids)
        )
    else:
        conn.execute("UPDATE bt_path_coords SET active=0")
    conn.commit()
    conn.close()


def fetch_sensor_coords():
    """Return {group_name: {sensor_id: {lat, lon, name, site_code}}} — active sensors only."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT sensor_id, group_name, lat, lon, name, site_code FROM sensor_coords WHERE active=1"
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["group_name"], {})[r["sensor_id"]] = {
            "lat": r["lat"], "lon": r["lon"],
            "name": r["name"] or r["sensor_id"],
            "site_code": r["site_code"],
        }
    return result


def fetch_bt_path_coords():
    """Return {path_id: {name, coords: [[lat,lon],...]}} — active paths only."""
    import json
    conn = get_connection()
    rows = conn.execute(
        "SELECT path_id, name, coords FROM bt_path_coords WHERE active=1"
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["path_id"]] = {"name": r["name"], "coords": json.loads(r["coords"])}
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def fetch_sensor_health_history(limit=30):
    """Return per-run health data for sensor check endpoints, newest-first.
    One row per sensor endpoint per run (up to limit runs × 3 endpoints)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.run_id, r.run_at, tr.test_name, tr.status, tr.check_summary, tr.failure_reason
        FROM runs r
        JOIN test_results tr ON tr.run_id = r.run_id
        WHERE tr.test_name IN ('Traffic Detection Live', 'VMS Live Data', 'Bluetooth Paths Live (FCD)')
        ORDER BY r.run_at DESC
        LIMIT ?
    """, (limit * 3,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_sensor_stability():
    """Return per-sensor history: [{group_name, sensor_id, history: [{run_at, status}]}]"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT group_name, sensor_id, run_at, status FROM sensor_results ORDER BY group_name, sensor_id, run_at"
    ).fetchall()
    conn.close()

    sensors = {}
    for row in rows:
        key = (row["group_name"], row["sensor_id"])
        if key not in sensors:
            sensors[key] = {"group_name": row["group_name"], "sensor_id": row["sensor_id"], "history": []}
        sensors[key]["history"].append({"run_at": row["run_at"], "status": row["status"]})
    return list(sensors.values())
