"""
path_history_merge.py - move the monitor history of a deleted BT path onto the path that replaced it.

OMNIA does not let a path be edited into another geometry and never hands a deleted id out again, so a path that is
re-drawn comes back under a new id. Everything we know about it (history.db: sensor_results, bt_path_last_ts) is
keyed by the old id. Merging re-keys the old history to the new id, so the path keeps one continuous history.

Pure functions over a sqlite connection; the command line (tools/merge_path_history.py) adds the backup and the
dry run. Only the 'Bluetooth Paths' group is touched.
"""

GROUP = "Bluetooth Paths"


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def plan(conn, pairs):
    """For each (old id, new id): what a merge would move and whether it is safe. Returns a list of dicts."""
    out = []
    olds = {o for o, _ in pairs}
    for old, new in pairs:
        row = {"old": old, "new": new, "old_rows": 0, "new_rows": 0, "shared_runs": 0, "problem": None}
        out.append(row)
        if not (old.isdigit() and new.isdigit()):
            row["problem"] = "path ids must be numbers"
            continue
        if old == new:
            row["problem"] = "old and new id are the same"
            continue
        if new in olds:
            row["problem"] = "the new id is itself an old id in this list"
            continue
        q = "SELECT COUNT(*) FROM sensor_results WHERE group_name=? AND sensor_id=?"
        row["old_rows"] = _count(conn, q, GROUP, old)
        row["new_rows"] = _count(conn, q, GROUP, new)
        row["shared_runs"] = _count(
            conn, "SELECT COUNT(*) FROM sensor_results a JOIN sensor_results b ON a.run_id=b.run_id AND a.group_name=b.group_name "
                  "WHERE a.group_name=? AND a.sensor_id=? AND b.sensor_id=?", GROUP, old, new)
        if row["old_rows"] == 0:
            row["problem"] = "the old id has no history to move"
        elif row["shared_runs"]:
            row["problem"] = "both ids have a result in the same run (%d); refusing to mix them" % row["shared_runs"]
    return out


def apply(conn, pairs):
    """Re-key the history of every safe pair in one transaction. Returns the plan with 'moved' filled in."""
    steps = plan(conn, pairs)
    with conn:
        for s in steps:
            s["moved"] = 0
            if s["problem"]:
                continue
            cur = conn.execute("UPDATE sensor_results SET sensor_id=? WHERE group_name=? AND sensor_id=?", (s["new"], GROUP, s["old"]))
            s["moved"] = cur.rowcount
            # the newest measurement timestamp is kept; the old path's row goes away
            has_new = _count(conn, "SELECT COUNT(*) FROM bt_path_last_ts WHERE path_id=?", s["new"])
            if has_new:
                conn.execute("DELETE FROM bt_path_last_ts WHERE path_id=?", (s["old"],))
            else:
                conn.execute("UPDATE bt_path_last_ts SET path_id=? WHERE path_id=?", (s["new"], s["old"]))
    return steps
