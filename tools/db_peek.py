"""
db_peek.py - quick look inside results/history.db (the ITS health monitor store).

Usage (from the traffic-control-room folder):

    python tools/db_peek.py                     # schema + row counts + 1 sample row per table
    python tools/db_peek.py "SELECT ..."        # run any SQL, pretty-printed
    python tools/db_peek.py -q stale-timeline   # run a named canned query (see QUERIES below)
    python tools/db_peek.py -l                  # list the canned queries

Read-only: opens the file with mode=ro, so you cannot change anything.
No install needed - uses Python's built-in sqlite3.
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent.parent / "results" / "history.db"

QUERIES = {
    # ---- what the DB stores -------------------------------------------------
    "groups": """
        SELECT group_name, status, COUNT(*) n
        FROM sensor_results GROUP BY group_name, status ORDER BY group_name, n DESC
    """,
    "runs-cadence": """
        SELECT substr(run_at,1,10) day, COUNT(*) runs,
               MIN(substr(run_at,12,5)) first, MAX(substr(run_at,12,5)) last
        FROM runs GROUP BY day ORDER BY day DESC LIMIT 20
    """,
    # ---- the staleness question ------------------------------------------------
    "stale-timeline": """
        SELECT run_at,
               SUM(status='ok')      AS ok,
               SUM(status='stale')   AS stale,
               SUM(status='failing') AS failing,
               SUM(status='missing') AS missing
        FROM sensor_results
        WHERE group_name='Bluetooth Paths'
        GROUP BY run_id ORDER BY run_at
    """,
    "stale-by-day": """
        SELECT substr(run_at,1,10) day,
               MAX(CASE WHEN status='stale' THEN 1 ELSE 0 END) any_stale,
               COUNT(DISTINCT run_id) runs
        FROM sensor_results
        WHERE group_name='Bluetooth Paths'
        GROUP BY day ORDER BY day
    """,
    "stale-paths-now": """
        SELECT sr.sensor_id, bt.name, sr.run_at
        FROM sensor_results sr
        LEFT JOIN bt_path_coords bt ON bt.path_id = sr.sensor_id
        WHERE sr.group_name='Bluetooth Paths' AND sr.status='stale'
          AND sr.run_id = (SELECT run_id FROM sensor_results
                           WHERE group_name='Bluetooth Paths'
                           ORDER BY run_at DESC LIMIT 1)
        ORDER BY CAST(sr.sensor_id AS INTEGER)
    """,
    "path-history": """
        -- status of one path over time; edit the id
        SELECT run_at, status, data
        FROM sensor_results
        WHERE group_name='Bluetooth Paths' AND sensor_id='169'
        ORDER BY run_at
    """,
    "data-populated": """
        -- how often the per-path `data` blob is actually filled (mostly NULL)
        SELECT substr(run_at,1,10) day, COUNT(*) rows_with_data
        FROM sensor_results
        WHERE group_name='Bluetooth Paths' AND data IS NOT NULL
        GROUP BY day ORDER BY day
    """,
    "feed-state": "SELECT * FROM feed_state",
}


def show_table(rows, headers):
    if not rows:
        print("(no rows)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))
    print(f"\n{len(rows)} row(s)")


def run_sql(sql):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.execute(sql)
    rows = cur.fetchall()
    headers = [d[0] for d in cur.description] if cur.description else []
    show_table(rows, headers)


def schema():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print(f"{DB}  ({DB.stat().st_size/1e6:.1f} MB)\n")
    tabs = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tabs:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        cols = [(r[1], r[2]) for r in con.execute(f'PRAGMA table_info("{t}")')]
        print(f"### {t}   ({n:,} rows)")
        print("    " + ", ".join(f"{a} {b}" for a, b in cols))
        row = con.execute(f'SELECT * FROM "{t}" LIMIT 1').fetchone()
        if row:
            for k in row.keys():
                v = "" if row[k] is None else str(row[k]).replace("\n", " ")
                if len(v) > 88:
                    v = v[:88] + "..."
                print(f"      {k:22} = {v}")
        print()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        schema()
    elif args[0] == "-l":
        for k, v in QUERIES.items():
            print(f"  {k:16} {' '.join(v.split())[:80]}")
    elif args[0] == "-q":
        run_sql(QUERIES[args[1]])
    else:
        run_sql(args[0])
