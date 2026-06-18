"""
Weekly digest email — sent every Monday at 08:00 Cyprus time.

Summarises the past 7 days of sensor health and flags:
  - Always-off sensors (0% healthy all week)
  - Persistently unstable sensors (below 70% for 2+ weeks)
  - Sensors that degraded vs the previous week
  - Sensors that recovered vs the previous week
  - Sensors retired (removed from API feed) this week
"""

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
    """Return (sensors, bt_paths) as separate sets of (group_name, sensor_id)."""
    conn = get_connection()
    sc = conn.execute("SELECT group_name, sensor_id FROM sensor_coords WHERE active=1").fetchall()
    bt = conn.execute("SELECT path_id FROM bt_path_coords WHERE active=1").fetchall()
    conn.close()
    sensors = {(r["group_name"], r["sensor_id"]) for r in sc}
    bt_paths = {("Bluetooth Paths", r["path_id"]) for r in bt}
    return sensors, bt_paths


def fetch_retired_this_week():
    """Return sensors that became inactive in the last 7 days."""
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    inactive_sc = conn.execute(
        "SELECT group_name, sensor_id FROM sensor_coords WHERE active=0"
    ).fetchall()

    retired = []
    for r in inactive_sc:
        last = conn.execute(
            "SELECT MAX(run_at) as last_seen FROM sensor_results WHERE group_name=? AND sensor_id=?",
            (r["group_name"], r["sensor_id"])
        ).fetchone()
        if last and last["last_seen"] and last["last_seen"] >= cutoff:
            retired.append({"group": r["group_name"], "sensor_id": r["sensor_id"], "last_seen": last["last_seen"][:10]})

    inactive_bt = conn.execute(
        "SELECT path_id FROM bt_path_coords WHERE active=0"
    ).fetchall()
    for r in inactive_bt:
        last = conn.execute(
            "SELECT MAX(run_at) as last_seen FROM sensor_results WHERE group_name='Bluetooth Paths' AND sensor_id=?",
            (r["path_id"],)
        ).fetchone()
        if last and last["last_seen"] and last["last_seen"] >= cutoff:
            retired.append({"group": "Bluetooth Paths", "sensor_id": r["path_id"], "last_seen": last["last_seen"][:10]})

    conn.close()
    return retired


def build_digest():
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=7)
    prev_week_start = today - timedelta(days=14)

    daily = fetch_sensor_health_by_day(14)
    active_sensors, active_bt = fetch_active_sensors()
    active = active_sensors | active_bt
    retired = fetch_retired_this_week()

    this_week_days = [(week_start      + timedelta(days=i)).isoformat() for i in range(7)]
    prev_week_days = [(prev_week_start + timedelta(days=i)).isoformat() for i in range(7)]

    def week_pct(key, days):
        good = total = 0
        for d in days:
            s = daily.get(key, {}).get(d, {})
            good  += s.get("good", 0)
            total += s.get("total", 0)
        return _health_pct(good, total)

    sensor_stats = []
    for key in active:
        this_pct = week_pct(key, this_week_days)
        prev_pct = week_pct(key, prev_week_days)
        if this_pct is None and prev_pct is None:
            continue
        sensor_stats.append({
            "group": key[0],
            "sensor_id": key[1],
            "this_pct": this_pct,
            "prev_pct": prev_pct,
        })

    always_off = [s for s in sensor_stats if s["this_pct"] is not None and s["this_pct"] == 0]
    persistent = [s for s in sensor_stats
                  if s["this_pct"] is not None and s["this_pct"] < 70
                  and s["prev_pct"] is not None and s["prev_pct"] < 70
                  and s["this_pct"] > 0]
    degraded   = sorted(
        [s for s in sensor_stats
         if s["this_pct"] is not None and s["prev_pct"] is not None
         and s["this_pct"] < s["prev_pct"] - 15
         and s not in always_off and s not in persistent],
        key=lambda x: x["this_pct"] - x["prev_pct"]
    )
    recovered  = sorted(
        [s for s in sensor_stats
         if s["this_pct"] is not None and s["prev_pct"] is not None
         and s["this_pct"] > s["prev_pct"] + 15],
        key=lambda x: -(x["this_pct"] - x["prev_pct"])
    )

    non_bt_stats   = [s for s in sensor_stats if s["group"] != "Bluetooth Paths"]
    total_sensors  = len(non_bt_stats)
    healthy_count  = sum(1 for s in non_bt_stats if s["this_pct"] is not None and s["this_pct"] >= 90)
    unstable_count = sum(1 for s in non_bt_stats if s["this_pct"] is not None and 0 < s["this_pct"] < 90)
    offline_count  = sum(1 for s in non_bt_stats if s["this_pct"] is not None and s["this_pct"] == 0)

    return {
        "week_start": week_start.strftime("%d %b %Y"),
        "week_end":   today.strftime("%d %b %Y"),
        "total":      total_sensors,
        "healthy":    healthy_count,
        "unstable":   unstable_count,
        "offline":    offline_count,
        "always_off": always_off,
        "persistent": persistent,
        "degraded":   degraded,
        "recovered":  recovered,
        "retired":    retired,
    }


