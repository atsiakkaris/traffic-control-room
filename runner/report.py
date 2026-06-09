"""
report.py – Generate a static HTML report from the SQLite history DB.
"""

import os
import json
from pathlib import Path
from datetime import datetime

from db import get_connection, fetch_recent_runs, fetch_results_for_run

REPORT_PATH = Path("reports/latest.html")


def generate_report() -> str:
    REPORT_PATH.parent.mkdir(exist_ok=True)

    runs = fetch_recent_runs(60)
    if not runs:
        REPORT_PATH.write_text("<html><body>No runs yet.</body></html>")
        return str(REPORT_PATH)

    latest_run = runs[0]
    latest_results = fetch_results_for_run(latest_run["run_id"])

    # Build run trend data for chart (last 30 runs, chronological)
    chart_runs = list(reversed(runs[:30]))
    chart_labels = [r["run_at"][:10] + " " + r["run_at"][11:16] for r in chart_runs]
    chart_passed = [r["passed"] for r in chart_runs]
    chart_failed = [r["failed"] + r["errored"] for r in chart_runs]

    # Group latest results
    groups = {}
    for r in latest_results:
        groups.setdefault(r["group_name"], []).append(r)

    # Build history table rows
    history_rows = ""
    for run in runs[:20]:
        total = run["total"] or 1
        pct = round(run["passed"] / total * 100)
        bar_colour = "#28a745" if run["failed"] == 0 and run["errored"] == 0 else "#dc3545"
        history_rows += f"""
        <tr>
            <td>{run['run_at'][:19].replace('T',' ')}</td>
            <td><span style="color:{bar_colour};font-weight:bold">{run['passed']}/{total}</span></td>
            <td>{run['failed']}</td>
            <td>{run['errored']}</td>
            <td>
                <div style="background:#e9ecef;border-radius:4px;height:14px;width:120px">
                  <div style="background:{bar_colour};height:14px;border-radius:4px;width:{pct}%"></div>
                </div>
            </td>
        </tr>"""

    # Build latest test result rows
    test_rows = ""
    for group_name, results in groups.items():
        test_rows += f'<tr><td colspan="6" style="background:#343a40;color:white;padding:8px 12px;font-weight:bold">{group_name}</td></tr>'
        for r in results:
            colour = {"pass": "#d4edda", "fail": "#f8d7da", "error": "#fff3cd"}.get(r["status"], "#fff")
            icon = {"pass": "✅", "fail": "❌", "error": "⚠️"}.get(r["status"], "")
            reason = r.get("failure_reason") or ""
            test_rows += f"""
            <tr style="background:{colour}">
                <td>{r['test_name']}</td>
                <td>{icon} {r['status'].upper()}</td>
                <td>{r.get('status_code') or '—'}</td>
                <td>{r.get('response_ms') or '—'} ms</td>
                <td style="font-size:12px;word-break:break-word">{reason}</td>
                <td style="font-size:11px;color:#888">{r['endpoint']}</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SWARCO API Test History</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f4f6f8; color: #333; }}
  header {{ background: #1a1a2e; color: white; padding: 20px 32px; }}
  header h1 {{ font-size: 1.4rem; }}
  header p  {{ font-size: 0.85rem; opacity: .7; margin-top: 4px; }}
  .content {{ max-width: 1200px; margin: 24px auto; padding: 0 16px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }}
  .card {{ background: white; border-radius: 8px; padding: 20px 24px; flex: 1; min-width: 150px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .card .val {{ font-size: 2rem; font-weight: 700; }}
  .card .lbl {{ font-size: 0.8rem; color: #888; margin-top: 4px; }}
  .green {{ color: #28a745; }} .red {{ color: #dc3545; }} .orange {{ color: #fd7e14; }}
  .section {{ background: white; border-radius: 8px; padding: 20px 24px; margin-bottom: 24px;
              box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .section h2 {{ font-size: 1rem; margin-bottom: 16px; color: #555; text-transform: uppercase;
                 letter-spacing: .05em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f8f9fa; text-align: left; padding: 8px 10px; border-bottom: 2px solid #dee2e6; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  tr:hover td {{ background: rgba(0,0,0,.02); }}
  .chart-wrap {{ height: 200px; }}
</style>
</head>
<body>
<header>
  <h1>🚦 SWARCO API Test Suite</h1>
  <p>Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC · Historical data since first run</p>
</header>

<div class="content">

  <!-- Summary cards -->
  <div class="cards">
    <div class="card">
      <div class="val {'green' if latest_run['failed']==0 and latest_run['errored']==0 else 'red'}">{latest_run['passed']}/{latest_run['total']}</div>
      <div class="lbl">Passed (latest run)</div>
    </div>
    <div class="card">
      <div class="val {'red' if latest_run['failed'] > 0 else 'green'}">{latest_run['failed']}</div>
      <div class="lbl">Failed</div>
    </div>
    <div class="card">
      <div class="val {'orange' if latest_run['errored'] > 0 else 'green'}">{latest_run['errored']}</div>
      <div class="lbl">Errored</div>
    </div>
    <div class="card">
      <div class="val">{len(runs)}</div>
      <div class="lbl">Total runs recorded</div>
    </div>
  </div>

  <!-- Trend chart -->
  <div class="section">
    <h2>Pass / Fail Trend (last 30 runs)</h2>
    <div class="chart-wrap">
      <canvas id="trendChart"></canvas>
    </div>
  </div>

  <!-- Latest run results -->
  <div class="section">
    <h2>Latest Run — {latest_run['run_at'][:19].replace('T',' ')} UTC</h2>
    <table>
      <thead><tr>
        <th>Test</th><th>Result</th><th>HTTP</th><th>Time</th><th>Failure reason</th><th>Endpoint</th>
      </tr></thead>
      <tbody>{test_rows}</tbody>
    </table>
  </div>

  <!-- Run history -->
  <div class="section">
    <h2>Run History</h2>
    <table>
      <thead><tr><th>Date (UTC)</th><th>Passed</th><th>Failed</th><th>Errored</th><th>Pass rate</th></tr></thead>
      <tbody>{history_rows}</tbody>
    </table>
  </div>

</div>

<script>
const ctx = document.getElementById('trendChart').getContext('2d');
new Chart(ctx, {{
  type: 'bar',
  data: {{
    labels: {json.dumps(chart_labels)},
    datasets: [
      {{ label: 'Passed', data: {json.dumps(chart_passed)}, backgroundColor: '#28a745' }},
      {{ label: 'Failed/Error', data: {json.dumps(chart_failed)}, backgroundColor: '#dc3545' }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{
      x: {{ stacked: true }},
      y: {{ stacked: true, beginAtZero: true, ticks: {{ stepSize: 1 }} }}
    }}
  }}
}});
</script>
</body></html>"""

    REPORT_PATH.write_text(html)
    return str(REPORT_PATH)
