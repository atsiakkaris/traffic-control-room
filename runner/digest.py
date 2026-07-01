import os
import sys
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CYPRUS_TZ = ZoneInfo("Asia/Nicosia")
GOOD_STATUSES = {"working", "ok"}
DASHBOARD_URL = "https://atsiakkaris.github.io/traffic-control-room/reports/latest.html"


def _health_pct(good, total):
    return round(good / total * 100) if total else None


def _badge(pct):
    if pct is None:
        return '<span style="background:#e5e7eb;color:#6b7280;padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap">No data</span>'
    if pct == 100:
        bg, fg, label = "#e1f5ee", "#085041", "Always on"
    elif pct >= 90:
        bg, fg, label = "#c0dd97", "#27500a", "Healthy"
    elif pct >= 70:
        bg, fg, label = "#faeeda", "#633806", "Intermittent"
    elif pct >= 40:
        bg, fg, label = "#fac775", "#412402", "Unstable"
    elif pct > 0:
        bg, fg, label = "#f09595", "#501313", "Critical"
    else:
        bg, fg, label = "#e24b4a", "#ffffff", "Always off"
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap">{label} ({pct}%)</span>'


def fetch_sensor_health_by_day(days_back):
    """Return {(group_name, sensor_id): {date_str: {good, total}}} for the last N days."""
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    rows = conn.execute(
        "SELECT group_name, sensor_id, run_at, status FROM sensor_results WHERE run_at >= ? ORDER BY run_at",
        (cutoff,)
    ).fetchall()
    conn.close()

    data = {}
    for r in rows:
        key = (r["group_name"], r["sensor_id"])
        day = r["run_at"][:10]
        if key not in data:
            data[key] = {}
        if day not in data[key]:
            data[key][day] = {"good": 0, "total": 0}
        data[key][day]["total"] += 1
        if r["status"] in GOOD_STATUSES:
            data[key][day]["good"] += 1
    return data


def fetch_active_sensors():
    """Return (sensors, bt_paths) as sets of (group_name, sensor_id), plus a name lookup dict."""
    conn = get_connection()
    sc = conn.execute("SELECT group_name, sensor_id, name FROM sensor_coords WHERE active=1").fetchall()
    bt = conn.execute("SELECT path_id, name FROM bt_path_coords WHERE active=1").fetchall()
    conn.close()
    sensors  = {(r["group_name"], r["sensor_id"]) for r in sc}
    bt_paths = {("Bluetooth Paths", r["path_id"]) for r in bt}
    names    = {(r["group_name"], r["sensor_id"]): r["name"] or r["sensor_id"] for r in sc}
    names   |= {("Bluetooth Paths", r["path_id"]): r["name"] or r["path_id"] for r in bt}
    return sensors, bt_paths, names


