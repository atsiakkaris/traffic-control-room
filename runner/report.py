"""
report.py - Generate a static HTML report from the SQLite history DB.
"""

import os
import re
import json
import html as _html
import yaml
from pathlib import Path
from datetime import datetime, timezone, timedelta


def _json_safe(obj):
    """json.dumps for embedding inside a <script> block. Escapes '<' so a
    string like '</script>' in external data (sensor names, VMS messages)
    can't break out of the script element. Produces valid JSON/JS either way."""
    return json.dumps(obj).replace("<", "\\u003c")

from db import get_connection, fetch_recent_runs, fetch_results_for_run, fetch_sensor_stability, fetch_sensor_statuses_for_run, fetch_sensor_coords, fetch_bt_path_coords, fetch_sensor_live_data_for_run, fetch_sensor_health_history, fetch_sensor_status_counts, fetch_sensor_projects
from stability import CYPRUS_TZ, GOOD_STATUSES, tier_for_counts, health_color, health_pct, HEALTH_WARNING_PCT, TIER_MIN_RUNS

_PROJECTS_CSV = Path(__file__).parent.parent / "config" / "projects.csv"


def _load_project_accountability():
    """Return {project_name: accountability} from config/projects.csv.

    Sensor-to-project assignment is computed elsewhere (qa.py, matched
    against the reference spreadsheet) and persisted to the sensor_projects
    DB table — this report only needs the project's accountability, which is
    a small tracked file, not the (gitignored) reference spreadsheet itself.
    """
    import csv as _csv
    status = {}
    if _PROJECTS_CSV.exists():
        with open(_PROJECTS_CSV, newline='', encoding='utf-8-sig') as f:
            for r in _csv.DictReader(f):
                proj = (r.get('project') or '').strip()
                acct = (r.get('accountability') or '').strip().lower()
                if proj:
                    status[proj] = acct or 'supported'
    return status

# Load UI labels from config — falls back to defaults if file is missing
_LABELS_PATH    = Path(__file__).parent.parent / "config" / "ui_labels.yaml"
_ENDPOINTS_PATH = Path(__file__).parent.parent / "config" / "endpoints.yaml"
try:
    _UI = yaml.safe_load(_LABELS_PATH.read_text(encoding="utf-8"))
except Exception:
    _UI = {}
try:
    _ENDPOINTS_CONFIG = yaml.safe_load(_ENDPOINTS_PATH.read_text(encoding="utf-8"))
except Exception:
    _ENDPOINTS_CONFIG = {"groups": []}

def _lbl(section, key, default=""):
    return (_UI.get(section) or {}).get(key, default)

# Per-group UI metadata keyed by DB group name
GROUP_META: dict = _UI.get("groups", {})

# Mapping: test_name → {group, check} for endpoints that drive group health %
HEALTH_ENDPOINTS: dict = {}
for _g in _ENDPOINTS_CONFIG.get("groups", []):
    for _ep in _g.get("endpoints", []):
        if "health_check" in _ep:
            HEALTH_ENDPOINTS[_ep["name"]] = {
                "group": _g["name"],
                # DB group_name the per-sensor rows are stored under (may differ
                # from the dashboard group, e.g. Bluetooth Paths within Bluetooth).
                "sensor_group": _ep.get("sensor_group", _g["name"]),
                "check": _ep["health_check"],
            }


def _to_cyprus(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp string to Cyprus time, formatted for display."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(CYPRUS_TZ)
    return local.strftime("%d/%m/%y %H:%M")

REPORT_PATH = Path("reports/latest.html")


def parse_vms_detail(text):
    if not text or "vms_controller_status" not in text:
        return None
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
    "no_measurement": "No measurement",
    "no_status": "No status reported",
    "not_working": "VMS not working",
    "malfunctioning": "Speed = -1 (sensor fault)",
    "failing": "No speed or travel time",
    "missing": "Not present in feed",
    "stale": "Feed data is stale",
}

STATUS_TOOLTIP = {
    "working":        "Sensor responded with a valid positive speed — vehicles detected and hardware functioning normally.",
    "ok":             "Sensor responded correctly in this check.",
    "no_traffic":     "Sensor is communicating but reported speed = 0. No vehicles were detected at this location during the check.",
    "no_measurement": "Sensor appears in the inventory feed but sent no data in this check. It may be offline or temporarily excluded from the live feed.",
    "no_status":      "VMS controller exists in the system but did not send any status in this run. It may be unreachable or misconfigured.",
    "not_working":    "VMS controller explicitly reported a fault. The sign may be physically damaged or its communication link is down.",
    "malfunctioning": "Sensor reported speed = -1, which is the hardware fault code. The loop detector is likely damaged or requires maintenance.",
    "failing":        "Bluetooth path reported no speed or travel time data. This usually means no vehicles were detected on the route, or the Bluetooth readers at each end lost connectivity.",
    "missing":        "Sensor was expected in the live feed based on the inventory, but was not present in this run.",
    "stale":          "The feed's publication timestamp is older than expected. Data may not reflect current conditions.",
}

CHECK_DESCRIPTION = {
    "Bluetooth Inventory":        "Checks for a valid response and reports the total device count.",
    "Bluetooth Paths Inventory":  "Checks for a valid response and counts the paths.",
    "Bluetooth Paths Live (FCD)": "Checks feed freshness and whether each path is reporting speed and travel time.",
    "Traffic Detection Inventory":"Checks for a valid response.",
    "Traffic Detection Live":     "Checks feed freshness and reports the status of each sensor (working, no traffic, malfunctioning, or no data).",
    "VMS Inventory":              "Checks for a valid response.",
    "VMS Live Data":              "Checks feed freshness and reports how many controllers are working, not working, or not sending any status.",
}

GROUP_DISPLAY = {k: v.get("display", k) for k, v in GROUP_META.items()}

SENSOR_CHECKS = {ep["check"] for ep in HEALTH_ENDPOINTS.values()}
# HEALTH_WARNING_PCT is imported from stability.py (single source of truth,
# shared with qa.py) — re-exported here for the many local references below.


# ── HTML/JS generation helpers (group-meta driven) ────────────────────────────

def _chart_legend_html(group_meta):
    """Coloured line + label for each group — shown below the trend chart."""
    parts = []
    for gname, meta in group_meta.items():
        color = meta.get("color", "#6b7280")
        label = meta.get("history_label", gname)
        parts.append(
            f'<span style="display:flex;align-items:center;gap:5px">'
            f'<span style="width:22px;height:3px;border-radius:2px;'
            f'background:{color};display:inline-block"></span>{label}</span>'
        )
    return "\n        ".join(parts)


def _history_header_cells(group_meta):
    """One <th> per group for the run-history table."""
    return "".join(
        f'<th>{meta.get("history_label", gname)}</th>'
        for gname, meta in group_meta.items()
    )


def _chart_datasets_js(group_meta, chart_series):
    """JS array contents for the Chart.js trend chart — one dataset per group."""
    datasets = []
    for gname, meta in group_meta.items():
        datasets.append(
            "{ "
            f'label: {json.dumps(meta.get("history_label", gname))}, '
            f'data: {chart_series[gname]}, '
            f'borderColor: {json.dumps(meta.get("color", "#6b7280"))}, '
            "backgroundColor: 'transparent', tension: 0.3, pointRadius: 3, spanGaps: true"
            " }"
        )
    return ",\n      ".join(datasets)


def _map_layer_buttons(group_meta, bt_paths_label):
    """Toggle buttons for the map — one per group layer, plus BT paths polyline."""
    buttons = []
    for gname, meta in group_meta.items():
        key = meta.get("layer_key", "")
        if not key:
            continue
        label = meta.get("map_label", gname)
        buttons.append(
            f'<button class="map-toggle active" data-layer="{key}" '
            f'onclick="toggleLayer(this,\'{key}\')">{label}</button>'
        )
        if gname == "Bluetooth":
            buttons.append(
                f'<button class="map-toggle active" data-layer="paths" '
                f'onclick="toggleLayer(this,\'paths\')">{bt_paths_label}</button>'
            )
    return "\n".join(buttons)


def _health_color(pct):
    # Thin wrapper kept for the many call sites below; logic lives in stability.py.
    return health_color(pct)


def _fault_streak_start(history):
    """The earliest run in the sensor's *current* unbroken run of bad statuses,
    or None if its latest run was good. That run is when the outage was first
    detected — the number a contractor can be held to."""
    if not history or history[-1]["status"] in GOOD_STATUSES:
        return None
    start = history[-1]
    for h in reversed(history):
        if h["status"] in GOOD_STATUSES:
            break
        start = h
    return start


def _current_state(history):
    """(label, colour, tooltip, down_days) describing what the sensor is doing NOW.

    Deliberately separate from the lifetime stability tier: a sensor can be
    'Critical' on its record yet 'Working' today, and vice versa. The control
    room acts on this column; the tier says whether the sensor can be trusted.
    """
    if not history:
        return ("No data", "#9ca3af", "No runs recorded", None)

    last = history[-1]
    if last["status"] in GOOD_STATUSES:
        return ("Working", "#1d9e75",
                f"Reporting normally as of {_to_cyprus(last['run_at'])}", 0)

    start = _fault_streak_start(history)
    started_at = datetime.fromisoformat(start["run_at"].replace("Z", "+00:00"))
    down_days = (datetime.now(timezone.utc) - started_at).days
    reason = STATUS_LABEL.get(last["status"], last["status"])

    if not any(h["status"] in GOOD_STATUSES for h in history):
        # Never once produced a good reading since we started watching it.
        return (f"Never worked ({down_days}d)" if down_days else "Never worked",
                "#a32d2d",
                f"No good reading since first seen {_to_cyprus(history[0]['run_at'])} — currently {reason}",
                down_days)

    label = "Down <1d" if down_days < 1 else f"Down {down_days}d"
    return (label, "#e24b4a",
            f"Failing since {_to_cyprus(start['run_at'])} — currently {reason}",
            down_days)


def _humanize_failure(check_name, full_failure_reason):
    """Return a short, plain-English explanation of a check failure.
    Receives the full failure_reason string so regexes can find sub-parts
    even when the detail itself contains ' | ' delimiters.
    """
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


def _unassigned_tooltip(source):
    """Plain-language reason a sensor has no project, from sensor_projects.source.

    The two cases need different responses: a missing reference row is a
    data-entry task, while a row claimed by a nearer sensor may be a mis-mapping.
    Written for readers who have never seen the spreadsheet.
    """
    src = source or ""
    dist = src.partition(":")[2]
    metres = f"{int(dist):,}".replace(",", " ") if dist.isdigit() else None

    if src.startswith("unmatched_no_ref"):
        near = f" The nearest one is {metres} m away." if metres else ""
        return ("No row for this sensor in the reference spreadsheet, so nobody is "
                f"recorded as owning it.{near} Add it to QA Locations.xlsx.")
    if src.startswith("unmatched_ref_taken"):
        near = f" {metres} m away" if metres else " nearby"
        return (f"A reference row sits{near}, but a closer sensor already claimed it — "
                "each row can own only one sensor. This site probably needs its own "
                "row in QA Locations.xlsx.")
    if src == "unmatched_no_coords":
        return "The API reports no coordinates for this sensor, so it cannot be matched to a reference row."
    return "Not matched to any reference spreadsheet row."


def _search_tokens(display_sensor_id):
    """Identifiers a row can be reached by in an exact (quoted) search, sorted.

    Tokens come from the rendered label only — the site code, the name words, and
    for Bluetooth paths either endpoint. The raw sensor_id is deliberately excluded:
    it is often not on screen (BT path 100 renders as "Strovolou-30881"), so matching
    it would return rows with no visible connection to the query.
    """
    return sorted({t for t in re.split(r"[^0-9a-z]+", str(display_sensor_id).lower()) if t})