def _sensor_table(sensors, show_prev=False):
    if not sensors:
        return '<p style="color:#6b7280;font-size:13px;margin:4px 0">None this week.</p>'
    rows = ""
    for s in sensors[:30]:
        prev_cell = f'<td style="padding:6px 12px">{_badge(s["prev_pct"])}</td>' if show_prev else ""
        rows += f"""
        <tr>
          <td style="padding:6px 12px;font-size:12px;color:#6b7280">{s['group']}</td>
          <td style="padding:6px 12px;font-family:monospace;font-size:13px">{s['sensor_id']}</td>
          <td style="padding:6px 12px">{_badge(s['this_pct'])}</td>
          {prev_cell}
        </tr>"""
    note = f'<p style="font-size:11px;color:#9ca3af;margin-top:4px">Showing top 30 of {len(sensors)}</p>' if len(sensors) > 30 else ""
    prev_header = '<th style="padding:6px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Prev week</th>' if show_prev else ""
    return f"""
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <thead><tr style="border-bottom:1px solid #e5e7eb">
        <th style="padding:6px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Group</th>
        <th style="padding:6px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Sensor ID</th>
        <th style="padding:6px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">This week</th>
        {prev_header}
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>{note}"""


def build_html(d):
    generated_at = datetime.now(CYPRUS_TZ).strftime("%d %b %Y %H:%M EEST")

    def section(title, color, content, open_by_default=True):
        open_attr = " open" if open_by_default else ""
        return f"""
        <details{open_attr} style="margin-bottom:28px">
          <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;margin-bottom:10px">
            <span style="font-size:15px;font-weight:600;color:{color}">{title}</span>
            <span style="font-size:11px;color:#9ca3af;margin-left:4px">(click to collapse)</span>
          </summary>
          {content}
        </details>"""

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
        <th style="padding:5px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Sensor</th>
        <th style="padding:5px 12px;text-align:left;font-weight:500;color:#6b7280;font-size:12px">Last seen</th>
      </tr></thead><tbody>{retired_rows}</tbody>
    </table>""" if d["retired"] else '<p style="color:#6b7280;font-size:13px;margin:4px 0">None this week.</p>'

    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#111;max-width:680px;margin:0 auto;padding:24px">

    <h2 style="margin:0 0 4px;font-size:20px">Weekly Network Health Digest</h2>
    <p style="color:#6b7280;font-size:13px;margin:0 0 24px">{d['week_start']} — {d['week_end']} &nbsp;·&nbsp; Generated {generated_at}</p>

    <div style="display:flex;gap:16px;margin-bottom:28px;flex-wrap:wrap">
      <div style="flex:1;min-width:120px;background:#f9fafb;border-radius:10px;padding:14px 18px;text-align:center">
        <div style="font-size:22px;font-weight:700">{d['total']}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:2px">Total sensors</div>
      </div>
      <div style="flex:1;min-width:120px;background:#ecfdf5;border-radius:10px;padding:14px 18px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:#1d9e75">{d['healthy']}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:2px">Healthy ≥90%</div>
      </div>
      <div style="flex:1;min-width:120px;background:#fffbeb;border-radius:10px;padding:14px 18px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:#e58e0a">{d['unstable']}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:2px">Unstable</div>
      </div>
      <div style="flex:1;min-width:120px;background:#fef2f2;border-radius:10px;padding:14px 18px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:#e24b4a">{d['offline']}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:2px">Always off</div>
      </div>
    </div>

    {section("🔴 Always off this week (0%)", "#e24b4a", _sensor_table(d['always_off']))}
    {section("🟠 Persistently unstable (&lt;70% for 2+ weeks)", "#e58e0a", _sensor_table(d['persistent'], show_prev=True))}
    {section("📉 Degraded vs last week (>15% drop)", "#e58e0a", _sensor_table(d['degraded'], show_prev=True))}
    {section("📈 Recovered vs last week (>15% improvement)", "#1d9e75", _sensor_table(d['recovered'], show_prev=True))}
    {section("🗑️ Retired this week (removed from API feed)", "#6b7280", retired_table)}

    <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb">
      <a href="{DASHBOARD_URL}" style="display:inline-block;background:#1d9e75;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:13px;font-weight:500">Open live dashboard →</a>
    </div>

    <p style="color:#9ca3af;font-size:11px;margin-top:24px">Cyprus ITS Infrastructure Monitor · Sent every Monday 08:00 EEST</p>
    </body></html>"""


def send_digest():
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pw   = os.environ.get("GMAIL_APP_PW", "")
    recipients = [r.strip() for r in os.environ.get("NOTIFY_EMAIL", gmail_user).split(",") if r.strip()]

    if not gmail_user or not gmail_pw:
        log.error("GMAIL_USER / GMAIL_APP_PW not set — cannot send digest.")
        sys.exit(1)

    d    = build_digest()
    html = build_html(d)
    week = d["week_start"]

    subject = (
        f"⚠️ Weekly Digest {week} — {d['offline']} offline, {d['unstable']} unstable"
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
    send_digest()