def fetch_retired_this_week():
    """Return sensors that became inactive in the last 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = get_connection()

    sensor_rows = conn.execute("""
        SELECT sc.group_name, sc.sensor_id, MAX(sr.run_at) AS last_seen
        FROM sensor_coords sc
        LEFT JOIN sensor_results sr ON sr.group_name = sc.group_name AND sr.sensor_id = sc.sensor_id
        WHERE sc.active = 0
        GROUP BY sc.group_name, sc.sensor_id
        HAVING last_seen >= ?
    """, (cutoff,)).fetchall()

    bt_rows = conn.execute("""
        SELECT bt.path_id, MAX(sr.run_at) AS last_seen
        FROM bt_path_coords bt
        LEFT JOIN sensor_results sr ON sr.group_name = 'Bluetooth Paths' AND sr.sensor_id = bt.path_id
        WHERE bt.active = 0
        GROUP BY bt.path_id
        HAVING last_seen >= ?
    """, (cutoff,)).fetchall()

    conn.close()

    retired = [
        {"group": r["group_name"], "sensor_id": r["sensor_id"], "last_seen": r["last_seen"][:10]}
        for r in sensor_rows
    ]
    retired += [
        {"group": "Bluetooth Paths", "sensor_id": r["path_id"], "last_seen": r["last_seen"][:10]}
        for r in bt_rows
    ]
    return retired


def _counts(stats):
    return {
        "total":        len(stats),
        "always_on":    sum(1 for s in stats if s["this_pct"] is not None and s["this_pct"] == 100),
        "healthy":      sum(1 for s in stats if s["this_pct"] is not None and 90 <= s["this_pct"] < 100),
        "intermittent": sum(1 for s in stats if s["this_pct"] is not None and 70 <= s["this_pct"] < 90),
        "unstable":     sum(1 for s in stats if s["this_pct"] is not None and 40 <= s["this_pct"] < 70),
        "critical":     sum(1 for s in stats if s["this_pct"] is not None and 0 < s["this_pct"] < 40),
        "offline":      sum(1 for s in stats if s["this_pct"] is not None and s["this_pct"] == 0),
    }


def _derive_check_frequency(daily, today):
    """Estimate the check interval label from the number of runs per sensor this week."""
    week_days = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
    totals = [
        sum(days_data.get(d, {}).get("total", 0) for d in week_days)
        for days_data in daily.values()
        if any(days_data.get(d, {}).get("total", 0) for d in week_days)
    ]
    if not totals:
        return "periodically"
    runs_per_day = (sum(totals) / len(totals)) / 7
    if runs_per_day >= 1:
        hours = round(24 / runs_per_day)
        return f"every {hours} hour{'s' if hours != 1 else ''}"
    return "daily"


def build_digest():
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=7)
    prev_week_start = today - timedelta(days=14)

    daily = fetch_sensor_health_by_day(14)
    active_sensors, active_bt, sensor_names = fetch_active_sensors()
    active = active_sensors | active_bt
    retired = fetch_retired_this_week()

    conn = get_connection()
    run_count = conn.execute(
        "SELECT COUNT(DISTINCT run_at) FROM sensor_results WHERE run_at >= ?",
        (week_start.isoformat(),)
    ).fetchone()[0]
    conn.close()

    check_freq = _derive_check_frequency(daily, today)

    this_week_days = [(week_start      + timedelta(days=i)).isoformat() for i in range(7)]
    prev_week_days = [(prev_week_start + timedelta(days=i)).isoformat() for i in range(7)]

    def week_pct(key, days):
        good = total = 0
        for d in days:
            s = daily.get(key, {}).get(d, {})
            good  += s.get("good", 0)
            total += s.get("total", 0)
        return _health_pct(good, total)

    sensor_stats = [
        {
            "group":     key[0],
            "sensor_id": key[1],
            "name":      sensor_names.get(key, str(key[1])),
            "this_pct":  week_pct(key, this_week_days),
            "prev_pct":  week_pct(key, prev_week_days),
        }
        for key in active
        if week_pct(key, this_week_days) is not None or week_pct(key, prev_week_days) is not None
    ]

    always_off = sorted(
        [s for s in sensor_stats if s["this_pct"] is not None and s["this_pct"] == 0],
        key=lambda x: x["group"]
    )
    persistent = sorted(
        [s for s in sensor_stats
         if s["this_pct"] is not None and s["this_pct"] < 70
         and s["prev_pct"] is not None and s["prev_pct"] < 70
         and s["this_pct"] > 0],
        key=lambda x: x["this_pct"]
    )
    degraded = sorted(
        [s for s in sensor_stats
         if s["this_pct"] is not None and s["prev_pct"] is not None
         and s["this_pct"] < s["prev_pct"] - 15
         and s not in always_off and s not in persistent],
        key=lambda x: x["this_pct"] - x["prev_pct"]
    )
    recovered = sorted(
        [s for s in sensor_stats
         if s["this_pct"] is not None and s["prev_pct"] is not None
         and s["this_pct"] > s["prev_pct"] + 15],
        key=lambda x: -(x["this_pct"] - x["prev_pct"])
    )

    non_bt_stats = [s for s in sensor_stats if s["group"] != "Bluetooth Paths"]
    bt_stats     = [s for s in sensor_stats if s["group"] == "Bluetooth Paths"]

    # per-group breakdown (non-BT), preserving natural order
    group_names = list(dict.fromkeys(s["group"] for s in non_bt_stats))
    groups = {g: _counts([s for s in non_bt_stats if s["group"] == g]) for g in group_names}

    return {
        "week_start":  week_start.strftime("%d %b %Y"),
        "week_end":    today.strftime("%d %b %Y"),
        "run_count":   run_count,
        "check_freq":  check_freq,
        **_counts(non_bt_stats),
        "groups":      groups,
        "bt":          _counts(bt_stats),
        "always_off":  always_off,
        "persistent":  persistent,
        "degraded":    degraded,
        "recovered":   recovered,
        "retired":     retired,
    }


def _sensor_table(sensors, show_prev=False):
    if not sensors:
        return '<p style="color:#6b7280;font-size:13px;margin:4px 0">None this week.</p>'
    rows = ""
    for s in sensors[:30]:
        prev_cell = f'<td style="padding:6px 12px;white-space:nowrap;width:1%;text-align:right">{_badge(s["prev_pct"])}</td>' if show_prev else ""
        rows += f"""
        <tr style="border-bottom:1px solid #f3f4f6">
          <td style="padding:6px 12px;font-size:13px">{s['name']}</td>
          <td style="padding:6px 12px;white-space:nowrap;width:1%;text-align:right">{_badge(s['this_pct'])}</td>
          {prev_cell}
        </tr>"""
    note = f'<p style="font-size:11px;color:#9ca3af;margin-top:4px"><strong>Showing top 30 of {len(sensors)}</strong></p>' if len(sensors) > 30 else ""
    prev_header = '<th style="padding:6px 12px;text-align:right;font-weight:500;color:#6b7280;font-size:12px;white-space:nowrap">Prev week</th>' if show_prev else ""
    return f"""
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <thead><tr style="border-bottom:1px solid #e5e7eb">
        <th style="padding:6px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Name</th>
        <th style="padding:6px 12px;text-align:right;font-weight:500;color:#6b7280;font-size:12px;white-space:nowrap">This week</th>
        {prev_header}
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>{note}"""


def _split_table(sensors, show_prev=False):
    """Render one sub-table per group, Bluetooth Paths last."""
    if not sensors:
        return '<p style="color:#6b7280;font-size:13px;margin:4px 0">None this week.</p>'
    # collect groups preserving insertion order, BT last
    groups = []
    for s in sensors:
        if s["group"] not in groups and s["group"] != "Bluetooth Paths":
            groups.append(s["group"])
    if any(s["group"] == "Bluetooth Paths" for s in sensors):
        groups.append("Bluetooth Paths")
    out = ""
    for g in groups:
        subset = [s for s in sensors if s["group"] == g]
        out += f'<p style="font-size:12px;font-weight:600;color:#374151;margin:16px 0 6px">{g}</p>'
        out += _sensor_table(subset, show_prev=show_prev)
    return out


def _always_off_summary(sensors):
    """Returns the always-visible summary text for the always-off section."""
    if not sensors:
        return "None this week — all sensors reported at least some activity."
    # count per group, BT last
    groups = {}
    for s in sensors:
        groups.setdefault(s["group"], 0)
        groups[s["group"]] += 1
    bt_count = groups.pop("Bluetooth Paths", 0)
    parts = [f"<strong>{c} {g}</strong>" for g, c in groups.items()]
    if bt_count:
        parts.append(f"<strong>{bt_count} Bluetooth path{'s' if bt_count != 1 else ''}</strong>")
    if not parts:
        return "None this week — all sensors reported at least some activity."
    joined = ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0])
    return (f"{joined} recorded no successful checks during this period. "
            f"Possible causes include equipment faults, loss of network connectivity, or sensors "
            f"that have been physically removed but not yet formally retired. "
            f"These should be investigated or decommissioned.")


def build_html(d):
    generated_at = datetime.now(CYPRUS_TZ).strftime("%d %b %Y %H:%M EEST")

    def section(title, color, description, content):
        return f"""
        <div style="margin-bottom:28px">
          <h3 style="font-size:15px;font-weight:600;color:{color};margin:0 0 6px">{title}</h3>
          <p style="font-size:13px;color:#374151;margin:0 0 10px">{description}</p>
          <details open>
            <summary style="cursor:pointer;list-style:none;font-size:12px;color:#9ca3af;margin-bottom:8px">Show breakdown ▾</summary>
            {content}
          </details>
        </div>"""

    retired_rows = "".join(
        f'<tr><td style="padding:5px 12px;font-size:12px;color:#6b7280">{r["group"]}</td>'
        f'<td style="padding:5px 12px;font-family:monospace;font-size:13px">{r["sensor_id"]}</td>'
        f'<td style="padding:5px 12px;font-size:12px;color:#6b7280">{r["last_seen"]}</td></tr>'
        for r in d["retired"]
    )
    retired_table = f"""
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <thead><tr style="border-bottom:1px solid #e5e7eb">
        <th style="padding:5px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Group</th>
        <th style="padding:5px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">ID</th>
        <th style="padding:5px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Last seen</th>
      </tr></thead><tbody>{retired_rows}</tbody>
    </table>""" if d["retired"] else '<p style="color:#6b7280;font-size:13px;margin:4px 0">None this week.</p>'

    badge_legend = f"""
    <div style="background:#f9fafb;border-radius:8px;padding:12px 16px;margin-bottom:28px;font-size:12px">
      <div style="font-weight:600;color:#374151;margin-bottom:8px">How to read the health badges</div>
      <p style="color:#6b7280;margin:0 0 8px">
        The <strong>health %</strong> is the share of automated checks (run {d['check_freq']}) that returned
        a successful response during the week. A sensor at 100% responded correctly to every check;
        one at 0% failed every check.
      </p>
      <div>
        <span style="display:inline-block;background:#e1f5ee;color:#085041;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Always on — 100%</span>
        <span style="display:inline-block;background:#c0dd97;color:#27500a;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Healthy — 90–99%</span>
        <span style="display:inline-block;background:#faeeda;color:#633806;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Intermittent — 70–89%</span>
        <span style="display:inline-block;background:#fac775;color:#412402;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Unstable — 40–69%</span>
        <span style="display:inline-block;background:#f09595;color:#501313;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Critical — 1–39%</span>
        <span style="display:inline-block;background:#e24b4a;color:#ffffff;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Always off — 0%</span>
      </div>
    </div>"""

    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#111;max-width:680px;margin:0 auto;padding:24px">

    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:4px">
      <tr>
        <td><h2 style="margin:0;font-size:20px">Cyprus ITS — Weekly Network Health Digest</h2></td>
        <td align="right" style="white-space:nowrap"><a href="{DASHBOARD_URL}" style="display:inline-block;background:#1d9e75;color:#fff;text-decoration:none;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:500;white-space:nowrap">See dashboard →</a></td>
      </tr>
    </table>
    <p style="color:#6b7280;font-size:13px;margin:0 0 12px">{d['week_start']} — {d['week_end']} &nbsp;·&nbsp; Generated {generated_at} &nbsp;·&nbsp; {d['run_count']} runs</p>
    <p style="font-size:13px;color:#374151;margin:0 0 16px">
      This report summarises the health of Cyprus' ITS infrastructure sensors for the past week.
      It highlights sensors that went offline, are performing below expectations, or have changed
      significantly since last week.
    </p>

    {badge_legend}

    <div style="font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Sensors</div>
    <table width="100%" cellspacing="4" cellpadding="0" style="margin-bottom:12px;border-collapse:separate;border-spacing:4px">
      <tr>
        <td width="14%" style="background:#f9fafb;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700">{d['total']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Monitored</div>
        </td>
        <td width="14%" style="background:#e1f5ee;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#085041">{d['always_on']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Always on (100%)</div>
        </td>
        <td width="14%" style="background:#ecfdf5;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#1d9e75">{d['healthy']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Healthy (90–99%)</div>
        </td>
        <td width="14%" style="background:#faeeda;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#633806">{d['intermittent']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Intermittent (70–89%)</div>
        </td>
        <td width="14%" style="background:#fffbeb;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e58e0a">{d['unstable']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Unstable (40–69%)</div>
        </td>
        <td width="14%" style="background:#fde8e8;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#9b1c1c">{d['critical']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Critical (1–39%)</div>
        </td>
        <td width="14%" style="background:#fef2f2;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e24b4a">{d['offline']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Always off (0%)</div>
        </td>
      </tr>
    </table>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:16px">
      <thead>
        <tr style="border-bottom:1px solid #e5e7eb">
          <th style="padding:5px 8px;text-align:left;font-weight:500;color:#9ca3af">Group</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#9ca3af">Total</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#085041">Always on</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#1d9e75">Healthy</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#633806">Intermittent</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#e58e0a">Unstable</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#9b1c1c">Critical</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#e24b4a">Always off</th>
        </tr>
      </thead>
      <tbody>
        {"".join(f'''<tr style="border-bottom:1px solid #f3f4f6">
          <td style="padding:5px 8px;color:#374151">{g}</td>
          <td style="padding:5px 8px;text-align:center;color:#374151">{c['total']}</td>
          <td style="padding:5px 8px;text-align:center;color:#085041">{c['always_on']}</td>
          <td style="padding:5px 8px;text-align:center;color:#1d9e75">{c['healthy']}</td>
          <td style="padding:5px 8px;text-align:center;color:#633806">{c['intermittent']}</td>
          <td style="padding:5px 8px;text-align:center;color:#e58e0a">{c['unstable']}</td>
          <td style="padding:5px 8px;text-align:center;color:#9b1c1c">{c['critical']}</td>
          <td style="padding:5px 8px;text-align:center;color:#e24b4a">{c['offline']}</td>
        </tr>''' for g, c in d['groups'].items())}
      </tbody>
    </table>

    <div style="font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Bluetooth Paths</div>
    <table width="100%" cellspacing="4" cellpadding="0" style="margin-bottom:28px;border-collapse:separate;border-spacing:4px">
      <tr>
        <td width="14%" style="background:#f9fafb;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700">{d['bt']['total']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Monitored</div>
        </td>
        <td width="14%" style="background:#e1f5ee;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#085041">{d['bt']['always_on']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Always on (100%)</div>
        </td>
        <td width="14%" style="background:#ecfdf5;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#1d9e75">{d['bt']['healthy']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Healthy (90–99%)</div>
        </td>
        <td width="14%" style="background:#faeeda;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#633806">{d['bt']['intermittent']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Intermittent (70–89%)</div>
        </td>
        <td width="14%" style="background:#fffbeb;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e58e0a">{d['bt']['unstable']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Unstable (40–69%)</div>
        </td>
        <td width="14%" style="background:#fde8e8;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#9b1c1c">{d['bt']['critical']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Critical (1–39%)</div>
        </td>
        <td width="14%" style="background:#fef2f2;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e24b4a">{d['bt']['offline']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Always off (0%)</div>
        </td>
      </tr>
    </table>

    {section("🔴 Always off this week",
             "#e24b4a",
             _always_off_summary(d['always_off']),
             _split_table(d['always_off']) if d['always_off'] else "")}
    {section("🟠 Persistently underperforming",
             "#e58e0a",
             "Sensors and paths that have been below 70% health for at least two consecutive weeks.",
             _split_table(d['persistent'], show_prev=True))}
    {section("📉 Degraded since last week",
             "#e58e0a",
             "Sensors and paths whose health dropped by more than 15 percentage points compared to the previous week.",
             _split_table(d['degraded'], show_prev=True))}
    {section("📈 Recovered since last week",
             "#1d9e75",
             "Sensors and paths whose health improved by more than 15 percentage points compared to the previous week.",
             _split_table(d['recovered'], show_prev=True))}
    {section("🗑️ Retired this week",
             "#6b7280",
             "Sensors that were removed from the API feed this week. They are no longer being monitored.",
             retired_table)}

    <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb">
      <a href="{DASHBOARD_URL}" style="display:inline-block;background:#1d9e75;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:13px;font-weight:500">See dashboard →</a>
    </div>

    <p style="color:#9ca3af;font-size:11px;margin-top:24px">Cyprus ITS Infrastructure Monitor · Sent every Monday at 07:30 EEST</p>
    </body></html>"""


