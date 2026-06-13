"""
report.py - Generate a static HTML report from the SQLite history DB.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone

from zoneinfo import ZoneInfo
from db import get_connection, fetch_recent_runs, fetch_results_for_run, fetch_sensor_stability, fetch_sensor_statuses_for_run, fetch_sensor_coords, fetch_bt_path_coords, fetch_sensor_live_data_for_run

CYPRUS_TZ = ZoneInfo("Asia/Nicosia")


def _to_cyprus(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp string to Cyprus time, formatted for display."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(CYPRUS_TZ)
    return local.strftime("%Y-%m-%d %H:%M")

REPORT_PATH = Path("reports/latest.html")


def parse_vms_detail(text):
    if not text or "vms_controller_status" not in text:
        return None
    import re
    working = re.search(r"Working: (\d+)", text)
    not_working = re.search(r"Not working: (\d+)", text)
    no_status = re.search(r"No status: (\d+)", text)
    ids_match = re.search(r"Not working: \d+ — ([\d ,\(\)a-zA-Z]+?)(?:\||\Z)", text)
    ns_ids_match = re.search(r"No status: \d+ — ([\d ,]+?)(?:\||\Z)", text)
    return {
        "working": int(working.group(1)) if working else 0,
        "not_working": int(not_working.group(1)) if not_working else 0,
        "no_status": int(no_status.group(1)) if no_status else 0,
        "not_working_ids": ids_match.group(1).strip() if ids_match else "",
        "no_status_ids": ns_ids_match.group(1).strip() if ns_ids_match else "",
    }


def parse_bt_detail(text):
    if not text or "bt_paths_speed_and_traveltime" not in text:
        return None
    import re
    speed_ok = re.search(r"Speed OK: (\d+)/(\d+)", text)
    failing = re.search(r"Failing paths: (.+?)$", text)
    return {
        "speed_ok": int(speed_ok.group(1)) if speed_ok else 0,
        "total": int(speed_ok.group(2)) if speed_ok else 0,
        "failing_paths": failing.group(1).strip() if failing else "",
    }


def parse_sensor_detail(text):
    if not text or "sensor_speed_status" not in text:
        return None
    import re
    working = re.search(r"Working: (\d+)/(\d+)", text)
    no_traffic = re.search(r"No traffic \(speed=0\): (\d+)", text)
    malfunction = re.search(r"Malfunctioning \(speed=-1\): (\d+)", text)
    no_meas = re.search(r"No measurement: (\d+)", text)
    mal_ids = re.search(r"Malfunctioning \(speed=-1\): \d+ — ([\d ,]+?)(?:\||\Z)", text)
    avg_flow = re.search(r"Avg flow rate: ([\d.]+)", text)
    return {
        "working": int(working.group(1)) if working else 0,
        "total": int(working.group(2)) if working else 0,
        "no_traffic": int(no_traffic.group(1)) if no_traffic else 0,
        "malfunctioning": int(malfunction.group(1)) if malfunction else 0,
        "no_measurement": int(no_meas.group(1)) if no_meas else 0,
        "mal_ids": mal_ids.group(1).strip() if mal_ids else "",
        "avg_flow": float(avg_flow.group(1)) if avg_flow else 0,
    }


STATUS_COLOR = {
    "working": "#1d9e75",
    "ok": "#1d9e75",
    "no_traffic": "#9ca3af",
    "no_measurement": "#9ca3af",
    "no_status": "#9ca3af",
    "not_working": "#e24b4a",
    "malfunctioning": "#e24b4a",
    "failing": "#e24b4a",
    "missing": "#e58e0a",
    "stale": "#e58e0a",
}

STATUS_LABEL = {
    "working": "Working",
    "ok": "OK",
    "no_traffic": "No traffic",
    "no_measurement": "No data",
    "no_status": "No status reported",
    "not_working": "Controller not working",
    "malfunctioning": "Speed = -1 (sensor fault)",
    "failing": "No speed or travel time",
    "missing": "Not present in feed",
    "stale": "Feed data is stale",
}

GOOD_STATUSES = {"working", "ok"}

GROUP_DISPLAY = {"Traffic Detection": "Traffic Detection (SWARCO)"}

SENSOR_CHECKS = {"sensor_speed_status", "vms_controller_status", "bt_paths_speed_and_traveltime"}
HEALTH_WARNING_PCT = 80


def _extract_health_pct(check_summary, check_name):
    """Extract a 0-100 health percentage from a check_summary string for a given sensor check."""
    import re
    if not check_summary or check_name not in check_summary:
        return None
    if check_name == "sensor_speed_status":
        m = re.search(r"Working: (\d+)/(\d+)", check_summary)
        if m and int(m.group(2)) > 0:
            return int(m.group(1)) / int(m.group(2)) * 100
    elif check_name == "vms_controller_status":
        w  = re.search(r"Working: (\d+)", check_summary)
        nw = re.search(r"Not working: (\d+)", check_summary)
        ns = re.search(r"No status: (\d+)", check_summary)
        if w:
            total = int(w.group(1)) + (int(nw.group(1)) if nw else 0) + (int(ns.group(1)) if ns else 0)
            return int(w.group(1)) / total * 100 if total else None
    elif check_name == "bt_paths_speed_and_traveltime":
        m = re.search(r"Speed OK: (\d+)/(\d+)", check_summary)
        if m and int(m.group(2)) > 0:
            return int(m.group(1)) / int(m.group(2)) * 100
    return None


def _health_color(pct):
    if pct is None:
        return "#9ca3af"
    return "#1d9e75" if pct >= 90 else ("#e58e0a" if pct >= HEALTH_WARNING_PCT else "#e24b4a")


def _humanize_failure(check_name, full_failure_reason):
    """Return a short, plain-English explanation of a check failure.
    Receives the full failure_reason string so regexes can find sub-parts
    even when the detail itself contains ' | ' delimiters.
    """
    import re
    fr = full_failure_reason or ""
    if check_name == "feed_freshness":
        m = re.search(r"feed_freshness: ([^|]+)", fr)
        return m.group(1).strip() if m else fr
    if check_name == "valid_xml":
        return "Response is not valid XML — the API may be down or returning an error page"
    if check_name == "vms_controller_status":
        m = re.search(r"Not working: (\d+)", fr)
        n = int(m.group(1)) if m else "?"
        return f"{n} VMS controller(s) reported as not working"
    if check_name == "sensor_speed_status":
        m = re.search(r"Malfunctioning \(speed=-1\): (\d+)", fr)
        n = int(m.group(1)) if m else "?"
        return f"{n} traffic sensor(s) reporting speed = -1 (hardware fault)"
    if check_name == "bt_paths_speed_and_traveltime":
        m = re.search(r"Speed OK: (\d+)/(\d+)", fr)
        if m:
            failing = int(m.group(2)) - int(m.group(1))
            return f"{failing} BT path(s) reporting no speed or travel time (no vehicles detected, or sensor issue)"
        return "Some BT paths are missing speed or travel time data"
    if check_name == "predefined_paths_count":
        return "No predefined BT paths found in the feed"
    # fallback: extract just the detail portion after "check_name: "
    m = re.search(re.escape(check_name) + r": ([^|]+)", fr)
    return m.group(1).strip() if m else fr


def build_sensor_stability_html(sensors, bt_path_names=None, all_sensor_coords=None):
    """Build the sensor stability panel HTML with a group dropdown."""
    if not sensors:
        return "<p style='color:var(--color-text-secondary);font-size:13px'>No sensor data recorded yet.</p>"

    bt_path_names = bt_path_names or {}
    all_sensor_coords = all_sensor_coords or {}

    groups = sorted({s["group_name"] for s in sensors})

    options = '<option value="all">All groups</option>'
    for g in groups:
        display_g = GROUP_DISPLAY.get(g, g)
        options += f'<option value="{g}">{display_g}</option>'

    rows = ""
    def _sensor_sort_key(x):
        try:
            return (x["group_name"], int(x["sensor_id"]))
        except (ValueError, TypeError):
            return (x["group_name"], x["sensor_id"])

    for s in sorted(sensors, key=_sensor_sort_key):
        # Compute human-readable display ID
        if s["group_name"] == "Bluetooth Paths":
            display_sensor_id = bt_path_names.get(s["sensor_id"], s["sensor_id"])
        elif s["group_name"] == "Traffic Detection":
            td_info = all_sensor_coords.get("Traffic Detection", {}).get(s["sensor_id"], {})
            sc = td_info.get("site_code")
            nm = td_info.get("name", s["sensor_id"])
            display_sensor_id = f"{sc} ({nm})" if sc else nm
        elif s["group_name"] == "VMS":
            vms_info = all_sensor_coords.get("VMS", {}).get(s["sensor_id"], {})
            nm = vms_info.get("name", "")
            display_sensor_id = f"{nm} ({s['sensor_id']})" if nm and nm != s["sensor_id"] else s["sensor_id"]
        else:
            display_sensor_id = s["sensor_id"]

        display_group = GROUP_DISPLAY.get(s["group_name"], s["group_name"])

        history = s["history"]
        total = len(history)
        good = sum(1 for h in history if h["status"] in GOOD_STATUSES)
        pct = round(good / total * 100) if total else 0

        if pct == 100:
            badge_bg, badge_color, badge_label = "#e1f5ee", "#0f6e56", "Always on"
        elif pct == 0:
            badge_bg, badge_color, badge_label = "#fcebeb", "#a32d2d", "Always off"
        elif pct >= 70:
            badge_bg, badge_color, badge_label = "#faeeda", "#854f0b", "Mostly on"
        else:
            badge_bg, badge_color, badge_label = "#faeeda", "#854f0b", "Unstable"

        # Sparkline: last 40 runs as tiny squares with rich tooltips
        sparks = ""
        for h in history[-40:]:
            c = STATUS_COLOR.get(h["status"], "#9ca3af")
            reason = STATUS_LABEL.get(h["status"], h["status"])
            ts = _to_cyprus(h["run_at"])
            sparks += f'<span title="{ts} — {reason}" style="display:inline-block;width:6px;height:14px;border-radius:2px;background:{c};margin-right:1px"></span>'

        # Last issue: most recent non-good entry
        last_bad = next(
            (h for h in reversed(history) if h["status"] not in GOOD_STATUSES),
            None
        )
        if last_bad:
            issue_label = STATUS_LABEL.get(last_bad["status"], last_bad["status"])
            issue_color = STATUS_COLOR.get(last_bad["status"], "#9ca3af")
            last_issue_html = f'<span style="font-size:11px;color:{issue_color}">{issue_label}</span>'
        else:
            last_issue_html = '<span style="font-size:11px;color:#1d9e75">—</span>'

        # Last seen working: most recent good entry
        last_good = next(
            (h for h in reversed(history) if h["status"] in GOOD_STATUSES),
            None
        )
        if last_good:
            last_good_html = f'<span style="font-size:11px;color:var(--color-text-secondary)">{_to_cyprus(last_good["run_at"])}</span>'
        else:
            last_good_html = '<span style="font-size:11px;color:#e24b4a">Never</span>'

        rows += f"""
        <tr data-group="{s['group_name']}">
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{display_group}</td>
          <td style="font-size:12px;color:var(--color-text-primary);font-family:monospace;max-width:260px;word-break:break-word;white-space:normal">{display_sensor_id}</td>
          <td style="white-space:nowrap">{sparks}</td>
          <td><span style="font-size:11px;font-weight:500;padding:2px 8px;border-radius:10px;background:{badge_bg};color:{badge_color}">{badge_label}</span></td>
          <td>{last_issue_html}</td>
          <td>{last_good_html}</td>
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{good}/{total}</td>
        </tr>"""

    # Per-group good/total counts for dynamic bar
    import json as _json
    group_stats = {"all": {"good": 0, "total": 0}}
    for s in sensors:
        g = s["group_name"]
        last_status = s["history"][-1]["status"] if s["history"] else "unknown"
        is_good = last_status in GOOD_STATUSES
        if g not in group_stats:
            group_stats[g] = {"good": 0, "total": 0}
        group_stats[g]["total"] += 1
        group_stats["all"]["total"] += 1
        if is_good:
            group_stats[g]["good"] += 1
            group_stats["all"]["good"] += 1
    group_stats_json = _json.dumps(group_stats)

    return f"""
    <div style="display:flex;align-items:center;justify-content:flex-end;margin-bottom:16px">
      <select id="groupFilter" onchange="filterGroup(this.value)"
              style="font-size:13px;padding:5px 10px;border-radius:8px;border:0.5px solid var(--color-border-tertiary);
                     background:var(--color-background-primary);color:var(--color-text-primary);cursor:pointer">
        {options}
      </select>
    </div>
    <table id="sensorTable">
      <thead><tr><th>Group</th><th>Sensor ID</th><th>History (last 40 runs)</th><th>Stability</th><th>Last issue</th><th>Last working</th><th>Runs</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <script>
    var _groupStats = {group_stats_json};
    function filterGroup(val) {{
      document.querySelectorAll('#sensorTable tbody tr').forEach(function(tr) {{
        tr.style.display = (val === 'all' || tr.dataset.group === val) ? '' : 'none';
      }});
      var stats = _groupStats[val] || _groupStats['all'];
      var pct = stats.total > 0 ? Math.round(stats.good / stats.total * 100) : 0;
      var color = pct >= 90 ? '#1d9e75' : (pct >= 55 ? '#e58e0a' : '#e24b4a');
      var fill = document.getElementById('sensorBarFill');
      if (fill) {{ fill.style.width = pct + '%'; fill.style.background = color; }}
    }}
    </script>"""


def generate_report() -> str:
    REPORT_PATH.parent.mkdir(exist_ok=True)

    runs = fetch_recent_runs(60)
    if not runs:
        REPORT_PATH.write_text("<html><body>No runs yet.</body></html>")
        return str(REPORT_PATH)

    latest_run = runs[0]
    latest_results = fetch_results_for_run(latest_run["run_id"])

    # Group latest results by group
    groups = {}
    for r in latest_results:
        groups.setdefault(r["group_name"], []).append(r)

    # Chart data
    chart_runs = list(reversed(runs[:30]))
    chart_passed = json.dumps([r["passed"] for r in chart_runs])
    chart_failed = json.dumps([r["failed"] + r["errored"] for r in chart_runs])

    # Sensor stability (coord lookups fetched below with map data)
    all_sensors = fetch_sensor_stability()

    # Per-sensor statuses for the latest run (used for full ID lists in cards)
    latest_sensor_statuses = fetch_sensor_statuses_for_run(latest_run["run_id"])

    # Build group status cards
    def group_status_card(group_name, icon, results):
        all_pass = all(r["status"] == "pass" for r in results)
        any_error = any(r["status"] == "error" for r in results)

        # Compute minimum sensor health across all endpoints in this group
        min_health_pct = None
        for r in results:
            cs = r.get("check_summary", "") or ""
            for cn in SENSOR_CHECKS:
                pct = _extract_health_pct(cs, cn)
                if pct is not None:
                    min_health_pct = pct if min_health_pct is None else min(min_health_pct, pct)

        sensor_degraded = min_health_pct is not None and min_health_pct < HEALTH_WARNING_PCT

        if not all_pass:
            status_color = "#e58e0a" if any_error else "#e24b4a"
            status_bg    = "#faeeda" if any_error else "#fcebeb"
            status_label = "Degraded" if any_error else "Feed issue"
            status_icon  = "ti-alert-triangle" if any_error else "ti-circle-x"
        elif sensor_degraded:
            status_color = "#e58e0a"
            status_bg    = "#faeeda"
            status_label = f"Degraded ({round(min_health_pct)}%)"
            status_icon  = "ti-alert-triangle"
        else:
            status_color = "#1d9e75"
            status_bg    = "#e1f5ee"
            status_label = "Operational" + (f" ({round(min_health_pct)}%)" if min_health_pct is not None else "")
            status_icon  = "ti-circle-check"

        pass_count = sum(1 for r in results if r["status"] == "pass")

        detail_rows = ""
        for r in results:
            cs = r.get("check_summary", "") or ""

            # Dot color: feed failure = red/amber; sensor check = health-based; otherwise green
            if r["status"] != "pass":
                dot_color = "#e58e0a" if r["status"] == "error" else "#e24b4a"
            else:
                h_pct = None
                for cn in SENSOR_CHECKS:
                    h_pct = _extract_health_pct(cs, cn)
                    if h_pct is not None:
                        break
                dot_color = _health_color(h_pct) if h_pct is not None else "#1d9e75"

            # Failure lines: only for non-passing feed-level checks (sensor checks excluded)
            failure_lines = ""
            if r["status"] != "pass" and r.get("failure_reason"):
                import re as _re2
                fr = r["failure_reason"]
                check_names_found = _re2.findall(r"(?:^| \| )([a-z][a-z_]+): ", fr)
                feed_checks = [cn for cn in check_names_found if cn not in SENSOR_CHECKS]
                if feed_checks:
                    seen = set()
                    for cname in feed_checks:
                        if cname not in seen:
                            seen.add(cname)
                            label = _humanize_failure(cname, fr)
                            failure_lines += f'<div style="font-size:11px;color:{dot_color};padding:2px 0 0 16px;line-height:1.5">{label}</div>'
                else:
                    failure_lines += f'<div style="font-size:11px;color:{dot_color};padding:2px 0 0 16px;line-height:1.5">{fr}</div>'

            # Name suffix: device count for BT Inventory; health fraction for sensor checks
            name_suffix = ""
            import re as _re
            if r["test_name"] == "Bluetooth Inventory" and cs:
                m = _re.search(r"bt_site_count: (\d+)", cs)
                if m:
                    name_suffix = f' <span style="font-size:11px;color:var(--color-text-secondary)">— {m.group(1)} devices</span>'
            elif "sensor_speed_status" in cs:
                m = _re.search(r"Working: (\d+)/(\d+)", cs)
                if m:
                    pct = int(m.group(1)) / int(m.group(2)) * 100
                    name_suffix = f' <span style="font-size:11px;color:{_health_color(pct)}">— {m.group(1)}/{m.group(2)} working</span>'
            elif "vms_controller_status" in cs:
                w = _re.search(r"Working: (\d+)", cs)
                nw = _re.search(r"Not working: (\d+)", cs)
                ns = _re.search(r"No status: (\d+)", cs)
                if w:
                    total = int(w.group(1)) + (int(nw.group(1)) if nw else 0) + (int(ns.group(1)) if ns else 0)
                    pct = int(w.group(1)) / total * 100 if total else 0
                    name_suffix = f' <span style="font-size:11px;color:{_health_color(pct)}">— {w.group(1)}/{total} working</span>'
            elif "bt_paths_speed_and_traveltime" in cs:
                m = _re.search(r"Speed OK: (\d+)/(\d+)", cs)
                if m:
                    pct = int(m.group(1)) / int(m.group(2)) * 100
                    name_suffix = f' <span style="font-size:11px;color:{_health_color(pct)}">— {m.group(1)}/{m.group(2)} with data</span>'

            detail_rows += f"""
            <div style="padding:6px 0;border-bottom:0.5px solid var(--color-border-tertiary)">
              <div style="display:flex;align-items:center;gap:8px">
                <span style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0"></span>
                <span style="font-size:13px;color:var(--color-text-primary);flex:1">{r['test_name']}{name_suffix}</span>
              </div>
              {failure_lines}
            </div>"""

        # Extra detail block — always shown for sensor checks; uses sensor_results DB for full ID lists
        extra = ""
        sensor_data_key = "Bluetooth Paths" if group_name == "Bluetooth" else group_name
        sensor_data = latest_sensor_statuses.get(sensor_data_key, {})

        def _collapsible_ids(label, color, ids):
            if not ids:
                return ""
            id_list = ", ".join(ids)
            return f"""
            <details style="margin-top:6px">
              <summary style="cursor:pointer;font-size:11px;color:{color};list-style:none;display:flex;align-items:center;gap:5px;user-select:none">
                <i class="ti ti-chevron-right" style="font-size:11px;transition:transform .15s" aria-hidden="true"></i>
                <b>{len(ids)}</b>&nbsp;{label}
              </summary>
              <div style="margin-top:4px;padding:6px 8px;background:var(--color-background-primary);border-radius:6px;
                          font-family:monospace;font-size:11px;color:var(--color-text-secondary);line-height:1.8;overflow-wrap:break-word">
                {id_list}
              </div>
            </details>"""

        for r in results:
            cs = r.get("check_summary", "") or ""
            if "vms_controller_status" in cs:
                d = parse_vms_detail(cs)
                if d:
                    vms_total = d['working'] + d['not_working'] + d['no_status']
                    vms_pct = round(d['working'] / vms_total * 100) if vms_total else 0
                    bar_color = _health_color(vms_pct)
                    not_working_ids = sensor_data.get("not_working", [])
                    no_status_ids   = sensor_data.get("no_status", [])
                    extra += f"""
                    <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                      <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">VMS Controllers</div>
                      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                        <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                          <div style="width:{vms_pct}%;height:6px;background:{bar_color};border-radius:3px"></div>
                        </div>
                        <span style="color:var(--color-text-primary);font-weight:500">{d['working']}/{vms_total}</span>
                      </div>
                      <div style="display:flex;gap:16px;margin-bottom:4px">
                        <span style="color:#1d9e75"><b>{d['working']}</b> working</span>
                        <span style="color:#e24b4a"><b>{d['not_working']}</b> not working</span>
                        <span style="color:#888"><b>{d['no_status']}</b> no status</span>
                      </div>
                      {_collapsible_ids("not working", "#e24b4a", not_working_ids)}
                      {_collapsible_ids("no status", "#888", no_status_ids)}
                    </div>"""
            elif "bt_paths_speed_and_traveltime" in cs:
                d = parse_bt_detail(cs)
                if d:
                    pct = round(d['speed_ok'] / d['total'] * 100) if d['total'] else 0
                    bar_color = _health_color(pct)
                    failing_ids = sensor_data.get("failing", [])
                    extra += f"""
                    <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                      <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">BT Paths with data</div>
                      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
                        <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                          <div style="width:{pct}%;height:6px;background:{bar_color};border-radius:3px"></div>
                        </div>
                        <span style="color:var(--color-text-primary);font-weight:500">{d['speed_ok']}/{d['total']}</span>
                      </div>
                      {_collapsible_ids("failing paths", "#e24b4a", failing_ids)}
                    </div>"""
            elif "sensor_speed_status" in cs:
                d = parse_sensor_detail(cs)
                if d:
                    td_pct = round(d['working'] / d['total'] * 100) if d['total'] else 0
                    bar_color = _health_color(td_pct)
                    malfunctioning_ids = sensor_data.get("malfunctioning", [])
                    no_traffic_ids     = sensor_data.get("no_traffic", [])
                    no_measurement_ids = sensor_data.get("no_measurement", [])
                    extra += f"""
                    <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                      <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">Traffic Detection (SWARCO) — {d['total']} total</div>
                      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                        <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                          <div style="width:{td_pct}%;height:6px;background:{bar_color};border-radius:3px"></div>
                        </div>
                        <span style="color:var(--color-text-primary);font-weight:500">{d['working']}/{d['total']}</span>
                      </div>
                      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px">
                        <span style="color:#1d9e75"><b>{d['working']}</b> working</span>
                        <span style="color:#888"><b>{d['no_traffic']}</b> no traffic</span>
                        <span style="color:#e24b4a"><b>{d['malfunctioning']}</b> malfunctioning</span>
                        <span style="color:#888"><b>{d['no_measurement']}</b> no data</span>
                      </div>
                      {_collapsible_ids("malfunctioning", "#e24b4a", malfunctioning_ids)}
                      {_collapsible_ids("no traffic", "#888", no_traffic_ids)}
                      {_collapsible_ids("no measurement data", "#888", no_measurement_ids)}
                    </div>"""

        return f"""
        <div style="background:var(--color-background-primary);border:0.5px solid var(--color-border-tertiary);border-radius:12px;padding:20px;flex:1;min-width:260px">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
            <i class="ti {icon}" style="font-size:22px;color:var(--color-text-secondary)" aria-hidden="true"></i>
            <div style="flex:1">
              <div style="font-size:15px;font-weight:500;color:var(--color-text-primary)">{GROUP_DISPLAY.get(group_name, group_name)}</div>
              <div style="font-size:12px;color:var(--color-text-secondary)">{pass_count}/{len(results)} checks passing</div>
            </div>
            <span style="display:flex;align-items:center;gap:5px;font-size:12px;font-weight:500;padding:4px 10px;border-radius:20px;background:{status_bg};color:{status_color}">
              <i class="ti {status_icon}" style="font-size:14px" aria-hidden="true"></i>{status_label}
            </span>
          </div>
          <div style="border-top:0.5px solid var(--color-border-tertiary);padding-top:8px">
            {detail_rows}
          </div>
          {extra}
        </div>"""

    group_icons = {"VMS": "ti-road-sign", "Bluetooth": "ti-bluetooth", "Traffic Detection": "ti-traffic-cone"}
    group_cards = ""
    for gname, gresults in sorted(groups.items()):
        icon = group_icons.get(gname, "ti-device-analytics")
        group_cards += group_status_card(gname, icon, gresults)

    # Run history rows
    history_rows = ""
    for run in runs[:20]:
        total = run["total"] or 1
        pct = round(run["passed"] / total * 100)
        ok = run["failed"] == 0 and run["errored"] == 0
        bar_color = "#1d9e75" if ok else "#e24b4a"
        ts = _to_cyprus(run["run_at"])
        history_rows += f"""
        <tr>
          <td style="color:var(--color-text-secondary);font-size:13px">{ts}</td>
          <td style="font-weight:500;color:{'#1d9e75' if ok else '#e24b4a'}">{run['passed']}/{total}</td>
          <td style="color:{'#e24b4a' if run['failed'] > 0 else 'var(--color-text-secondary)'}">{run['failed']}</td>
          <td style="color:{'#e58e0a' if run['errored'] > 0 else 'var(--color-text-secondary)'}">{run['errored']}</td>
          <td>
            <div style="background:var(--color-border-tertiary);border-radius:3px;height:6px;width:100px">
              <div style="background:{bar_color};height:6px;border-radius:3px;width:{pct}%"></div>
            </div>
          </td>
        </tr>"""

    # Map data
    all_coords = fetch_sensor_coords()
    all_bt_paths = fetch_bt_path_coords()

    # Build stability html now that coord lookups are available
    _bt_path_names = {pid: p["name"] for pid, p in all_bt_paths.items()}
    sensor_stability_html = build_sensor_stability_html(all_sensors, _bt_path_names, all_coords)
    live_data = fetch_sensor_live_data_for_run(latest_run["run_id"])

    # Build sensor list for map: includes live measurements for rich popups
    map_sensors = []
    for group_name_c, sensors_dict in all_coords.items():
        group_live = live_data.get(group_name_c, {})
        for sid, c in sensors_dict.items():
            entry = group_live.get(sid, {})
            st = entry.get("status", "unknown")
            color = STATUS_COLOR.get(st, "#6b7280")
            label = STATUS_LABEL.get(st, "Unknown")
            # Compute human-readable display name for popup title/body
            if group_name_c == "Traffic Detection":
                sc = c.get("site_code")
                nm = c.get("name", sid)
                display_name = f"{sc} ({nm})" if sc else nm
            else:
                display_name = c["name"]
            map_sensors.append({
                "id": sid, "group": group_name_c,
                "group_display": GROUP_DISPLAY.get(group_name_c, group_name_c),
                "name": c["name"], "display_name": display_name,
                "lat": c["lat"], "lon": c["lon"],
                "status": st, "label": label, "color": color,
                "data": entry.get("data", {}),
            })

    # Build BT path list with live speed/travel-time data
    bt_group_live = live_data.get("Bluetooth Paths", {})
    map_bt_paths = []
    for pid, p in all_bt_paths.items():
        entry = bt_group_live.get(pid, {})
        st = entry.get("status", "unknown")
        color = STATUS_COLOR.get(st, "#6b7280")
        map_bt_paths.append({
            "id": pid, "name": p["name"],
            "coords": p["coords"], "status": st, "color": color,
            "data": entry.get("data", {}),
        })

    map_sensors_json = json.dumps(map_sensors)
    map_bt_paths_json = json.dumps(map_bt_paths)
    has_map_data = bool(map_sensors or map_bt_paths)
    map_script_html = _build_map_script(map_sensors_json, map_bt_paths_json) if has_map_data else ""

    if not has_map_data:
        map_panel_html = '<p style="color:var(--color-text-secondary);font-size:13px">No coordinate data yet — run the test suite once to populate the map.</p>'
    else:
        map_panel_html = (
            '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">'
            '<span style="font-size:11px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-right:4px">Show:</span>'
            '<button class="map-toggle active" id="btn-showall" onclick="toggleShowAll(this)" style="margin-right:4px">Show all</button>'
            '<button class="map-toggle active" data-layer="td" onclick="toggleLayer(this,\'td\')">Traffic Detection (SWARCO)</button>'
            '<button class="map-toggle active" data-layer="bt" onclick="toggleLayer(this,\'bt\')">Bluetooth Sites</button>'
            '<button class="map-toggle active" data-layer="vms" onclick="toggleLayer(this,\'vms\')">VMS</button>'
            '<button class="map-toggle active" data-layer="paths" onclick="toggleLayer(this,\'paths\')">BT Paths</button>'
            '<span style="flex:1"></span>'
            '<button class="map-toggle active" data-filter="all" onclick="setFilter(this,\'all\')">All</button>'
            '<button class="map-toggle" data-filter="issues" onclick="setFilter(this,\'issues\')">Issues only</button>'
            '</div>'
            '<div id="sensorMap" style="height:520px;border-radius:8px;overflow:hidden;border:0.5px solid var(--color-border-tertiary);position:relative">'
            '<div id="mapInfoPanel" style="display:none;position:absolute;top:10px;right:10px;z-index:1000;background:#fff;border-radius:10px;box-shadow:0 3px 14px rgba(0,0,0,0.22);min-width:220px;max-width:280px;font-size:12px;overflow:hidden">'
            '<div style="display:flex;align-items:center;justify-content:space-between;padding:9px 14px 7px;border-bottom:1px solid #eee">'
            '<span id="mapInfoTitle" style="font-weight:700;font-size:13px;color:#1a1a2e"></span>'
            '<button onclick="closeMapPanel()" style="background:none;border:none;cursor:pointer;color:#9ca3af;font-size:18px;line-height:1;padding:0 0 0 10px">&times;</button>'
            '</div>'
            '<div id="mapInfoBody" style="padding:10px 14px 12px"></div>'
            '</div>'
            '</div>'
        )

    run_time = _to_cyprus(latest_run["run_at"])
    total_runs_label = len(runs)

    # Chart labels in Cyprus time
    chart_labels = json.dumps([_to_cyprus(r["run_at"]) for r in chart_runs])

    # Per-panel progress bar percentages
    latest_total = latest_run["total"] or 1
    overall_pct = round(latest_run["passed"] / latest_total * 100)
    overall_bar_color = "#1d9e75" if overall_pct >= 90 else ("#e58e0a" if overall_pct >= 55 else "#e24b4a")

    trend_passed_total = sum(r["passed"] for r in chart_runs)
    trend_total = sum((r["total"] or 1) for r in chart_runs)
    trend_pct = round(trend_passed_total / trend_total * 100) if trend_total else 0
    trend_bar_color = "#1d9e75" if trend_pct >= 90 else ("#e58e0a" if trend_pct >= 55 else "#e24b4a")

    sensor_good = sum(1 for s in all_sensors if s["history"] and s["history"][-1]["status"] in GOOD_STATUSES)
    sensor_total_count = len(all_sensors) or 1
    sensor_pct = round(sensor_good / sensor_total_count * 100)
    sensor_bar_color = "#1d9e75" if sensor_pct >= 90 else ("#e58e0a" if sensor_pct >= 55 else "#e24b4a")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ITS Infrastructure Health</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #f5f6f8; --surface: #ffffff; --border: rgba(0,0,0,0.08);
    --text: #1a1a2e; --muted: #6b7280; --header-bg: #1a1a2e; --subsurf: #f1f0e8;
    --color-background-primary: #ffffff; --color-background-secondary: #f5f6f8;
    --color-text-primary: #1a1a2e; --color-text-secondary: #6b7280;
    --color-border-tertiary: rgba(0,0,0,0.08);
  }}
  body.dark {{
    --bg: #111318; --surface: #1c1f26; --border: rgba(255,255,255,0.08);
    --text: #f0f2f5; --muted: #9ca3af; --header-bg: #0d0f14; --subsurf: #1a1d24;
    --color-background-primary: #1c1f26; --color-background-secondary: #111318;
    --color-text-primary: #f0f2f5; --color-text-secondary: #9ca3af;
    --color-border-tertiary: rgba(255,255,255,0.08);
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: var(--bg); color: var(--text); min-height: 100vh; transition: background .2s, color .2s; }}
  header {{ background: var(--header-bg); color: white; padding: 20px 28px;
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }}
  header h1 {{ font-size: 1.15rem; font-weight: 500; letter-spacing: -0.01em; }}
  header .meta {{ font-size: 11px; opacity: 0.45; margin-top:3px; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 20px 16px; }}
  .section-label {{ font-size: 11px; font-weight: 500; letter-spacing: 0.08em;
                    text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }}
  .group-cards {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .panel {{ background: var(--surface); border: 0.5px solid var(--border);
            border-radius: 12px; margin-bottom: 20px; overflow: hidden; transition: background .2s, border-color .2s; }}
  .panel-header {{ padding: 14px 20px; display: flex; align-items: center;
                   justify-content: space-between; cursor: pointer; user-select: none; }}
  .panel-header:hover {{ background: var(--subsurf); }}
  .panel-title {{ font-size: 11px; font-weight: 500; letter-spacing: 0.07em;
                  text-transform: uppercase; color: var(--muted); }}
  .panel-chevron {{ width: 26px; height: 26px; border-radius: 6px; border: 0.5px solid var(--border);
                    background: var(--subsurf); display: flex; align-items: center; justify-content: center;
                    color: var(--muted); font-size: 13px; transition: transform .2s, background .2s; }}
  .panel-chevron.open {{ transform: rotate(180deg); }}
  .panel-bar {{ height: 3px; width: 100%; background: var(--border); }}
  .panel-bar-fill {{ height: 3px; }}
  .panel-body {{ padding: 16px 20px 18px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ font-size: 11px; font-weight: 500; letter-spacing: 0.05em; text-transform: uppercase;
        color: var(--muted); padding: 8px 12px; border-bottom: 0.5px solid var(--border); text-align: left; }}
  td {{ padding: 10px 12px; border-bottom: 0.5px solid var(--border); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  .dm-btn {{ display: flex; align-items: center; gap: 6px; font-size: 11px; padding: 5px 11px;
             border-radius: 7px; border: 0.5px solid rgba(255,255,255,0.2);
             background: rgba(255,255,255,0.07); color: rgba(255,255,255,0.75);
             cursor: pointer; transition: background .15s; }}
  .dm-btn:hover {{ background: rgba(255,255,255,0.14); }}
  details[open] summary .ti-chevron-right {{ transform: rotate(90deg); }}
  details summary::-webkit-details-marker {{ display: none; }}
  .map-toggle {{
    font-size:11px;font-weight:500;padding:4px 12px;border-radius:6px;cursor:pointer;
    border:0.5px solid var(--border);background:var(--surface);color:var(--muted);transition:all .15s;
  }}
  .map-toggle.active {{ background:var(--header-bg);color:#fff;border-color:var(--header-bg); }}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
<header>
  <div>
    <h1><i class="ti ti-traffic-lights" style="font-size:17px;vertical-align:-2px;margin-right:8px" aria-hidden="true"></i>ITS Infrastructure Health</h1>
    <div class="meta">Last checked {run_time} EET &nbsp;·&nbsp; {total_runs_label} runs recorded</div>
  </div>
  <div style="display:flex;align-items:center;gap:18px">
    <div style="display:flex;gap:14px;font-size:12px;opacity:0.55">
      <span><i class="ti ti-circle-check" style="color:#1d9e75;vertical-align:-1px;margin-right:4px"></i>Pass</span>
      <span><i class="ti ti-circle-x" style="color:#e24b4a;vertical-align:-1px;margin-right:4px"></i>Fail</span>
      <span><i class="ti ti-alert-triangle" style="color:#e58e0a;vertical-align:-1px;margin-right:4px"></i>Error</span>
    </div>
    <button class="dm-btn" onclick="toggleDark()" id="dmBtn" aria-label="Toggle dark mode">
      <i class="ti ti-moon" id="dmIcon" aria-hidden="true"></i>
      <span id="dmLabel">Dark</span>
    </button>
  </div>
</header>

<div class="wrap">

  <div class="panel" id="p-groups">
    <div class="panel-header" onclick="togglePanel('p-groups')">
      <span class="panel-title">Infrastructure groups</span>
      <div class="panel-chevron open" id="c-p-groups"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar"><div class="panel-bar-fill" style="width:{overall_pct}%;background:{overall_bar_color}"></div></div>
    <div class="panel-body" id="b-p-groups">
      <div class="group-cards">
        {group_cards}
      </div>
    </div>
  </div>

  <div class="panel" id="p-map">
    <div class="panel-header" onclick="togglePanel('p-map')">
      <span class="panel-title">Sensor map</span>
      <div class="panel-chevron open" id="c-p-map"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar"><div class="panel-bar-fill" style="width:{sensor_pct}%;background:{sensor_bar_color}"></div></div>
    <div class="panel-body" id="b-p-map" style="padding:12px 20px 16px">
      {map_panel_html}
    </div>
  </div>

  <div class="panel" id="p-sensors">
    <div class="panel-header" onclick="togglePanel('p-sensors')">
      <span class="panel-title">Sensor stability</span>
      <div class="panel-chevron open" id="c-p-sensors"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar"><div class="panel-bar-fill" id="sensorBarFill" style="width:{sensor_pct}%;background:{sensor_bar_color}"></div></div>
    <div class="panel-body" id="b-p-sensors">
      {sensor_stability_html}
    </div>
  </div>

  <div class="panel" id="p-trend">
    <div class="panel-header" onclick="togglePanel('p-trend')">
      <span class="panel-title">Pass / fail trend — last {len(chart_runs)} runs</span>
      <div class="panel-chevron open" id="c-p-trend"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar"><div class="panel-bar-fill" style="width:{trend_pct}%;background:{trend_bar_color}"></div></div>
    <div class="panel-body" id="b-p-trend">
      <div style="position:relative;height:180px">
        <canvas id="trendChart" role="img" aria-label="Stacked bar chart of passed and failed test counts across recent runs"></canvas>
      </div>
      <div style="display:flex;gap:16px;margin-top:10px;font-size:12px;color:var(--muted)">
        <span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:#1d9e75;display:inline-block"></span>Passed</span>
        <span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:#e24b4a;display:inline-block"></span>Failed / errored</span>
      </div>
    </div>
  </div>

  <div class="panel" id="p-history">
    <div class="panel-header" onclick="togglePanel('p-history')">
      <span class="panel-title">Run history</span>
      <div class="panel-chevron open" id="c-p-history"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar"><div class="panel-bar-fill" style="width:{overall_pct}%;background:{overall_bar_color}"></div></div>
    <div class="panel-body" id="b-p-history">
      <table>
        <thead><tr><th>Time (EET)</th><th>Passed</th><th>Failed</th><th>Errored</th><th>Pass rate</th></tr></thead>
        <tbody>{history_rows}</tbody>
      </table>
    </div>
  </div>

</div>

<script>
function togglePanel(id) {{
  var b = document.getElementById('b-' + id);
  var c = document.getElementById('c-' + id);
  var open = b.style.display !== 'none';
  b.style.display = open ? 'none' : '';
  c.classList.toggle('open', !open);
}}

var _dark = true;
document.body.classList.add('dark');
document.getElementById('dmIcon').className = 'ti ti-sun';
document.getElementById('dmLabel').textContent = 'Light';
function toggleDark() {{
  _dark = !_dark;
  document.body.classList.toggle('dark', _dark);
  document.getElementById('dmIcon').className = _dark ? 'ti ti-sun' : 'ti ti-moon';
  document.getElementById('dmLabel').textContent = _dark ? 'Light' : 'Dark';
  var tc = '#9ca3af';
  var gc = _dark ? 'rgba(255,255,255,0.06)' : 'rgba(128,128,128,0.1)';
  if (window._trendChart) {{
    window._trendChart.options.scales.x.ticks.color = tc;
    window._trendChart.options.scales.y.ticks.color = tc;
    window._trendChart.options.scales.y.grid.color = gc;
    window._trendChart.update();
  }}
}}

window._trendChart = new Chart(document.getElementById('trendChart'), {{
  type: 'bar',
  data: {{
    labels: {chart_labels},
    datasets: [
      {{ label: 'Passed', data: {chart_passed}, backgroundColor: '#1d9e75' }},
      {{ label: 'Failed/Error', data: {chart_failed}, backgroundColor: '#e24b4a' }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ stacked: true, ticks: {{ font: {{ size: 11 }}, color: '#9ca3af', maxRotation: 45, autoSkip: true, maxTicksLimit: 15 }} }},
      y: {{ stacked: true, beginAtZero: true, ticks: {{ stepSize: 1, font: {{ size: 11 }}, color: '#9ca3af' }}, grid: {{ color: 'rgba(128,128,128,0.1)' }} }}
    }}
  }}
}});
</script>

{map_script_html}
</body></html>"""

    REPORT_PATH.write_text(html, encoding="utf-8")
    return str(REPORT_PATH)


