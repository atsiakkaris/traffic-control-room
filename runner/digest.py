import os
import re
import sys
import html
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection, fetch_sensor_projects, fetch_sensor_stability
from labels import sensor_display_name, with_id
from stability import (CYPRUS_TZ, GOOD_STATUSES, EXCLUDED_COMMISSIONING,
                       STABILITY_TIERS, tier_for, health_pct,
                       load_project_accountability, contract_census)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DASHBOARD_URL = "https://atsiakkaris.github.io/traffic-control-room/"
# Tagged with UTM params so GoatCounter can attribute these clicks to the
# digest email specifically, separate from other traffic to the same URL.
DASHBOARD_URL_TAGGED = DASHBOARD_URL + "?utm_source=digest&utm_medium=email&utm_campaign=weekly_digest"

# Percentage ranges for the digest's badge labels, taken straight from the tier
# definitions so they can't drift out of sync with the actual thresholds.
_TIER_RANGE = {t.key: t.range_label for t in STABILITY_TIERS}

# The shared tiers are LIFETIME-scoped on the dashboard, where "Always off" is a
# claim that the sensor has never once worked. The digest recomputes them over a
# single week, so that label would be a different (and much stronger) statement
# than the data supports. Rename the zero tier for this surface only.
_WEEK_TIER_LABEL = {"offline": "No good runs"}
_TIER_RANGE["offline"] = "0% this week"


def _tier_label(tier):
    """Week-scoped label for a shared tier — see _WEEK_TIER_LABEL."""
    return _WEEK_TIER_LABEL.get(tier.key, tier.label)


def _badge(pct):
    if pct is None:
        return '<span style="background:#e5e7eb;color:#6b7280;padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap">No data</span>'
    tier = tier_for(pct)
    return f'<span style="background:{tier.bg};color:{tier.fg};padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap">{_tier_label(tier)} ({pct}%)</span>'


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
    sc = conn.execute("SELECT group_name, sensor_id, name, site_code FROM sensor_coords WHERE active=1").fetchall()
    bt = conn.execute("SELECT path_id, name FROM bt_path_coords WHERE active=1").fetchall()
    conn.close()
    sensors  = {(r["group_name"], r["sensor_id"]) for r in sc}
    bt_paths = {("Bluetooth Paths", r["path_id"]) for r in bt}
    # Same label the dashboard shows, so a sensor never appears under two names.
    names    = {(r["group_name"], r["sensor_id"]):
                sensor_display_name(r["group_name"], r["sensor_id"], r["name"], r["site_code"])
                for r in sc}
    names   |= {("Bluetooth Paths", r["path_id"]):
                sensor_display_name("Bluetooth Paths", r["path_id"], r["name"])
                for r in bt}
    return sensors, bt_paths, names