def send_digest():
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pw   = os.environ.get("GMAIL_APP_PW", "")
    recipients = [r.strip() for r in os.environ.get("NOTIFY_EMAIL", gmail_user).split(",") if r.strip()]

    if not gmail_user or not gmail_pw:
        log.error("GMAIL_USER / GMAIL_APP_PW not set — cannot send digest.")
        sys.exit(1)

    log.info("Sending digest to: %s", ", ".join(recipients))
    d    = build_digest()
    html = build_html(d)
    week = d["week_start"]

    subject = (
        f"Weekly Infrastructure Status: {week} — {d['offline']} offline, {d['unstable']} unstable"
        if d["offline"] or d["unstable"]
        else f"✅ Weekly Digest {week} — Network healthy"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pw)
            server.sendmail(gmail_user, recipients, msg.as_string())
        log.info("Weekly digest sent to %s", ", ".join(recipients))
    except Exception as e:
        log.error("Failed to send digest: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    if "--preview" in sys.argv:
        import webbrowser
        d    = build_digest()
        html = build_html(d)
        out  = Path(__file__).parent.parent / "reports" / "digest_preview.html"
        out.write_text(html, encoding="utf-8")
        print(f"Preview saved: {out}")
        webbrowser.open(str(out))
    else:
        send_digest()