def build_sensor_stability_html(sensors, bt_path_names=None, all_sensor_coords=None, trend_data_json="null", day_labels_json="null", bt_path_coords=None, sensor_projects=None, project_acct=None):
    """Build the sensor stability panel HTML with a group dropdown."""
    if not sensors:
        return "<p style='color:var(--color-text-secondary);font-size:13px'>No sensor data recorded yet.</p>"

    bt_path_names = bt_path_names or {}
    all_sensor_coords = all_sensor_coords or {}
    bt_path_coords = bt_path_coords or {}
    sensor_projects = sensor_projects or {}
    project_acct = project_acct or {}

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

        tokens_attr = _html.escape(" ".join(_search_tokens(display_sensor_id)))

        # Names come from the external API inventory / reference sheet — escape
        # before they land in any HTML text or attribute context below.
        display_sensor_id = _html.escape(str(display_sensor_id))

        display_group = GROUP_DISPLAY.get(s["group_name"], s["group_name"])

        # Resolve coords for map-link
        coord_info = {}
        bt_path_line = None
        if s["group_name"] in ("Traffic Detection", "Bluetooth", "VMS"):
            coord_info = (all_sensor_coords or {}).get(s["group_name"], {}).get(s["sensor_id"], {})
        elif s["group_name"] == "Bluetooth Paths":
            bt_path_line = bt_path_coords.get(s["sensor_id"])
        has_coords = bool(coord_info.get("lat") and coord_info.get("lon"))
        has_bt_path = bool(bt_path_line and bt_path_line.get("coords"))

        history = s["history"]
        statuses = [h["status"] for h in history]
        # The tier is a LIFETIME rating: "can I trust this sensor?" It is
        # deliberately insensitive to recent noise. Whether the sensor is down
        # *right now* is a different question, answered by the Current state
        # column below — never by this badge.
        total_runs = len(statuses)
        good_runs = sum(1 for st in statuses if st in GOOD_STATUSES)
        pct = health_pct(good_runs, total_runs) or 0
        tier = tier_for_counts(good_runs, total_runs)

        badge_bg, badge_color, badge_label = tier.bg, tier.fg, tier.label
        badge_tip = f"{tier.tooltip} · {good_runs} of {total_runs} runs good (lifetime)"

        # Too few runs to rate confidently — show a neutral "collecting data"
        # badge instead of a falsely precise tier.
        if total_runs < TIER_MIN_RUNS:
            badge_bg, badge_color = "#e5e7eb", "#6b7280"
            badge_label = "Collecting data"
            badge_tip = f"Only {total_runs} run(s) recorded — need {TIER_MIN_RUNS} for a reliable lifetime rating."

        # Current operational state: is it working right now, and if not, for
        # how long has it been down? This is what the control room acts on.
        state_label, state_color, state_tip, _down_days = _current_state(history)
        state_cell = f'<span title="{_html.escape(state_tip)}" style="font-size:11px;color:{state_color};cursor:help;white-space:nowrap">{state_label}</span>'

        # Awaiting power / decommissioned: not expected to work. Override the
        # health badge with a neutral one and mark the row so it can be excluded
        # from the panel's health bar and sorting. Wins over "collecting data".
        commissioning = _commissioning(sensor_projects, s["group_name"], s["sensor_id"])
        excluded = commissioning in _EXCLUDED_COMMISSIONING
        if excluded:
            badge_bg, badge_color = "#e5e7eb", "#6b7280"
            badge_label = _COMMISSIONING_LABEL[commissioning]
            badge_tip = _COMMISSIONING_TIP[commissioning]

        # Sparkline: last 20 runs as tiny squares with rich tooltips
        sparks = ""
        for h in history[-20:]:
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
            issue_label   = STATUS_LABEL.get(last_bad["status"], last_bad["status"])
            issue_color   = STATUS_COLOR.get(last_bad["status"], "#9ca3af")
            issue_tooltip = STATUS_TOOLTIP.get(last_bad["status"], "")
            last_issue_html = f'<span title="{issue_tooltip}" style="font-size:11px;color:{issue_color};cursor:help">{issue_label}</span>'
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

        if has_coords:
            lat_v = coord_info["lat"]
            lon_v = coord_info["lon"]
            map_icon = (
                f'<span onclick="event.stopPropagation();flyToSensor(this)" '
                f'data-lat="{lat_v}" data-lon="{lon_v}" '
                f'data-sid="{s["sensor_id"]}" data-mapgroup="{s["group_name"]}" '
                f'title="Show on map" style="cursor:pointer;margin-left:5px;opacity:0.5;font-size:10px">&#x1F4CD;</span>'
            )
            sid_cell = f'{display_sensor_id}{map_icon}'
        elif has_bt_path:
            map_icon = (
                f'<span onclick="event.stopPropagation();flyToBtPath(this)" '
                f'data-pathid="{s["sensor_id"]}" '
                f'title="Show on map" style="cursor:pointer;margin-left:5px;opacity:0.5;font-size:10px">&#x1F4CD;</span>'
            )
            sid_cell = f'{display_sensor_id}{map_icon}'
        else:
            sid_cell = display_sensor_id

        # Project + accountability — who owns this sensor, so a failure has an owner to contact.
        # Bluetooth paths are combinations of sensors, not owned equipment, so ownership
        # doesn't apply to them at all.
        proj_info = sensor_projects.get(s["group_name"], {}).get(s["sensor_id"])
        proj_name = proj_info["project"] if proj_info else None
        proj_acct = project_acct.get(proj_name, "supported") if proj_name else None
        proj_name_esc = _html.escape(str(proj_name)) if proj_name else ""  # external data
        if s["group_name"] in _NON_OWNED_GROUPS:
            project_cell = '<span title="Bluetooth paths are sensor combinations, not owned equipment" style="font-size:11px;color:#6b7280;cursor:help">n/a</span>'
        elif proj_name and proj_acct == "out_of_support":
            project_cell = f'<span title="Out of support — failure expected, not actionable" style="font-size:11px;color:#7f8c8d;cursor:help">{proj_name_esc}</span>'
        elif proj_name:
            project_cell = f'<span style="font-size:11px;color:var(--color-text-secondary)">{proj_name_esc}</span>'
        else:
            reason = _html.escape(_unassigned_tooltip(proj_info["source"] if proj_info else None))
            project_cell = f'<span title="{reason}" style="font-size:11px;color:#9ca3af;cursor:help">—</span>'

        # Composite key avoids ID collisions when multiple groups share a sensor_id (e.g. TD and BT both have id "1")
        composite_id = f"{s['group_name']}|{s['sensor_id']}"
        safe_sid = composite_id.replace("'", "\\'")
        rows += f"""
        <tr data-group="{s['group_name']}" data-display="{(display_sensor_id or s['sensor_id']).lower()}" data-tokens="{tokens_attr}" data-pct="{'' if excluded else pct}" data-awaiting="{'1' if excluded else '0'}" onclick="_toggleTrend('{safe_sid}',this)" style="cursor:pointer">
          <td style="width:18px;padding-right:4px"><span id="chev-{composite_id}" style="font-size:9px;color:var(--color-text-secondary);display:inline-block;transition:transform .2s">&#9654;</span></td>
          <td style="font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{display_group}</td>
          <td style="font-size:12px;font-family:monospace;max-width:260px;word-break:break-word;white-space:normal">{sid_cell}</td>
          <td style="white-space:nowrap">{project_cell}</td>
          <td style="white-space:nowrap">{state_cell}</td>
          <td style="white-space:nowrap">{sparks}</td>
          <td style="white-space:nowrap"><span title="{badge_tip}" style="font-size:11px;font-weight:500;padding:2px 8px;border-radius:10px;background:{badge_bg};color:{badge_color};cursor:help">{badge_label}</span></td>
          <td>{last_issue_html}</td>
          <td>{last_good_html}</td>
        </tr>
        <tr id="trend-{composite_id}" style="display:none">
          <td colspan="9" style="padding:0">
            <div style="padding:14px 16px;background:var(--color-background-secondary);border-bottom:0.5px solid var(--color-border-tertiary)">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
                <span style="font-size:12px;font-weight:500">Daily health % — {display_sensor_id or s['sensor_id']}</span>
                <div style="display:flex;gap:6px">
                  <button id="btn7-{composite_id}" onclick="event.stopPropagation();_setTrendWindow('{safe_sid}',7)"
                    style="font-size:11px;padding:3px 10px;border-radius:6px;border:0.5px solid var(--color-border-tertiary);background:var(--header-bg);color:#fff;cursor:pointer">7 days</button>
                  <button id="btn30-{composite_id}" onclick="event.stopPropagation();_setTrendWindow('{safe_sid}',30)"
                    style="font-size:11px;padding:3px 10px;border-radius:6px;border:0.5px solid var(--color-border-tertiary);background:var(--color-background-primary);color:var(--color-text-secondary);cursor:pointer">30 days</button>
                </div>
              </div>
              <div style="position:relative;height:110px"><canvas id="chart-{composite_id}" role="img" aria-label="Daily health percentage for {display_sensor_id or s['sensor_id']}"></canvas></div>
            </div>
          </td>
        </tr>"""

    # Per-group good/total counts for dynamic bar. Awaiting-power sensors are
    # excluded — they aren't expected to work, so they shouldn't drag the bar down.
    group_stats = {"all": {"good": 0, "total": 0}}
    for s in sensors:
        if _is_excluded_commissioning(sensor_projects, s["group_name"], s["sensor_id"]):
            continue
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
    group_stats_json = json.dumps(group_stats)

    return f"""
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px">
      <input id="stabilitySearch" type="text" placeholder="Search sensors… (use &quot;10&quot; for an exact match)"
             title="Type any part of a sensor ID to filter. Wrap it in double quotes for an exact match &mdash; &quot;10&quot; finds sensor 10 only, not 1001 or 1040."
             oninput="_applyStabilityFilters()"
             style="font-size:13px;padding:5px 10px;border-radius:8px;border:0.5px solid var(--color-border-tertiary);
                    background:var(--color-background-primary);color:var(--color-text-primary);min-width:180px;flex:1"/>
      <select id="groupFilter" onchange="_applyStabilityFilters()"
              style="font-size:13px;padding:5px 10px;border-radius:8px;border:0.5px solid var(--color-border-tertiary);
                     background:var(--color-background-primary);color:var(--color-text-primary);cursor:pointer">
        {options}
      </select>
      <select id="sortOrder" onchange="_applyStabilityFilters()"
              style="font-size:13px;padding:5px 10px;border-radius:8px;border:0.5px solid var(--color-border-tertiary);
                     background:var(--color-background-primary);color:var(--color-text-primary);cursor:pointer">
        <option value="default">Sort: Group / ID</option>
        <option value="worst">Worst first</option>
        <option value="best">Best first</option>
      </select>
    </div>
    <table id="sensorTable">
      <thead><tr><th style="width:18px"></th><th>Group</th><th>Sensor ID</th><th>Project</th>
        <th title="What the sensor is doing right now, and how long it has been failing">Current state</th>
        <th>History (last 20 runs)</th>
        <th title="Lifetime reliability across every run ever recorded — how much this sensor can be trusted">Stability (lifetime)</th>
        <th>Last issue</th><th>Last working</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <script>
    var _groupStats = {group_stats_json};
    function _applyStabilityFilters() {{
      var group  = document.getElementById('groupFilter').value;
      var search = (document.getElementById('stabilitySearch').value || '').toLowerCase().trim();
      var sort   = document.getElementById('sortOrder').value;
      var tbody  = document.querySelector('#sensorTable tbody');

      // "10" (quoted) means exact match, so searching for sensor 10 doesn't also return 1001, 1040...
      var exact = search.length > 1 && search.charAt(0) === '"' && search.charAt(search.length - 1) === '"';
      if (exact) search = search.slice(1, -1).trim();

      // sort sensor rows if needed
      if (sort !== 'default') {{
        var sensorRows = Array.from(tbody.querySelectorAll('tr[data-group]'));
        sensorRows.sort(function(a, b) {{
          var pa = parseInt(a.dataset.pct, 10);
          var pb = parseInt(b.dataset.pct, 10);
          return sort === 'worst' ? pa - pb : pb - pa;
        }});
        sensorRows.forEach(function(tr) {{
          var sid = tr.getAttribute('onclick') && tr.getAttribute('onclick').match(/'([^']+)'/);
          var trow = sid ? document.getElementById('trend-' + sid[1]) : null;
          tbody.appendChild(tr);
          if (trow) tbody.appendChild(trow);
        }});
      }}

      // apply visibility filters
      tbody.querySelectorAll('tr').forEach(function(tr) {{
        if (!tr.dataset.group) return;
        var groupMatch  = group === 'all' || tr.dataset.group === group;
        var display     = tr.dataset.display || '';
        var searchMatch = !search || (exact
          ? (' ' + (tr.dataset.tokens || '') + ' ').indexOf(' ' + search + ' ') !== -1
          : display.indexOf(search) !== -1);
        var visible = groupMatch && searchMatch;
        tr.style.display = visible ? '' : 'none';
        var sid = tr.getAttribute('onclick') && tr.getAttribute('onclick').match(/'([^']+)'/);
        if (sid) {{
          var trow = document.getElementById('trend-' + sid[1]);
          if (trow && !visible) trow.style.display = 'none';
        }}
      }});

      // update progress bar
      var stats = _groupStats[group] || _groupStats['all'];
      var pct = stats.total > 0 ? Math.round(stats.good / stats.total * 100) : 0;
      var color = pct >= 90 ? '#1d9e75' : (pct >= 55 ? '#e58e0a' : '#e24b4a');
      var fill = document.getElementById('sensorBarFill');
      if (fill) {{ fill.style.width = pct + '%'; fill.style.background = color; }}
      var wrap = document.getElementById('sensorBarWrap');
      if (wrap) {{ wrap.title = pct + '% of sensors had a good status in the last run'; }}
    }}
    // keep old name as alias for dynamic bar (called from group card dropdown)
    function filterGroup(val) {{
      document.getElementById('groupFilter').value = val;
      _applyStabilityFilters();
    }}
    var _trendData      = {trend_data_json};
    var _dayLabels30    = {day_labels_json};
    var _openTrend      = null;
    var _sensorBarChart = null;

    function _toggleTrend(sid, row) {{
      var trow = document.getElementById('trend-' + sid);
      var chev = document.getElementById('chev-' + sid);
      if (!trow) return;
      var opening = trow.style.display === 'none';
      if (_openTrend && _openTrend !== sid) {{
        var prev = document.getElementById('trend-' + _openTrend);
        var prevChev = document.getElementById('chev-' + _openTrend);
        if (prev) prev.style.display = 'none';
        if (prevChev) prevChev.style.transform = '';
        if (_sensorBarChart) {{ _sensorBarChart.destroy(); _sensorBarChart = null; }}
      }}
      if (opening) {{
        trow.style.display = '';
        if (chev) chev.style.transform = 'rotate(90deg)';
        _openTrend = sid;
        _buildTrendChart(sid, 7);
        setTimeout(function() {{ trow.scrollIntoView({{behavior:'smooth', block:'nearest'}}); }}, 30);
      }} else {{
        trow.style.display = 'none';
        if (chev) chev.style.transform = '';
        if (_sensorBarChart) {{ _sensorBarChart.destroy(); _sensorBarChart = null; }}
        _openTrend = null;
      }}
    }}

    function _setTrendWindow(sid, days) {{
      var btn7  = document.getElementById('btn7-'  + sid);
      var btn30 = document.getElementById('btn30-' + sid);
      var activeStyle  = 'font-size:11px;padding:3px 10px;border-radius:6px;border:0.5px solid var(--color-border-tertiary);background:var(--header-bg);color:#fff;cursor:pointer';
      var inactiveStyle = 'font-size:11px;padding:3px 10px;border-radius:6px;border:0.5px solid var(--color-border-tertiary);background:var(--color-background-primary);color:var(--color-text-secondary);cursor:pointer';
      if (btn7)  btn7.style.cssText  = days === 7  ? activeStyle : inactiveStyle;
      if (btn30) btn30.style.cssText = days === 30 ? activeStyle : inactiveStyle;
      _buildTrendChart(sid, days);
    }}

    function _buildTrendChart(sid, days) {{
      var allData = (_trendData && _trendData[sid]) || [];
      var data    = allData.slice(allData.length - days);
      var labels  = _dayLabels30.slice(_dayLabels30.length - days);
      var colors  = data.map(function(v) {{
        if (v === null || v === undefined) return '#d1d5db';
        return v >= 90 ? '#1d9e75' : (v >= 55 ? '#e58e0a' : '#e24b4a');
      }});
      var ctx = document.getElementById('chart-' + sid);
      if (!ctx) return;
      if (_sensorBarChart) {{ _sensorBarChart.destroy(); _sensorBarChart = null; }}
      _sensorBarChart = new Chart(ctx, {{
        type: 'bar',
        data: {{
          labels: labels,
          datasets: [{{
            data: data,
            backgroundColor: colors,
            borderRadius: 3,
            borderSkipped: false,
          }}]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{ callbacks: {{ label: function(c) {{ return c.parsed.y !== null ? c.parsed.y + '% healthy' : 'No data'; }} }} }}
          }},
          scales: {{
            y: {{ min: 0, max: 100, ticks: {{ callback: function(v) {{ return v + '%'; }}, font: {{ size: 10 }} }}, grid: {{ color: 'rgba(128,128,128,0.1)' }} }},
            x: {{ ticks: {{ font: {{ size: 10 }}, maxRotation: 45 }}, grid: {{ display: false }} }}
          }}
        }}
      }});
    }}
    </script>"""


