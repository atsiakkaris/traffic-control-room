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
    """)
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
                  status, status_code, expected_code, response_ms, failure_reason):
    conn = get_connection()
    conn.execute(
        """INSERT INTO test_results
           (run_id, group_name, test_name, endpoint, method, status,
            status_code, expected_code, response_ms, failure_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (run_id, group_name, test_name, endpoint, method, status,
         status_code, expected_code, response_ms, failure_reason)
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


def insert_sensor_result(run_id, run_at, group_name, sensor_id, status):
    conn = get_connection()
    conn.execute(
        "INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status) VALUES (?,?,?,?,?)",
        (run_id, run_at, group_name, sensor_id, status)
    )
    conn.commit()
    conn.close()


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
