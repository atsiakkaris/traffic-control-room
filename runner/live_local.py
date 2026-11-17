"""
live_local.py - the Control Room's LOCAL copy of the Live page.

Same page as the published docs/live.html (runner/live_ui.py, built from the same data by report.live_page_ctx), plus one
thing the published page deliberately never has: each sensor's contract (project). It is added only when "QA Locations.xlsx"
is on this PC (the repo is public and the workbook is git-ignored, so CI never has it). The project names come from the
sensor_projects table that update_projects.py fills from the workbook; if the workbook was saved after that table was last
updated, the page says so.
"""
from datetime import datetime, timezone
from pathlib import Path

WORKBOOK = Path(__file__).parent.parent / "QA Locations.xlsx"


def workbook_present(path=WORKBOOK):
    return Path(path).is_file()


def stale_note(workbook_mtime, projects_updated_iso):
    """A warning when the workbook was saved after the stored projects were last updated (None when they agree or it is unknown).

    workbook_mtime: seconds since the epoch; projects_updated_iso: the newest sensor_projects.updated_at (ISO text)."""
    try:
        upd = datetime.fromisoformat(str(projects_updated_iso).replace("Z", "+00:00"))
        if upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    wb = datetime.fromtimestamp(workbook_mtime, timezone.utc)
    if (wb - upd).total_seconds() > 120:
        return ("QA Locations.xlsx was saved after the contracts shown here were last updated (%s). Run update_projects.bat "
                "to bring them up to date." % upd.astimezone().strftime("%d/%m/%y %H:%M"))
    return None


def build(workbook=WORKBOOK):
    """The whole local Live page as one HTML string."""
    import attention
    import db
    import live_ui
    import report
    from stability import load_project_accountability

    runs = db.fetch_recent_runs(1)
    if not runs:
        return "<html><body style='font:15px Arial;padding:30px'>No monitor runs in the database yet.</body></html>"
    latest = runs[0]
    all_coords = db.fetch_sensor_coords()
    all_bt_paths = db.fetch_bt_path_coords()
    sensor_projects = db.fetch_sensor_projects()
    project_acct = load_project_accountability()
    keys = {(g, sid) for g, d in all_coords.items() for sid in d} | {("Bluetooth Paths", pid) for pid in all_bt_paths}
    active = [s for s in db.fetch_sensor_stability() if (s["group_name"], s["sensor_id"]) in keys]
    ctx, bt_now = report.live_page_ctx(active, all_coords, all_bt_paths, sensor_projects, project_acct,
                                       report._to_cyprus(latest["run_at"]), latest["run_at"])
    if workbook_present(workbook):
        conn = db.get_connection()
        newest = conn.execute("SELECT MAX(updated_at) FROM sensor_projects").fetchone()[0]
        conn.close()
        attention.add_projects(ctx, sensor_projects, project_acct, bt_now, all_coords.get("Bluetooth", {}),
                               stale_note(Path(workbook).stat().st_mtime, newest))
    else:
        ctx["projects_note"] = "QA Locations.xlsx was not found on this PC, so contracts are not shown here."
    return live_ui.render(ctx)