def _sensor_display_name(group_name, sensor_id, bt_path_names, all_sensor_coords):
    """Human-readable sensor label, matching the stability panel's logic."""
    if group_name == "Bluetooth Paths":
        return bt_path_names.get(sensor_id, sensor_id)
    if group_name == "Traffic Detection":
        info = all_sensor_coords.get("Traffic Detection", {}).get(sensor_id, {})
        sc, nm = info.get("site_code"), info.get("name", sensor_id)
        return f"{sc} ({nm})" if sc else nm
    if group_name == "VMS":
        info = all_sensor_coords.get("VMS", {}).get(sensor_id, {})
        nm = info.get("name", "")
        return f"{nm} ({sensor_id})" if nm and nm != sensor_id else sensor_id
    return sensor_id


# Groups that are not individually-owned equipment and so have no project.
# Bluetooth "paths" are computed from pairs of BT sensors, not physical devices.
_NON_OWNED_GROUPS = {"Bluetooth Paths"}

# Commissioning states that mean a sensor isn't expected to be working, so it's
# excluded from health statistics and shown with a distinct neutral badge.
_COMMISSIONING_LABEL = {
    "not_electrified": "Awaiting power",
    "decommissioned":  "Decommissioned",
}
_COMMISSIONING_TIP = {
    "not_electrified": ("Marked pending power connection in the reference sheet — "
                        "not expected to work yet, excluded from health statistics."),
    "decommissioned":  ("Marked inactive / decommissioned in the reference sheet — "
                        "excluded from health statistics."),
}
_EXCLUDED_COMMISSIONING = set(_COMMISSIONING_LABEL)


def _commissioning(sensor_projects, group_name, sensor_id):
    """Commissioning state for a sensor: 'active' (default), 'not_electrified',
    or 'decommissioned'. The latter two are excluded from health statistics."""
    info = (sensor_projects or {}).get(group_name, {}).get(sensor_id)
    return info.get("commissioning", "active") if info else "active"


def _is_excluded_commissioning(sensor_projects, group_name, sensor_id):
    """True if the sensor should be left out of health statistics because it is
    awaiting power or decommissioned (not expected to be working)."""
    return _commissioning(sensor_projects, group_name, sensor_id) in _EXCLUDED_COMMISSIONING


def _commissioning_note(group_name, awaiting_by_group, decommissioned_by_group):
    """Grey '· N awaiting power · M decommissioned' suffix for a group card,
    naming the sensors excluded from that group's working ratio."""
    parts = []
    aw = awaiting_by_group.get(group_name, 0)
    de = decommissioned_by_group.get(group_name, 0)
    if aw:
        parts.append(f"{aw} awaiting power")
    if de:
        parts.append(f"{de} decommissioned")
    if not parts:
        return ""
    return (f' <span title="Excluded from the working ratio" '
            f'style="font-size:11px;color:#6b7280">· {" · ".join(parts)}</span>')


