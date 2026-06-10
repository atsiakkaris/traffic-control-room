"""
report.py - Generate a static HTML report from the SQLite history DB.
"""

import os
import json
from pathlib import Path
from datetime import datetime

from db import get_connection, fetch_recent_runs, fetch_results_for_run, fetch_sensor_stability

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
}

STATUS_LABEL = {
    "working": "Working",
    "ok": "OK",
    "no_traffic": "No traffic",
    "no_measurement": "No data",
    "no_status": "No status",
    "not_working": "Not working",
    "malfunctioning": "Malfunctioning",
    "failing": "Failing",
}

GOOD_STATUSES = {"working", "ok"}


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

        # Sparkline: last 40 runs as tiny squares
        sparks = ""
        for h in history[-40:]:
            c = STATUS_COLOR.get(h["status"], "#9ca3af")
            label = STATUS_LABEL.get(h["status"], h["status"])
            ts = h["run_at"][:16].replace("T", " ")
            sparks += f'<span title="{ts}: {label}" style="display:inline-block;width:6px;height:14px;border-radius:2px;background:{c};margin-right:1px"></span>'

        gid = s["group_name"].replace(" ", "_")
        rows += f"""
        <tr data-group="{s['group_name']}" data-gid="{gid}">
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{s['group_name']}</td>
          <td style="font-size:13px;color:var(--color-text-primary);font-family:monospace">{s['sensor_id']}</td>
          <td style="white-space:nowrap">{sparks}</td>
          <td><span style="font-size:11px;font-weight:500;padding:2px 8px;border-radius:10px;background:{badge_bg};color:{badge_color}">{badge_label}</span></td>
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{good}/{total} runs</td>
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
      <thead><tr><th>Group</th><th>Sensor ID</th><th>History (last 40 runs)</th><th>Status</th><th>Runs</th></tr></thead>
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
    chart_labels = json.dumps([r["run_at"][:10] + " " + r["run_at"][11:16] for r in chart_runs])
    chart_passed = json.dumps([r["passed"] for r in chart_runs])
    chart_failed = json.dumps([r["failed"] + r["errored"] for r in chart_runs])

    # Sensor stability
    all_sensors = fetch_sensor_stability()
    sensor_stability_html = build_sensor_stability_html(all_sensors)

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
            detail_rows += f"""
            <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:0.5px solid var(--color-border-tertiary)">
              <span style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0"></span>
              <span style="font-size:13px;color:var(--color-text-primary);flex:1">{r['test_name']}</span>
              <span style="font-size:12px;color:var(--color-text-secondary)">{r.get('response_ms') or '—'} ms</span>
            </div>"""

        # Extra detail block for failures
        extra = ""
        for r in results:
            if r["status"] != "pass" and r.get("failure_reason"):
                fr = r["failure_reason"]
                if "vms_controller_status" in fr:
                    d = parse_vms_detail(fr)
                    if d:
                        extra += f"""
                        <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                          <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">VMS Controllers</div>
                          <div style="display:flex;gap:16px;margin-bottom:8px">
                            <span style="color:#1d9e75"><b>{d['working']}</b> working</span>
                            <span style="color:#e24b4a"><b>{d['not_working']}</b> not working</span>
                            <span style="color:#888"><b>{d['no_status']}</b> no status</span>
                          </div>
                          {"<div style='color:var(--color-text-secondary);line-height:1.6'>Not working: " + d['not_working_ids'] + "</div>" if d['not_working_ids'] else ""}
                          {"<div style='color:var(--color-text-secondary);line-height:1.6;margin-top:4px'>No status: " + d['no_status_ids'] + "</div>" if d['no_status_ids'] else ""}
                        </div>"""
                elif "bt_paths_speed_and_traveltime" in fr:
                    d = parse_bt_detail(fr)
                    if d:
                        pct = round(d['speed_ok'] / d['total'] * 100) if d['total'] else 0
                        extra += f"""
                        <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                          <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">BT Paths with data</div>
                          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
                            <div style="flex:1;height:6px;background:var(--color-border-tertiary);border-radius:3px">
                              <div style="width:{pct}%;height:6px;background:#1d9e75;border-radius:3px"></div>
                            </div>
                            <span style="color:var(--color-text-primary);font-weight:500">{d['speed_ok']}/{d['total']}</span>
                          </div>
                          <div style="color:var(--color-text-secondary);line-height:1.6">Failing: {d['failing_paths']}</div>
                        </div>"""
                elif "sensor_speed_status" in fr:
                    d = parse_sensor_detail(fr)
                    if d:
                        extra += f"""
                        <div style="margin-top:12px;padding:12px;background:var(--color-background-secondary);border-radius:8px;font-size:12px">
                          <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">Sensor breakdown — {d['total']} total</div>
                          <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px">
                            <span style="color:#1d9e75"><b>{d['working']}</b> working</span>
                            <span style="color:#888"><b>{d['no_traffic']}</b> no traffic</span>
                            <span style="color:#e24b4a"><b>{d['malfunctioning']}</b> malfunctioning</span>
                            <span style="color:#888"><b>{d['no_measurement']}</b> no data</span>
                          </div>
                          {"<div style='color:var(--color-text-secondary);line-height:1.6'>Malfunctioning IDs: " + d['mal_ids'] + "</div>" if d['mal_ids'] else ""}
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
        ts = run["run_at"][:19].replace("T", " ")
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

    run_time = latest_run["run_at"][:19].replace("T", " ")
    total_runs_label = len(runs)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SWARCO Infrastructure Health</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #f5f6f8; --surface: #ffffff; --border: rgba(0,0,0,0.08);
    --text: #1a1a2e; --muted: #6b7280; --header-bg: #1a1a2e;
    --color-background-primary: #ffffff;
    --color-background-secondary: #f5f6f8;
    --color-text-primary: #1a1a2e;
    --color-text-secondary: #6b7280;
    --color-border-tertiary: rgba(0,0,0,0.08);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #111318; --surface: #1c1f26; --border: rgba(255,255,255,0.08);
             --text: #f0f2f5; --muted: #9ca3af; --header-bg: #0d0f14;
             --color-background-primary: #1c1f26;
             --color-background-secondary: #111318;
             --color-text-primary: #f0f2f5;
             --color-text-secondary: #9ca3af;
             --color-border-tertiary: rgba(255,255,255,0.08); }}
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: var(--bg); color: var(--text); min-height: 100vh; }}
  header {{ background: var(--header-bg); color: white; padding: 24px 32px;
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }}
  header h1 {{ font-size: 1.2rem; font-weight: 500; letter-spacing: -0.01em; }}
  header .meta {{ font-size: 12px; opacity: 0.5; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
  .section-label {{ font-size: 11px; font-weight: 500; letter-spacing: 0.08em;
                    text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }}
  .group-cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .panel {{ background: var(--surface); border: 0.5px solid var(--border);
            border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ font-size: 11px; font-weight: 500; letter-spacing: 0.05em; text-transform: uppercase;
        color: var(--muted); padding: 8px 12px; border-bottom: 0.5px solid var(--border); text-align: left; }}
  td {{ padding: 10px 12px; border-bottom: 0.5px solid var(--border); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
</style>
</head>
<body>
<header>
  <div>
    <h1><i class="ti ti-traffic-lights" style="font-size:18px;vertical-align:-2px;margin-right:8px" aria-hidden="true"></i>SWARCO Infrastructure Health</h1>
    <div class="meta">Last checked {run_time} UTC &nbsp;·&nbsp; {total_runs_label} runs recorded</div>
  </div>
  <div style="display:flex;gap:16px;font-size:12px;opacity:0.6">
    <span><i class="ti ti-circle-check" style="color:#1d9e75;vertical-align:-1px;margin-right:4px"></i>Pass</span>
    <span><i class="ti ti-circle-x" style="color:#e24b4a;vertical-align:-1px;margin-right:4px"></i>Fail</span>
    <span><i class="ti ti-alert-triangle" style="color:#e58e0a;vertical-align:-1px;margin-right:4px"></i>Error</span>
  </div>
</header>

<div class="wrap">

  <div class="section-label">Infrastructure groups</div>
  <div class="group-cards">
    {group_cards}
  </div>

  <div class="panel">
    <div class="section-label" style="margin-bottom:6px">Pass / fail trend — last {len(chart_runs)} runs</div>
    <div style="position:relative;height:180px">
      <canvas id="trendChart" role="img" aria-label="Stacked bar chart showing passed and failed test counts across recent runs"></canvas>
    </div>
    <div style="display:flex;gap:20px;margin-top:12px;font-size:2px;color:var(--muted)">
      <span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:#1d9e75;display:inline-block"></span>Passed</span>
      <span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:#e24b4a;display:inline-block"></span>Failed / errored</span>
    </div>
  </div>

  <div class="panel">
    {sensor_stability_html}
  </div>

  <div class="panel">
    <div class="section-label" style="margin-bottom:16px">Run history</div>
    <table>
      <thead><tr><th>Time (UTC)</th><th>Passed</th><th>Failed</th><th>Errored</th><th>Pass rate</th></tr></thead>
      <tbody>{history_rows}</tbody>
    </table>
  </div>

</div>

<script>
new Chart(document.getElementById('trendChart'), {{
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