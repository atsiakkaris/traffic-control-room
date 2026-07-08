"""
Database helpers — SQLite-backed test history.
"""

import json
import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "results/history.db")

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS runs (
        run_id  TEXT PRIMARY KEY,
        run_at  TEXT NOT NULL,
        total   INTEGER DEFAULT 0,
        passed  INTEGER DEFAULT 0,
        failed  INTEGER DEFAULT 0,
        errored INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS test_results (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id         TEXT NOT NULL,
        group_name     TEXT NOT NULL,
        test_name      TEXT NOT NULL,
        endpoint       TEXT NOT NULL,
        method         TEXT NOT NULL DEFAULT 'GET',
        status         TEXT NOT NULL,
        status_code    INTEGER,
        expected_code  INTEGER DEFAULT 200,
        response_ms    REAL,
        failure_reason TEXT,
        check_summary  TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
    );

    CREATE TABLE IF NOT EXISTS sensor_results (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     TEXT NOT NULL,
        run_at     TEXT NOT NULL,
        group_name TEXT NOT NULL,
        sensor_id  TEXT NOT NULL,
        status     TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_sensor_results_sensor
        ON sensor_results (group_name, sensor_id, run_at);

    CREATE TABLE IF NOT EXISTS sensor_coords (
        sensor_id  TEXT NOT NULL,
        group_name TEXT NOT NULL,
        lat        REAL NOT NULL,
        lon        REAL NOT NULL,
        name       TEXT,
        site_code  TEXT,
        active     INTEGER NOT NULL DEFAULT 1,
        last_seen  TEXT,
        PRIMARY KEY (sensor_id, group_name)
    );

    CREATE TABLE IF NOT EXISTS bt_path_coords (
        path_id TEXT PRIMARY KEY,
        name    TEXT,
        coords  TEXT,
        active  INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS sensor_projects (
        sensor_id     TEXT NOT NULL,
        group_name    TEXT NOT NULL,
        project       TEXT,
        source        TEXT,
        commissioning TEXT NOT NULL DEFAULT 'active',
        updated_at    TEXT,
        PRIMARY KEY (sensor_id, group_name)
    );
"""


# DB paths whose schema+migrations have already been applied in this process.
# The schema is self-healing (any script that connects first gets an up-to-date
# DB), but running the full CREATE-TABLE script + PRAGMA-based migration checks
# on every single connection is pure overhead — run_tests.py opens dozens of
# connections per run. Applying it once per path keeps the self-healing property
# while skipping the repeated reflection. Keyed by path so tests that point
# DB_PATH at a fresh file still get migrated.
_SCHEMA_APPLIED = set()


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if DB_PATH not in _SCHEMA_APPLIED:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        _SCHEMA_APPLIED.add(DB_PATH)
    return conn


def _has_column(conn, table, column):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _migrate(conn):
    """Apply any missing schema migrations in order."""
    if not _has_column(conn, "test_results", "check_summary"):
        conn.execute("ALTER TABLE test_results ADD COLUMN check_summary TEXT")

    if not _has_column(conn, "sensor_results", "data"):
        conn.execute("ALTER TABLE sensor_results ADD COLUMN data TEXT")

    if not _has_table(conn, "sensor_coords"):
        conn.execute("""
            CREATE TABLE sensor_coords (
                sensor_id  TEXT NOT NULL,
                group_name TEXT NOT NULL,
                lat        REAL NOT NULL,
                lon        REAL NOT NULL,
                name       TEXT,
                site_code  TEXT,
                PRIMARY KEY (sensor_id, group_name)
            )
        """)
    else:
        if not _has_column(conn, "sensor_coords", "site_code"):
            conn.execute("ALTER TABLE sensor_coords ADD COLUMN site_code TEXT")
        if not _has_column(conn, "sensor_coords", "active"):
            conn.execute("ALTER TABLE sensor_coords ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if not _has_column(conn, "sensor_coords", "last_seen"):
            conn.execute("ALTER TABLE sensor_coords ADD COLUMN last_seen TEXT")

    if not _has_table(conn, "bt_path_coords"):
        conn.execute("""
            CREATE TABLE bt_path_coords (
                path_id TEXT PRIMARY KEY,
                name    TEXT,
                coords  TEXT,
                active  INTEGER NOT NULL DEFAULT 1
            )
        """)
    elif not _has_column(conn, "bt_path_coords", "active"):
        conn.execute("ALTER TABLE bt_path_coords ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

    if not _has_table(conn, "sensor_projects"):
        conn.execute("""
            CREATE TABLE sensor_projects (
                sensor_id     TEXT NOT NULL,
                group_name    TEXT NOT NULL,
                project       TEXT,
                source        TEXT,
                commissioning TEXT NOT NULL DEFAULT 'active',
                updated_at    TEXT,
                PRIMARY KEY (sensor_id, group_name)
            )
        """)
    elif not _has_column(conn, "sensor_projects", "commissioning"):
        conn.execute("ALTER TABLE sensor_projects ADD COLUMN commissioning TEXT NOT NULL DEFAULT 'active'")

    conn.commit()


def init_db():
    # get_connection() already applies _SCHEMA + _migrate; kept as an explicit
    # entry point for callers (e.g. run_tests.py) that want to ensure the DB
    # exists before doing anything else.
    get_connection().close()


def insert_run(run_id, run_at, totals):
    conn = get_connection()
    conn.execute(
        "INSERT INTO runs (run_id, run_at, total, passed, failed, errored) VALUES (?,?,?,?,?,?)",
        (run_id, run_at, totals["total"], totals["passed"], totals["failed"], totals["errored"])
    )
    conn.commit()
    conn.close()


def insert_result(run_id, group_name, test_name, endpoint, method,
                  status, status_code, expected_code, response_ms,
                  failure_reason, check_summary=None):
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


def insert_sensor_result(run_id, run_at, group_name, sensor_id, status, data=None):
    conn = get_connection()
    conn.execute(
        "INSERT INTO sensor_results (run_id, run_at, group_name, sensor_id, status, data) VALUES (?,?,?,?,?,?)",
        (run_id, run_at, group_name, sensor_id, status, json.dumps(data) if data else None)
    )
    conn.commit()
    conn.close()


def upsert_sensor_coords(group_name, coords_dict):
    """coords_dict: {sensor_id: {lat, lon, name, site_code?}}"""
    now = datetime.now(timezone.utc).isoformat()
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


def upsert_bt_path_coords(paths_dict):
    """paths_dict: {path_id: {name, coords: [[lat,lon],...]}}"""
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


def upsert_sensor_projects(group_name, projects_dict):
    """projects_dict: {sensor_id: {project, source, commissioning?}}. project may
    be None to clear a sensor's assignment (e.g. it no longer matches a reference
    row). commissioning defaults to 'active' and is 'not_electrified' for sensors
    the reference sheet marks as awaiting power / not yet commissioned."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    for sid, p in projects_dict.items():
        conn.execute(
            """INSERT INTO sensor_projects (sensor_id, group_name, project, source, commissioning, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(sensor_id, group_name) DO UPDATE
               SET project=excluded.project, source=excluded.source,
                   commissioning=excluded.commissioning, updated_at=excluded.updated_at""",
            (sid, group_name, p.get("project"), p.get("source"),
             p.get("commissioning") or "active", now)
        )
    conn.commit()
    conn.close()


def fetch_sensor_projects():
    """Return {group_name: {sensor_id: {project, source, commissioning}}} for
    sensors that have either a known project or a non-active commissioning state."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT sensor_id, group_name, project, source, commissioning FROM sensor_projects "
        "WHERE project IS NOT NULL OR commissioning != 'active'"
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["group_name"], {})[r["sensor_id"]] = {
            "project": r["project"], "source": r["source"],
            "commissioning": r["commissioning"] or "active",
        }
    return result


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
        """SELECT group_name, sensor_id, status FROM sensor_results
           WHERE run_id = ? ORDER BY group_name, status, sensor_id""",
        (run_id,)
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        result.setdefault(row["group_name"], {}).setdefault(row["status"], []).append(row["sensor_id"])
    return result


def fetch_sensor_live_data_for_run(run_id):
    """Return {group_name: {sensor_id: {status, data}}} for a specific run."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT group_name, sensor_id, status, data FROM sensor_results WHERE run_id = ?",
        (run_id,)
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        result.setdefault(row["group_name"], {})[row["sensor_id"]] = {
            "status": row["status"],
            "data": json.loads(row["data"]) if row["data"] else {},
        }
    return result


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
            "lat": r["lat"],
            "lon": r["lon"],
            "name": r["name"] or r["sensor_id"],
            "site_code": r["site_code"],
        }
    return result


def fetch_bt_path_coords():
    """Return {path_id: {name, coords: [[lat,lon],...]}} — active paths only."""
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


def fetch_sensor_health_history(limit=30, live_test_names=None):
    """Return per-run health data for live sensor endpoints, newest-first.

    live_test_names: list of test_name strings to include. If omitted, all tests are returned.
    limit: number of distinct runs to cover.
    """
    conn = get_connection()
    if live_test_names:
        placeholders = ",".join("?" * len(live_test_names))
        rows = conn.execute(f"""
            SELECT r.run_id, r.run_at, tr.test_name, tr.status, tr.check_summary, tr.failure_reason
            FROM (SELECT run_id, run_at FROM runs ORDER BY run_at DESC LIMIT ?) r
            JOIN test_results tr ON tr.run_id = r.run_id
            WHERE tr.test_name IN ({placeholders})
            ORDER BY r.run_at DESC
        """, [limit] + list(live_test_names)).fetchall()
    else:
        rows = conn.execute("""
            SELECT r.run_id, r.run_at, tr.test_name, tr.status, tr.check_summary, tr.failure_reason
            FROM runs r
            JOIN test_results tr ON tr.run_id = r.run_id
            ORDER BY r.run_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_sensor_status_counts(limit=200):
    """Return {run_id: {group_name: {status: count}}} for the most recent `limit`
    runs, aggregated from per-sensor rows. This is the authoritative source for
    group health percentages — callers derive good/total from the counts instead
    of re-parsing the human-readable check_summary text."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT sr.run_id, sr.group_name, sr.status, COUNT(*) AS n
        FROM (SELECT run_id FROM runs ORDER BY run_at DESC LIMIT ?) r
        JOIN sensor_results sr ON sr.run_id = r.run_id
        GROUP BY sr.run_id, sr.group_name, sr.status
    """, (limit,)).fetchall()
    conn.close()
    result = {}
    for row in rows:
        result.setdefault(row["run_id"], {}).setdefault(row["group_name"], {})[row["status"]] = row["n"]
    return result


def fetch_sensor_ids_for_run(run_id, group_name):
    """Return the set of sensor IDs recorded for a given run and group."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT sensor_id FROM sensor_results WHERE run_id=? AND group_name=?",
        (run_id, group_name)
    ).fetchall()
    conn.close()
    return {r["sensor_id"] for r in rows}


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