def build_accountability_rollup_html(sensors, bt_path_names, all_sensor_coords, sensor_projects, project_acct):
    """Group sensors that are DOWN RIGHT NOW by the project responsible for them,
    longest outage first — the list you act on today.

    Deliberately keyed off current state, not the lifetime tier: a sensor that
    was repaired yesterday must drop off this list, and one that died this
    morning must appear on it however good its record used to be. The lifetime
    tier rides along only as context ("is this a repeat offender?").
    """
    bt_path_names = bt_path_names or {}
    sensor_projects = sensor_projects or {}
    project_acct = project_acct or {}

    # Collect currently-failing sensors, bucketed by project name (None -> "Unassigned")
    buckets = {}   # project_name -> {"acct": str, "sensors": [ ... ]}
    for s in sensors:
        if s["group_name"] in _NON_OWNED_GROUPS:
            continue  # BT paths are sensor combinations, not owned equipment
        if _is_excluded_commissioning(sensor_projects, s["group_name"], s["sensor_id"]):
            continue  # awaiting power or decommissioned — not a fault
        history = s["history"]
        if not history:
            continue
        if history[-1]["status"] in GOOD_STATUSES:
            continue  # working right now — nothing to chase

        state_label, _color, state_tip, down_days = _current_state(history)
        statuses = [h["status"] for h in history]
        good_runs = sum(1 for st in statuses if st in GOOD_STATUSES)
        tier = tier_for_counts(good_runs, len(statuses))

        proj_info = sensor_projects.get(s["group_name"], {}).get(s["sensor_id"])
        proj = proj_info["project"] if proj_info and proj_info["project"] else None
        acct = project_acct.get(proj, "supported") if proj else "unassigned"
        key = proj or "Unassigned"
        buckets.setdefault(key, {"acct": acct, "sensors": []})
        buckets[key]["sensors"].append({
            "display": _html.escape(str(_sensor_display_name(s["group_name"], s["sensor_id"], bt_path_names, all_sensor_coords))),
            "group": GROUP_DISPLAY.get(s["group_name"], s["group_name"]),
            "state": state_label, "state_tip": state_tip,
            "down_days": down_days or 0, "tier": tier,
        })

    if not buckets:
        return ('<p style="color:var(--color-text-secondary);font-size:13px;padding:4px 0">'
                'Every sensor is reporting right now — nothing needs attention. &#127881;</p>')

    actionable = {k: v for k, v in buckets.items() if v["acct"] != "out_of_support"}
    out_of_support = {k: v for k, v in buckets.items() if v["acct"] == "out_of_support"}

    def _project_block(name, bucket, dim=False):
        name = _html.escape(str(name))  # project name originates from external data
        # Longest outage first — the strongest case to put to a contractor.
        rows = sorted(bucket["sensors"], key=lambda x: -x["down_days"])
        n = len(rows)
        base_color = "var(--color-text-secondary)" if dim else "var(--color-text-primary)"
        if name == "Unassigned":
            tag = '<span style="font-size:10px;color:#c0392b;margin-left:8px">no owner known</span>'
        elif dim:
            tag = '<span style="font-size:10px;color:#7f8c8d;margin-left:8px">out of support — expected, not actionable</span>'
        else:
            tag = ''
        body = ""
        for r in rows:
            t = r["tier"]
            # Outage duration drives the case; the lifetime tier is context —
            # "has this one always been trouble, or is this a new fault?"
            down = (f'<span title="{_html.escape(r["state_tip"])}" style="font-size:11px;font-weight:600;'
                    f'color:{"#7f8c8d" if dim else "#e24b4a"};cursor:help;white-space:nowrap">{r["state"]}</span>')
            badge = (f'<span title="Lifetime record: {t.tooltip}" style="font-size:10px;font-weight:500;padding:1px 7px;'
                     f'border-radius:10px;background:{t.bg};color:{t.fg};cursor:help">{t.label}</span>')
            body += (f'<tr style="border-top:0.5px solid var(--color-border-tertiary)">'
                     f'<td style="padding:5px 8px;font-size:12px;color:var(--color-text-secondary);white-space:nowrap">{r["group"]}</td>'
                     f'<td style="padding:5px 8px;font-size:12px;font-family:monospace;color:{base_color}">{r["display"]}</td>'
                     f'<td style="padding:5px 8px;text-align:right;white-space:nowrap">{down}</td>'
                     f'<td style="padding:5px 8px;text-align:right;white-space:nowrap">{badge}</td>'
                     f'</tr>')
        th = ("padding:4px 8px;font-size:10px;font-weight:500;letter-spacing:0.04em;"
              "text-transform:uppercase;color:var(--color-text-secondary)")
        head = (f'<thead><tr>'
                f'<th style="{th};text-align:left" title="Which sensor system this device belongs to">Group</th>'
                f'<th style="{th};text-align:left" title="The sensor that is not reporting">Sensor</th>'
                f'<th style="{th};text-align:right" title="How long it has been failing continuously.">Down for</th>'
                f'<th style="{th};text-align:right" title="Its reliability across every run ever recorded — is this a new fault or a repeat offender?">Lifetime record</th>'
                f'</tr></thead>')
        return (f'<details style="margin-bottom:6px">'
                f'<summary style="cursor:pointer;padding:7px 10px;border-radius:6px;'
                f'background:var(--color-background-secondary);font-size:13px;font-weight:500;color:{base_color};'
                f'display:flex;align-items:center;justify-content:space-between;list-style:none">'
                f'<span>{name}{tag}</span>'
                f'<span style="font-size:12px;color:{"#7f8c8d" if dim else "#e24b4a"};font-weight:600;white-space:nowrap">{n} down</span>'
                f'</summary>'
                f'<table style="width:100%;border-collapse:collapse;margin:2px 0 8px">{head}<tbody>{body}</tbody></table>'
                f'</details>')

    def _project_sort_key(name, group):
        """Worst first: the project with the longest-running outage, then the
        one with the most sensors down, then alphabetical for stability."""
        rows = group[name]["sensors"]
        longest = max((r["down_days"] for r in rows), default=0)
        return (-longest, -len(rows), name)

    html = ""
    if actionable:
        for name in sorted(actionable, key=lambda k: _project_sort_key(k, actionable)):
            html += _project_block(name, actionable[name])
    else:
        html += ('<p style="color:var(--color-text-secondary);font-size:13px;padding:4px 0">'
                 'No actionable projects have failing sensors. &#9989;</p>')

    if out_of_support:
        oos_total = sum(len(v["sensors"]) for v in out_of_support.values())
        html += (f'<div style="margin-top:14px;padding-top:10px;border-top:0.5px solid var(--color-border-tertiary)">'
                 f'<div style="font-size:11px;font-weight:500;letter-spacing:0.05em;text-transform:uppercase;'
                 f'color:var(--color-text-secondary);margin-bottom:8px">'
                 f'Out of support &middot; {oos_total} down &middot; failure expected, not actionable</div>')
        for name in sorted(out_of_support, key=lambda k: _project_sort_key(k, out_of_support)):
            html += _project_block(name, out_of_support[name], dim=True)
        html += '</div>'

    return html


def _pct_from_counts(counts):
    """Health % from a {status: count} dict, or None when there are no sensors.
    'good' = working/ok (stability.GOOD_STATUSES); everything else counts against.
    This is the authoritative computation — no check_summary string parsing."""
    if not counts:
        return None
    total = sum(counts.values())
    if not total:
        return None
    good = sum(n for st, n in counts.items() if st in GOOD_STATUSES)
    return good / total * 100


def _build_health_by_run(raw_health, status_counts):
    """Build {run_id: {group_name: pct, 'feed_issues': [...]}}.

    Percentages come from per-sensor status counts (status_counts, keyed by the
    sensor group_name); feed_issues come from the endpoint-level pass/fail in
    raw_health (test_results). raw_health also determines which runs/groups the
    chart covers."""
    health_by_run = {}
    for row in raw_health:
        rid = row["run_id"]
        entry = health_by_run.setdefault(rid, {"feed_issues": []})
        ep_info = HEALTH_ENDPOINTS.get(row["test_name"])
        if not ep_info:
            continue
        grp = ep_info["group"]
        counts = status_counts.get(rid, {}).get(ep_info["sensor_group"])
        entry[grp] = _pct_from_counts(counts)
        if row["status"] != "pass":
            entry["feed_issues"].append(grp)
    return health_by_run


def _build_chart_data(chart_runs, health_by_run):
    """Return (labels_json, chart_series_dict, x_min_json, x_max_json)."""
    label_list = [_to_cyprus(r["run_at"]) for r in chart_runs]
    chart_series = {
        gname: json.dumps([
            (lambda v: round(v, 1) if v is not None else None)(
                health_by_run.get(r["run_id"], {}).get(gname)
            )
            for r in chart_runs
        ])
        for gname in GROUP_META
    }
    labels_json = json.dumps(label_list)
    x_min = json.dumps(label_list[-30] if len(label_list) > 30 else label_list[0])
    x_max = json.dumps(label_list[-1] if label_list else "")
    return labels_json, chart_series, x_min, x_max


def _build_sensor_trend_data(all_sensors):
    """Return (trend_data_json, day_labels_json) for the per-sensor sparkline charts."""
    today = datetime.now(timezone.utc).date()
    days30 = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
    day_labels = [d.strftime("%d/%m") for d in days30]

    daily = {}
    for s in all_sensors:
        key = f"{s['group_name']}|{s['sensor_id']}"
        daily[key] = {}
        for h in s["history"]:
            day = h["run_at"][:10]
            if day not in daily[key]:
                daily[key][day] = {"good": 0, "total": 0}
            daily[key][day]["total"] += 1
            if h["status"] in GOOD_STATUSES:
                daily[key][day]["good"] += 1

    trend_data = {}
    for s in all_sensors:
        key = f"{s['group_name']}|{s['sensor_id']}"
        row = []
        for d in days30:
            stats = daily.get(key, {}).get(d.isoformat(), {})
            t = stats.get("total", 0)
            row.append(round(stats.get("good", 0) / t * 100) if t else None)
        trend_data[key] = row

    return json.dumps(trend_data), json.dumps(day_labels)


def _build_map_sensor_list(all_coords, live_data, sensor_projects=None):
    """Return list of sensor dicts for the Leaflet map."""
    sensor_projects = sensor_projects or {}
    sensors = []
    for group_name, sensors_dict in all_coords.items():
        group_live = live_data.get(group_name, {})
        for sid, c in sensors_dict.items():
            entry = group_live.get(sid, {})
            st = entry.get("status", "unknown")
            if group_name == "Traffic Detection":
                sc = c.get("site_code")
                nm = c.get("name", sid)
                display_name = f"{sc} ({nm})" if sc else nm
            else:
                display_name = c["name"]
            proj_info = sensor_projects.get(group_name, {}).get(sid)
            comm = proj_info.get("commissioning", "active") if proj_info else "active"
            comm_label = {"not_electrified": "Awaiting power — not yet electrified",
                          "decommissioned": "Decommissioned"}.get(comm)
            sensors.append({
                "id": sid, "group": group_name,
                "group_display": GROUP_DISPLAY.get(group_name, group_name),
                "name": c["name"], "display_name": display_name,
                "lat": c["lat"], "lon": c["lon"],
                "status": st,
                "label": STATUS_LABEL.get(st, "Unknown"),
                "color": "#9ca3af" if comm_label else STATUS_COLOR.get(st, "#6b7280"),
                "data": entry.get("data", {}),
                "project": proj_info["project"] if proj_info else None,
                "comm_label": comm_label,
            })
    return sensors


def _build_map_bt_path_list(all_bt_paths, live_data):
    """Return list of BT path dicts for the Leaflet map."""
    bt_group_live = live_data.get("Bluetooth Paths", {})
    paths = []
    for pid, p in all_bt_paths.items():
        entry = bt_group_live.get(pid, {})
        st = entry.get("status", "unknown")
        paths.append({
            "id": pid, "name": p["name"],
            "coords": p["coords"], "status": st,
            "color": STATUS_COLOR.get(st, "#6b7280"),
            "data": entry.get("data", {}),
        })
    return paths


def _build_history_playback(all_sensors):
    """Return JSON string of the last 30 runs for the map playback slider."""
    run_timeline = {}
    for s in all_sensors:
        for h in s["history"]:
            rat = h["run_at"]
            if rat not in run_timeline:
                run_timeline[rat] = {"run_at": _to_cyprus(rat), "statuses": {}}
            run_timeline[rat]["statuses"][s["sensor_id"]] = h["status"]
    # Sort by the raw ISO timestamp key — NOT the formatted dd/mm/yy display
    # string, which sorts wrongly across month boundaries ("01/07" < "30/06").
    runs_sorted = [run_timeline[rat] for rat in sorted(run_timeline)]
    return _json_safe(runs_sorted[-30:])


