"""
report.py - Generate a static HTML report from the SQLite history DB.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

from db import get_connection, fetch_recent_runs, fetch_results_for_run, fetch_sensor_stability, fetch_sensor_statuses_for_run

CYPRUS_TZ = timezone(timedelta(hours=3))  # EET/EEST — UTC+3 (summer); update to +2 in winter if needed


def _to_cyprus(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp string to Cyprus time, formatted for display."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(CYPRUS_TZ)
    return local.strftime("%Y-%m-%d %H:%M")

REPORT_PATH = Path("reports/latest.html")


def parse_vms_detail(failure_reason):
    if not failure_reason or "vms_controller_status" not in failure_reason:
        return None
    import re
    working = re.search(r"Working: (\d+)", failure_reason)
    not_working = re.search(r"Not working: (\d+)", failure_reason)
    no_status = re.search(r"No status: (\d+)", failure_reason)
    ids_match = re.search(r"Not working: \d+ — ([\d ,\(\)a-zA-Z]+?)(?:\||\Z)", failure_reason)
    ns_ids_match = re.search(r"No status: \d+ — ([\d ,]+?)(?:\||\Z)", failure_reason)
    return {
        "working": int(working.group(1)) if working else 0,
        "not_working": int(not_working.group(1)) if not_working else 0,
        "no_status": int(no_status.group(1)) if no_status else 0,
        "not_working_ids": ids_match.group(1).strip() if ids_match else "",
        "no_status_ids": ns_ids_match.group(1).strip() if ns_ids_match else "",
    }


def parse_bt_detail(failure_reason):
    if not failure_reason or "bt_paths_speed_and_traveltime" not in failure_reason:
        return None
    import re
    speed_ok = re.search(r"Speed OK: (\d+)/(\d+)", failure_reason)
    failing = re.search(r"Failing paths: (.+?)$", failure_reason)
    return {
        "speed_ok": int(speed_ok.group(1)) if speed_ok else 0,
        "total": int(speed_ok.group(2)) if speed_ok else 0,
        "failing_paths": failing.group(1).strip() if failing else "",
    }


def parse_sensor_detail(failure_reason):
    if not failure_reason or "sensor_speed_status" not in failure_reason:
        return None
    import re
    working = re.search(r"Working: (\d+)/(\d+)", failure_reason)
    no_traffic = re.search(r"No traffic \(speed=0\): (\d+)", failure_reason)
    malfunction = re.search(r"Malfunctioning \(speed=-1\): (\d+)", failure_reason)
    no_meas = re.search(r"No measurement: (\d+)", failure_reason)
    mal_ids = re.search(r"Malfunctioning \(speed=-1\): \d+ — ([\d ,]+?)(?:\||\Z)", failure_reason)
    avg_flow = re.search(r"Avg flow rate: ([\d.]+)", failure_reason)
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


def _humanize_failure(check_name, detail):
    """Return a short, plain-English explanation of a check failure."""
    import re
    if check_name == "feed_freshness":
        return detail  # already readable: "Feed is 42 min old (limit: 15 min)"
    if check_name == "valid_xml":
        return "Response is not valid XML — the API may be down or returning an error page"
    if check_name == "vms_controller_status":
        m = re.search(r"Not working: (\d+)", detail)
        n = int(m.group(1)) if m else "?"
        return f"{n} VMS controller(s) reported as not working"
    if check_name == "sensor_speed_status":
        m = re.search(r"Malfunctioning \(speed=-1\): (\d+)", detail)
        n = int(m.group(1)) if m else "?"
        return f"{n} traffic sensor(s) reporting speed = -1 (hardware fault)"
    if check_name == "bt_paths_speed_and_traveltime":
        m = re.search(r"Speed OK: (\d+)/(\d+)", detail)
        if m:
            failing = int(m.group(2)) - int(m.group(1))
            return f"{failing} BT path(s) have no speed or travel time data"
        return "Some BT paths are missing speed or travel time data"
    if check_name == "predefined_paths_count":
        return "No predefined BT paths found in the feed"
    return detail


def build_sensor_stability_html(sensors):
    """Build the sensor stability panel HTML with a group dropdown."""
    if not sensors:
        return "<p style='color:var(--color-text-secondary);font-size:13px'>No sensor data recorded yet.</p>"

    groups = sorted({s["group_name"] for s in sensors})

    options = '<option value="all">All groups</option>'
    for g in groups:
        options += f'<option value="{g}">{g}</option>'

    rows = ""
    for s in sorted(sensors, key=lambda x: (x["group_name"], x["sensor_id"])):
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
            badge_bg, badge_color, badge_label = "#faece7", "#993c1d", "Unstable"

        # Sparkline: last 40 runs as tiny squares with rich tooltips
        sparks = ""
        for h in history[-40:]:
            c = STATUS_COLOR.get(h["status"], "#9ca3af")
            reason = STATUS_LABEL.get(h["status"], h["status"])
            ts = _to_cyprus(h["run_at"])
            sparks += f'<span title="{ts} — {reason}" style="display:inline-block;width:6px;height:14px;border-radius:2px;background:{c};margin-right:1px"></span>'

        # Last issue: most recent non-good entry, coloured to match the sparkline
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

        rows += f"""
        <tr data-group="{s['group_name']}">
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{s['group_name']}</td>
          <td style="font-size:13px;color:var(--color-text-primary);font-family:monospace">{s['sensor_id']}</td>
          <td style="white-space:nowrap">{sparks}</td>
          <td><span style="font-size:11px;font-weight:500;padding:2px 8px;border-radius:10px;background:{badge_bg};color:{badge_color}">{badge_label}</span></td>
          <td>{last_issue_html}</td>
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{good}/{total}</td>
        </tr>"""

    return f"""
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px">
      <div class="section-label" style="margin-bottom:0">Sensor stability</div>
      <select id="groupFilter" onchange="filterGroup(this.value)"
              style="font-size:13px;padding:5px 10px;border-radius:8px;border:0.5px solid var(--color-border-tertiary);
                     background:var(--color-background-primary);color:var(--color-text-primary);cursor:pointer">
        {options}
      </select>
    </div>
    <table id="sensorTable">
      <thead><tr><th>Group</th><th>Sensor ID</th><th>History (last 40 runs)</th><th>Stability</th><th>Last issue</th><th>Runs</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <script>
    function filterGroup(val) {{
      document.querySelectorAll('#sensorTable tbody tr').forEach(function(tr) {{
        tr.style.display = (val === 'all' || tr.dataset.group === val) ? '' : 'none';
      }});
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

    # Sensor stability
    all_sensors = fetch_sensor_stability()
    sensor_stability_html = build_sensor_stability_html(all_sensors)

    # Per-sensor statuses for the latest run (used for full ID lists in cards)
    latest_sensor_statuses = fetch_sensor_statuses_for_run(latest_run["run_id"])

    # Build group status cards
    def group_status_card(group_name, icon, results):
        all_pass = all(r["status"] == "pass" for r in results)
        any_error = any(r["status"] == "error" for r in results)
        status_color = "#1d9e75" if all_pass else ("#e58e0a" if any_error else "#e24b4a")
        status_bg = "#e1f5ee" if all_pass else ("#faeeda" if any_error else "#fcebeb")
        status_label = "Operational" if all_pass else ("Degraded" if any_error else "Issues detected")
        status_icon = "ti-circle-check" if all_pass else ("ti-alert-triangle" if any_error else "ti-circle-x")
        pass_count = sum(1 for r in results if r["status"] == "pass")

        detail_rows = ""
        for r in results:
            dot_color = "#1d9e75" if r["status"] == "pass" else ("#e58e0a" if r["status"] == "error" else "#e24b4a")

            failure_lines = ""
            if r["status"] != "pass" and r.get("failure_reason"):
                for part in r["failure_reason"].split(" | "):
                    if ": " in part:
                        cname, cdetail = part.split(": ", 1)
                        label = _humanize_failure(cname.strip(), cdetail.strip())
                    else:
                        label = part  # e.g. "Expected HTTP 200, got 503"
                    failure_lines += f'<div style="font-size:11px;color:{dot_color};padding:2px 0 0 16px;line-height:1.5">{label}</div>'

            # For Bluetooth Inventory, show device count from check_summary
            name_suffix = ""
            if r["test_name"] == "Bluetooth Inventory" and r.get("check_summary"):
                import re as _re
                m = _re.search(r"bt_site_count: (\d+)", r["check_summary"])
                if m:
                    name_suffix = f' <span style="font-size:11px;color:var(--color-text-secondary)">— {m.group(1)} devices</span>'

            detail_rows += f"""
            <div style="padding:6px 0;border-bottom:0.5px solid var(--color-border-tertiary)">
              <div style="display:flex;align-items:center;gap:8px">
                <span style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0"></span>
                <span style="font-size:13px;color:var(--color-text-primary);flex:1">{r['test_name']}{name_suffix}</span>
              </div>
              {failure_lines}
            </div>"""

        # Extra detail block — uses sensor_results DB for full untruncated ID lists
        extra = ""
        sensor_data = latest_sensor_statuses.get(group_name, {})

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
                          font-family:monospace;font-size:11px;color:var(--color-text-secondary);line-height:1.8;word-break:break-all">
                {id_list}
              </div>
            </details>"""

        for r in results:
            if r["status"] != "pass" and r.get("failure_reason"):
                fr = r["failure_reason"]
                if "vms_controller_status" in fr:
                    d = parse_vms_detail(fr)
                    if d:
                        vms_total = d['working'] + d['not_working'] + d['no_status']
                        vms_pct = round(d['working'] / vms_total * 100) if vms_total else 0
                        not_working_ids = sensor_data.get("not_working", [])
                        no_status_ids   = sensor_data.get("no_status", [])
                        extra += f"""
                        <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                          <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">VMS Controllers</div>
                          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                            <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                              <div style="width:{vms_pct}%;height:6px;background:#1d9e75;border-radius:3px"></div>
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
                elif "bt_paths_speed_and_traveltime" in fr:
                    d = parse_bt_detail(fr)
                    if d:
                        pct = round(d['speed_ok'] / d['total'] * 100) if d['total'] else 0
                        failing_ids = sensor_data.get("failing", [])
                        extra += f"""
                        <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                          <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">BT Paths with data</div>
                          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
                            <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                              <div style="width:{pct}%;height:6px;background:#1d9e75;border-radius:3px"></div>
                            </div>
                            <span style="color:var(--color-text-primary);font-weight:500">{d['speed_ok']}/{d['total']}</span>
                          </div>
                          {_collapsible_ids("failing paths", "#e24b4a", failing_ids)}
                        </div>"""
                elif "sensor_speed_status" in fr:
                    d = parse_sensor_detail(fr)
                    if d:
                        td_pct = round(d['working'] / d['total'] * 100) if d['total'] else 0
                        malfunctioning_ids  = sensor_data.get("malfunctioning", [])
                        no_traffic_ids      = sensor_data.get("no_traffic", [])
                        no_measurement_ids  = sensor_data.get("no_measurement", [])
                        extra += f"""
                        <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                          <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">Sensor breakdown — {d['total']} total</div>
                          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                            <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                              <div style="width:{td_pct}%;height:6px;background:#1d9e75;border-radius:3px"></div>
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
              <div style="font-size:15px;font-weight:500;color:var(--color-text-primary)">{group_name}</div>
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

    run_time = _to_cyprus(latest_run["run_at"])
    total_runs_label = len(runs)

    # Chart labels in Cyprus time
    chart_labels = json.dumps([_to_cyprus(r["run_at"]) for r in chart_runs])

    # Per-panel progress bar percentages
    latest_total = latest_run["total"] or 1
    overall_pct = round(latest_run["passed"] / latest_total * 100)
    overall_bar_color = "#1d9e75" if overall_pct == 100 else ("#e58e0a" if overall_pct >= 50 else "#e24b4a")

    trend_passed_total = sum(r["passed"] for r in chart_runs)
    trend_total = sum((r["total"] or 1) for r in chart_runs)
    trend_pct = round(trend_passed_total / trend_total * 100) if trend_total else 0
    trend_bar_color = "#1d9e75" if trend_pct == 100 else ("#e58e0a" if trend_pct >= 50 else "#e24b4a")

    sensor_good = sum(1 for s in all_sensors if s["history"] and s["history"][-1]["status"] in GOOD_STATUSES)
    sensor_total_count = len(all_sensors) or 1
    sensor_pct = round(sensor_good / sensor_total_count * 100)
    sensor_bar_color = "#1d9e75" if sensor_pct == 100 else ("#e58e0a" if sensor_pct >= 50 else "#e24b4a")

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
</style>
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

  <div class="panel" id="p-sensors">
    <div class="panel-header" onclick="togglePanel('p-sensors')">
      <span class="panel-title">Sensor stability</span>
      <div class="panel-chevron open" id="c-p-sensors"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar"><div class="panel-bar-fill" style="width:{sensor_pct}%;background:{sensor_bar_color}"></div></div>
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

var _dark = false;
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
</body></html>"""

    REPORT_PATH.write_text(html)
    return str(REPORT_PATH)