def fetch_retired_this_week():
    """Return sensors that became inactive in the last 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = get_connection()

    sensor_rows = conn.execute("""
        SELECT sc.group_name, sc.sensor_id, sc.name, sc.site_code, MAX(sr.run_at) AS last_seen
        FROM sensor_coords sc
        LEFT JOIN sensor_results sr ON sr.group_name = sc.group_name AND sr.sensor_id = sc.sensor_id
        WHERE sc.active = 0
        GROUP BY sc.group_name, sc.sensor_id
        HAVING last_seen >= ?
    """, (cutoff,)).fetchall()

    bt_rows = conn.execute("""
        SELECT bt.path_id, bt.name, MAX(sr.run_at) AS last_seen
        FROM bt_path_coords bt
        LEFT JOIN sensor_results sr ON sr.group_name = 'Bluetooth Paths' AND sr.sensor_id = bt.path_id
        WHERE bt.active = 0
        GROUP BY bt.path_id
        HAVING last_seen >= ?
    """, (cutoff,)).fetchall()

    conn.close()

    retired = [
        {"group": r["group_name"], "sensor_id": r["sensor_id"], "name": r["name"],
         "site_code": r["site_code"], "last_seen": r["last_seen"][:10]}
        for r in sensor_rows
    ]
    retired += [
        {"group": "Bluetooth Paths", "sensor_id": r["path_id"], "name": r["name"],
         "site_code": None, "last_seen": r["last_seen"][:10]}
        for r in bt_rows
    ]
    return retired


def _retired_label(row):
    """Same label as the dashboard, with the raw id kept for quoting to the contractor."""
    label = sensor_display_name(row["group"], row["sensor_id"],
                                row.get("name"), row.get("site_code"))
    return with_id(html.escape(label), row["sensor_id"])


def fetch_excluded_commissioning():
    """{(group_name, sensor_id)} for sensors not expected to be working.

    An unpowered VMS is published by the API and reports not_working forever.
    Without this, the digest names all 38 of them under "No good runs this week"
    and the real faults are lost in the noise. The dashboard already excludes
    them; this keeps the weekly email agreeing with it.
    """
    projects = fetch_sensor_projects()
    return {
        (group, sensor_id)
        for group, sensors in projects.items()
        for sensor_id, info in sensors.items()
        if info.get("commissioning") in EXCLUDED_COMMISSIONING
    }


def fetch_contract_census(active_keys):
    """Per-contract sensor census — the same computation the dashboard's
    "Sensors by contract" panel uses (stability.contract_census), so the email
    and the live dashboard never disagree. `active_keys` is the set of
    (group_name, sensor_id) currently active, so retired equipment is excluded.

    A 'fault' here is a persistent problem (failed >=80% of the last 20 runs),
    not a sensor that merely blipped in the latest run.
    """
    owned = [s for s in fetch_sensor_stability()
             if (s["group_name"], s["sensor_id"]) in active_keys]
    return contract_census(owned, fetch_sensor_projects(), load_project_accountability())


def _counts(stats):
    counts = {"total": len(stats)}
    counts.update({tier.key: 0 for tier in STABILITY_TIERS})
    for s in stats:
        if s["this_pct"] is not None:
            counts[tier_for(s["this_pct"]).key] += 1
    return counts


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
    # Awaiting-power and decommissioned sensors are not faults — same rule the
    # dashboard applies, so the two never disagree about who is failing.
    active = (active_sensors | active_bt) - fetch_excluded_commissioning()
    retired = fetch_retired_this_week()
    census = fetch_contract_census(active_sensors | active_bt)

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
        return health_pct(good, total)

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
        "census":      census,
        "sensor_names": sensor_names,
    }


def _sensor_table(sensors, show_prev=False):
    if not sensors:
        return '<p style="color:#6b7280;font-size:13px;margin:4px 0">None this week.</p>'
    rows = ""
    for s in sensors[:30]:
        prev_cell = f'<td style="padding:6px 12px;white-space:nowrap;width:1%;text-align:right">{_badge(s["prev_pct"])}</td>' if show_prev else ""
        rows += f"""
        <tr style="border-bottom:1px solid #f3f4f6">
          <td style="padding:6px 12px;font-size:13px">{html.escape(str(s['name']))}</td>
          <td style="padding:6px 12px;white-space:nowrap;width:1%;text-align:right">{_badge(s['this_pct'])}</td>
          {prev_cell}
        </tr>"""
    note = f'<p style="font-size:11px;color:#9ca3af;margin-top:4px"><strong>Showing top 30 of {len(sensors)}</strong></p>' if len(sensors) > 30 else ""
    prev_header = '<th style="padding:6px 12px;text-align:right;font-weight:500;color:#6b7280;font-size:12px;white-space:nowrap">Previous week</th>' if show_prev else ""
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


_CONTRACT_STATUS_CHIP = {
    "supported":      ("In support",     "#0f6e56", "#e1f5ee"),
    "out_of_support": ("Out of support", "#7f8c8d", "#f3f4f6"),
    "unassigned":     ("No plan",        "#a32d2d", "#fcebeb"),
}


def _group_disp(group):
    # The digest has no ui_labels context; the DB group names are already the
    # display names for the owned groups (Traffic Detection / VMS).
    return group


def _contract_summary_html(census, sensor_names):
    """Per-contract census for the weekly email: an at-a-glance counts table,
    then a collapsible list of the failing sensors under each contract that has
    any. Same data and definitions as the dashboard's "Sensors by contract"
    panel (stability.contract_census) so the two never disagree."""
    if not census:
        return '<p style="color:#6b7280;font-size:13px;margin:4px 0">No contract data yet.</p>'

    th = "padding:5px 10px;font-size:11px;font-weight:500;color:#9ca3af;text-transform:uppercase;letter-spacing:0.04em"
    summary_rows = ""
    for c in census:
        label, fg, bg = _CONTRACT_STATUS_CHIP.get(c["acct"], ("—", "#6b7280", "#f3f4f6"))
        groups = ", ".join(f'{_group_disp(g)} {n}' for g, n in sorted(c["groups"].items()))
        fault_color = "#7f8c8d" if c["acct"] == "out_of_support" else "#e24b4a"
        fault_cell = (f'<span style="color:{fault_color};font-weight:700">{c["down"]}</span>'
                      if c["down"] else '<span style="color:#9ca3af">0</span>')
        notlive = (f'<span style="color:#7f8c8d">{c["not_live"]}</span>'
                   if c["not_live"] else '<span style="color:#9ca3af">0</span>')
        summary_rows += (
            f'<tr style="border-top:1px solid #f3f4f6">'
            f'<td style="padding:6px 10px">'
            f'<div style="font-size:13px;color:#111827">{html.escape(str(c["name"]))}</div>'
            f'<div style="font-size:10px;color:#9ca3af">{groups}</div></td>'
            f'<td style="padding:6px 10px;text-align:center"><span style="font-size:10px;font-weight:600;'
            f'padding:1px 7px;border-radius:10px;background:{bg};color:{fg};white-space:nowrap">{label}</span></td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:13px;color:#111827">{c["total"]}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:13px;color:#1d9e75">{c["working"]}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:13px">{fault_cell}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:13px">{notlive}</td>'
            f'</tr>'
        )
    table = (
        f'<table style="border-collapse:collapse;width:100%">'
        f'<thead><tr style="border-bottom:1px solid #e5e7eb">'
        f'<th style="{th};text-align:left">Contract</th>'
        f'<th style="{th};text-align:center">Status</th>'
        f'<th style="{th};text-align:right">Total</th>'
        f'<th style="{th};text-align:right">Working</th>'
        f'<th style="{th};text-align:right">Faults</th>'
        f'<th style="{th};text-align:right">Not live</th>'
        f'</tr></thead><tbody>{summary_rows}</tbody></table>'
    )

    # Per-contract failing-sensor detail, longest outage first.
    details = ""
    for c in census:
        if not c["faults"]:
            continue
        label, fg, bg = _CONTRACT_STATUS_CHIP.get(c["acct"], ("—", "#6b7280", "#f3f4f6"))
        rows = ""
        for f in sorted(c["faults"], key=lambda x: -x["down_days"]):
            name = html.escape(str(sensor_names.get((f["group"], f["sensor_id"]), f["sensor_id"])))
            failed = f["window_total"] - f["window_good"]
            rows += (
                f'<tr style="border-top:1px solid #f3f4f6">'
                f'<td style="padding:4px 10px;font-size:11px;color:#6b7280;white-space:nowrap">{_group_disp(f["group"])}</td>'
                f'<td style="padding:4px 10px;font-size:12px;color:#111827">{name}</td>'
                f'<td style="padding:4px 10px;text-align:right;font-size:11px;color:#e24b4a;white-space:nowrap">failed {failed}/{f["window_total"]}</td>'
                f'<td style="padding:4px 10px;text-align:right;font-size:11px;color:#6b7280;white-space:nowrap">{f["state"]}</td>'
                f'</tr>'
            )
        details += (
            f'<details style="margin-top:8px">'
            f'<summary style="cursor:pointer;list-style:none;padding:7px 10px;border-radius:6px;background:#f9fafb;'
            f'font-size:13px;font-weight:600;color:#111827">'
            f'{html.escape(str(c["name"]))} '
            f'<span style="font-weight:400;color:#9ca3af;font-size:11px">— {len(c["faults"])} failing</span></summary>'
            f'<table style="border-collapse:collapse;width:100%;margin:2px 0 6px"><tbody>{rows}</tbody></table>'
            f'</details>'
        )

    return table + (f'<div style="margin-top:12px"><div style="font-size:11px;font-weight:600;'
                    f'color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:2px">'
                    f'Which sensors are failing</div>{details}</div>' if details else "")


def _render_note_html(text):
    """Editor's note body: raw HTML passes through untouched — no escaping —
    so bold/italic/color spans/links typed directly as HTML tags just work.
    Not escaping is deliberate here: DIGEST_NOTE is operator-authored (a
    GitHub repo variable only repo admins can set), unlike sensor names or
    any other field in this file that originates from the API feed. The
    only added convenience is turning blank lines into paragraph breaks and
    grouping consecutive "- " lines into a bullet list, so the author isn't
    forced to hand-write <p>/<ul> for ordinary text.

    If the text already starts with a tag (e.g. pasted from a WYSIWYG editor
    that emitted its own <p>/<ul> structure), it's passed through completely
    unmodified instead — auto-wrapping it too would double-nest <p><p>...
    </p></p>, and re-splitting already-tagged paragraphs on blank lines would
    just as easily mangle a <ul> whose <li> lines aren't blank-line separated."""
    text = text.strip()
    if text.startswith('<'):
        return text

    parts = []
    for block in re.split(r'\n\s*\n', text):
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if lines and all(l.startswith('- ') for l in lines):
            items = "".join(f'<li style="margin-bottom:4px">{l[2:]}</li>' for l in lines)
            parts.append(f'<ul style="margin:0 0 10px;padding-left:20px">{items}</ul>')
        else:
            parts.append(f'<p style="margin:0 0 10px">{"<br>".join(lines)}</p>')
    return "".join(parts)