def generate_report() -> str:
    REPORT_PATH.parent.mkdir(exist_ok=True)

    runs = fetch_recent_runs(200)
    if not runs:
        REPORT_PATH.write_text("<html><body>No runs yet.</body></html>")
        return str(REPORT_PATH)

    conn = get_connection()
    first_run_at = conn.execute("SELECT MIN(run_at) FROM runs").fetchone()[0]
    conn.close()
    first_run_date = _to_cyprus(first_run_at).split(" ")[0] if first_run_at else "—"

    latest_run = runs[0]
    latest_results = fetch_results_for_run(latest_run["run_id"])

    # Group latest results by group
    groups = {}
    for r in latest_results:
        groups.setdefault(r["group_name"], []).append(r)

    # Chart data
    chart_runs = list(reversed(runs[:200]))

    # Sensor stability (coord lookups fetched below with map data)
    all_sensors = fetch_sensor_stability()

    # Sensor health history — build per-run lookup keyed by run_id → group_name.
    # Percentages derive from per-sensor status counts; feed_issues from the
    # endpoint pass/fail carried in raw_health.
    # Project ownership + commissioning — fetched once, shared by the group cards,
    # stability panel, map pop-ups, the "attention needed" rollup, and (below)
    # the health-percentage exclusion.
    sensor_projects = fetch_sensor_projects()
    project_acct    = _load_project_accountability()

    # Sensors awaiting power / decommissioned aren't expected to be working, so
    # they're dropped by ID from the health-percentage counts — otherwise e.g.
    # 39 not-yet-electrified VMS would hold the group at <10%. Same exclusion the
    # stability panel and the "X/Y working" line already apply.
    excluded_health_ids = {
        (grp, sid)
        for grp, sdict in sensor_projects.items()
        for sid, info in sdict.items()
        if info.get("commissioning") in _EXCLUDED_COMMISSIONING
    }

    raw_health = fetch_sensor_health_history(200, live_test_names=list(HEALTH_ENDPOINTS.keys()))
    status_counts = fetch_sensor_status_counts(200, excluded=excluded_health_ids)
    health_by_run = _build_health_by_run(raw_health, status_counts)

    chart_labels, chart_series, chart_x_min, chart_x_max = _build_chart_data(chart_runs, health_by_run)

    # Per-sensor statuses for the latest run (used for full ID lists in cards)
    latest_sensor_statuses = fetch_sensor_statuses_for_run(latest_run["run_id"])
    # Per-group counts of sensors excluded from health stats, by reason. Surfaced
    # separately in the group cards so the reader sees why the live total is lower.
    awaiting_by_group = {}
    decommissioned_by_group = {}
    for grp, sdict in sensor_projects.items():
        aw = sum(1 for info in sdict.values() if info.get("commissioning") == "not_electrified")
        de = sum(1 for info in sdict.values() if info.get("commissioning") == "decommissioned")
        if aw:
            awaiting_by_group[grp] = aw
        if de:
            decommissioned_by_group[grp] = de

    # Fetched here (rather than later with the rest of the map data) because
    # _live_total needs the registered active-sensor count below.
    all_coords = fetch_sensor_coords()

    def _live_total(group_name, working):
        """Count of registered active sensors for this group that are expected
        to work — i.e. active in sensor_coords AND not awaiting-power /
        decommissioned. Counts the intersection directly rather than subtracting
        commissioning tallies: a sensor can be decommissioned in sensor_projects
        yet already dropped from active coords, so subtracting the two counts
        would exclude it twice. Uses the registered count (not the count that
        reported in this run's feed) so a sensor that goes dark shows as a
        numerator drop against a stable denominator. Clamped to at least
        `working` as a final safety net against stale data."""
        expected = sum(
            1 for sid in all_coords.get(group_name, {})
            if not _is_excluded_commissioning(sensor_projects, group_name, sid)
        )
        return max(expected, working)


    # Build group status cards
    def group_status_card(group_name, icon, results):
        all_pass = all(r["status"] == "pass" for r in results)
        any_error = any(r["status"] == "error" for r in results)

        # Compute minimum sensor health across all endpoints in this group,
        # from the latest run's per-sensor status counts (authoritative).
        latest_counts = status_counts.get(latest_run["run_id"], {})
        min_health_pct = None
        degraded_test_name = None
        for r in results:
            ep_info = HEALTH_ENDPOINTS.get(r["test_name"])
            if not ep_info:
                continue
            pct = _pct_from_counts(latest_counts.get(ep_info["sensor_group"]))
            if pct is not None and (min_health_pct is None or pct < min_health_pct):
                min_health_pct = pct
                degraded_test_name = r.get("test_name")

        sensor_degraded = min_health_pct is not None and min_health_pct < HEALTH_WARNING_PCT

        if not all_pass:
            status_color = "#e58e0a" if any_error else "#e24b4a"
            status_bg    = "#faeeda" if any_error else "#fcebeb"
            status_label = "Degraded" if any_error else "Feed issue"
            status_icon  = "ti-alert-triangle" if any_error else "ti-circle-x"
            failing_tests = [r["test_name"] for r in results if r["status"] != "pass"]
            status_tip   = (
                ("⚠ One or more endpoints returned an error or timed out." if any_error
                 else "✗ Feed check failed — data from this group may be missing or stale.")
                + f"\nFailing: {', '.join(failing_tests)}"
            )
        elif sensor_degraded:
            status_color = "#e58e0a"
            status_bg    = "#faeeda"
            status_label = f"Deteriorated ({round(min_health_pct)}%)"
            status_icon  = "ti-alert-triangle"
            status_tip   = (
                f"⚠ Sensor health is below {HEALTH_WARNING_PCT}% (currently {round(min_health_pct)}%).\n"
                f"Threshold: ≥90% = Operational · ≥{HEALTH_WARNING_PCT}% = Deteriorated · below = Feed issue\n"
                f"Driven by: {degraded_test_name}"
            )
        else:
            status_color = "#1d9e75"
            status_bg    = "#e1f5ee"
            status_label = "Operational" + (f" ({round(min_health_pct)}%)" if min_health_pct is not None else "")
            status_icon  = "ti-circle-check"
            status_tip   = (
                (f"✓ All sensors reporting normally ({round(min_health_pct)}% healthy).\n"
                 f"Threshold: ≥90% = Operational · ≥{HEALTH_WARNING_PCT}% = Deteriorated")
                if min_health_pct is not None
                else "✓ All feed checks passing."
            )

        pass_count = sum(1 for r in results if r["status"] == "pass")

        detail_rows = ""
        for r in results:
            cs = r.get("check_summary", "") or ""

            # Dot color + tooltip: feed failure = red/amber; sensor check = health-based; otherwise green
            # Parse individual check results from check_summary for the tooltip
            check_lines = []
            for part in cs.split(" | "):
                part = part.strip()
                if part.startswith("[✓]") or part.startswith("[✗]"):
                    icon = "✓" if part.startswith("[✓]") else "✗"
                    rest = part[3:].strip()
                    check_lines.append(f"  {icon} {rest}")

            if r["status"] != "pass":
                dot_color = "#e58e0a" if r["status"] == "error" else "#e24b4a"
                base = ("⚠ Error: endpoint timed out or returned an unexpected response."
                        if r["status"] == "error"
                        else "✗ Feed check failed.")
                dot_tip = base + ("\n\nChecks:\n" + "\n".join(check_lines) if check_lines else "")
            else:
                ep_info = HEALTH_ENDPOINTS.get(r["test_name"])
                h_pct = _pct_from_counts(latest_counts.get(ep_info["sensor_group"])) if ep_info else None
                dot_color = _health_color(h_pct) if h_pct is not None else "#1d9e75"
                if h_pct is not None:
                    status_word = "good" if h_pct >= 90 else "deteriorated"
                    dot_tip = (
                        f"{'✓' if h_pct >= 90 else '⚠'} Sensor health: {round(h_pct)}% ({status_word})\n"
                        f"≥90% = green · ≥{HEALTH_WARNING_PCT}% = amber · below = red\n"
                        + ("\nChecks:\n" + "\n".join(check_lines) if check_lines else "")
                    )
                else:
                    dot_tip = "✓ All checks passed." + ("\n\nChecks:\n" + "\n".join(check_lines) if check_lines else "")

            # Failure lines: only for non-passing feed-level checks (sensor checks excluded)
            failure_lines = ""
            if r["status"] != "pass" and r.get("failure_reason"):
                fr = r["failure_reason"]
                check_names_found = re.findall(r"(?:^| \| )([a-z][a-z_]+): ", fr)
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
            if r["test_name"] == "Bluetooth Inventory" and cs:
                m = re.search(r"bt_site_count: (\d+)", cs)
                if m:
                    name_suffix = f' <span style="font-size:11px;color:var(--color-text-secondary)">— {m.group(1)} devices</span>'
            elif "sensor_speed_status" in cs:
                m = re.search(r"Working: (\d+)/(\d+)", cs)
                if m:
                    working = int(m.group(1))
                    live_total = _live_total(group_name, working)
                    pct = working / live_total * 100 if live_total else 0
                    name_suffix = f' <span style="font-size:11px;color:{_health_color(pct)}">— {working}/{live_total} working</span>'
                    name_suffix += _commissioning_note(group_name, awaiting_by_group, decommissioned_by_group)
            elif "vms_controller_status" in cs:
                w = re.search(r"Working: (\d+)", cs)
                nw = re.search(r"Not working: (\d+)", cs)
                ns = re.search(r"No status: (\d+)", cs)
                if w:
                    working = int(w.group(1))
                    live_total = _live_total(group_name, working)
                    pct = working / live_total * 100 if live_total else 0
                    name_suffix = f' <span style="font-size:11px;color:{_health_color(pct)}">— {working}/{live_total} working</span>'
                    name_suffix += _commissioning_note(group_name, awaiting_by_group, decommissioned_by_group)
            elif "bt_paths_speed_and_traveltime" in cs:
                m = re.search(r"Speed OK: (\d+)/(\d+)", cs)
                if m:
                    pct = int(m.group(1)) / int(m.group(2)) * 100
                    name_suffix = f' <span style="font-size:11px;color:{_health_color(pct)}">— {m.group(1)}/{m.group(2)} with data</span>'

            check_desc = CHECK_DESCRIPTION.get(r['test_name'], '')
            check_desc_html = f'<div style="font-size:11px;color:var(--color-text-secondary);padding-left:16px;margin-top:2px;line-height:1.4">{check_desc}</div>' if check_desc else ''
            detail_rows += f"""
            <div style="padding:6px 0;border-bottom:0.5px solid var(--color-border-tertiary)">
              <div style="display:flex;align-items:center;gap:8px">
                <span title="{dot_tip}" style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0;cursor:help"></span>
                <span style="font-size:13px;color:var(--color-text-primary);flex:1">{r['test_name']}{name_suffix}</span>
              </div>
              {check_desc_html}
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
                      <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">Bluetooth Paths with data</div>
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
                      <div style="font-weight:500;color:var(--color-text-primary);margin-bottom:8px">Traffic Detection Units — {d['total']} total</div>
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

        layer_key = (GROUP_META.get(group_name) or {}).get("layer_key", "")
        map_btn = (
            f'<div style="margin-top:12px;border-top:0.5px solid var(--color-border-tertiary);padding-top:10px">'
            f'<button onclick="focusMapLayer(\'{layer_key}\')" '
            f'style="font-size:11px;font-weight:500;color:var(--text);background:none;border:none;'
            f'cursor:pointer;padding:0;display:flex;align-items:center;gap:4px">'
            f'<i class="ti ti-map-pin" style="font-size:12px"></i>View on map</button></div>'
        ) if layer_key else ""
        return f"""
        <div style="background:var(--color-background-primary);border:0.5px solid var(--color-border-tertiary);border-radius:12px;padding:20px;flex:1;min-width:260px">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
            <i class="ti {icon}" style="font-size:22px;color:var(--color-text-secondary)" aria-hidden="true"></i>
            <div style="flex:1">
              <div style="font-size:15px;font-weight:500;color:var(--color-text-primary)">{GROUP_DISPLAY.get(group_name, group_name)}</div>
              <div style="font-size:12px;color:var(--color-text-secondary)">{pass_count}/{len(results)} checks passing</div>
            </div>
            <span title="{status_tip}" style="display:flex;align-items:center;gap:5px;font-size:12px;font-weight:500;padding:4px 10px;border-radius:20px;background:{status_bg};color:{status_color};cursor:help">
              <i class="ti {status_icon}" style="font-size:14px" aria-hidden="true"></i>{status_label}
            </span>
          </div>
          <div style="border-top:0.5px solid var(--color-border-tertiary);padding-top:8px">
            {detail_rows}
          </div>
          {extra}
          {map_btn}
        </div>"""

    group_cards = ""
    for gname, gresults in sorted(groups.items()):
        icon = (GROUP_META.get(gname) or {}).get("icon", "ti-device-analytics")
        group_cards += group_status_card(gname, icon, gresults)

    def _hcell(pct):
        if pct is None:
            return '<td style="padding:9px 14px;color:var(--color-text-secondary);font-size:12px">—</td>'
        color = _health_color(pct)
        bg    = "#e1f5ee" if pct >= 90 else ("#faeeda" if pct >= HEALTH_WARNING_PCT else "#fcebeb")
        tc    = "#0f6e56" if pct >= 90 else ("#854f0b" if pct >= HEALTH_WARNING_PCT else "#a32d2d")
        bw    = round(pct)
        return (f'<td style="padding:9px 14px">'
                f'<span style="font-size:11px;font-weight:500;padding:2px 7px;border-radius:20px;background:{bg};color:{tc}">{round(pct)}%</span>'
                f'<div style="display:inline-block;vertical-align:middle;margin-left:6px;width:52px;height:4px;'
                f'background:var(--color-border-tertiary);border-radius:2px">'
                f'<div style="width:{bw}%;height:4px;border-radius:2px;background:{color}"></div></div></td>')

    history_rows = ""
    for run in runs[:30]:
        h = health_by_run.get(run["run_id"], {})
        ts = _to_cyprus(run["run_at"])
        issues = h.get("feed_issues", [])
        if issues:
            feed_cell = (f'<td style="padding:9px 14px"><span style="font-size:11px;font-weight:500;padding:2px 7px;'
                         f'border-radius:20px;background:#fcebeb;color:#a32d2d">'
                         f'<i class="ti ti-alert-triangle" style="font-size:11px;vertical-align:-1px" aria-hidden="true"></i>'
                         f' {", ".join(issues)}</span></td>')
        else:
            feed_cell = ('<td style="padding:9px 14px"><span style="font-size:11px;font-weight:500;padding:2px 7px;'
                         'border-radius:20px;background:#e1f5ee;color:#0f6e56">'
                         '<i class="ti ti-circle-check" style="font-size:11px;vertical-align:-1px" aria-hidden="true"></i>'
                         ' All up</span></td>')
        group_cells = "".join(_hcell(h.get(gname)) for gname in GROUP_META)
        history_rows += f"""
        <tr>
          <td style="color:var(--color-text-secondary);font-size:13px;padding:9px 14px">{ts}</td>
          {group_cells}
          {feed_cell}
        </tr>"""

    # Map data (all_coords fetched earlier — needed by _live_total above)
    all_bt_paths = fetch_bt_path_coords()

    trend_data_json, day_labels_json = _build_sensor_trend_data(all_sensors)

    # Build set of active sensor keys from coord tables (which already filter active=1)
    _active_keys = set()
    for grp, sensors_dict in all_coords.items():
        for sid in sensors_dict:
            _active_keys.add((grp, sid))
    for pid in all_bt_paths:
        _active_keys.add(("Bluetooth Paths", pid))
    active_sensors = [s for s in all_sensors if (s["group_name"], s["sensor_id"]) in _active_keys]

    # Build stability html now that coord lookups are available
    _bt_path_names = {pid: p["name"] for pid, p in all_bt_paths.items()}
    sensor_stability_html = build_sensor_stability_html(active_sensors, _bt_path_names, all_coords, trend_data_json, day_labels_json, all_bt_paths,
                                                          sensor_projects=sensor_projects, project_acct=project_acct)
    accountability_html = build_accountability_rollup_html(active_sensors, _bt_path_names, all_coords, sensor_projects, project_acct)
    live_data = fetch_sensor_live_data_for_run(latest_run["run_id"])

    map_sensors       = _build_map_sensor_list(all_coords, live_data, sensor_projects)
    map_bt_paths      = _build_map_bt_path_list(all_bt_paths, live_data)
    map_sensors_json  = _json_safe(map_sensors)   # carry names + VMS message text
    map_bt_paths_json = _json_safe(map_bt_paths)   # carry route names
    history_playback_json = _build_history_playback(all_sensors)

    # Pre-compute group-driven HTML/JS snippets so injection sites stay clean
    _bt_paths_label   = _UI.get("bt_paths_map_label", "Bluetooth Paths")
    chart_legend      = _chart_legend_html(GROUP_META)
    history_th_cells  = _history_header_cells(GROUP_META)
    chart_datasets    = _chart_datasets_js(GROUP_META, chart_series)
    map_layer_buttons = _map_layer_buttons(GROUP_META, _bt_paths_label)

    has_map_data = bool(map_sensors or map_bt_paths)
    map_script_html = _build_map_script(map_sensors_json, map_bt_paths_json, history_playback_json, GROUP_META) if has_map_data else ""

    if not has_map_data:
        map_panel_html = '<p style="color:var(--color-text-secondary);font-size:13px">No coordinate data yet — run the test suite once to populate the map.</p>'
    else:
        map_panel_html = (
            '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">'
            '<span style="font-size:11px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-right:4px">Show:</span>'
            '<button class="map-toggle active" id="btn-showall" onclick="toggleShowAll(this)" style="margin-right:4px">Show all</button>'
            + map_layer_buttons
            + '<span style="flex:1"></span>'
            '<button class="map-toggle active" id="btn-cluster" onclick="toggleClustering(this)" title="Toggle marker clustering">Cluster</button>'
            '<button class="map-toggle active" data-filter="all" onclick="setFilter(this,\'all\')">All</button>'
            '<button class="map-toggle" data-filter="issues" onclick="setFilter(this,\'issues\')">Issues only</button>'
            '</div>'
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding:7px 10px;'
            'background:var(--surface);border:0.5px solid var(--border);border-radius:8px">'
            '<button onclick="stepRun(-1)" title="Previous run" style="background:none;border:none;cursor:pointer;font-size:14px;color:var(--muted);padding:2px 5px;line-height:1">&#9664;</button>'
            '<button id="playBtn" onclick="togglePlay()" title="Play / Pause" style="background:none;border:none;cursor:pointer;font-size:14px;color:var(--muted);padding:2px 5px;line-height:1">&#9654;</button>'
            '<button onclick="stepRun(1)" title="Next run" style="background:none;border:none;cursor:pointer;font-size:14px;color:var(--muted);padding:2px 5px;line-height:1">&#9654;&#9654;</button>'
            '<input type="range" id="playSlider" min="0" value="0" style="flex:1;accent-color:var(--header-bg)" oninput="setRun(+this.value)">'
            '<span id="playTimestamp" style="font-size:11px;color:var(--muted);min-width:150px;text-align:right;white-space:nowrap"></span>'
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
    last_run_utc_iso = latest_run["run_at"]
    staleness_threshold_hours = _UI.get("staleness_threshold_hours", 9)

    # Per-panel progress bar percentages
    latest_total = latest_run["total"] or 1
    overall_pct = round(latest_run["passed"] / latest_total * 100)
    overall_bar_color = "#1d9e75" if overall_pct >= 90 else ("#e58e0a" if overall_pct >= 55 else "#e24b4a")

    health_vals = [v for r in chart_runs for gname in GROUP_META
                   for v in [health_by_run.get(r["run_id"], {}).get(gname)] if v is not None]
    trend_pct = round(sum(health_vals) / len(health_vals)) if health_vals else 0
    trend_bar_color = _health_color(trend_pct)

    latest_h = health_by_run.get(latest_run["run_id"], {})
    latest_hvals = [v for gname in GROUP_META for v in [latest_h.get(gname)] if v is not None]
    history_pct = round(sum(latest_hvals) / len(latest_hvals)) if latest_hvals else overall_pct
    history_bar_color = _health_color(history_pct)

    _counted = [s for s in all_sensors
                if not _is_excluded_commissioning(sensor_projects, s["group_name"], s["sensor_id"])]
    sensor_good = sum(1 for s in _counted if s["history"] and s["history"][-1]["status"] in GOOD_STATUSES)
    sensor_total_count = len(_counted) or 1
    sensor_pct = round(sensor_good / sensor_total_count * 100)
    sensor_bar_color = "#1d9e75" if sensor_pct >= 90 else ("#e58e0a" if sensor_pct >= 55 else "#e24b4a")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_UI.get('page_title', 'ITS Infrastructure Health')}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
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
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;
            position: sticky; top: 0; z-index: 2000; }}
  header h1 {{ font-size: 1.15rem; font-weight: 500; letter-spacing: -0.01em; }}
  header .meta {{ font-size: 11px; opacity: 0.45; margin-top:3px; }}
  .wrap {{ max-width: 100%; margin: 0 auto; padding: 20px 32px; }}
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
  .col-layout {{ display:flex; gap:20px; align-items:flex-start; }}
  /* 55/45 split. Grow factors on a zero basis divide the space *after* the gap;
     a fixed 55%/45% basis plus the 20px gap would overflow the container. */
  .col-left   {{ flex:55 1 0; min-width:0; }}
  .col-right  {{ flex:45 1 0; min-width:0; position:sticky; top:20px; max-height:calc(100vh - 40px); overflow-y:auto; }}
  @media (max-width: 900px) {{
    .col-layout {{ flex-direction:column; }}
    .col-left, .col-right {{ flex:none; width:100%; max-height:none; position:static; }}
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  #sensorTable {{ min-width: 700px; }}
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
  .trend-btn {{
    font-size:11px;padding:3px 10px;border-radius:4px;cursor:pointer;
    border:1px solid var(--border);background:var(--surface);color:var(--muted);
  }}
  .trend-btn:hover {{ background:var(--header-bg);color:#fff;border-color:var(--header-bg); }}
  #trendChart {{ cursor: grab; }}
  #trendChart:active {{ cursor: grabbing; }}
  .leaflet-top {{ transition: top .1s; }}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script src="https://unpkg.com/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js"></script>
<script>
  (function() {{
    var lastRun = new Date("{last_run_utc_iso}").getTime();
    var thresholdMs = {staleness_threshold_hours} * 3600 * 1000;
    if (Date.now() - lastRun > thresholdMs) {{
      document.addEventListener('DOMContentLoaded', function() {{
        var b = document.getElementById('stale-banner');
        if (b) b.style.display = '';
      }});
    }}
  }})();
</script>
</head>
<body>
<div id="stale-banner" style="display:none;background:#7c3aed;color:#fff;text-align:center;padding:8px 16px;font-size:12px;font-weight:500"><i class="ti ti-alert-triangle" style="vertical-align:-2px;margin-right:6px"></i>Data may be outdated — monitoring may be disrupted.</div>
<header>
  <div>
    <h1><i class="ti ti-traffic-lights" style="font-size:17px;vertical-align:-2px;margin-right:8px" aria-hidden="true"></i>{_UI.get('page_title', 'ITS Infrastructure Health')}</h1>
    <div class="meta">Last checked {run_time} Cyprus time &nbsp;·&nbsp; running since {first_run_date}</div>
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
<div class="col-layout">
<div class="col-left">

  <details style="margin-bottom:16px;background:var(--color-background-secondary);border-radius:10px;border:0.5px solid var(--color-border-tertiary);padding:12px 16px;font-size:13px">
    <summary style="cursor:pointer;font-weight:600;color:var(--color-text-primary);list-style:none;display:flex;align-items:center;gap:6px">
      <i class="ti ti-info-circle" style="font-size:15px" aria-hidden="true"></i> How to read this report
    </summary>
    <div style="margin-top:12px;display:grid;grid-template-columns:1fr 1fr;gap:16px;color:var(--color-text-secondary)">

      <div>
        <div style="font-weight:600;color:var(--color-text-primary);margin-bottom:6px">Group status</div>
        <div style="margin-bottom:4px"><span style="background:#e1f5ee;color:#085041;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:500">Operational</span> — All checks passing, sensors healthy.</div>
        <div style="margin-bottom:4px"><span style="background:#faeeda;color:#633806;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:500">Deteriorated</span> — Checks pass but sensor health is below 80%.</div>
        <div style="margin-bottom:4px"><span style="background:#fcebeb;color:#e24b4a;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:500">Feed issue</span> — One or more API checks are failing.</div>
      </div>

      <div>
        <div style="font-weight:600;color:var(--color-text-primary);margin-bottom:6px">Traffic Detection sensor statuses</div>
        <div style="margin-bottom:4px"><span style="color:#1d9e75;font-weight:500">Working</span> — Sensor reported a positive speed; vehicles detected, hardware OK.</div>
        <div style="margin-bottom:4px"><span style="color:#9ca3af;font-weight:500">No traffic</span> — Sensor is communicating but reported speed = 0; no vehicles detected.</div>
        <div style="margin-bottom:4px"><span style="color:#e24b4a;font-weight:500">Malfunctioning</span> — Sensor reported speed = -1, the hardware fault code; loop detector likely damaged.</div>
        <div style="margin-bottom:4px"><span style="color:#9ca3af;font-weight:500">No measurement</span> — Sensor is in the inventory but sent no data this run.</div>
      </div>

      <div>
        <div style="font-weight:600;color:var(--color-text-primary);margin-bottom:6px">VMS statuses</div>
        <div style="margin-bottom:4px"><span style="color:#1d9e75;font-weight:500">Working</span> — Controller is active and responding correctly.</div>
        <div style="margin-bottom:4px"><span style="color:#e24b4a;font-weight:500">Not working</span> — Controller explicitly reported a fault; sign may be offline or damaged.</div>
        <div style="margin-bottom:4px"><span style="color:#9ca3af;font-weight:500">No status</span> — Controller exists in the system but sent no status in this run.</div>
      </div>

      <div>
        <div style="font-weight:600;color:var(--color-text-primary);margin-bottom:6px">Bluetooth Path statuses</div>
        <div style="margin-bottom:4px"><span style="color:#1d9e75;font-weight:500">Working</span> — Path has valid speed and travel time data.</div>
        <div style="margin-bottom:4px"><span style="color:#e24b4a;font-weight:500">Failing</span> — No speed or travel time reported; no vehicles detected on route or Bluetooth readers lost connectivity.</div>
      </div>

      <div style="grid-column:span 2">
        <div style="font-weight:600;color:var(--color-text-primary);margin-bottom:6px">Sensor stability panel</div>
        <div style="margin-bottom:4px">Each row shows a sensor's last 20 checks as coloured bars — <span style="color:#1d9e75;font-weight:500">green</span> = good, <span style="color:#e24b4a;font-weight:500">red</span> = fault, <span style="color:#9ca3af;font-weight:500">grey</span> = no data or no traffic. Hover over a bar to see the exact timestamp and status.</div>
        <div>The <strong>health badge</strong> (e.g. Healthy 95%) shows the percentage of all recorded checks that returned a good status since monitoring began. Click any row to expand a daily trend chart.</div>
      </div>
    </div>
  </details>

  <div class="panel" id="p-groups">
    <div class="panel-header" onclick="togglePanel('p-groups')">
      <div>
        <span class="panel-title">{_lbl('panels', 'groups', 'System Overview')}</span>
        <div style="font-size:11px;color:var(--color-text-secondary);margin-top:2px">Feed and sensor health per group — updated each run</div>
      </div>
      <div class="panel-chevron open" id="c-p-groups"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-body" id="b-p-groups">
      <div class="group-cards">
        {group_cards}
      </div>
    </div>
  </div>

  <div class="panel" id="p-map">
    <div class="panel-header" onclick="togglePanel('p-map')">
      <span class="panel-title">{_lbl('panels', 'map', 'Sensor map')}</span>
      <div class="panel-chevron open" id="c-p-map"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar" title="{sensor_pct}% of sensors had a good status in the last run" style="cursor:help"><div class="panel-bar-fill" style="width:{sensor_pct}%;background:{sensor_bar_color}"></div></div>
    <div class="panel-body" id="b-p-map" style="padding:12px 20px 16px">
      {map_panel_html}
    </div>
  </div>

  <div class="panel" id="p-trend">
    <div class="panel-header" onclick="togglePanel('p-trend')">
      <span class="panel-title">{_lbl('panels', 'health_trend', 'Sensor health trend')}</span>
      <div class="panel-chevron open" id="c-p-trend"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar" title="Average health across all recent runs (sensors combined): {trend_pct}%" style="cursor:help"><div class="panel-bar-fill" style="width:{trend_pct}%;background:{trend_bar_color}"></div></div>
    <div class="panel-body" id="b-p-trend">
      <div style="position:relative;height:180px">
        <canvas id="trendChart" role="img" aria-label="Line chart of sensor health percentages across recent runs"></canvas>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:10px;flex-wrap:wrap;gap:8px">
        <div style="display:flex;gap:16px;font-size:12px;color:var(--muted)">{chart_legend}</div>
        <div style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)">
          <span>Scroll to zoom &middot; drag to pan</span>
          <button onclick="(function(){{var c=window._healthTrendChart,l=c.data.labels;c.options.scales.x.min=l.length>30?l[l.length-30]:l[0];c.options.scales.x.max=l[l.length-1];c.update('none')}})()" class="trend-btn" title="Reset to last 30 runs">Reset</button>
        </div>
      </div>
    </div>
  </div>

  <div class="panel" id="p-history">
    <div class="panel-header" onclick="togglePanel('p-history')">
      <span class="panel-title">{_lbl('panels', 'history', 'Run history')} — last 30 runs</span>
      <div class="panel-chevron open" id="c-p-history"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar" title="Average health across all sensors in the last run: {history_pct}%" style="cursor:help"><div class="panel-bar-fill" style="width:{history_pct}%;background:{history_bar_color}"></div></div>
    <div class="panel-body" id="b-p-history">
      <p style="font-size:12px;color:var(--color-text-secondary);margin:0 0 12px">
        Each row is one automated test run. The percentage columns show how many sensors in that group
        returned a healthy status during that run. <strong>API response</strong> shows whether all endpoints
        were reachable during that run.
      </p>
      <table>
        <thead><tr><th>Time (Cyprus)</th>{history_th_cells}<th>API response</th></tr></thead>
        <tbody>{history_rows}</tbody>
      </table>
    </div>
  </div>

</div><!-- end left column -->
<div class="col-right">

  <div class="panel" id="p-accountability">
    <div class="panel-header" onclick="togglePanel('p-accountability')">
      <span class="panel-title">Attention needed, by project</span>
      <div class="panel-chevron open" id="c-p-accountability"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-body" id="b-p-accountability">
      <p style="font-size:12px;color:var(--color-text-secondary);margin:0 0 10px">
        Sensors that are <strong>not reporting right now</strong>, grouped by the project responsible
        and ordered by how long they have been down. The badge shows each sensor's lifetime record,
        for context.
      </p>
      {accountability_html}
    </div>
  </div>

  <div class="panel" id="p-sensors">
    <div class="panel-header" onclick="togglePanel('p-sensors')">
      <span class="panel-title">{_lbl('panels', 'stability', 'Sensor stability')}</span>
      <div class="panel-chevron open" id="c-p-sensors"><i class="ti ti-chevron-down" aria-hidden="true"></i></div>
    </div>
    <div class="panel-bar" id="sensorBarWrap" title="{sensor_pct}% of sensors had a good status in the last run" style="cursor:help"><div class="panel-bar-fill" id="sensorBarFill" style="width:{sensor_pct}%;background:{sensor_bar_color}"></div></div>
    <div class="panel-body" id="b-p-sensors" style="overflow-x:auto">
      <p style="font-size:12px;color:var(--color-text-secondary);margin:0 0 10px">
        <strong>Current state</strong> is what the sensor is doing right now.
        <strong>Stability</strong> is its lifetime record across every run ever taken —
        how much the sensor can be trusted. A sensor can be down today but have a good record, or vice versa.
      </p>
      {sensor_stability_html}
    </div>
  </div>

</div><!-- end right column -->
</div><!-- end flex row -->
</div><!-- end wrap -->

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
  var tc = _dark ? '#9ca3af' : '#4b5563';
  var gc = _dark ? 'rgba(255,255,255,0.06)' : 'rgba(128,128,128,0.1)';
  if (window._healthTrendChart) {{
    window._healthTrendChart.options.scales.x.ticks.color = tc;
    window._healthTrendChart.options.scales.y.ticks.color = tc;
    window._healthTrendChart.options.scales.y.grid.color = gc;
    window._healthTrendChart.update();
  }}
}}

window._healthTrendChart = new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: {chart_labels},
    datasets: [
      {chart_datasets}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ callbacks: {{ label: function(c) {{ return c.dataset.label + ': ' + c.parsed.y + '%'; }} }} }},
      zoom: {{
        limits: {{ x: {{ minRange: 5 }} }},
        pan: {{ enabled: true, mode: 'x' }},
        zoom: {{
          wheel: {{ enabled: true }},
          pinch: {{ enabled: true }},
          mode: 'x'
        }}
      }}
    }},
    scales: {{
      x: {{ min: {chart_x_min}, max: {chart_x_max}, ticks: {{ font: {{ size: 11 }}, color: '#9ca3af', maxRotation: 45, autoSkip: true, maxTicksLimit: 25 }} }},
      y: {{ min: 0, max: 100, ticks: {{ stepSize: 20, font: {{ size: 11 }}, color: '#9ca3af', callback: function(v) {{ return v + '%' }} }}, grid: {{ color: 'rgba(255,255,255,0.06)' }} }}
    }}
  }}
}});
</script>

{map_script_html}
</body></html>"""

    REPORT_PATH.write_text(html, encoding="utf-8")
    return str(REPORT_PATH)