def _build_map_script(map_sensors_json, map_bt_paths_json):
    return """<script>
var _sensors = """ + map_sensors_json + """;
var _btPaths  = """ + map_bt_paths_json + """;
var _activeFilter = 'all';
var _activeLayers = {td:true, bt:true, vms:true, paths:true};

var _map = L.map('sensorMap', {zoomControl:true}).setView([34.95, 33.15], 9);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}).addTo(_map);

var _layerGroups = {td:L.layerGroup(), bt:L.layerGroup(), vms:L.layerGroup(), paths:L.layerGroup()};
Object.values(_layerGroups).forEach(function(lg){ lg.addTo(_map); });

var GROUP_LAYER   = {'Traffic Detection':'td','Bluetooth':'bt','VMS':'vms'};
var ISSUE_STATUSES = ['malfunctioning','not_working','failing','stale','missing'];
var STATUS_LABELS  = {
  working:'Working', ok:'OK', no_traffic:'No traffic', no_measurement:'No data',
  no_status:'No status', not_working:'Not working', malfunctioning:'Malfunctioning',
  failing:'No speed / travel time', unknown:'No recent data'
};

/* -- Icon factory ------------------------------------------------- */
var ICON_CLASS = {'Traffic Detection':'ti-traffic-cone','Bluetooth':'ti-bluetooth','VMS':'ti-road-sign'};
var ICON_SIZE  = {'Traffic Detection':26,'Bluetooth':22,'VMS':28};
var ICON_SHAPE = {'VMS':'6px'};   // others default to 50% (circle)

function makeIcon(group, color, faded) {
  var ic   = ICON_CLASS[group] || 'ti-circle';
  var sz   = ICON_SIZE[group]  || 24;
  var br   = ICON_SHAPE[group] || '50%';
  var op   = faded ? '0.18' : '1';
  var html = '<div style="width:'+sz+'px;height:'+sz+'px;border-radius:'+br+';'+
             'background:'+color+';border:2.5px solid rgba(255,255,255,0.92);'+
             'display:flex;align-items:center;justify-content:center;'+
             'box-shadow:0 2px 7px rgba(0,0,0,0.32);font-size:'+(sz-11)+'px;'+
             'color:white;opacity:'+op+';transition:opacity .2s">'+
             '<i class="ti '+ic+'"></i></div>';
  return L.divIcon({className:'', html:html,
    iconSize:[sz,sz], iconAnchor:[sz/2,sz/2], popupAnchor:[0,-sz/2+2]});
}

/* -- Popup helpers ------------------------------------------------ */
function popRow(label, val, color) {
  if (val === null || val === undefined || val === '') return '';
  var v = color ? '<span style="color:'+color+';font-weight:600">'+val+'</span>' : '<b style="color:#1a1a2e">'+val+'</b>';
  return '<tr><td style="color:#888;padding:2px 12px 2px 0;white-space:nowrap">'+label+'</td><td>'+v+'</td></tr>';
}
function fmtSpeed(v) {
  if (v===null||v===undefined) return null;
  return v===-1 ? '−1 km/h (fault)' : v+' km/h';
}
function fmtFlow(v)  { return (v===null||v===undefined) ? null : v+' veh/hr'; }
function fmtTT(v) {
  if (v===null||v===undefined) return null;
  var mins=Math.floor(v/60), secs=Math.round(v%60);
  return (mins>0?mins+'m ':'')+secs+'s';
}

/* -- Marker factory ----------------------------------------------- */
function makeMarker(s) {
  var m = L.marker([s.lat, s.lon], {icon: makeIcon(s.group, s.color, false)});
  m._sensorStatus = s.status;
  m._sensorGroup  = GROUP_LAYER[s.group] || 'td';
  m._sensorColor  = s.color;
  m._sensorGroup2 = s.group;
  var d = s.data || {};
  var dataRows = '';
  if (s.group === 'Traffic Detection') {
    dataRows += popRow('Speed', fmtSpeed(d.speed_kmh),
                       d.speed_kmh===-1?'#e24b4a':(d.speed_kmh>0?'#1d9e75':null));
    dataRows += popRow('Flow rate', fmtFlow(d.flow_veh_hr));
  }
  if (s.group === 'VMS') {
    dataRows += popRow('Message', d.message || null);
  }
  var rows = popRow('ID', s.id)+popRow('Group', s.group_display||s.group)+
             popRow('Status', STATUS_LABELS[s.status]||s.status, s.color)+dataRows;
  var bodyHtml = '<table style="border-collapse:collapse;width:100%">'+rows+'</table>';
  m.on('click', function() { showMapPanel(s.display_name||s.name||'Sensor '+s.id, bodyHtml); });
  return m;
}

/* -- Path factory ------------------------------------------------- */
function pathStyle(status, faded) {
  var isIssue = ISSUE_STATUSES.indexOf(status) !== -1;
  var isOk    = status === 'ok' || status === 'working';
  var color   = isIssue ? '#e24b4a' : (isOk ? '#1d9e75' : '#6b7280');
  var weight  = isIssue ? 4 : (isOk ? 3 : 2);
  var opacity = faded ? 0.04 : (isIssue ? 0.9 : (isOk ? 0.75 : 0.3));
  return {color:color, weight:weight, opacity:opacity};
}

function makePath(p) {
  var latlngs = p.coords.map(function(c){return [c[0],c[1]];});
  var style   = pathStyle(p.status, false);
  var pl = L.polyline(latlngs, style);
  pl._pathStatus = p.status;
  var d = p.data || {};
  var rows = popRow('Route', p.name)+popRow('Path ID', p.id)+
             popRow('Status', STATUS_LABELS[p.status]||p.status, style.color)+
             popRow('Speed', fmtSpeed(d.speed_kmh))+
             popRow('Travel time', fmtTT(d.travel_time_s));
  var bodyHtml = '<table style="border-collapse:collapse;width:100%">'+rows+'</table>';
  pl._bodyHtml = bodyHtml;
  pl._pathName = 'BT Path '+p.name;
  return pl;
}

/* -- Build layers ------------------------------------------------- */
var _markers = [];
_sensors.forEach(function(s) {
  var lg = _layerGroups[GROUP_LAYER[s.group]];
  if (!lg) return;
  var m = makeMarker(s);
  m.addTo(lg);
  _markers.push(m);
});

var _paths = [];
var _highlighted = null;
_btPaths.forEach(function(p) {
  var pl = makePath(p);
  pl.addTo(_layerGroups.paths);
  pl.on('click', function() {
    if (_highlighted && _highlighted !== pl) {
      _highlighted.setStyle(pathStyle(_highlighted._pathStatus, false));
      _highlighted.bringToBack();
    }
    _highlighted = pl;
    pl.setStyle({color:'#facc15', weight:7, opacity:1});
    pl.bringToFront();
    showMapPanel(pl._pathName, pl._bodyHtml);
  });
  _paths.push(pl);
});

/* -- Legend ------------------------------------------------------- */
var _legend = L.control({position:'bottomright'});
var _legendOpen = true;
_legend.onAdd = function() {
  var d = L.DomUtil.create('div');
  d.style.cssText = 'background:#fff;border-radius:10px;font-size:11px;line-height:1.6;min-width:168px;'+
    'pointer-events:auto;cursor:default;box-shadow:0 2px 10px rgba(0,0,0,0.18);overflow:hidden';
  L.DomEvent.disableClickPropagation(d);
  function row(html) { return '<div style="display:flex;align-items:center;gap:7px;margin-bottom:3px">'+html+'</div>'; }
  function dot(color,shape) {
    shape = shape||'50%';
    return '<span style="width:13px;height:13px;border-radius:'+shape+';background:'+color+
           ';border:1.5px solid rgba(0,0,0,0.12);display:inline-block;flex-shrink:0"></span>';
  }
  function iconBox(ic, color, shape) {
    shape = shape||'50%';
    return '<span style="width:18px;height:18px;border-radius:'+shape+';background:'+color+
           ';display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;'+
           'box-shadow:0 1px 3px rgba(0,0,0,0.2)"><i class="ti '+ic+'" style="font-size:10px;color:white"></i></span>';
  }
  function line(color, w) {
    return '<span style="display:inline-block;width:26px;height:'+w+'px;background:'+color+
           ';border-radius:2px;flex-shrink:0"></span>';
  }
  var body =
    '<div style="font-weight:600;color:#444;font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:4px">Sensor type</div>'+
    row(iconBox('ti-traffic-cone','#6b7280')+'<span style="color:#1a1a2e">Traffic Detection (SWARCO)</span>')+
    row(iconBox('ti-bluetooth','#6b7280')+'<span style="color:#1a1a2e">Bluetooth Site</span>')+
    row(iconBox('ti-road-sign','#6b7280','6px')+'<span style="color:#1a1a2e">VMS Controller</span>')+
    row(line('#1d9e75','3')+'<span style="color:#1a1a2e">BT Path (OK)</span>')+
    row(line('#e24b4a','4')+'<span style="color:#1a1a2e">BT Path (issue)</span>')+
    row(line('#9ca3af','2')+'<span style="color:#1a1a2e">BT Path (no data)</span>')+
    '<div style="font-weight:600;color:#444;font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin:7px 0 4px">Status</div>'+
    row(dot('#1d9e75')+'<span style="color:#1a1a2e">Working / OK</span>')+
    row(dot('#e24b4a')+'<span style="color:#1a1a2e">Issue / Fault</span>')+
    row(dot('#9ca3af')+'<span style="color:#1a1a2e">No data / No status</span>')+
    row(dot('#e58e0a')+'<span style="color:#1a1a2e">Stale / Missing</span>');
  function render() {
    d.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;'+
      'padding:8px 12px;cursor:pointer;border-bottom:'+(_legendOpen?'1px solid #eee':'none')+'" id="_legendHdr">'+
      '<span style="font-weight:700;font-size:12px;color:#1a1a2e">Legend</span>'+
      '<span style="font-size:14px;color:#555;margin-left:10px;line-height:1">'+(_legendOpen?'&#x25BE;':'&#x25B4;')+'</span>'+
      '</div>'+
      (_legendOpen ? '<div style="padding:8px 12px 10px">'+body+'</div>' : '');
    d.querySelector('#_legendHdr').onclick = function() {
      _legendOpen = !_legendOpen;
      render();
    };
  }
  render();
  return d;
};
_legend.addTo(_map);

/* -- Visibility filter -------------------------------------------- */
function applyVisibility() {
  _markers.forEach(function(m) {
    var on = _activeLayers[m._sensorGroup] &&
             (_activeFilter === 'all' || ISSUE_STATUSES.indexOf(m._sensorStatus) !== -1);
    m.setIcon(makeIcon(m._sensorGroup2, m._sensorColor, !on));
  });
  _paths.forEach(function(p) {
    var on = _activeLayers.paths &&
             (_activeFilter === 'all' || ISSUE_STATUSES.indexOf(p._pathStatus) !== -1);
    p.setStyle(pathStyle(p._pathStatus, !on));
  });
}

function _syncShowAll() {
  var allOn = Object.values(_activeLayers).every(Boolean);
  var btn = document.getElementById('btn-showall');
  if (btn) btn.classList.toggle('active', allOn);
}

function toggleLayer(btn, key) {
  _activeLayers[key] = !_activeLayers[key];
  btn.classList.toggle('active', _activeLayers[key]);
  _syncShowAll();
  applyVisibility();
}

function toggleShowAll(btn) {
  var allOn = Object.values(_activeLayers).every(Boolean);
  var turnOn = !allOn;
  Object.keys(_activeLayers).forEach(function(k){ _activeLayers[k] = turnOn; });
  document.querySelectorAll('[data-layer]').forEach(function(b){ b.classList.toggle('active', turnOn); });
  btn.classList.toggle('active', turnOn);
  applyVisibility();
}

function setFilter(btn, val) {
  _activeFilter = val;
  document.querySelectorAll('[data-filter]').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  applyVisibility();
}

function showMapPanel(title, bodyHtml) {
  var panel = document.getElementById('mapInfoPanel');
  document.getElementById('mapInfoTitle').textContent = title;
  document.getElementById('mapInfoBody').innerHTML = bodyHtml;
  panel.style.display = '';
}
function closeMapPanel() {
  document.getElementById('mapInfoPanel').style.display = 'none';
  if (_highlighted) {
    _highlighted.setStyle(pathStyle(_highlighted._pathStatus, false));
    _highlighted.bringToBack();
    _highlighted = null;
  }
}
</script>"""