def _note_section(note):
    if not note or not note.strip():
        return ""
    return f"""
    <div class="dg-note" style="background:#fff8e6;border:1px solid #fac775;border-radius:8px;padding:14px 18px;margin-bottom:24px">
      <div class="dg-note-title" style="font-size:14px;font-weight:700;color:#633806;margin-bottom:2px">Editor's Note</div>
      <div class="dg-note-title" style="font-size:12px;font-weight:600;color:#633806;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">📌 This week</div>
      <div style="font-size:13px;color:#374151;line-height:1.5">{_render_note_html(note)}</div>
    </div>"""


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
        f'<tr><td style="padding:5px 12px;font-size:12px;color:#6b7280;white-space:nowrap">{r["group"]}</td>'
        f'<td style="padding:5px 12px;font-size:13px">{_retired_label(r)}</td>'
        f'<td style="padding:5px 12px;font-size:12px;color:#6b7280;white-space:nowrap">{r["last_seen"]}</td></tr>'
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

    badge_legend = f"""
    <div style="background:#f9fafb;border-radius:8px;padding:12px 16px;margin-bottom:28px;font-size:12px">
      <div style="font-weight:600;color:#374151;margin-bottom:8px">How to read the health badges</div>
      <p style="color:#6b7280;margin:0 0 8px">
        The <strong>health %</strong> is the share of automated checks (run {d['check_freq']}) that returned
        a successful response during the week. A sensor at 100% responded correctly to every check;
        one at 0% failed every check.
      </p>
      <div>
        <span style="display:inline-block;background:#e1f5ee;color:#085041;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Always on — {_TIER_RANGE['always_on']}</span>
        <span style="display:inline-block;background:#c0dd97;color:#27500a;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Healthy — 90–99%</span>
        <span style="display:inline-block;background:#faeeda;color:#633806;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Intermittent — 70–89%</span>
        <span style="display:inline-block;background:#fac775;color:#412402;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Unstable — 40–69%</span>
        <span style="display:inline-block;background:#f09595;color:#501313;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">Critical — 1–39%</span>
        <span style="display:inline-block;background:#e24b4a;color:#ffffff;padding:2px 8px;border-radius:10px;white-space:nowrap;margin:2px 4px 2px 0">No good runs — 0%</span>
      </div>
    </div>"""

    return f"""
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="color-scheme" content="light dark">
    <meta name="supported-color-schemes" content="light dark">
    <style>
      /* Dark-mode support for mail clients that honour prefers-color-scheme
         in an embedded <style> block (Apple Mail, new Outlook/Outlook.com,
         Gmail webmail/app, Yahoo). Clients that support neither (old
         Outlook desktop) just render the light version below untouched —
         there's no way around that short of the recipient updating Outlook.

         The rest of this file is plain inline-styled HTML (email-safe, no
         classes needed for the light theme itself). Rather than converting
         every element to a class, dark over rides target the literal inline
         color values directly via attribute substring selectors — e.g.
         [style*="color:#111827"] matches any element whose style attribute
         contains that exact substring. This only works because those hex
         values are used consistently for one semantic role each; the three
         tier colors that are ALSO used as text-on-their-own-matching-pastel-
         background (badge legend swatches, big stat tiles) are deliberately
         left out of these rules — overriding them here would repaint text
         that's sitting on an unchanged light chip, breaking contrast the
         other way. Those three (Always on / Intermittent / Critical) get
         explicit dg-c-good/warn/bad classes instead, only on the two
         neutral-background spots (small per-group table) where a literal
         override would otherwise go dark-on-dark. */
      @media (prefers-color-scheme: dark) {{
        body {{ background:#111318 !important; color:#f0f2f5 !important; }}
        [style*="color:#111827"], [style*="color:#374151"] {{ color:#f0f2f5 !important; }}
        [style*="color:#6b7280"] {{ color:#9ca3af !important; }}
        [style*="solid #e5e7eb"] {{ border-color:rgba(255,255,255,0.14) !important; }}
        [style*="solid #f3f4f6"] {{ border-color:rgba(255,255,255,0.08) !important; }}
        [style*="background:#f9fafb"] {{ background:#1c1f26 !important; }}
        [style*="background:#e5e7eb"] {{ background:#30363d !important; }}
        .dg-note {{ background:#241e0d !important; border-color:#7c5a15 !important; }}
        .dg-note-title {{ color:#fbbf24 !important; }}
        .dg-c-good {{ color:#34d399 !important; }}
        .dg-c-warn {{ color:#fbbf24 !important; }}
        .dg-c-bad  {{ color:#f87171 !important; }}
      }}
    </style>
    </head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#111;background:#ffffff;max-width:680px;margin:0 auto;padding:24px">

    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:4px">
      <tr>
        <td><h2 style="margin:0;font-size:20px">Cyprus ITS — Weekly Network Health Digest</h2></td>
        <td align="right" style="white-space:nowrap"><a href="{DASHBOARD_URL_TAGGED}" style="display:inline-block;background:#1d9e75;color:#fff;text-decoration:none;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:500;white-space:nowrap">See dashboard →</a></td>
      </tr>
    </table>
    <p style="color:#6b7280;font-size:13px;margin:0 0 12px">{d['week_start']} — {d['week_end']} &nbsp;·&nbsp; Generated {generated_at} &nbsp;·&nbsp; {d['run_count']} runs</p>
    <p style="font-size:13px;color:#374151;margin:0 0 16px">
      This report summarises the health of Cyprus' ITS infrastructure sensors for the past week.
      It highlights sensors that went offline, are performing below expectations, or have changed
      significantly since last week.
    </p>

    {_note_section(os.environ.get("DIGEST_NOTE", ""))}

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
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Always on ({_TIER_RANGE['always_on']})</div>
        </td>
        <td width="14%" style="background:#ecfdf5;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#1d9e75">{d['healthy']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Healthy ({_TIER_RANGE['healthy']})</div>
        </td>
        <td width="14%" style="background:#faeeda;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#633806">{d['intermittent']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Intermittent ({_TIER_RANGE['intermittent']})</div>
        </td>
        <td width="14%" style="background:#fffbeb;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e58e0a">{d['unstable']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Unstable ({_TIER_RANGE['unstable']})</div>
        </td>
        <td width="14%" style="background:#fde8e8;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#9b1c1c">{d['critical']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Critical ({_TIER_RANGE['critical']})</div>
        </td>
        <td width="14%" style="background:#fef2f2;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e24b4a">{d['offline']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">No good runs ({_TIER_RANGE['offline']})</div>
        </td>
      </tr>
    </table>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:16px">
      <thead>
        <tr style="border-bottom:1px solid #e5e7eb">
          <th style="padding:5px 8px;text-align:left;font-weight:500;color:#9ca3af">Group</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#9ca3af">Total</th>
          <th class="dg-c-good" style="padding:5px 8px;text-align:center;font-weight:500;color:#085041">Always on</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#1d9e75">Healthy</th>
          <th class="dg-c-warn" style="padding:5px 8px;text-align:center;font-weight:500;color:#633806">Intermittent</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#e58e0a">Unstable</th>
          <th class="dg-c-bad" style="padding:5px 8px;text-align:center;font-weight:500;color:#9b1c1c">Critical</th>
          <th style="padding:5px 8px;text-align:center;font-weight:500;color:#e24b4a">No good runs</th>
        </tr>
      </thead>
      <tbody>
        {"".join(f'''<tr style="border-bottom:1px solid #f3f4f6">
          <td style="padding:5px 8px;color:#374151">{g}</td>
          <td style="padding:5px 8px;text-align:center;color:#374151">{c['total']}</td>
          <td class="dg-c-good" style="padding:5px 8px;text-align:center;color:#085041">{c['always_on']}</td>
          <td style="padding:5px 8px;text-align:center;color:#1d9e75">{c['healthy']}</td>
          <td class="dg-c-warn" style="padding:5px 8px;text-align:center;color:#633806">{c['intermittent']}</td>
          <td style="padding:5px 8px;text-align:center;color:#e58e0a">{c['unstable']}</td>
          <td class="dg-c-bad" style="padding:5px 8px;text-align:center;color:#9b1c1c">{c['critical']}</td>
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
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Always on ({_TIER_RANGE['always_on']})</div>
        </td>
        <td width="14%" style="background:#ecfdf5;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#1d9e75">{d['bt']['healthy']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Healthy ({_TIER_RANGE['healthy']})</div>
        </td>
        <td width="14%" style="background:#faeeda;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#633806">{d['bt']['intermittent']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Intermittent ({_TIER_RANGE['intermittent']})</div>
        </td>
        <td width="14%" style="background:#fffbeb;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e58e0a">{d['bt']['unstable']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Unstable ({_TIER_RANGE['unstable']})</div>
        </td>
        <td width="14%" style="background:#fde8e8;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#9b1c1c">{d['bt']['critical']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">Critical ({_TIER_RANGE['critical']})</div>
        </td>
        <td width="14%" style="background:#fef2f2;border-radius:10px;padding:12px 8px;text-align:center">
          <div style="font-size:20px;font-weight:700;color:#e24b4a">{d['bt']['offline']}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:2px">No good runs ({_TIER_RANGE['offline']})</div>
        </td>
      </tr>
    </table>

    {section("📋 Sensors by contract",
             "#111827",
             "Every maintenance contract and the sensors it covers. A <strong>fault</strong> is a persistent problem &mdash; a sensor that failed <strong>at least 80% of its last 20 runs</strong>, not a one-off blip &mdash. Expand a contract to see which sensors are failing. <strong>No maintenance plan</strong> means the sensor is matched to no contract yet.",
             _contract_summary_html(d["census"], d["sensor_names"]))}
    {section("🔴 No good runs this week",
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
      <a href="{DASHBOARD_URL_TAGGED}" style="display:inline-block;background:#1d9e75;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:13px;font-weight:500">See dashboard →</a>
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
        # Preview-only convenience: paste the note into this gitignored file
        # instead of wrestling with multi-line env vars in cmd.exe (which has
        # no here-string and executes each pasted line as its own command).
        # Only read here, never in send_digest() — the real send must always
        # come from the DIGEST_NOTE repo variable, not a stray local file.
        note_file = Path(__file__).parent.parent / "digest_note.local.html"
        if not os.environ.get("DIGEST_NOTE") and note_file.exists():
            os.environ["DIGEST_NOTE"] = note_file.read_text(encoding="utf-8")
        d    = build_digest()
        html = build_html(d)
        out  = Path(__file__).parent.parent / "reports" / "digest_preview.html"
        out.write_text(html, encoding="utf-8")
        print(f"Preview saved: {out}")
        webbrowser.open(str(out))
    else:
        send_digest()