def _build_map_script(map_sensors_json, map_bt_paths_json, history_json, group_meta=None):
    gm = group_meta or {}
    layer_keys = [m["layer_key"] for m in gm.values() if m.get("layer_key")]

    # Pre-compute all group-driven JS snippets
    active_layers_js = json.dumps({**{k: True for k in layer_keys}, "paths": True})
    layer_groups_entries = "\n  ".join(
        f"{m['layer_key']}: L.markerClusterGroup(_clusterOpts),"
        for m in gm.values() if m.get("layer_key")
    )
    group_layer_js = json.dumps({g: m["layer_key"] for g, m in gm.items() if m.get("layer_key")})
    layer_keys_js  = json.dumps(layer_keys)
    icon_class_js  = json.dumps({g: m.get("icon", "ti-circle") for g, m in gm.items()})
    icon_size_js   = json.dumps({g: m.get("icon_size", 24) for g, m in gm.items()})
    icon_shape_js  = json.dumps({g: m["icon_shape"] for g, m in gm.items() if m.get("icon_shape")})
    legend_rows = "+".join(
        "row(iconBox({icon},{color}{shape})+'<span style=\"color:#1a1a2e\">{label}</span>')".format(
            icon=json.dumps(m.get("icon", "ti-circle")),
            color="'#6b7280'",
            shape=(f",{json.dumps(m['icon_shape'])}" if m.get("icon_shape") else ""),
            label=m.get("display", g),
        )
        for g, m in gm.items()
    )

    return ("""<script>
var _sensors  = """ + map_sensors_json + """;
var _btPaths  = """ + map_bt_paths_json + """;
var _history  = """ + history_json + """;
var _playIdx  = _history.length - 1;
var _playTimer = null;
var _activeFilter = 'all';
var _activeLayers = """ + active_layers_js + """;

var STATUS_COLOR_MAP = {
  working:'#1d9e75', ok:'#1d9e75', no_traffic:'#1d9e75',
  malfunctioning:'#e24b4a', not_working:'#e24b4a', failing:'#e24b4a',
  stale:'#e58e0a', missing:'#e58e0a',
  no_measurement:'#6b7280', no_status:'#6b7280', unknown:'#6b7280'
};

var _map = L.map('sensorMap', {zoomControl:true}).setView([34.95, 33.15], 9);
(function() {
  var hdr = document.querySelector('header');
  function _fixLeafletTop() {
    var h = hdr ? hdr.getBoundingClientRect().height : 0;
    document.querySelectorAll('.leaflet-top').forEach(function(el) { el.style.top = h + 'px'; });
  }
  _fixLeafletTop();
  window.addEventListener('resize', _fixLeafletTop);
})();
_map.on('click', function() { closeMapPanel(); });
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}).addTo(_map);

var _clusterOpts = {showCoverageOnHover:false, maxClusterRadius:50, disableClusteringAtZoom:13, chunkedLoading:true};
var _clustered = true;
var _layerGroups = {
  """ + layer_groups_entries + """
  paths: L.layerGroup(),
  arrows: L.layerGroup()
};
Object.values(_layerGroups).forEach(function(lg){ lg.addTo(_map); });

var GROUP_LAYER    = """ + group_layer_js + """;
var ISSUE_STATUSES = ['malfunctioning','not_working','failing','stale','missing'];
var STATUS_LABELS  = {
  working:'Working', ok:'OK', no_traffic:'No traffic', no_measurement:'No data',
  no_status:'No status', not_working:'Not working', malfunctioning:'Malfunctioning',
  failing:'No speed / travel time', unknown:'No recent data'
};

/* -- Icon factory ------------------------------------------------- */
var ICON_CLASS = """ + icon_class_js + """;
var ICON_SIZE  = """ + icon_size_js + """;
var ICON_SHAPE = """ + icon_shape_js + """;

function makeIcon(group, color) {
  var ic  = ICON_CLASS[group] || 'ti-circle';
  var sz  = ICON_SIZE[group]  || 24;
  var br  = ICON_SHAPE[group] || '50%';
  var html = '<div style="width:'+sz+'px;height:'+sz+'px;border-radius:'+br+';'+
             'background:'+color+';border:2.5px solid rgba(255,255,255,0.92);'+
             'display:flex;align-items:center;justify-content:center;'+
             'box-shadow:0 2px 7px rgba(0,0,0,0.32);font-size:'+(sz-11)+'px;'+
             'color:white">'+
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
  return v===-1 ? '\\u22121 km/h (fault)' : v+' km/h';
}
function fmtFlow(v)  { return (v===null||v===undefined) ? null : v+' veh/hr'; }
function fmtTT(v) {
  if (v===null||v===undefined) return null;
  var mins=Math.floor(v/60), secs=Math.round(v%60);
  return (mins>0?mins+'m ':'')+secs+'s';
}

/* -- Marker factory ----------------------------------------------- */
var _markersByGroup = {td:[], bt:[], vms:[]};

function makeMarker(s) {
  var m = L.marker([s.lat, s.lon], {icon: makeIcon(s.group, s.color)});
  m._sensorId     = s.id;
  m._sensorStatus = s.status;
  m._sensorGroup  = GROUP_LAYER[s.group] || s.group;
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
  var statusCell = s.comm_label
    ? popRow('Status', s.comm_label, '#6b7280')
    : popRow('Status', STATUS_LABELS[s.status]||s.status, s.color);
  var rows = popRow('ID', s.id)+popRow('Group', s.group_display||s.group)+
             popRow('Project', s.project || 'Unassigned', s.project?null:'#c0392b')+
             statusCell+dataRows;
  var bodyHtml = '<table style="border-collapse:collapse;width:100%">'+rows+'</table>';
  m.on('click', function(e) { L.DomEvent.stopPropagation(e); showMapPanel(s.display_name||s.name||'Sensor '+s.id, bodyHtml); });
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
  pl._pathId     = p.id;
  pl._pathStatus = p.status;
  var d = p.data || {};
  var rows = popRow('Route', p.name)+popRow('Path ID', p.id)+
             popRow('Status', STATUS_LABELS[p.status]||p.status, style.color)+
             popRow('Speed', fmtSpeed(d.speed_kmh))+
             popRow('Travel time', fmtTT(d.travel_time_s));
  var bodyHtml = '<table style="border-collapse:collapse;width:100%">'+rows+'</table>';
  pl._bodyHtml = bodyHtml;
  pl._pathName = 'BT Path '+p.name;
  // direction arrows along the path
  pl._decorator = L.polylineDecorator(pl, {
    patterns: [{
      offset: 20, repeat: 80,
      symbol: L.Symbol.arrowHead({
        pixelSize: 9, headAngle: 40,
        pathOptions: {color: '#555', fillOpacity: 0.7, weight: 0, fillColor: '#555', interactive: false}
      })
    }]
  });
  return pl;
}

/* -- Build layers ------------------------------------------------- */
var _markers = [];
_sensors.forEach(function(s) {
  var key = GROUP_LAYER[s.group];
  if (!key) return;
  var m = makeMarker(s);
  _markersByGroup[key].push(m);
  _markers.push(m);
});
""" + layer_keys_js + """.forEach(function(key) {
  _markersByGroup[key].forEach(function(m){ _layerGroups[key].addLayer(m); });
});

var _paths = [];
var _highlighted = null;
_btPaths.forEach(function(p) {
  var pl = makePath(p);
  pl.addTo(_layerGroups.paths);
  pl._decorator.addTo(_layerGroups.arrows);
  pl.on('click', function(e) {
    L.DomEvent.stopPropagation(e);
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
    """ + legend_rows + """+
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
  """ + layer_keys_js + """.forEach(function(key) {
    var lg = _layerGroups[key];
    lg.clearLayers();
    if (!_activeLayers[key]) return;
    _markersByGroup[key].forEach(function(m) {
      var visible = _activeFilter === 'all' || ISSUE_STATUSES.indexOf(m._sensorStatus) !== -1;
      if (visible) {
        m.setIcon(makeIcon(m._sensorGroup2, m._sensorColor));
        lg.addLayer(m);
      }
    });
  });
  var pathsOn = _activeLayers.paths;
  _paths.forEach(function(p) {
    var on = pathsOn &&
             (_activeFilter === 'all' || ISSUE_STATUSES.indexOf(p._pathStatus) !== -1);
    p.setStyle(pathStyle(p._pathStatus, !on));
  });
  if (pathsOn) {
    if (!_map.hasLayer(_layerGroups.arrows)) _map.addLayer(_layerGroups.arrows);
  } else {
    if (_map.hasLayer(_layerGroups.arrows)) _map.removeLayer(_layerGroups.arrows);
  }
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

/* -- Cluster toggle ----------------------------------------------- */
function _rebuildPointLayers() {
  """ + layer_keys_js + """.forEach(function(key) {
    _map.removeLayer(_layerGroups[key]);
    _layerGroups[key] = _clustered
      ? L.markerClusterGroup(_clusterOpts)
      : L.layerGroup();
    _map.addLayer(_layerGroups[key]);
  });
  applyVisibility();
}

function toggleClustering(btn) {
  _clustered = !_clustered;
  btn.classList.toggle('active', _clustered);
  _rebuildPointLayers();
}

/* -- Focus layer (called from group cards) ------------------------ */
function focusMapLayer(key) {
  Object.keys(_activeLayers).forEach(function(k){ _activeLayers[k] = (k === key); });
  document.querySelectorAll('[data-layer]').forEach(function(b){
    b.classList.toggle('active', b.dataset.layer === key);
  });
  var showAllBtn = document.getElementById('btn-showall');
  if (showAllBtn) showAllBtn.classList.remove('active');
  applyVisibility();
  var mapPanel = document.getElementById('p-map');
  if (mapPanel) mapPanel.scrollIntoView({behavior:'smooth', block:'start'});
  var pts = _markersByGroup[key] || [];
  if (pts.length) {
    var bounds = L.latLngBounds(pts.map(function(m){ return m.getLatLng(); }));
    _map.flyToBounds(bounds, {padding:[40,40], maxZoom:12, duration:0.8});
  }
}

/* -- Historical playback ----------------------------------------- */
function _updatePlayUI() {
  if (!_history.length) return;
  var slider  = document.getElementById('playSlider');
  var label   = document.getElementById('playTimestamp');
  var playBtn = document.getElementById('playBtn');
  if (slider) { slider.max = _history.length - 1; slider.value = _playIdx; }
  var run    = _history[_playIdx];
  var isLive = _playIdx === _history.length - 1;
  if (label) label.textContent = (isLive ? 'Last run \\u00b7 ' : 'Run ' + (_playIdx+1) + '/' + _history.length + ' \\u00b7 ') + (run.run_at || '');
  if (playBtn) playBtn.innerHTML = _playTimer ? '&#9646;&#9646;' : '&#9654;';
}

function setRun(idx) {
  _playIdx = Math.max(0, Math.min(+idx, _history.length - 1));
  var statuses = (_history[_playIdx] || {}).statuses || {};
  _markers.forEach(function(m) {
    var st = statuses[m._sensorId] || 'unknown';
    m._sensorStatus = st;
    m._sensorColor  = STATUS_COLOR_MAP[st] || '#6b7280';
  });
  _paths.forEach(function(p) {
    var st = statuses[p._pathId] || 'unknown';
    p._pathStatus = st;
  });
  applyVisibility();
  _updatePlayUI();
}

function stepRun(delta) { setRun(_playIdx + delta); }

function togglePlay() {
  if (_playTimer) {
    clearInterval(_playTimer); _playTimer = null;
    _updatePlayUI();
    return;
  }
  if (_playIdx >= _history.length - 1) setRun(0);
  _playTimer = setInterval(function() {
    if (_playIdx >= _history.length - 1) {
      clearInterval(_playTimer); _playTimer = null; _updatePlayUI(); return;
    }
    setRun(_playIdx + 1);
  }, 900);
  _updatePlayUI();
}

// Initialise slider at latest run
if (_history.length) { _updatePlayUI(); }

/* -- Fly to sensor (called from stability panel) ------------------ */
function flyToSensor(el) {
  var lat = parseFloat(el.dataset.lat);
  var lon = parseFloat(el.dataset.lon);
  var sid = el.dataset.sid;
  var grp = el.dataset.mapgroup;
  var key = GROUP_LAYER[grp];
  // ensure the layer is visible
  if (key && !_activeLayers[key]) {
    _activeLayers[key] = true;
    var layerBtn = document.querySelector('[data-layer="'+key+'"]');
    if (layerBtn) layerBtn.classList.add('active');
    applyVisibility();
  }
  // open map panel if collapsed, then scroll to it
  var mapBody = document.getElementById('b-p-map');
  var mapPanel = document.getElementById('p-map');
  if (mapBody && mapBody.style.display === 'none') {
    if (mapPanel) mapPanel.querySelector('.panel-header').click();
  }
  if (mapPanel) mapPanel.scrollIntoView({behavior:'smooth', block:'start'});
  // fly and open popup after animation
  _map.flyTo([lat, lon], 15, {duration:0.8});
  setTimeout(function() {
    _markers.forEach(function(m) {
      if (m._sensorId === sid && m._sensorGroup2 === grp) m.fire('click');
    });
    // highlight ring
    var ring = L.circleMarker([lat, lon], {
      radius: 18, color: '#facc15', weight: 5, fill: false, opacity: 1, interactive: false
    }).addTo(_map);
    setTimeout(function() { _map.removeLayer(ring); }, 3000);
  }, 900);
}

/* -- Fly to BT path (called from stability panel) ---------------- */
function flyToBtPath(el) {
  var pid = el.dataset.pathid;
  // ensure BT paths layer is visible
  if (!_activeLayers['bt']) {
    _activeLayers['bt'] = true;
    var layerBtn = document.querySelector('[data-layer="bt"]');
    if (layerBtn) layerBtn.classList.add('active');
    applyVisibility();
  }
  // open map panel if collapsed, then scroll to it
  var mapBody = document.getElementById('b-p-map');
  var mapPanel = document.getElementById('p-map');
  if (mapBody && mapBody.style.display === 'none') {
    if (mapPanel) mapPanel.querySelector('.panel-header').click();
  }
  if (mapPanel) mapPanel.scrollIntoView({behavior:'smooth', block:'start'});
  // find the polyline and fly to its bounds
  var target = null;
  _paths.forEach(function(p) { if (p._pathId === pid) target = p; });
  if (target) {
    _map.flyToBounds(target.getBounds(), {padding:[40,40], maxZoom:14, duration:0.8});
    setTimeout(function() { target.fire('click'); }, 900);
  }
}

/* -- Info panel --------------------------------------------------- */
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
</script>""")


if __name__ == "__main__":
    path = generate_report()
    print(f"Report written to {path}")